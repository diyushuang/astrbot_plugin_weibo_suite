"""图床 → 向量库/图库的反向收录巡检。

解决的缺口：正常链路只保证「归档时上传过的图会被写进 Qdrant」，但图床里
可能早有图片（另一台实例上传、手工上传、旧版本只上传没入向量库），这些图
在 `/溯源` 里查不到。本模块枚举图床文件，按 App 的三层去重思路补登记：

1. **文件名**：先比对 Qdrant payload 的 `file_name` 与直链 basename；命中即
   视为已收录，连下载都不做（对应参考 App 的 L1）。
2. **内容**：文件名没命中才下载并计算 pHash，再查 Qdrant 的 pHash 集合与本地
   图库；同图换个文件名也判为已收录（对应 L3）。
3. **业务身份**：微博图片按 `【微博@...】` / `【微博图集@...】` 目录识别，
   缩略图目录一律排除；同一内容在一次扫描里只补一条（对应 L2 的 pid 级去重）。

最关键的边界：**补录只写 Qdrant 与本地图库，绝不调用 `bed.store()`**。图片
已经在图床里了，再上传一次就是用户明令禁止的「图床重复收录」。
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import re
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import unquote, urlsplit

try:
    from astrbot.api import logger
except Exception:  # 兼容独立模块加载/本地自测环境
    import logging

    logger = logging.getLogger("bed_audit")

try:
    from ..shared.common import as_int
    from .features import is_degenerate_phash
    from .library import DEGENERATE_SKIP
    from .qdrant_client import VectorPointInput
    from .storage_utils import BedFile, bed_file_id
except ImportError:  # 兼容插件以独立模块方式加载
    from features import is_degenerate_phash  # type: ignore[no-redef]
    from library import DEGENERATE_SKIP  # type: ignore[no-redef]
    from qdrant_client import VectorPointInput  # type: ignore[no-redef]
    from storage_utils import BedFile, bed_file_id  # type: ignore[no-redef]

    from astrbot_plugin_weibo_suite.shared.common import as_int  # type: ignore[no-redef]

# 与 trace/constants.py 的 IMAGE_EXTS 保持一致；单独放一份是为了让本模块
# 不依赖宿主常量表，可被完全离线的单测直接 import。
IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "bmp", "tif", "tiff"})
# 参考 App 的 L1：只归一 SAF 风格的 ` (1)` / `(12)` 撞名后缀，4 位年份不算。
_DUP_SUFFIX_RE = re.compile(r"\s*\(\d{1,2}\)$")
_WEIBO_FILE_RE = re.compile(r"^【微博(?:图集)?@[^】]+】")
_WEIBO_DIR_MARK = "【微博图集@"
# 一次向 Qdrant 提交多少张补录图；与 pipeline 的批量口径接近，避免 embed
# 并发过高打满网关，也避免逐张 PUT 放大往返。
FLUSH_BATCH_SIZE = 20


def canonical_name(name: str) -> str:
    """归一 ` (1)` 撞名后缀，保留扩展名与原文件名大小写。

    例：`【微博@博主】20260101-1：图 (1).jpg` → `...图.jpg`。
    `旅行 (2024).jpg` 不处理（4 位年份不是 SAF 的撞名后缀）。
    """
    text = str(name or "").strip()
    base = posixpath.basename(text)
    stem, ext = os.path.splitext(base)
    while True:
        stripped = _DUP_SUFFIX_RE.sub("", stem)
        if not stripped or stripped == stem:
            break
        stem = stripped
    return stem + ext


def name_key(name: str) -> str:
    """文件名比较键：URL 解码 + casefold；中文不受影响。"""
    return unquote(str(name or "")).strip().casefold()


def url_keys(url: str) -> tuple[str, str]:
    """返回（完整 URL 键, 图床内路径键）；后者吸收自定义域名差异。"""
    text = str(url or "").strip()
    if not text:
        return "", ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return name_key(text), ""
    full = f"{parsed.netloc}{unquote(parsed.path)}".casefold()
    try:
        inner = bed_file_id(text)
    except ValueError:
        inner = ""
    return full, name_key(inner)


def is_thumbnail(entry: BedFile) -> bool:
    """缩略图目录判断：pipeline 固定用 `thumbnails/`，不收录。"""
    combined = f"/{entry.directory}/{entry.name}/".replace("\\", "/").casefold()
    return "thumbnails/" in combined


def is_image(entry: BedFile) -> bool:
    ext = os.path.splitext(entry.file_name or entry.name)[1].lower().lstrip(".")
    return ext in IMAGE_EXTS


def is_weibo_image(entry: BedFile) -> bool:
    """只认微博管线产物，避免把图床里其它业务的图也补进来。

    目录名带 `【微博图集@`，或文件名以 `【微博@` / `【微博图集@` 开头。
    `thumbnails/` 先排除，否则 720px 缩略图会被当成原图收录。
    """
    if not is_image(entry) or is_thumbnail(entry):
        return False
    if _WEIBO_DIR_MARK in unquote(f"{entry.directory}/{entry.name}"):
        return True
    return bool(_WEIBO_FILE_RE.match(unquote(entry.file_name or "")))


@dataclass
class IndexSnapshot:
    """Qdrant 现有 payload 收敛出的三套索引。"""

    names: set = field(default_factory=set)
    urls: set = field(default_factory=set)
    paths: set = field(default_factory=set)
    phashes: set = field(default_factory=set)
    points: int = 0


def build_index_snapshot(points: list) -> IndexSnapshot:
    """把 `scroll_payloads` 的结果整理成文件名/直链/pHash 集合。"""
    snapshot = IndexSnapshot()
    for point in points:
        if not isinstance(point, dict):
            continue
        payload = point.get("payload")
        if not isinstance(payload, dict):
            continue
        snapshot.points += 1
        file_name = payload.get("file_name")
        if isinstance(file_name, str) and file_name.strip():
            snapshot.names.add(name_key(canonical_name(file_name)))
        image_url = payload.get("image_url")
        if isinstance(image_url, str) and image_url.strip():
            try:
                full, inner = url_keys(image_url)
                base = posixpath.basename(unquote(urlsplit(image_url).path))
            except ValueError:
                full, inner, base = "", "", ""
            if full:
                snapshot.urls.add(full)
            if inner:
                snapshot.paths.add(inner)
            if base:
                snapshot.names.add(name_key(canonical_name(base)))
        phash = payload.get("phash")
        if isinstance(phash, str) and phash.strip():
            snapshot.phashes.add(phash.strip().lower())
    return snapshot


@dataclass
class BedAuditStats:
    """一次巡检的对外结果。"""

    mode: str = ""
    listed: int = 0
    weibo: int = 0
    known_by_name: int = 0
    known_by_content: int = 0
    missing: int = 0
    added_vector: int = 0
    added_library: int = 0
    duplicate_library: int = 0
    degenerate: int = 0
    errors: int = 0
    dry_run: bool = False
    elapsed: float = 0.0
    samples: list = field(default_factory=list)
    error_samples: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class BedAuditService:
    """执行一次图床反向收录巡检。

    `download` / `compute_features` / `remove_file` 由宿主注入，便于单测替换：
    - download(url) -> 本地临时路径；
    - compute_features(path, hash_size) -> (features, mime)；
    - remove_file(path) -> None（静默失败）。
    """

    def __init__(
        self,
        *,
        bed,
        vector,
        library,
        download,
        compute_features,
        remove_file,
        hash_size: int = 16,
    ):
        self.bed = bed
        self.vector = vector
        self.library = library
        self.download = download
        self.compute_features = compute_features
        self.remove_file = remove_file
        self.hash_size = int(hash_size)

    async def run(self, *, dry_run: bool = False, limit: int = 0) -> BedAuditStats:
        started = time.monotonic()
        stats = BedAuditStats(mode=str(getattr(self.bed, "mode", "") or ""), dry_run=dry_run)
        entries = await self.bed.list_files(limit=max(0, int(limit)))
        stats.listed = len(entries)
        candidates = [entry for entry in entries if is_weibo_image(entry)]
        stats.weibo = len(candidates)
        if not candidates:
            stats.elapsed = time.monotonic() - started
            return stats

        snapshot = await self._snapshot(candidates)
        self._phashes = snapshot.phashes
        pending: list[dict] = []
        for entry in candidates:
            key = name_key(canonical_name(entry.file_name))
            inner = name_key(entry.name)
            if key and key in snapshot.names:
                stats.known_by_name += 1
                continue
            if inner and inner in snapshot.paths:
                stats.known_by_name += 1
                continue
            full, inner_url = url_keys(entry.url)
            if entry.url and (full in snapshot.urls or (inner_url and inner_url in snapshot.paths)):
                stats.known_by_name += 1
                continue
            row = await self._inspect(entry, stats)
            if row is None:
                continue
            stats.missing += 1
            # 立即把待写入的文件名/直链记进快照：同一轮里若再遇到同名或同
            # 直链的图床记录，不再重复下载、更不产生第二条同身份向量点。
            snapshot.names.add(name_key(canonical_name(row["entry"].file_name)))
            if row["entry"].name:
                snapshot.paths.add(name_key(row["entry"].name))
            if row["entry"].url:
                full, inner = url_keys(row["entry"].url)
                if full:
                    snapshot.urls.add(full)
                if inner:
                    snapshot.paths.add(inner)
            if len(stats.samples) < 20:
                stats.samples.append(row["name"])
            if dry_run:
                snapshot.phashes.add(row["phash"])
                continue
            pending.append(row)
            snapshot.phashes.add(row["phash"])
            if len(pending) >= FLUSH_BATCH_SIZE:
                await self._flush(pending, stats)
                pending = []
        if pending and not dry_run:
            await self._flush(pending, stats)
        stats.elapsed = time.monotonic() - started
        return stats

    async def _snapshot(self, candidates: list) -> IndexSnapshot:
        """构建已收录快照。

        CFB 图床有公开直链，必须有 Qdrant 才能判断「收录」；Qdrant 查询失败
        直接抛出，不能按空索引继续补录——那会把所有图重复写一遍。
        local 模式没有直链，以本地图库 pHash 为准。
        """
        has_url = any(entry.url for entry in candidates)
        if not has_url:
            return IndexSnapshot()
        if not bool(getattr(self.vector, "qdrant_ready", False)):
            raise RuntimeError(
                "向量库未配置：图床巡检需要 vector_search.qdrant_url 才能判断哪些图已收录"
            )
        if not bool(getattr(self.vector, "enabled", False)):
            raise RuntimeError(
                "向量引擎未启用：补齐 vector_search.embed_base_url / embed_model "
                "后才能判断并补录图床图片"
            )
        points = await self.vector.scroll_payloads(
            key="image_url",
            page_size=512,
            with_payload_keys=["file_name", "image_url", "phash"],
        )
        return build_index_snapshot(points)

    async def _inspect(self, entry: BedFile, stats: BedAuditStats) -> dict | None:
        """下载（如有必要）并核对内容；未收录则返回待补录行。"""
        path = entry.local_path
        is_tmp = False
        # keep_tmp 只在成功返回待补录行时置真：那条路径要留给 _flush 写向量/图库
        # 之后再删。其余所有出口（判重、退化 pHash、异常、取消）都在 finally 里
        # 清掉临时文件，避免一次巡检在磁盘上留下成百上千个下载副本。
        keep_tmp = False
        try:
            if not path:
                path = await self.download(entry.url)
                is_tmp = True
            features, mime = await asyncio.to_thread(self.compute_features, path, self.hash_size)
            phash = str(getattr(features, "phash", "") or "").strip().lower()
            if is_degenerate_phash(phash):
                stats.degenerate += 1
                return None
            if phash in self._snapshot_phashes:
                stats.known_by_content += 1
                return None
            if not entry.url and self.library is not None:
                # local 模式没有公开直链，只能用本地图库判断「已收录」；
                # CFB 图床即使本地图库有同图，也可能没进 Qdrant，仍应补向量。
                hit = await asyncio.to_thread(self.library.find_by_phash, phash)
                if hit is not None:
                    stats.known_by_content += 1
                    return None
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            keep_tmp = True
            return {
                "entry": entry,
                "path": path,
                "is_tmp": is_tmp,
                "phash": phash,
                "mime": str(mime or ""),
                "width": int(getattr(features, "width", 0) or 0) or None,
                "height": int(getattr(features, "height", 0) or 0) or None,
                "size": int(size),
                "name": entry.name,
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            stats.errors += 1
            if len(stats.error_samples) < 10:
                stats.error_samples.append(f"{entry.name}: {str(exc)[:160]}")
            return None
        finally:
            if is_tmp and path and not keep_tmp:
                self.remove_file(path)

    @property
    def _snapshot_phashes(self) -> set:
        # run() 里把快照挂在实例上供 _inspect 读取；显式属性避免把大对象塞进
        # 每个待补录行，也避免重复查 Qdrant。
        return getattr(self, "_phashes", set())

    async def _flush(self, pending: list, stats: BedAuditStats) -> None:
        """批量写 Qdrant，再逐条写本地图库；任何一步失败都不吞。"""
        vector_entries = [row for row in pending if row["entry"].url]
        vector_ok = False
        try:
            if vector_entries:
                if not bool(getattr(self.vector, "enabled", False)):
                    raise RuntimeError(
                        "向量引擎未启用：补齐 vector_search.embed_base_url / embed_model "
                        "后才能把图床图片补进 Qdrant"
                    )
                inputs = [
                    VectorPointInput(
                        path=row["path"],
                        point_key=f"phash:{row['phash']}",
                        image_url=row["entry"].url,
                        file_size=row["size"],
                        width=row["width"],
                        height=row["height"],
                        mime=row["mime"],
                        file_name=row["entry"].file_name,
                    )
                    for row in vector_entries
                ]
                written = await self.vector.upsert_file_batch(inputs)
                stats.added_vector += int(written or 0)
            vector_ok = True
            for row in pending:
                await self._add_library(row, stats, vector_ok=vector_ok)
        finally:
            for row in pending:
                if row["is_tmp"]:
                    self.remove_file(row["path"])

    async def _add_library(self, row: dict, stats: BedAuditStats, *, vector_ok: bool) -> None:
        """补本地 pHash 图库；同图已在库时只计数，不报错。"""
        del vector_ok  # 保留参数：调用方明确表达「向量已成功」后再落库
        if self.library is None:
            return
        entry = row["entry"]
        payload = {
            "phash": row["phash"],
            "width": row["width"],
            "height": row["height"],
            "file_size": row["size"],
            "image_url": entry.url,
            "file_path": entry.local_path or "",
            "source": "bed_audit",
            "note": f"图床补录：{entry.name}",
        }
        try:
            entry_id, dup = await asyncio.to_thread(self.library.add_if_absent, payload)
            if entry_id is DEGENERATE_SKIP:
                stats.degenerate += 1
            elif dup is not None:
                stats.duplicate_library += 1
            else:
                stats.added_library += 1
        except Exception as exc:
            # 向量已经写入成功，图库失败只是哈希引擎少一条；不能让异常把
            # 整轮状态打成失败（下一轮会因 pHash 已在 Qdrant 而直接跳过）。
            stats.errors += 1
            if len(stats.error_samples) < 10:
                stats.error_samples.append(f"{entry.name}: 图库写入失败 {str(exc)[:120]}")
            logger.warning(f"图床补录写本地图库失败 {entry.name}: {exc}")


def format_bed_audit(stats: BedAuditStats, *, source: str = "手动") -> str:
    """把巡检结果压成群聊可读的短报告。"""
    lines = [
        f"🧾 图床收录检查（{source}）：图床 {stats.mode or '未知'}，"
        f"共 {stats.listed} 个文件、微博图 {stats.weibo} 张"
    ]
    if stats.dry_run:
        lines.append(
            f"· 只检查不写入：未收录 {stats.missing} 张"
            f"（文件名命中 {stats.known_by_name}，内容命中 {stats.known_by_content}）"
        )
    else:
        lines.append(
            f"· 检查结果：发现未收录 {stats.missing} 张，向量 +{stats.added_vector}，"
            f"图库 +{stats.added_library}"
        )
        lines.append(
            f"· 去重：文件名命中 {stats.known_by_name}，内容命中 {stats.known_by_content}"
            + (f"，图库已有 {stats.duplicate_library}" if stats.duplicate_library else "")
        )
    if stats.degenerate:
        lines.append(f"· 退化 pHash 跳过 {stats.degenerate} 张（纯色/大面积平坦图）")
    if stats.errors:
        lines.append(f"· 失败 {stats.errors} 张（详情见机器人日志）")
        for sample in stats.error_samples[:3]:
            lines.append(f"  - {sample}")
    if stats.dry_run and stats.samples:
        shown = "、".join(stats.samples[:5])
        lines.append(f"· 未收录示例：{shown}" + (" …" if stats.missing > 5 else ""))
    lines.append(f"· 耗时 {stats.elapsed:.1f}s")
    return "\n".join(lines)


def parse_audit_args(text: str) -> tuple[bool, int]:
    """解析 `微博 图床检查` 参数：dry / 数字上限。"""
    dry = False
    limit = 0
    for token in str(text or "").replace("，", " ").split():
        low = token.strip().lower()
        if low in ("dry", "check", "只查", "检查"):
            dry = True
            continue
        value = as_int(low, 0)
        if value > 0:
            limit = value
    return dry, limit
