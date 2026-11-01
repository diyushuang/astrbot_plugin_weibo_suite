"""文案库编排（P5/F3）：archive.db 即文案库，CSV 降级为导入/导出格式。

本模块只做编排与格式转换，清洗语义在 weibo/cleaner.py，SQL 在 weibo/archive_db.py。
设计约束（见 开发计划.md §三 F3 与 §5.4 ③）：

- 单一事实源：archive.db posts 表（cleaned_text 为正文），不再维护 CSV/MD 副本，
  weibo_post_map.json 与 build_weibo_map.py 一并退役。
- CSV/MD 只是迁移通道：导出给人看/给旧流程吃，导入把已有账号的现成 CSV 灌进库。
- 签到读库：copylib_snapshot() 返回 (指纹, 行列表)，由入口 main.py 转成 WeiboPost 交给
  溯源域；两域互不 import 的纪律靠这个窄接口维持。
"""

from __future__ import annotations

import asyncio
import csv
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .archive_db import ArchiveStore
from .constants import COPYLIB_CSV_HEADER, COPYLIB_DIR_NAME, COPYLIB_LIST_LIMIT
from .runtime import *

_COPY_TZ = timezone(timedelta(hours=8))
_MID_RE = re.compile(r"/status/(\d{6,})|/(\d{6,})$")


def _copy_text(value) -> str:
    return "" if value is None else str(value)


def _copy_created_at(value) -> int:
    """把 YYYY-MM-DD HH:MM[:SS] 按东八区换成秒级时间戳；失败返回 0。"""
    text = str(value or "").strip()
    if not text:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            moment = datetime.strptime(text, fmt).replace(tzinfo=_COPY_TZ)
        except ValueError:
            continue
        return int(moment.timestamp())
    return 0


def _copy_stamp(created_at: int) -> tuple[str, str]:
    """时间戳 → (YYYY-MM-DD HH:MM, YYYYMMDD)，按东八区。"""
    if not created_at:
        return "", ""
    moment = datetime.fromtimestamp(int(created_at), _COPY_TZ)
    return moment.strftime("%Y-%m-%d %H:%M"), moment.strftime("%Y%m%d")


def copylib_mid_from_link(link) -> str:
    """从微博链接取 mid（/status/<mid> 或 <uid>/<mid>）。"""
    text = str(link or "").strip()
    match = _MID_RE.search(text)
    if not match:
        return ""
    return match.group(1) or match.group(2) or ""


def copylib_uid_from_link(link) -> str:
    """从 https://weibo.com/<uid>/<mid> 取 uid；取不到返回空串。"""
    match = re.search(r"weibo\.com/(\d{5,})/", str(link or ""))
    return match.group(1) if match else ""


class CopylibMixin:
    """文案库：清洗回填、列表、导出、导入与签到快照。"""

    def _copylib_dir(self) -> Path:
        """导出目录：<data_dir>/copylib/（与 archive / weibo 同级）。

        数据目录未就绪时抛错而不是回落 `Path(".")`：回落会把用户文案写进
        AstrBot 安装目录（测试里则是插件源码树），既污染发行包又难排查。
        """
        base = getattr(self, "_data_dir", None)
        if not base:
            raise RuntimeError("数据目录尚未就绪，无法确定文案导出位置")
        path = Path(base) / COPYLIB_DIR_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def copylib_store(self) -> ArchiveStore | None:
        store = getattr(self, "archive_store", None)
        if not callable(store):
            return None
        return store()

    def copylib_enabled(self) -> bool:
        ready = getattr(self, "archive_ready", None)
        if callable(ready):
            return bool(ready())
        return self.copylib_store() is not None

    # ------------------------------------------------------------------
    # 概览与列表
    # ------------------------------------------------------------------
    def copylib_status(self, uid: str = "") -> dict:
        """文案库总览：总条数 / 已清洗 / 未清洗 / 长文 / 转发 / 账号维度。"""
        store = self.copylib_store()
        if store is None:
            return {"ok": False, "error": "归档库未就绪（请先启用归档）"}
        stats = store.copy_stats(uid or None)
        stats["ok"] = True
        stats["enabled"] = self.copylib_enabled()
        stats["dirty"] = max(0, stats["total"] - stats["cleaned"])
        stats["accounts"] = store.copy_accounts()
        return stats

    def format_copylib_status(self, uid: str = "") -> str:
        stats = self.copylib_status(uid)
        if not stats.get("ok"):
            return f"文案库不可用：{stats.get('error', '未知原因')}"
        lines = [
            "文案库（archive.db）" + (f" · 账号 {uid}" if uid else ""),
            f"· 帖子 {stats['total']} 条（已清洗 {stats['cleaned']}，待清洗 {stats['dirty']}）",
            f"· 长文 {stats['long']} 条 · 转发 {stats['retweet']} 条"
            f" · 带图 {stats['with_pics']} 条",
        ]
        accounts = stats.get("accounts") or []
        if accounts and not uid:
            lines.append(
                "· 账号："
                + "，".join(
                    f"{item['uid']}（{item['author'] or '未知'}）{item['total']} 条"
                    for item in accounts[:6]
                )
            )
        return "\n".join(lines)

    def copylib_list(
        self, uid: str = "", keyword: str = "", limit: int = COPYLIB_LIST_LIMIT
    ) -> list[dict]:
        store = self.copylib_store()
        if store is None:
            return []
        rows = store.list_copy_posts(
            uid or None, keyword=keyword, limit=max(1, int(limit)), offset=0
        )
        out = []
        for row in rows:
            published, _day = _copy_stamp(int(row.get("created_at") or 0))
            text = _copy_text(row.get("cleaned_text")) or _copy_text(row.get("text"))
            out.append(
                {
                    "mid": row.get("mid", ""),
                    "uid": row.get("uid", ""),
                    "author": row.get("author", ""),
                    "link": row.get("link", ""),
                    "published_at": published,
                    "text": text,
                    "is_long": int(row.get("is_long") or 0),
                    "is_retweet": int(row.get("is_retweet") or 0),
                    "cleaned": bool(str(row.get("cleaned_text") or "").strip()),
                }
            )
        return out

    def format_copylib_list(self, rows: list[dict], uid: str = "") -> str:
        if not rows:
            return "文案库为空" + (f"（账号 {uid}）" if uid else "")
        blocks = []
        for index, row in enumerate(rows, 1):
            author = row["author"] or row["uid"]
            head = f"{index}. {row['published_at'] or '时间未知'} · 微博@{author}"
            flags = []
            if row["is_long"]:
                flags.append("长文")
            if row["is_retweet"]:
                flags.append("转发")
            if not row["cleaned"]:
                flags.append("未清洗")
            if flags:
                head += "（" + "、".join(flags) + "）"
            text = row["text"].strip() or "（无正文）"
            if len(text) > 120:
                text = text[:120] + "…"
            blocks.append(head + "\n" + text)
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # 清洗回填
    # ------------------------------------------------------------------
    def _copylib_clean_rows(self, rows: list[dict]) -> int:
        """同步清洗一批帖子（在线程里跑），返回清洗条数。"""
        from .cleaner import clean_mblog_text

        store = self.copylib_store()
        if store is None:
            return 0
        cleaned = 0
        for row in rows:
            mid = str(row.get("mid") or "").strip()
            if not mid:
                continue
            try:
                result = clean_mblog_text(_copy_text(row.get("text")))
            except Exception as e:  # 单条失败不影响整批
                logger.warning(f"文案清洗失败（mid={mid}）: {e}")
                continue
            store.mark_post_cleaned(mid, result)
            cleaned += 1
        return cleaned

    async def copylib_rebuild(self, uid: str = "", limit: int = 0) -> dict:
        """把尚未清洗的帖子补齐 cleaned_text；limit=0 表示不设上限。"""
        store = self.copylib_store()
        if store is None:
            return {"ok": False, "error": "归档库未就绪", "scanned": 0, "cleaned": 0}
        try:
            rows = await asyncio.to_thread(store.iter_uncleaned_posts, uid or None, int(limit or 0))
        except Exception as e:
            return {"ok": False, "error": f"读取待清洗列表失败: {e}", "scanned": 0, "cleaned": 0}
        if not rows:
            return {"ok": True, "scanned": 0, "cleaned": 0}
        try:
            cleaned = await asyncio.to_thread(self._copylib_clean_rows, rows)
        except ImportError:
            return {"ok": False, "error": "文案清洗模块缺失", "scanned": len(rows), "cleaned": 0}
        except Exception as e:
            return {"ok": False, "error": f"清洗失败: {e}", "scanned": len(rows), "cleaned": 0}
        return {"ok": True, "scanned": len(rows), "cleaned": cleaned}

    # ------------------------------------------------------------------
    # 导出 / 导入
    # ------------------------------------------------------------------
    def _copylib_rows_for_export(self, uid: str = "") -> list[dict]:
        store = self.copylib_store()
        if store is None:
            return []
        return store.list_copy_posts(uid or None, limit=10000, offset=0)

    def _copylib_write_csv(self, uid: str = "") -> dict:
        """把库里的文案导成与 Node 版同构的 CSV（UTF-8 BOM）。"""
        rows = self._copylib_rows_for_export(uid)
        if not rows:
            return {"ok": False, "error": "文案库为空，无可导出内容", "path": "", "count": 0}
        stamp = datetime.now(_COPY_TZ).strftime("%Y%m%d-%H%M%S")
        path = self._copylib_dir() / f"微博原创文案_{uid or 'all'}_{stamp}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(COPYLIB_CSV_HEADER)
            for index, row in enumerate(rows, 1):
                published, _day = _copy_stamp(int(row.get("created_at") or 0))
                author = _copy_text(row.get("author"))
                writer.writerow(
                    [
                        index,
                        published,
                        f"微博@{author}" if author else "微博",
                        _copy_text(row.get("cleaned_text")) or _copy_text(row.get("text")),
                        _copy_text(row.get("link")),
                    ]
                )
        return {"ok": True, "path": str(path), "count": len(rows)}

    async def copylib_export(self, uid: str = "") -> dict:
        try:
            return await asyncio.to_thread(self._copylib_write_csv, uid)
        except Exception as e:
            return {"ok": False, "error": f"导出失败: {e}", "path": "", "count": 0}

    def _copylib_write_markdown(self, uid: str = "") -> dict:
        """按年分组的 Markdown 导出（人读用；不作为长期存储）。"""
        rows = self._copylib_rows_for_export(uid)
        if not rows:
            return {"ok": False, "error": "文案库为空，无可导出内容", "path": "", "count": 0}
        stamp = datetime.now(_COPY_TZ).strftime("%Y%m%d-%H%M%S")
        path = self._copylib_dir() / f"微博原创文案_{uid or 'all'}_{stamp}.md"
        lines = [f"# 微博原创文案（账号 {uid or '全部'}）", ""]
        current_year = ""
        for index, row in enumerate(rows, 1):
            published, day = _copy_stamp(int(row.get("created_at") or 0))
            year = day[:4]
            if year and year != current_year:
                current_year = year
                lines.extend(["", "---", "", f"## {year} 年", ""])
            author = _copy_text(row.get("author"))
            text = (_copy_text(row.get("cleaned_text")) or _copy_text(row.get("text"))).strip()
            suffix = f" —— {published} · 微博@{author}" if author else f" —— {published}"
            lines.append(f"{index}. {text}{suffix}".rstrip())
            lines.append("")
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return {"ok": True, "path": str(path), "count": len(rows)}

    async def copylib_export_markdown(self, uid: str = "") -> dict:
        try:
            return await asyncio.to_thread(self._copylib_write_markdown, uid)
        except Exception as e:
            return {"ok": False, "error": f"导出失败: {e}", "path": "", "count": 0}

    @staticmethod
    def _copylib_read_csv(path: Path) -> list[dict]:
        """读 Node 版 CSV（BOM + 5 列），返回原始条目。"""
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if not header:
                return []
            index = {name.strip(): position for position, name in enumerate(header)}
            out = []
            for row in reader:
                if not row or all(not str(cell).strip() for cell in row):
                    continue

                def cell(key: str, position: int, _row=row) -> str:
                    where = index.get(key, position)
                    return str(_row[where]) if where < len(_row) else ""

                out.append(
                    {
                        "published_at": cell("发布时间", 1),
                        "source": cell("来源", 2),
                        "text": cell("文案", 3),
                        "link": cell("微博链接", 4),
                    }
                )
            return out

    async def copylib_import(self, path: str, uid: str = "") -> dict:
        """把 CSV 文案灌进 archive.db（按 mid upsert，幂等）。"""
        file_path = Path(str(path or "")).expanduser()
        if not file_path.is_file():
            return {"ok": False, "error": f"文件不存在：{file_path}", "records": 0, "imported": 0}
        store = self.copylib_store()
        if store is None:
            return {"ok": False, "error": "归档库未就绪", "records": 0, "imported": 0}
        try:
            items = await asyncio.to_thread(self._copylib_read_csv, file_path)
        except Exception as e:
            return {"ok": False, "error": f"CSV 解析失败: {e}", "records": 0, "imported": 0}
        imported = await asyncio.to_thread(self._copylib_write_rows, store, items, uid)
        return {"ok": True, "records": len(items), "imported": imported}

    @staticmethod
    def _copylib_write_rows(store, items: list[dict], uid: str) -> int:
        """逐条 upsert（同步 SQLite）：整体放线程，别在事件循环里写大文件。"""
        imported = 0
        for item in items:
            mid = copylib_mid_from_link(item["link"])
            account = copylib_uid_from_link(item["link"]) or str(uid or "")
            if not mid or not account:
                continue
            row = {
                "mid": mid,
                "uid": account,
                "author": str(item["source"] or "").replace("微博@", "").strip(),
                "link": item["link"],
                "created_at": _copy_created_at(item["published_at"]),
                "created_raw": item["published_at"],
                "text": item["text"],
                "text_raw": "",
                "source": "csv",
            }
            try:
                store.upsert_post(row)
                store.mark_post_cleaned(mid, item["text"])
            except Exception as e:
                logger.warning(f"文案导入单条失败（mid={mid}）: {e}")
                continue
            imported += 1
        return imported

    # ------------------------------------------------------------------
    # 签到快照（入口 main.py 桥接用）
    # ------------------------------------------------------------------
    def copylib_snapshot(self, uid: str = "") -> tuple[str, list[dict]]:
        """返回 (指纹, 行列表)：指纹变了才重新加载，签到热路径零解析。"""
        store = self.copylib_store()
        if store is None:
            return "", []
        try:
            signature = store.copy_signature()
            rows = store.list_copy_entries(uid or None)
        except Exception as e:
            logger.warning(f"文案库快照读取失败: {e}")
            return "", []
        return signature, rows
