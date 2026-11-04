"""各业务域共用的告警/错误持久化。"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .common import ADDRESS_PLACEHOLDER, CREDENTIAL_PLACEHOLDER, sanitize_error_text

ERRLOG_BUFFER = 2000
ERRLOG_MAX_BYTES = 512 * 1024
PLUGIN_THREAD_TAG = "weibo-suite"

# 面板/日志回放的错误文本统一脱敏：采集侧存的是完整异常，常带图床 URL、
# 带签名的查询串和代理凭据。URL、裸 IP 与裸 key=value 复用
# common.sanitize_error_text 的口径（统一占位符），这里只补它没覆盖的几类。
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|access_token|auth|authcode|sign|signature|key|secret|password|passwd)=)[^&\s'\"<>]+"
)
_PROXY_USERINFO_RE = re.compile(r"\b[\w.+-]+:[^\s@/'\"<>]+@[\w.-]+(?::\d+)?")
_BARE_FILE_PATH_RE = re.compile(r"(?:^|(?<=[\s（(【]))/file/[^\s'\"<>，。；：、）】]+")
# 面板每条错误最多展示这么多字符；完整原文仍在 errors.log 里
ERRLOG_MSG_LIMIT = 1000
# 写盘/轮转失败的上报间隔：磁盘满时每批都失败，不限制会把日志刷成瀑布
_WRITE_FAIL_LOG_INTERVAL = 60.0
# 专门用于"留存本身失败"的 logger：关掉 propagate 断开与插件日志器的链路，
# 避免「写盘失败 → 记日志 → 又进这个 handler」的自我喂养；无 handler 时
# 落到 logging.lastResort（stderr），容器日志里仍看得见。
_WRITER_LOG = logging.getLogger(f"{PLUGIN_THREAD_TAG}.errlog-writer")
_WRITER_LOG.propagate = False
# 必须自己钉住级别，不能留给"NOTSET 就继承根日志器"的默认行为：这条告警是
# "磁盘满了、留存其实已经失效"的唯一信号，宿主把根日志器调到 ERROR（配置里
# 把日志级别调高是常见操作）时，继承来的有效级别会把 warning 直接吞掉，面板
# 仍显示"已留存 N 条"。CI 的 Python 3.10 job 就是这样红的（旧版 isEnabledFor
# 的缓存还会让根级别变化在子日志器上滞后生效）。
_WRITER_LOG.setLevel(logging.WARNING)


def _redact_error_msg(value: Any, limit: int = ERRLOG_MSG_LIMIT) -> str:
    """脱敏一条错误文本：隐藏地址、查询串里的凭据与代理账号。

    地址类沿用 `sanitize_error_text` 的 `<地址已隐藏>`；凭据类用
    `<凭据已隐藏>`。额外覆盖它没管的裸 `/file/...`、`?token=...&sign=...`
    和 `user:pass@host` 三种形态。
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    # 先按既有口径隐藏 URL / IP。limit 给足长度：截断后再匹配会把 URL 剩半截
    text = sanitize_error_text(raw, limit=max(len(raw), 1))
    text = _QUERY_SECRET_RE.sub(rf"\1{CREDENTIAL_PLACEHOLDER}", text)
    text = _PROXY_USERINFO_RE.sub(CREDENTIAL_PLACEHOLDER, text)
    text = _BARE_FILE_PATH_RE.sub(ADDRESS_PLACEHOLDER, text)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


class _ErrorLogStore(logging.Handler):
    """本插件 warning/error 的留存器：内存环形缓冲 + 后台线程写 errors.log。

    整机被内存顶死后主日志往往来不及刷盘，diagnostic.log 只覆盖相册重负载
    动作；这里把发送超时、检查失败、相册上传失败、风控冷却等所有告警留存
    下来，重启后面板仍能还原"死机前最后在报什么"。

    emit() 只在日志调用方线程里做两件轻活——过滤和进缓冲，真正的落盘交给一条
    常驻后台线程（daemon，随进程退出即止）。早期版本在 emit() 里同步
    open+write+flush+fsync，而日志调用方就是 asyncio 事件循环线程：本插件有
    二十多处 logger.warning，风控期一轮能触发好几次，慢盘/网络盘上单次 fsync
    可达数十毫秒，会直接卡住轮询任务与指令响应。现在写盘与轮转都在后台线程，
    事件循环只付一次 deque.append 的代价。

    JSONL 一行一条（msg 里的换行会被转义），重启后按行回读进缓冲。
    挂在 AstrBot 官方的插件日志器（v4.28+）或共享日志器（旧版）上，配合
    src_root 目录过滤只收本插件的告警；异常绝不外抛（留存本身不能变成新的
    故障点），磁盘满/读不动时静默放弃。
    """

    # 后台线程每次醒来最多连续处理多少条待写记录，防一次积压把它占死
    _DRAIN_MAX = 200
    # 单批字节上限：轮转按字节判定，批太大就会一次把文件顶过阈值且事后无人
    # 再轮转。取阈值的 1/4，留足轮转把文件砍回一半后的余量。
    _BATCH_MAX_BYTES = ERRLOG_MAX_BYTES // 4
    # 待写队列上限：写盘长期跟不上时丢弃最旧的，缓冲与文件都不会无限膨胀
    _QUEUE_MAX = 2000

    def __init__(
        self,
        path: Path,
        src_root: str = "",
        buffer_size: int = ERRLOG_BUFFER,
    ):
        super().__init__(level=logging.WARNING)
        self._path = path
        # 只收本插件目录下代码发出的记录：旧版 AstrBot 的共享日志器上还会
        # 流过核心与其它插件的日志，按源码目录区分（v4.28+ 的专属插件日志器
        # 天然只有本插件记录，此过滤是双保险）
        self._src_root = os.path.normcase(src_root) if src_root else ""
        self._buf: deque[dict[str, Any]] = deque(maxlen=buffer_size)
        self._lock = threading.Lock()
        # 待落盘的 JSONL 行；_wakeup 只做"有活干了"的通知，不传数据
        self._pending: deque[str] = deque(maxlen=self._QUEUE_MAX)
        self._wakeup = threading.Event()
        self._closing = False
        # 入队序号与"已处理到第几号"：flush_to_disk 靠这两个数分辨
        # "队列真空了"与"线程还没轮到"，避免提前返回
        self._enqueued = 0
        self._drained_seq = 0
        # 写盘失败限频状态（见 _report_write_failure）
        self._last_write_error_ts = 0.0
        self._suppressed_write_errors = 0
        self._writer = threading.Thread(
            target=self._write_loop,
            name=f"{PLUGIN_THREAD_TAG}-errlog-writer",
            daemon=True,
        )
        self._writer.start()

    # ---------------- 后台写盘 ----------------

    def _write_loop(self) -> None:
        """常驻后台线程：等通知 → 批量取走待写行 → 一次落盘。

        批量写顺带把 fsync 从"每条一次"降到"每批一次"，慢盘上的总开销也下来了。
        线程内任何异常都不外抛，否则会让整条写盘链路静默失效。
        """
        while True:
            self._wakeup.wait()
            if self._closing:
                return
            self._wakeup.clear()
            while True:
                batch = self._take_batch()
                if not batch:
                    break
                self._write_batch(batch)
                # 计数放在写完之后：flush_to_disk 看到 _drained_seq 追平
                # 就说明数据确实落盘了，而不是"刚从队列里取走"
                with self._lock:
                    self._drained_seq += len(batch)

    def _take_batch(self) -> list[str]:
        """取下一批待写行：行数不超过 _DRAIN_MAX，字节数不超过 _BATCH_MAX_BYTES。

        只按行数切会在"多行短记录"时把一批攒到几百 KB，而轮转是按字节判定的，
        超大批次一次就能把文件顶过阈值且事后无人再来轮转。所以这里按字节再收
        一道口。允许单行超标（否则写不出去），但仅限它自己占一批。
        """
        batch: list[str] = []
        size = 0
        with self._lock:
            while self._pending and len(batch) < self._DRAIN_MAX:
                line = self._pending[0]
                n = len(line.encode("utf-8"))
                if batch and size + n > self._BATCH_MAX_BYTES:
                    break
                self._pending.popleft()
                batch.append(line)
                size += n
        return batch

    def _report_write_failure(self, what: str, exc: OSError) -> None:
        """写盘/轮转失败限频上报。

        以前这里是 except OSError: pass —— 磁盘满、只读挂载、目录被删都会让
        留存静默失效，而面板仍然显示"已留存 N 条"，排查时最具误导性。
        现在每 60 秒最多出一条，期间被压掉的次数一并带上。

        `_last_write_error_ts` 的初值 0.0 是"从没上报过"的哨兵，必须显式判掉：
        `time.monotonic()` 是开机以来的秒数，容器与新开机的机器上完全可能还
        不到 `_WRITE_FAIL_LOG_INTERVAL`，直接相减会把**第一次**失败算成"窗口内
        的重复失败"吞掉——正好是这段代码要消灭的那个静默。CI 上"同一份代码
        有时红有时绿"就是这个：Actions 的 runner 每次都是新开机器，跑得快的那
        几次撞进开机后第一分钟内。
        """
        now = time.monotonic()
        with self._lock:
            if self._last_write_error_ts and now - self._last_write_error_ts < (
                _WRITE_FAIL_LOG_INTERVAL
            ):
                self._suppressed_write_errors += 1
                return
            self._last_write_error_ts = now
            suppressed = self._suppressed_write_errors
            self._suppressed_write_errors = 0
        note = f"（此前 {suppressed} 次同类失败已抑制）" if suppressed else ""
        _WRITER_LOG.warning("%s: %r%s", what, exc, note)

    def _write_batch(self, batch: list[str]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = "".join(batch)
            self._rotate_if_needed(len(payload.encode("utf-8")))
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                # 整机被顶死时最怕进程来不及退出就没了：这一批已经攒好，
                # 一次 fsync 换确定性留存，且不再发生在事件循环线程里
                os.fsync(f.fileno())
        except OSError as exc:
            self._report_write_failure("写入 errors.log 失败", exc)

    def _rotate_if_needed(self, incoming: int = 0) -> None:
        """超过阈值只留后半段。

        调用方已在后台线程，整读整写不会阻塞事件循环。`incoming` 是马上要追加
        的字节数：只在写前判"当前是否超限"的话，最后一批会合法地把文件重新顶
        回阈值之上，而之后不再有写入来触发轮转。带上 incoming 一起判，收尾时
        文件就停在阈值以内。
        """
        try:
            if not self._path.exists():
                return
            if self._path.stat().st_size + incoming <= ERRLOG_MAX_BYTES:
                return
            lines = self._path.read_text(encoding="utf-8").splitlines(True)
            half = "".join(lines[len(lines) // 2 :])
            # 后半段自身仍可能超限（例如单批就是几百 KB 的巨型记录）：
            # 再砍到只剩最后一小段，必要时连最后一行也丢掉，别让文件无限长
            if len(half.encode("utf-8")) > ERRLOG_MAX_BYTES:
                keep, size = [], 0
                for ln in reversed(lines):
                    b = len(ln.encode("utf-8"))
                    if size + b > ERRLOG_MAX_BYTES:
                        break
                    keep.append(ln)
                    size += b
                half = "".join(reversed(keep))
            self._path.write_text(half, encoding="utf-8")
        except OSError as exc:
            self._report_write_failure("轮转 errors.log 失败", exc)

    # ---------------- logging.Handler ----------------

    def emit(self, record: logging.LogRecord) -> None:
        """只过滤 + 进缓冲 + 入待写队列，不碰磁盘。"""
        try:
            if self._src_root:
                pn = os.path.normcase(getattr(record, "pathname", "") or "")
                if not pn.startswith(self._src_root):
                    return
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "src": f"{record.funcName}:{record.lineno}",
                "msg": record.getMessage(),
            }
            line = json.dumps(entry, ensure_ascii=False) + "\n"
        except Exception:
            return
        with self._lock:
            self._buf.append(entry)
            self._pending.append(line)
            self._enqueued += 1
        self._wakeup.set()

    def flush_to_disk(self, timeout: float = 3.0) -> bool:
        """把当前待写队列排空后再返回；全部落地返回 True，超时返回 False。

        terminate 与 clear 都靠它拿到"写盘线程已经不再持有旧数据"的确证。
        不能只盯着 _pending 变空就返回——emit 入队与线程出队之间有一个窗口，
        那一瞬间 _pending 恰好为空但数据还没落到文件上，提前返回会让调用方
        误以为已经写完。_drained_seq 记录线程"已经处理到第几号入队"，与
        _enqueued 对比即可分辨"真空了"还是"还没轮到"。
        """
        self._wakeup.set()
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if not self._pending and self._drained_seq >= self._enqueued:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def close(self, timeout: float = 3.0) -> None:
        """排空待写队列 → 停掉后台线程（logging 在 Handler 关闭时会调用）。

        先 flush 再置 _closing：反过来的话最后一轮还没写完线程就退出，
        末几条告警（往往正是崩溃前那批）会留在内存里丢掉。
        """
        # 排空可能因为写盘线程已死而永远等不到，此时自己的 close 不能变成
        # 阻塞点；给一个短兜底超时即可
        with contextlib.suppress(Exception):
            self.flush_to_disk(timeout)
        self._closing = True
        self._wakeup.set()
        with contextlib.suppress(Exception):
            self._writer.join(timeout=1.0)
        super().close()

    def restore(self) -> int:
        """启动时回读文件尾部，重启后面板还能看到上一次崩溃前的告警。"""
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        n = 0
        with self._lock:
            for line in lines[-ERRLOG_BUFFER:]:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("msg"):
                    item["restored"] = True  # 面板据此显示"上次运行"标记
                    self._buf.append(item)
                    n += 1
        return n

    def snapshot(self, limit: int) -> list[dict[str, Any]]:
        """最近 limit 条，最新在前（面板直接渲染）。

        返回前逐条脱敏：留存的是完整异常，常带图床 URL、带签名的查询串和
        代理凭据，面板会原样展示，不能直接交出去。字段名保持不变，调用方
        拿到的结构不受影响，只清洗 msg。
        """
        with self._lock:
            items = list(self._buf)
        return [
            {**item, "msg": _redact_error_msg(item.get("msg"))} for item in reversed(items[-limit:])
        ]

    def clear(self) -> int:
        """清空缓冲并截断文件，返回清掉的条数。

        待写队列里的行也必须一起丢掉，否则面板刚显示"已清空"，后台线程又把
        清空前的告警写回文件，重启后它们会被回读出来，看起来像清空没生效。
        丢弃要等线程真把手上的批处理完再做：线程可能已 pop 出一批正待落盘，
        那一批不在 _pending 里，直接清文件会被它随后追加回去——所以先 flush
        排空（连带把那批写进文件），再清队列 + 截断，顺序不能反。
        """
        self.flush_to_disk()
        with self._lock:
            n = len(self._buf)
            self._buf.clear()
            self._pending.clear()
            # 计数器同步归零，flush_to_disk 之后才能继续正确判定"已排空"
            self._enqueued = 0
            self._drained_seq = 0
        with contextlib.suppress(OSError):
            self._path.write_text("", encoding="utf-8")
        return n


def install_error_log(plugin: Any, path: Path) -> _ErrorLogStore:
    """Attach a fresh store to the plugin's official logger, replacing stale stores."""
    target = getattr(plugin, "logger", None)
    if not isinstance(target, logging.Logger):
        target = logging.getLogger("astrbot")
    for handler in list(target.handlers):
        if isinstance(handler, _ErrorLogStore):
            target.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()
    store = _ErrorLogStore(
        path,
        src_root=str(Path(__file__).resolve().parents[1]),
        buffer_size=ERRLOG_BUFFER,
    )
    target.addHandler(store)
    plugin._errlog = store
    plugin._errlog_target = target
    return store


def remove_error_log(plugin: Any) -> None:
    target = getattr(plugin, "_errlog_target", None)
    store = getattr(plugin, "_errlog", None)
    if target is not None and store is not None:
        target.removeHandler(store)
    if store is not None:
        store.close()
    plugin._errlog = None
    plugin._errlog_target = None
