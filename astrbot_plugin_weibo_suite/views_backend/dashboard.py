"""统一 WebUI 控制面板后端：微博监控 / 归档 / 管线 / 文案 / 溯源。

页面本体在 views/dashboard/（index.html 与 app.js）。Dashboard 把页面的桥接请求转发到
/api/v1/plugins/extensions/<插件名>/<endpoint>，本模块注册对应的路由：
只读的 overview 快照，加上与聊天指令同语义的增删操作和手动检查。
统计与最近动态的采集在 main.py 的业务路径里完成，这里只做展示与转发。

本模块不反向导入 main（AstrBot 以插件名为包名加载 main.py，from .main import
会把它再执行一遍，指令装饰器会重复注册）；main.py 需要分享的模块级工具
（uid 提取、代理打码）在注册时以参数传入。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import inspect
import re
import time
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

from astrbot.api import logger

# 面板单次返回的明细条数上限（队列、动态、错误留存）。与 main.py 共用同一份
# 取值，见 constants.py——以前两处各写一份、靠注释提醒同步，容易改漏。
from ..constants import (
    ACTIVITY_PAGE_LIMIT,
    ERRLOG_PAGE_LIMIT,
    PENDING_PAGE_LIMIT,
)

_URL_OR_IP_RE = re.compile(
    r"(?:https?|ftp)://[^\s'\"<>，。；：、）】]+|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b",
    re.IGNORECASE,
)
_UPLOAD_SUFFIXES = {".csv", ".md", ".markdown", ".json"}
_UPLOAD_MAX_BYTES = 64 * 1024 * 1024

try:  # 新版 AstrBot：astrbot.api.web 提供请求代理与响应助手
    from astrbot.api.web import error_response, json_response
    from astrbot.api.web import request as web_request

    _WEB_AVAILABLE = True
except ImportError:  # 旧版 Dashboard 走 Quart 原生上下文，做最小等价实现
    try:
        from quart import jsonify

        class web_request:
            """最小请求代理：面板只需要读 JSON body。"""

            @staticmethod
            async def json(default: Any = None) -> Any:
                from quart import request

                data = await request.get_json(silent=True)
                if isinstance(data, dict):
                    return data
                return {} if default is None else default

        def json_response(payload: dict):
            return jsonify(payload)

        def error_response(message: str, status_code: int = 400):
            resp = jsonify({"status": "error", "message": message})
            resp.status_code = status_code
            return resp

        _WEB_AVAILABLE = True
    except ImportError:
        # 连 quart 都没有的极简环境：面板 API 不可用，但不能拖垮插件本体加载
        _WEB_AVAILABLE = False

        def json_response(payload: dict):
            raise RuntimeError("AstrBot WebUI 不可用（缺少 quart），控制面板未启用")

        def error_response(message: str, status_code: int = 400):
            raise RuntimeError("AstrBot WebUI 不可用（缺少 quart），控制面板未启用")

        class web_request:
            @staticmethod
            async def json(default: Any = None) -> Any:
                return {} if default is None else default


# 面板手工添加 @全体 关键词时的单条长度与总数量上限。判定侧不看这些值——
# 配置里手工写更多/更长的关键词照样生效，这里只挡面板上误把一整段微博正文
# 当关键词粘进来的操作（那种关键词永远命中不了，却会留在配置里没人发现）。
AT_ALL_KEYWORD_MAX_LEN = 30
AT_ALL_KEYWORD_MAX_COUNT = 20


def register_dashboard(
    plugin,
    plugin_name: str,
    version: str,
    extract_uid: Callable[[str], str | None],
    redact_proxy: Callable[[str], str],
) -> bool:
    """把面板 API 挂到 Dashboard；AstrBot 不支持插件 Web API 时返回 False。"""
    context = getattr(plugin, "context", None)
    register = getattr(context, "register_web_api", None)
    if not callable(register) or not _WEB_AVAILABLE:
        return False
    api = DashboardAPI(plugin, plugin_name, version, extract_uid, redact_proxy)
    # 挂到 plugin 上供卸载流程调用 shutdown()：面板起的重扫/回填任务如果不先
    # 取消，trace 域关掉 sqlite 后它们还会往已关闭的连接里写。宿主目前没有通用
    # 清理挂点，terminate() 里需要补一行（见交付说明）。
    try:
        plugin._dashboard_api = api
    except Exception as e:  # 宿主若用 __slots__，挂不上也不该影响路由注册
        logger.warning(f"{plugin_name} 保存面板实例失败，后台任务将无法随卸载取消: {e!r}")
    routes = (
        (f"/{plugin_name}/overview", api.overview, ["GET"], "微博转发面板总览"),
        (f"/{plugin_name}/errors", api.errors, ["GET"], "错误日志留存"),
        (
            f"/{plugin_name}/preview-push",
            api.preview_push,
            ["GET"],
            "推送预览（按当前配置渲染样例）",
        ),
        (f"/{plugin_name}/check-now", api.check_now, ["POST"], "立即检查一轮"),
        (f"/{plugin_name}/cancel-pending", api.cancel_pending, ["POST"], "取消一条待推送微博"),
        (
            f"/{plugin_name}/restore-pending",
            api.restore_pending,
            ["POST"],
            "恢复一条已取消的推送",
        ),
        (
            f"/{plugin_name}/stop-push-round",
            api.stop_push_round,
            ["POST"],
            "停止本轮推送",
        ),
        (f"/{plugin_name}/clear-errors", api.clear_errors, ["POST"], "清空错误留存"),
        (f"/{plugin_name}/accounts", api.accounts, ["POST"], "增删监控博主"),
        (
            f"/{plugin_name}/at-all-uids",
            api.at_all_uids,
            ["POST"],
            "增删 @全体 博主名单",
        ),
        (
            f"/{plugin_name}/at-all-settings",
            api.at_all_settings,
            ["POST"],
            "@全体 总开关与关键词限定",
        ),
        (f"/{plugin_name}/sessions", api.sessions, ["POST"], "增删推送目标"),
        (f"/{plugin_name}/album-rules", api.album_rules, ["POST"], "增删相册规则"),
        (
            f"/{plugin_name}/keyword-rules",
            api.keyword_rules,
            ["POST"],
            "增删关键词相册路由规则",
        ),
        (f"/{plugin_name}/archive/overview", api.archive_overview, ["GET"], "归档总览"),
        (f"/{plugin_name}/archive/list", api.archive_list, ["GET"], "最近归档列表"),
        (
            f"/{plugin_name}/archive/backfill",
            api.archive_backfill,
            ["GET", "POST"],
            "回填状态与启动/停止",
        ),
        (
            f"/{plugin_name}/archive/reconcile",
            api.archive_reconcile,
            ["GET", "POST"],
            "归档对账与缺口报告",
        ),
        (
            f"/{plugin_name}/archive/import",
            api.archive_import,
            ["POST"],
            "导入 JSON 归档种子",
        ),
        (f"/{plugin_name}/pipeline/status", api.pipeline_status, ["GET"], "图床管线状态"),
        (f"/{plugin_name}/pipeline/retry", api.pipeline_retry, ["POST"], "补跑管线缺口"),
        (
            f"/{plugin_name}/pipeline/naming",
            api.pipeline_naming,
            ["GET", "POST"],
            "读取/保存命名模板与分类映射",
        ),
        (f"/{plugin_name}/copylib/overview", api.copylib_overview, ["GET"], "文案库总览"),
        (f"/{plugin_name}/copylib/list", api.copylib_list, ["GET", "POST"], "最近文案"),
        (
            f"/{plugin_name}/copylib/import",
            api.copylib_import,
            ["POST"],
            "导入 CSV/Markdown 文案",
        ),
        (f"/{plugin_name}/trace/status", api.trace_status, ["GET"], "溯源与签到状态"),
        (f"/{plugin_name}/trace/rescan", api.trace_rescan, ["POST"], "启动图库重扫"),
        (
            f"/{plugin_name}/trace/repair-history",
            api.trace_repair_history,
            ["POST"],
            "修复会话历史脏键",
        ),
    )
    ok = False
    for route, handler, methods, desc in routes:
        try:
            register(route, handler, methods, desc)
            ok = True
        except Exception as e:
            logger.warning(f"{plugin_name} 注册面板接口 {route} 失败: {e!r}")
    return ok


class _ApiResult:
    def __init__(self, text: str):
        self.text = text


class _ApiEvent:
    """后台面板动作使用的最小事件对象；不会伪装成真实群消息。"""

    def __init__(self, message: str = ""):
        self.message_str = message
        self.messages: list[str] = []

    @staticmethod
    def plain_result(text: str) -> _ApiResult:
        return _ApiResult(str(text or ""))

    async def send(self, result: _ApiResult) -> None:
        if result.text:
            self.messages.append(result.text)


class DashboardAPI:
    """面板 API 的各 handler；持有插件实例，直接读它的运行时状态。"""

    def __init__(
        self,
        plugin,
        plugin_name: str,
        version: str,
        extract_uid: Callable[[str], str | None],
        redact_proxy: Callable[[str], str],
    ):
        self.p = plugin
        self.name = plugin_name
        self.version = version
        self._extract_uid = extract_uid
        self._redact_proxy = redact_proxy
        self._log = logger
        self._jobs: set[asyncio.Task] = set()
        # 任务对象单独持有：_trace_job 会原样返回给前端，Task 不可 JSON 序列化
        self._trace_task: asyncio.Task | None = None
        self._trace_job: dict[str, Any] = {"running": False, "messages": []}

    async def shutdown(self) -> None:
        """取消面板起的后台任务（重扫、回填等），并等它们真正退出。

        插件卸载时 trace 域会关掉图库的 sqlite 连接，而这些任务还在
        `to_thread` 里往库里写；不先取消就会出现"写已关闭连接"。调用点放在
        关闭各域资源之前（`weibo/plugin.py::terminate()`）。
        """
        tasks = [task for task in self._jobs if not task.done()]
        current = self._trace_task
        if current is not None and not current.done() and current not in tasks:
            tasks.append(current)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()
        self._trace_task = None
        self._trace_job["running"] = False

    async def close(self) -> None:
        """shutdown() 的同义入口：插件卸载钩子统一按 close() 命名调用。"""
        await self.shutdown()

    # ---------------- 工具 ----------------

    async def _body(self) -> dict:
        try:
            data = await web_request.json(default={})
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _now(self) -> float:
        return time.time()

    # ---------------- 只读总览 ----------------

    async def overview(self):
        p = self.p
        now = self._now()
        poll_interval = max(30, p._int_cfg("poll_interval", 120))
        risk_blocked = now < p._blocked_until
        identity_custom = bool(p._custom_cookies)

        accounts = []
        by_uid = p.stats.get("by_uid") or {}
        at_all_uid_list = p._at_all_uids()
        at_all_uids = set(at_all_uid_list)
        # @全体成员的设置项集中在一份数据里回给面板（总开关 / 博主名单 / 关键词），
        # 状态区不再重复放一份，免得两处口径漂移
        at_all_settings = {
            "at_all": bool(p.config.get("at_all", False)),
            "keywords": p._at_all_keywords(),
            # 「能不能真的 @ 到」的两道硬条件也一并回给面板：QQ 的 24 小时次数
            # 台账与最近一次的投递结果（ok / degraded / skipped / failed /
            # unknown），面板据此显示"上次 @全体 到底生效没有"
            "quota_used": p._at_all_quota_used(),
            "quota_limit": p._at_all_limit(),
            "last": dict(p._at_all_last or {}),
            "bloggers": [
                {
                    "uid": uid,
                    "name": p._account_name(uid),
                    "monitored": uid in p.accounts,
                }
                for uid in at_all_uid_list
            ],
        }
        for uid, info in p.accounts.items():
            accounts.append(
                {
                    "uid": uid,
                    "name": str(info.get("name") or uid),
                    "seen_count": len(info.get("seen_ids") or []),
                    "baseline_done": bool(info.get("baseline_done")),
                    "fail_count": int(info.get("fail_count") or 0),
                    "next_retry_ts": float(info.get("next_retry_ts") or 0),
                    "push_ok": int((by_uid.get(uid) or {}).get("push_ok") or 0),
                    "at_all": uid in at_all_uids,
                }
            )

        rules = p._album_rules()
        album_rules = [
            {"uid": uid, "name": p._account_name(uid), "gid": gid, "album": album}
            for (uid, gid), album in rules.items()
        ]

        keyword_rules = [
            {
                "uid": uid or "",
                "name": p._account_name(uid) if uid else "全局",
                "keyword": kw,
                "gid": gid,
                "album": album,
            }
            for uid, kw, gid, album in p._album_keyword_rules()
        ]

        pending = [
            {
                "post_id": str(item.get("post_id") or ""),
                "uid": str(item.get("uid") or ""),
                "name": p._account_name(str(item.get("uid") or "")),
                "text": str(item.get("text") or "")[:120],
                "created_ts": float(item.get("created_ts") or 0),
                "retries": int(item.get("retries") or 0),
                "video": bool(item.get("video_files")),
                # 视频没下来时面板要能说出为什么（旧状态文件没有该字段）
                "video_fail": str(item.get("video_fail") or "")[:160],
                "video_note": bool(item.get("video_note")),
                # 协议端拒收的现场（回执/体量/被拒次数）：条目出队后只能从最近
                # 动态里看，还在队里时这里就能看到，省得去翻错误留存
                "reject": p._reject_reason(item)[:240],
                "sending": bool(item.get("last_send_ts")),
            }
            for item in p.pending[-PENDING_PAGE_LIMIT:]
        ]

        activity = list(p.activity)[-ACTIVITY_PAGE_LIMIT:]

        # 已取消（撤销窗口内可恢复）：条目已不在 pending 里，面板要单独一块展示，
        # 否则用户点完取消就再也找不回来。剩余可恢复秒数由后端算——窗口常量只
        # 在 main.py 有一份，面板照着算迟早会与后端对不上
        undo_seconds = p.push_cancel_undo_seconds()
        now = self._now()
        cancelled = [
            {
                "post_id": str(entry.get("post_id") or ""),
                "uid": str(entry.get("uid") or ""),
                "name": p._account_name(str(entry.get("uid") or "")),
                "text": str(entry.get("text") or "")[:120],
                "cancelled_ts": float(entry.get("cancelled_ts") or 0),
                "retries": int(entry.get("retries") or 0),
                "video": bool(entry.get("video_files")),
                "was_sending": bool(entry.get("cancel_was_sending")),
                "remain_seconds": max(
                    0, int(undo_seconds - (now - float(entry.get("cancelled_ts") or 0)))
                ),
            }
            for entry in p.cancelled
        ]

        return json_response(
            {
                "version": self.version,
                "status": {
                    "poll_running": bool(p._poll_task and not p._poll_task.done()),
                    "checking": bool(p._checking),
                    "last_check_ts": float(p.last_check_ts or 0),
                    "next_check_ts": (
                        float(p.last_check_ts or 0) + poll_interval if p.last_check_ts else 0.0
                    ),
                    "poll_interval": poll_interval,
                    "pending_count": len(p.pending),
                    "pending_retrying": sum(
                        1 for item in p.pending if int(item.get("retries") or 0) > 0
                    ),
                    # 面板手动取消：「停止本轮」只在真有轮次在跑时才有效，
                    # 撤销窗口秒数由后端下发（窗口常量只在 main.py 有一份）
                    "push_round_active": bool(p._push_round_active),
                    "push_round_stopping": bool(p._push_round_stop),
                    "cancelled_count": len(p.cancelled),
                    "cancel_undo_seconds": int(undo_seconds),
                    "risk_blocked": risk_blocked,
                    "risk_level": int(p._risk_level or 0),
                    "risk_until_ts": float(p._blocked_until or 0) if risk_blocked else 0.0,
                    "identity_mode": "custom" if identity_custom else "visitor",
                    "identity_cookie_keys": sorted(p._custom_cookies),
                    "visitor_ts": float(p._visitor_ts or 0),
                    "renew_fail_count": int(p._renew_fail_count or 0),
                    "proxy": self._redact_proxy(p._proxy) if p._proxy else "",
                    "ffmpeg": bool(p._ffmpeg),
                    "album_enabled": bool(p.config.get("album_enabled", True)),
                    # 在途后台任务：非零且有错误时配合错误日志定位卡在哪一步
                    "bg_tasks": {
                        "video_download": len(p._video_tasks),
                        "video_preparing": len(p._video_preparing),
                        "album_upload": len(p._album_tasks),
                    },
                    # 服务器水位：两次"无响应"事故都是内存顶穿，死机前最后一眼
                    # 面板数据里应有内存余量；非 Linux 拿不到时为 null
                    "mem": p._mem_snapshot(),
                    "disk": p._disk_snapshot(),
                },
                "accounts": accounts,
                "at_all_settings": at_all_settings,
                "sessions": p._sessions(),
                "album_rules": album_rules,
                "keyword_rules": keyword_rules,
                "pending": pending,
                "cancelled": cancelled,
                "stats": p.stats,
                "activity": activity,
            }
        )

    async def errors(self):
        """错误留存快照（warning/error 全量留存，重启后仍有崩溃前的记录）。"""
        store = getattr(self.p, "_errlog", None)
        items = store.snapshot(ERRLOG_PAGE_LIMIT) if store is not None else []
        return json_response({"errors": items})

    async def clear_errors(self):
        store = getattr(self.p, "_errlog", None)
        if store is None:
            return error_response("错误留存未启用（initialize 未完成或挂载失败）", 404)
        cleared = await asyncio.to_thread(store.clear)
        self._log.info(f"面板清空错误留存 {cleared} 条")
        return json_response({"cleared": cleared})

    async def preview_push(self):
        """按当前配置渲染一条样例推送（样例是虚构的，不发消息、不进队列）。"""
        try:
            return json_response(self.p.preview_push())
        except Exception as e:
            return error_response(f"推送预览生成失败: {self._safe_error(e)}", 500)

    # ---------------- 操作 ----------------

    async def cancel_pending(self):
        """取消一条待推送微博（进可撤销保留区，不再重发）。

        这是"推送一直失败时的人工出口"：等 max_pending_retries 烧完最长要十几
        分钟，期间面板原本没有任何手段叫停。取消只影响还没发出去的部分——条目
        正在发送时只能打标记，由推送循环在本条收尾时处置（已发出的收不回）。
        """
        p = self.p
        body = await self._body()
        post_id = str(body.get("post_id") or "").strip()
        if not post_id:
            return error_response("缺少 post_id")
        ok, message = p.cancel_pending(post_id)
        if not ok:
            return error_response(message, 409)
        self._log.info(f"面板取消推送 {post_id}：{message}")
        return json_response({"ok": True, "message": message})

    async def restore_pending(self):
        """把撤销窗口内取消掉的条目放回待推送队列（误点取消的补救入口）。"""
        p = self.p
        body = await self._body()
        post_id = str(body.get("post_id") or "").strip()
        if not post_id:
            return error_response("缺少 post_id")
        ok, message = p.restore_pending(post_id)
        if not ok:
            return error_response(message, 409)
        self._log.info(f"面板恢复推送 {post_id}：{message}")
        return json_response({"ok": True, "message": message})

    async def stop_push_round(self):
        """停止本轮推送：剩余条目原样留队、不消耗重试次数，下一轮照常重试。"""
        p = self.p
        ok, message = p.stop_push_round()
        if not ok:
            return error_response(message, 409)
        self._log.info(f"面板停止本轮推送：{message}")
        return json_response({"ok": True, "message": message})

    async def check_now(self):
        p = self.p
        if p._checking:
            return error_response("已有检查任务在进行中，请稍候", 409)
        if not p.accounts:
            return error_response("尚未监控任何账号，请先添加监控博主")
        if self._now() < p._blocked_until:
            remain = int((p._blocked_until - self._now()) // 60) + 1
            return error_response(f"微博风控冷却中（约 {remain} 分钟后自动恢复）", 409)
        if p._manual_check_task and not p._manual_check_task.done():
            return error_response("手动检查已在进行中", 409)

        async def _run():
            try:
                new_count = await p.check_all()
                p._record_event("info", detail=f"手动检查完成，检测到 {new_count} 条新微博")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log.warning(f"手动检查失败: {e!r}")
                p._record_event("error", detail=f"手动检查失败: {e}")

        p._manual_check_task = asyncio.create_task(_run())
        return json_response({"started": True})

    async def accounts(self):
        p = self.p
        body = await self._body()
        action = str(body.get("action") or "")
        if action == "add":
            uid = self._extract_uid(str(body.get("uid") or "")) or ""
            if not uid:
                return error_response("请输入有效的微博 uid 或主页链接")
            p._sync_accounts_from_config()
            if uid in p.accounts:
                return error_response(f"账号 {uid} 已在监控列表中")
            # 拉昵称会走真实的微博请求（带全局请求间隔），最多等 10 秒：
            # 挂太久前端会一直转圈、用户重复点击又会叠加重试，超时先用 uid 占位
            nickname = ""
            nickname_timeout = False
            try:
                nickname = await asyncio.wait_for(p._fetch_nickname(uid), timeout=10)
            except asyncio.TimeoutError:
                nickname_timeout = True
                self._log.info(f"面板添加账号 {uid} 拉取昵称超时，先用 uid 占位")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log.info(f"面板添加账号 {uid} 拉取昵称失败: {e!r}")
            p.accounts[uid] = {
                "name": nickname or uid,
                "seen_ids": [],
                "baseline_done": False,
                "fail_count": 0,
                "next_retry_ts": 0.0,
            }
            p._set_cfg("monitored_uids", list(p.accounts.keys()))
            p._save_config()
            p._save_state()
            name = p._account_name(uid)
            p._record_event("info", uid=uid, name=name, detail="通过面板添加监控")
            result = {"uid": uid, "name": name, "nickname_ok": bool(nickname)}
            if nickname_timeout:
                result["message"] = "昵称获取超时，稍后可在面板刷新"
            return json_response(result)
        if action == "remove":
            uid = str(body.get("uid") or "").strip()
            p._sync_accounts_from_config()
            if uid not in p.accounts:
                return error_response(f"账号 {uid} 不在监控列表中")
            name = str(p.accounts.pop(uid).get("name") or uid)
            p._set_cfg("monitored_uids", list(p.accounts.keys()))
            p._save_config()
            p._save_state()
            p._record_event("info", uid=uid, name=name, detail="通过面板取消监控")
            return json_response({"removed": uid, "name": name})
        return error_response("不支持的操作")

    async def at_all_uids(self):
        """@全体 博主名单的增删（面板博主行开关与「推送 @全体成员」卡片共用）。"""
        p = self.p
        body = await self._body()
        action = str(body.get("action") or "")
        uid = self._extract_uid(str(body.get("uid") or "")) or ""
        if not uid:
            return error_response("请输入有效的微博 uid 或主页链接")
        current = p._at_all_uids()
        if action == "add":
            p._sync_accounts_from_config()
            if uid not in p.accounts:
                return error_response(f"账号 {uid} 不在监控列表中，请先添加监控")
            if uid in current:
                return error_response(f"账号 {uid} 已在 @全体名单中")
            p._set_cfg("at_all_uids", [*current, uid])
        elif action == "remove":
            # 移出不要求仍在监控列表：取消监控后留在名单里的 uid 也该能从面板清掉
            if uid not in current:
                return error_response(f"账号 {uid} 不在 @全体名单中")
            p._set_cfg("at_all_uids", [u for u in current if u != uid])
        else:
            return error_response("不支持的操作")
        p._save_config()
        name = p._account_name(uid)
        on = action == "add"
        p._record_event(
            "info",
            uid=uid,
            name=name,
            detail="通过面板加入 @全体名单" if on else "通过面板移出 @全体名单",
        )
        return json_response({"uid": uid, "name": name, "at_all": on})

    async def at_all_settings(self):
        """@全体成员的总开关与关键词限定（面板「推送 @全体成员」卡片）。

        名单（哪些博主）在 at_all_uids 里增删，这里只动另外两道闸门：总开关
        与关键词。判定口径由 main.py 的 _at_all_hit 负责，本接口只管写配置。
        """
        p = self.p
        body = await self._body()
        action = str(body.get("action") or "")
        if action == "set_switch":
            if "value" not in body:
                return error_response("缺少 value（true 开启 / false 关闭）")
            on = bool(body.get("value"))
            p._set_cfg("at_all", on)
            p._save_config()
            p._record_event("info", detail=f"面板{'开启' if on else '关闭'} @全体成员总开关")
            return json_response({"at_all": on})
        if action in ("add_keyword", "remove_keyword"):
            kw = str(body.get("keyword") or "").strip()
            if not kw:
                return error_response("请输入关键词")
            if len(kw) > AT_ALL_KEYWORD_MAX_LEN:
                return error_response(f"单个关键词最长 {AT_ALL_KEYWORD_MAX_LEN} 字")
            kws = p._at_all_keywords()
            adding = action == "add_keyword"
            if adding:
                if kw in kws:
                    return error_response(f"关键词「{kw}」已在限定列表中")
                if len(kws) >= AT_ALL_KEYWORD_MAX_COUNT:
                    return error_response(f"关键词最多 {AT_ALL_KEYWORD_MAX_COUNT} 个，先删些再添加")
                p._set_cfg("at_all_keywords", [*kws, kw])
            else:
                if kw not in kws:
                    return error_response(f"关键词「{kw}」不在限定列表中")
                p._set_cfg("at_all_keywords", [k for k in kws if k != kw])
            p._save_config()
            p._record_event(
                "info", detail=f"面板{'加入' if adding else '移除'} @全体关键词「{kw}」"
            )
            return json_response({"keywords": p._at_all_keywords()})
        return error_response("不支持的操作")

    async def sessions(self):
        p = self.p
        body = await self._body()
        action = str(body.get("action") or "")
        umo = str(body.get("umo") or "").strip()
        if action == "add":
            parts = umo.split(":")
            if len(parts) != 3 or not all(parts):
                return error_response(
                    "会话 ID 格式应为 平台:消息类型:会话号，例如 aiocqhttp:GroupMessage:123456"
                )
            sessions = p._sessions()
            if umo in sessions:
                return error_response("该会话已在推送列表中")
            sessions.append(umo)
            p._set_cfg("push_sessions", sessions)
            p._save_config()
            p._record_event("info", detail=f"通过面板新增推送目标 {umo}")
            return json_response({"added": umo})
        if action == "remove":
            sessions = p._sessions()
            if umo not in sessions:
                return error_response("该会话不在推送列表中")
            sessions.remove(umo)
            p._set_cfg("push_sessions", sessions)
            p._save_config()
            p._record_event("info", detail=f"通过面板移除推送目标 {umo}")
            return json_response({"removed": umo})
        return error_response("不支持的操作")

    async def album_rules(self):
        p = self.p
        body = await self._body()
        action = str(body.get("action") or "")
        if action == "add":
            uid = self._extract_uid(str(body.get("uid") or "")) or ""
            gid = str(body.get("gid") or "").strip()
            album = str(body.get("album") or "").strip()
            if not uid or not gid or not album:
                return error_response("需要 uid、群号、相册名（或 ID）三项")
            if not gid.isdigit():
                return error_response("群号应为纯数字")
            p._sync_accounts_from_config()
            if uid not in p.accounts:
                return error_response(f"账号 {uid} 不在监控列表中，请先添加监控")
            rules = p._album_rules()
            rules[(uid, gid)] = album
            p._set_cfg("album_rules", [f"{u}:{g}:{a}" for (u, g), a in rules.items()])
            p._save_config()
            p._record_event(
                "info",
                uid=uid,
                name=p._account_name(uid),
                detail=f"面板绑定相册：群 {gid} 相册「{album}」",
            )
            return json_response({"uid": uid, "gid": gid, "album": album})
        if action == "remove":
            uid = str(body.get("uid") or "").strip()
            gid = str(body.get("gid") or "").strip()
            rules = p._album_rules()
            if (uid, gid) not in rules:
                return error_response("该规则不存在（可能已被移除）")
            rules.pop((uid, gid))
            p._set_cfg("album_rules", [f"{u}:{g}:{a}" for (u, g), a in rules.items()])
            p._save_config()
            p._record_event("info", uid=uid, detail=f"面板移除相册绑定：群 {gid}")
            return json_response({"removed": f"{uid}:{gid}"})
        return error_response("不支持的操作")

    async def keyword_rules(self):
        """关键词相册路由规则的增删。uid 为空表示全局规则，非空表示博主规则。"""
        p = self.p
        body = await self._body()
        action = str(body.get("action") or "")
        if action == "add":
            raw_uid = str(body.get("uid") or "").strip()
            keyword = str(body.get("keyword") or "").strip()
            gid = str(body.get("gid") or "").strip()
            album = str(body.get("album") or "").strip()
            if not keyword or not gid or not album:
                return error_response("需要关键词、群号、相册名（或 ID）")
            if ":" in keyword:
                return error_response("关键词不能包含冒号")
            if not gid.isdigit():
                return error_response("群号应为纯数字")
            uid = ""
            if raw_uid:
                uid = self._extract_uid(raw_uid) or ""
                if not uid:
                    return error_response(
                        "博主 uid 无法识别（填纯数字或主页链接，留空表示全局规则）"
                    )
                p._sync_accounts_from_config()
                if uid not in p.accounts:
                    return error_response(f"账号 {uid} 不在监控列表中，请先添加监控")
            uid = uid or None  # 全局规则统一存 None，与 _parse_keyword_rules 一致
            rules = p._album_keyword_rules()
            for i, (u, k, g, _) in enumerate(rules):
                if u == uid and k == keyword and g == gid:
                    rules[i] = (uid, keyword, gid, album)
                    break
            else:
                rules.append((uid, keyword, gid, album))
            p._set_cfg(
                "album_keyword_rules",
                [f"{u}:{k}:{g}:{a}" if u else f"{k}:{g}:{a}" for u, k, g, a in rules],
            )
            p._save_config()
            scope = f"博主 {uid}" if uid else "全局"
            p._record_event(
                "info",
                uid=uid or "",
                name=p._account_name(uid) if uid else "",
                detail=f"面板设置关键词路由（{scope}）：「{keyword}」→ 群 {gid} 相册「{album}」",
            )
            return json_response({"uid": uid or "", "keyword": keyword, "gid": gid, "album": album})
        if action == "remove":
            uid = str(body.get("uid") or "").strip() or None
            keyword = str(body.get("keyword") or "").strip()
            gid = str(body.get("gid") or "").strip()
            rules = p._album_keyword_rules()
            if not any(u == uid and k == keyword and g == gid for u, k, g, _ in rules):
                return error_response("该规则不存在（可能已被移除）")
            rules = [r for r in rules if not (r[0] == uid and r[1] == keyword and r[2] == gid)]
            p._set_cfg(
                "album_keyword_rules",
                [f"{u}:{k}:{g}:{a}" if u else f"{k}:{g}:{a}" for u, k, g, a in rules],
            )
            p._save_config()
            scope = f"博主 {uid}" if uid else "全局"
            p._record_event(
                "info",
                uid=uid or "",
                detail=f"面板移除关键词路由（{scope}）：「{keyword}」群 {gid}",
            )
            return json_response({"removed": f"{uid or ''}:{keyword}:{gid}"})
        return error_response("不支持的操作")

    # ---------------- P6：敏感信息与上传适配 ----------------

    @staticmethod
    def _safe_error(value: Any, limit: int = 240) -> str:
        text = _URL_OR_IP_RE.sub("<地址已隐藏>", str(value or ""))
        return text[:limit].rstrip() + ("…" if len(text) > limit else "")

    def _record_unified(self, detail: str) -> None:
        record = getattr(self.p, "_record_event", None)
        if not callable(record):
            return
        try:
            record("info", detail=f"面板：{detail}")
        except Exception as exc:
            self._log.debug(f"记录面板动态失败: {exc!r}")

    _UPLOAD_TTL_SECONDS = 6 * 3600
    _UPLOAD_KEEP_FILES = 50

    @classmethod
    def _prune_upload_dir(cls, folder: Path) -> None:
        """清理过期的面板上传文件：它们只供紧接着的一次导入使用。"""
        try:
            entries = sorted(
                (path for path in folder.iterdir() if path.is_file()),
                key=lambda path: path.stat().st_mtime,
            )
        except OSError:
            return
        now = time.time()
        for index, path in enumerate(entries):
            try:
                stale = now - path.stat().st_mtime > cls._UPLOAD_TTL_SECONDS
            except OSError:
                continue
            if stale or index < len(entries) - cls._UPLOAD_KEEP_FILES:
                with contextlib.suppress(OSError):
                    path.unlink()

    async def _read_upload(self, body: dict) -> tuple[Path | None, str]:
        suffix = Path(str(body.get("filename") or "upload.bin")).suffix.lower()
        raw_b64 = str(body.get("content_base64") or "").strip()
        data: bytes | None = None
        if raw_b64:
            # base64 先按长度预检再解码：直接 b64decode 会把超大请求整块解进内存
            if len(raw_b64) * 3 / 4 > _UPLOAD_MAX_BYTES:
                return None, "上传文件超过 64MB"
            try:
                data = base64.b64decode(raw_b64, validate=True)
            except (ValueError, TypeError):
                return None, "上传内容不是有效的 base64"
        else:
            storage = None
            name = str(body.get("filename") or "")
            for attribute in ("files", "form"):
                source = getattr(web_request, attribute, None)
                if source is None:
                    continue
                try:
                    value = source() if callable(source) else source
                    if inspect.isawaitable(value):
                        value = await value
                except Exception:
                    continue
                if hasattr(value, "get"):
                    storage = value.get("file") or value.get("upload")
                    if storage is None and hasattr(value, "values"):
                        storage = next(iter(value.values()), None)
                    if storage is not None:
                        name = str(getattr(storage, "filename", "") or name)
                        suffix = Path(name).suffix.lower()
                        break
            if storage is None:
                return None, "请选择文件（或提交 filename + content_base64）"
            declared = getattr(storage, "size", None)
            if isinstance(declared, int) and declared > _UPLOAD_MAX_BYTES:
                return None, "上传文件超过 64MB"
            reader = getattr(storage, "read", None)
            if not callable(reader):
                return None, "上传对象不支持读取"
            data = reader()
            if inspect.isawaitable(data):
                data = await data
            if not isinstance(data, bytes):
                data = bytes(data or b"")
        if suffix not in _UPLOAD_SUFFIXES:
            return None, "只接受 CSV、Markdown 或 JSON 文件"
        if not data:
            return None, "上传文件为空"
        if len(data) > _UPLOAD_MAX_BYTES:
            return None, "上传文件超过 64MB"
        base = getattr(self.p, "_data_dir", None)
        if not base:
            # 不回落 Path.cwd()：那会把用户上传件写进 AstrBot 安装目录
            return None, "数据目录尚未就绪，暂时无法接收上传"
        folder = Path(base) / "webui_uploads"
        # 建目录与清理过期上传件都是同步磁盘操作，放线程避免卡住事件循环
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(self._prune_upload_dir, folder)
        # 文件名用内容摘要：内建 hash() 受 PYTHONHASHSEED 影响，跨进程不可复现
        digest = hashlib.sha1(data).hexdigest()[:12]
        target = folder / f"{int(time.time())}-{digest}{suffix}"
        # 写盘同样放线程：64MB 的同步写会卡住事件循环
        await asyncio.to_thread(target.write_bytes, data)
        return target, ""

    @staticmethod
    def _row_summary(row: dict) -> dict:
        text = str(row.get("cleaned_text") or row.get("text") or "")
        return {
            "mid": str(row.get("mid") or ""),
            "uid": str(row.get("uid") or ""),
            "author": str(row.get("author") or ""),
            "created_at": int(row.get("created_at") or 0),
            "link": str(row.get("link") or ""),
            "text": text[:280],
            "pic_total": int(row.get("pic_total") or 0),
            "is_long": bool(row.get("is_long")),
            "is_retweet": bool(row.get("is_retweet")),
            "cleaned": bool(str(row.get("cleaned_text") or "").strip()),
        }

    # ---------------- P6：归档与回填 ----------------

    async def archive_overview(self):
        p = self.p
        store = p.archive_store() if callable(getattr(p, "archive_store", None)) else None
        if store is None:
            return json_response(
                {
                    "enabled": False,
                    "posts": 0,
                    "media": {},
                    "copylib": {},
                    "backfill": {},
                    "recent": [],
                }
            )
        try:
            posts = await asyncio.to_thread(store.count_posts)
            media = await asyncio.to_thread(store.media_stats)
            copylib = await asyncio.to_thread(store.copy_stats)
            recent = await p.archive_query("", limit=8, offset=0)
        except Exception as exc:
            return error_response(f"读取归档总览失败：{self._safe_error(exc)}", 500)
        return json_response(
            {
                "enabled": True,
                "posts": posts,
                "media": media,
                "copylib": copylib,
                "backfill": {
                    "running": bool(p.archive_backfill_running()),
                    "stats": dict(getattr(p, "_backfill_last_stats", {}) or {}),
                },
                "recent": [self._row_summary(row) for row in recent],
            }
        )

    async def archive_list(self):
        body = await self._body()
        uid = self._extract_uid(str(body.get("uid") or "")) or str(body.get("uid") or "")
        try:
            limit = max(1, min(50, int(body.get("limit") or 10)))
            offset = max(0, int(body.get("offset") or 0))
        except (TypeError, ValueError):
            return error_response("limit/offset 必须为整数")
        rows = await self.p.archive_query(uid, limit=limit, offset=offset)
        return json_response(
            {
                "uid": uid,
                "limit": limit,
                "offset": offset,
                "items": [self._row_summary(row) for row in rows],
            }
        )

    async def archive_backfill(self):
        p = self.p
        body = await self._body()
        if not body:
            return json_response(
                {
                    "running": bool(p.archive_backfill_running()),
                    "stats": dict(getattr(p, "_backfill_last_stats", {}) or {}),
                }
            )
        if str(body.get("action") or "") == "stop":
            p.archive_backfill_stop()
            self._record_unified("请求停止微博回填")
            return json_response({"stopped": True})
        uid = self._extract_uid(str(body.get("uid") or "")) or ""
        if not uid:
            return error_response("请输入有效微博 uid")
        if p.archive_backfill_running():
            return error_response("已有回填任务在跑", 409)
        since = str(body.get("since") or "").strip()[:10]
        full = bool(body.get("full"))

        async def runner():
            try:
                await p.archive_backfill(uid, since=since, full=full)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning(f"面板回填失败 uid={uid}: {exc!r}")

        task = asyncio.create_task(runner())
        p._backfill_task = task
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)
        self._record_unified(f"启动回填 {uid}")
        return json_response({"started": True, "uid": uid, "since": since, "full": full})

    async def archive_reconcile(self):
        body = await self._body()
        uid = self._extract_uid(str(body.get("uid") or "")) or str(body.get("uid") or "")
        report = await self.p.archive_reconcile(uid)
        if report.get("error"):
            return error_response(report["error"], 409)
        self._record_unified(f"完成归档对账 {uid or '全部账号'}")
        return json_response(report)

    async def archive_import(self):
        body = await self._body()
        path, error = await self._read_upload(body)
        if path is None:
            return error_response(error)
        if path.suffix.lower() != ".json":
            return error_response("归档导入只接受 JSON 种子文件")
        result = await self.p.archive_import_json(str(path))
        if result.get("error"):
            return error_response(self._safe_error(result["error"]), 400)
        self._record_unified(f"导入归档种子 {path.name}")
        return json_response(result)

    # ---------------- P6：图床管线与命名 ----------------

    async def pipeline_status(self):
        try:
            stats = self.p.pipeline_stats()
        except Exception as exc:
            return error_response(f"读取管线状态失败：{self._safe_error(exc)}", 500)
        return json_response(
            {
                "status": stats,
                "queue_worker": bool(
                    getattr(self.p, "_pipeline_worker_task", None)
                    and not self.p._pipeline_worker_task.done()
                ),
            }
        )

    async def pipeline_retry(self):
        body = await self._body()
        try:
            limit = max(1, min(500, int(body.get("limit") or 200)))
        except (TypeError, ValueError):
            return error_response("limit 必须为整数")
        result = await self.p.pipeline_backfill(limit)
        if result.get("error"):
            return error_response(self._safe_error(result["error"]), 409)
        self._record_unified(f"补跑图床管线，入队 {result.get('queued', 0)} 帖")
        return json_response(result)

    async def pipeline_naming(self):
        body = await self._body()
        config = self.p.config
        group = config.get("pipeline") if hasattr(config, "get") else None
        if not isinstance(group, MutableMapping):
            group = {}
        if body:
            directory = str(body.get("naming_directory") or "").strip()
            filename = str(body.get("naming_file") or "").strip()
            categories = body.get("categories")
            if not directory or not filename:
                return error_response("目录模板与文件名模板不能为空")
            if not isinstance(categories, dict):
                return error_response("categories 必须是 JSON 对象")
            group["naming_directory"] = directory
            group["naming_file"] = filename
            group["categories"] = {
                str(key).strip(): str(value).strip()
                for key, value in categories.items()
                if str(key).strip() and str(value).strip()
            }
            config["pipeline"] = group
            self.p._save_config()
            self._record_unified("更新图床命名模板与博主分类")
        return json_response(
            {
                "naming_directory": str(
                    group.get("naming_directory") or "{category}/【微博图集@{account}】/"
                ),
                "naming_file": str(
                    group.get("naming_file") or "【微博@{account}】{date}-{index}：{title}.jpg"
                ),
                "categories": group.get("categories") or {},
            }
        )

    # ---------------- P6：文案库 ----------------

    async def copylib_overview(self):
        stats = self.p.copylib_status()
        if not stats.get("ok"):
            return error_response(stats.get("error", "文案库不可用"), 409)
        return json_response(stats)

    async def copylib_list(self):
        body = await self._body()
        uid = self._extract_uid(str(body.get("uid") or "")) or ""
        keyword = str(body.get("keyword") or "").strip()[:60]
        try:
            limit = max(1, min(50, int(body.get("limit") or 10)))
        except (TypeError, ValueError):
            return error_response("limit 必须为整数")
        rows = self.p.copylib_list(uid, keyword, limit)
        return json_response({"uid": uid, "keyword": keyword, "items": rows})

    async def copylib_import(self):
        body = await self._body()
        path, error = await self._read_upload(body)
        if path is None:
            return error_response(error)
        if path.suffix.lower() not in {".csv", ".md", ".markdown"}:
            return error_response("文案导入只接受 CSV 或 Markdown 文件")
        uid = self._extract_uid(str(body.get("uid") or "")) or ""
        result = await self.p.copylib_import(str(path), uid)
        if not result.get("ok"):
            return error_response(self._safe_error(result.get("error")), 400)
        self._record_unified(f"导入文案 {path.name}")
        return json_response(result)

    # ---------------- P6：溯源与签到 ----------------

    async def trace_status(self):
        trace = getattr(self.p, "_trace_plugin", None)
        if trace is None:
            return error_response("溯源域未初始化", 409)
        try:
            library = await asyncio.to_thread(trace.library.stats)
        except Exception as exc:
            return error_response(f"读取图库状态失败：{self._safe_error(exc)}", 500)
        vector: dict[str, Any] = {
            "enabled": bool(getattr(trace.vector, "enabled", False)),
            "model": str(getattr(trace.vector, "embed_model", "") or ""),
            "collection": str(getattr(trace.vector, "collection", "") or ""),
            "count": None,
        }
        if vector["enabled"]:
            try:
                vector["count"] = await trace.vector.count()
            except Exception as exc:
                vector["error"] = self._safe_error(exc)
        checkin = trace.config.get("checkin") if hasattr(trace.config, "get") else {}
        checkin_mode = str(checkin.get("mode") or "quote") if hasattr(checkin, "get") else "quote"
        return json_response(
            {
                "library": library,
                "vector": vector,
                "checkin_mode": checkin_mode,
                "rescan": dict(self._trace_job),
            }
        )

    async def trace_rescan(self):
        trace = getattr(self.p, "_trace_plugin", None)
        if trace is None:
            return error_response("溯源域未初始化", 409)
        current = self._trace_task
        if current is not None and not current.done():
            return error_response("图库重扫已在进行中", 409)
        body = await self._body()
        force = bool(body.get("force"))
        event = _ApiEvent("溯源重扫 force" if force else "溯源重扫")

        async def runner():
            self._trace_job.update(running=True, messages=[], error="")
            try:
                async for result in trace.rescan(event):
                    text = str(getattr(result, "text", "") or "")
                    if text:
                        self._trace_job["messages"].append(text)
                self._trace_job["messages"].extend(event.messages)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._trace_job["error"] = self._safe_error(exc)
                self._log.warning(f"面板重扫失败: {exc!r}")
            finally:
                self._trace_job["running"] = False

        task = asyncio.create_task(runner())
        self._trace_task = task
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)
        self._record_unified(f"启动图库重扫（force={force}）")
        return json_response({"started": True, "force": force})

    async def trace_repair_history(self):
        trace = getattr(self.p, "_trace_plugin", None)
        if trace is None:
            return error_response("溯源域未初始化", 409)
        try:
            cleaned = int(trace._repair_history_keys())
        except Exception as exc:
            return error_response(f"历史键修复失败：{self._safe_error(exc)}", 500)
        self._record_unified(f"修复会话历史键 {cleaned} 条")
        return json_response({"cleaned": cleaned})
