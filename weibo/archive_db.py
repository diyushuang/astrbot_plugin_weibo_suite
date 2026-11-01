"""微博归档库：纯同步 SQLite 存储层。

P3 归档阶段的唯一持久化入口（设计见 开发计划.md §三 F1）。

设计约束：

- **纯同步 + 单 threading.Lock**：所有公开方法都可在任意线程调用，类自己不做
  asyncio（异步侧由 ArchiveMixin 用 asyncio.to_thread 或直接同步短查询包住）。
- **连接参数固定**：check_same_thread=False + WAL + synchronous=NORMAL +
  busy_timeout=5000；插件热重载残留的旧连接靠 busy_timeout 排队而不是立刻
  "database is locked"。
- **库文件 / 媒体根由调用方传入**，媒体文件布局 <media_root>/<uid>/<mid>/<pid>.<ext>；
  media_file() 对相对 local_path 用 media_root 解析。
- **状态机 rank 单调前进**：已经落盘/入册的图不会因为一次重试失败被降级回
  pending（失败只加 attempts + error），否则对账会永远显示"缺图"。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .constants import MEDIA_STATES

# 存储层自带 logger：它不依赖 astrbot 运行时（单测/脚本可直接用），
# 又必须在 schema 降级这类"能毁库"的场景留下明确告警。
logger = logging.getLogger("weibo_suite.archive_db")

_SCHEMA_VERSION = 2
# 批量查媒体时一条 WHERE mid IN (...) 里塞多少个占位符。
#
# 这里不是"撞上限才分批"，而是**主动分批**：SQLite 的绑定变量上限在编译期由
# SQLITE_LIMIT_VARIABLE_NUMBER 决定，3.32 起默认 32766，但 3.32 之前默认只有
# 999，且各发行版/嵌入式绑定可能把下限调得更低（aarch64 的安卓旧版就是 999）。
# 一条 5000 个 mid 的 IN 在新版上能过、在旧版上直接 "too many SQL variables"，
# 这种"只在某些用户那里炸"的错误最难查，所以按 500 一批发，代价只是几次往返。
# 拆成模块级常量纯粹是为了让测试能把它调小、真正触发分批分支。
_BULK_MEDIA_CHUNK = 500

# 媒体状态机的权威顺序（合同第 2 节，定义在 weibo/constants.py）：下标即 rank，
# "达到或超过该 rank" 才计入该态；这里只把顺序翻成 rank 映射，不另存一份字面量。
_MEDIA_STATE_RANK: dict[str, int] = {state: rank for rank, state in enumerate(MEDIA_STATES)}
# 状态推进时要写的时间戳列；pending 没有对应列。
STATE_AT_COLUMN: dict[str, str] = {
    "downloaded": "downloaded_at",
    "album": "album_at",
    "bed": "bed_at",
    "thumb": "thumb_at",
    "vector": "vector_at",
}

# set_post_time 允许改的列（白名单即 SQL 列名来源，防注入）。
_POST_TIME_COLUMNS = frozenset({"pushed_at", "cleaned_at", "reconciled_at"})

_POST_COLUMNS: tuple[str, ...] = (
    "mid",
    "uid",
    "author",
    "bid",
    "link",
    "created_at",
    "created_raw",
    "text",
    "text_raw",
    "text_full",
    "is_long",
    "is_retweet",
    "orig_text",
    "pic_total",
    "pics_json",
    "video_src",
    "raw_json",
    "source",
    "pushed_at",
    "cleaned_at",
    "reconciled_at",
    "cleaned_text",
    "updated_at",
)

_MEDIA_COLUMNS: tuple[str, ...] = (
    "mid",
    "pid",
    "idx",
    "url",
    "alt_url",
    "ext",
    "kind",
    "local_path",
    "size",
    "state",
    "state_rank",
    "attempts",
    "error",
    "downloaded_at",
    "album_at",
    "bed_at",
    "thumb_at",
    "vector_at",
    "thumb_url",
    "updated_at",
)

# 新建 posts 行时各列的落库默认值（空值/None/-1 才用默认值）。
_POST_INSERT_DEFAULTS: dict[str, Any] = {
    "author": "",
    "bid": "",
    "link": "",
    "created_at": 0,
    "created_raw": "",
    "text": "",
    "text_raw": "",
    "text_full": 0,
    "is_long": 0,
    "is_retweet": 0,
    "orig_text": "",
    "pic_total": -1,
    "pics_json": "[]",
    "video_src": "",
    "raw_json": "",
    "source": "monitor",
    "pushed_at": "",
    "cleaned_at": "",
    "reconciled_at": "",
    "cleaned_text": "",
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    mid          TEXT PRIMARY KEY,
    uid          TEXT NOT NULL,
    author       TEXT NOT NULL DEFAULT '',
    bid          TEXT NOT NULL DEFAULT '',
    link         TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL DEFAULT 0,
    created_raw  TEXT NOT NULL DEFAULT '',
    text         TEXT NOT NULL DEFAULT '',
    text_raw     TEXT NOT NULL DEFAULT '',
    text_full    INTEGER NOT NULL DEFAULT 0,
    is_long      INTEGER NOT NULL DEFAULT 0,
    is_retweet   INTEGER NOT NULL DEFAULT 0,
    orig_text    TEXT NOT NULL DEFAULT '',
    pic_total    INTEGER NOT NULL DEFAULT -1,
    pics_json    TEXT NOT NULL DEFAULT '[]',
    video_src    TEXT NOT NULL DEFAULT '',
    raw_json     TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'monitor',
    pushed_at    TEXT NOT NULL DEFAULT '',
    cleaned_at   TEXT NOT NULL DEFAULT '',
    reconciled_at TEXT NOT NULL DEFAULT '',
    cleaned_text TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_uid_created ON posts(uid, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);

CREATE TABLE IF NOT EXISTS media (
    mid          TEXT NOT NULL,
    pid          TEXT NOT NULL,
    idx          INTEGER NOT NULL DEFAULT 0,
    url          TEXT NOT NULL DEFAULT '',
    alt_url      TEXT NOT NULL DEFAULT '',
    ext          TEXT NOT NULL DEFAULT 'jpg',
    kind         TEXT NOT NULL DEFAULT 'image',
    local_path   TEXT NOT NULL DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    state        TEXT NOT NULL DEFAULT 'pending',
    state_rank   INTEGER NOT NULL DEFAULT 0,
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT '',
    downloaded_at TEXT NOT NULL DEFAULT '',
    album_at     TEXT NOT NULL DEFAULT '',
    bed_at       TEXT NOT NULL DEFAULT '',
    thumb_at     TEXT NOT NULL DEFAULT '',
    vector_at    TEXT NOT NULL DEFAULT '',
    thumb_url    TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (mid, pid)
);
CREATE INDEX IF NOT EXISTS idx_media_mid ON media(mid);
CREATE INDEX IF NOT EXISTS idx_media_state ON media(state);

CREATE TABLE IF NOT EXISTS backfill_cursor (
    uid                TEXT PRIMARY KEY,
    next_page          INTEGER NOT NULL DEFAULT 1,
    oldest_created_at  INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'running',
    updated_at         TEXT NOT NULL
);
"""

_POST_UPSERT_SQL = (
    f"INSERT INTO posts ({', '.join(_POST_COLUMNS)})"
    f" VALUES ({', '.join('?' for _ in _POST_COLUMNS)})"
    " ON CONFLICT(mid) DO UPDATE SET"
    + ", ".join(f" {column} = excluded.{column}" for column in _POST_COLUMNS if column != "mid")
)

_MEDIA_INSERT_SQL = (
    f"INSERT INTO media ({', '.join(_MEDIA_COLUMNS)})"
    f" VALUES ({', '.join('?' for _ in _MEDIA_COLUMNS)})"
)

_STATE_SEL = ", ".join(
    f"COALESCE(SUM(CASE WHEN m.state_rank >= {rank} THEN 1 ELSE 0 END), 0) AS {state}_count"
    for rank, state in enumerate(MEDIA_STATES)
)


def _utc_iso() -> str:
    """UTC ISO 时间串（合同规定的写入格式，秒级）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _is_blank(value: Any) -> bool:
    """合同里的"空值"：None / 空串（含纯空白）/ -1（未知图数等哨兵）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return value == -1


def _merge(old: Any, new: Any) -> Any:
    """补空不覆盖：新值为空且旧值非空时保留旧值。"""
    if _is_blank(new) and not _is_blank(old):
        return old
    return new


def _merge_text(
    old: Any,
    new: Any,
    *,
    old_full: int = 0,
    new_full: int = 0,
) -> tuple[str, int]:
    """正文合并：更长者胜，且 text_full=1 一旦建立就不被非全文摘要降级。

    补全长文是把解析侧截断的摘要换成全文；若沿用"非空即覆盖"，时间线刷新或
    重复导入带来的短摘要会把全文顶掉，库里就永久只剩摘要。反之全文也可能比
    旧摘要长，所以正文取长的那份、text_full 只升不降。
    """
    old_text = _as_text(old)
    new_text = _as_text(new)
    if not new_text.strip():
        return old_text, 1 if old_full else 0
    if not old_text.strip():
        return new_text, 1 if new_full else 0
    if len(new_text) >= len(old_text):
        return new_text, 1 if (new_full or old_full) else 0
    return old_text, 1 if old_full else 0


def _to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_flag(value: Any) -> int:
    """布尔/0-1/字符串真假统一成 0/1。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return 1 if value else 0
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "yes", "y", "on"} else 0
    return 0


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _pick(post: Mapping[str, Any], keys: Sequence[str]) -> Any:
    """按别名优先级取第一个"非空"值；全都空时返回 None。"""
    for key in keys:
        if key in post and not _is_blank(post[key]):
            return post[key]
    return None


def _dump_pics(pics: Sequence[Any]) -> str:
    try:
        return json.dumps(list(pics), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "[]"


def _post_values(post: Mapping[str, Any]) -> dict[str, Any]:
    """把调用方传入的帖子映射归一成 posts 各列的值。

    兼容两种来源：合同列名（mid/created_at/text_raw/pics_json）与 monitor._parse_post
    的返回（id/created_ts/raw_text/time/pics）。未提供的字段留空，由 _upsert_post
    的"补空不覆盖"规则决定是否落默认值。
    """
    mid = _as_text(_pick(post, ("mid", "id", "post_id"))).strip()
    if not mid:
        raise ValueError("upsert_post 缺少必填字段 mid")
    uid = _as_text(_pick(post, ("uid", "user_id"))).strip()
    if not uid:
        raise ValueError("upsert_post 缺少必填字段 uid")

    pics = post.get("pics")
    pics_list = list(pics) if isinstance(pics, (list, tuple)) else []

    pics_json = _as_text(_pick(post, ("pics_json",))).strip()
    if pics_json:
        try:
            json.loads(pics_json)
        except (TypeError, ValueError):
            pics_json = ""
    if not pics_json and pics_list:
        pics_json = _dump_pics(pics_list)
    if pics_json == "[]":
        # 显式空列表在 upsert 语义里等同于"这次没拿到图信息"，不能抹掉已有图。
        pics_json = ""

    if "pic_total" in post:
        pic_total = max(-1, _to_int(post.get("pic_total"), -1))
    elif pics_list:
        pic_total = len(pics_list)
    else:
        pic_total = -1

    created_at = 0
    created_raw = _as_text(_pick(post, ("created_raw", "created_at_raw", "time")))
    for key in ("created_at", "created_ts"):
        if key in post and not _is_blank(post[key]):
            created_at = max(0, _to_int(post[key]))
            break
    if not created_raw and isinstance(post.get("created_at"), str):
        created_raw = post["created_at"]

    return {
        "mid": mid,
        "uid": uid,
        "author": _as_text(_pick(post, ("author", "screen_name"))),
        "bid": _as_text(_pick(post, ("bid", "mb"))),
        "link": _as_text(_pick(post, ("link",))),
        "created_at": created_at,
        "created_raw": created_raw,
        "text": _as_text(_pick(post, ("text", "t"))),
        "text_raw": _as_text(_pick(post, ("text_raw", "raw_text"))),
        "text_full": _as_flag(_pick(post, ("text_full",))),
        "is_long": _as_flag(_pick(post, ("is_long", "lt"))),
        "is_retweet": _as_flag(_pick(post, ("is_retweet", "rt"))),
        "orig_text": _as_text(_pick(post, ("orig_text",))),
        "pic_total": pic_total,
        "pics_json": pics_json,
        "video_src": _as_text(_pick(post, ("video_src",))),
        "raw_json": _as_text(_pick(post, ("raw_json", "raw"))),
        "source": _as_text(_pick(post, ("source",))) or "monitor",
        "pushed_at": _as_text(_pick(post, ("pushed_at",))),
        "cleaned_at": _as_text(_pick(post, ("cleaned_at",))),
        "reconciled_at": _as_text(_pick(post, ("reconciled_at",))),
    }


def _post_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        parsed = json.loads(data.get("pics_json") or "[]")
    except (TypeError, ValueError):
        parsed = []
    if isinstance(parsed, list):
        data["pics"] = [item for item in parsed if isinstance(item, dict)]
    else:
        data["pics"] = []
    return data


def _empty_stats() -> dict[str, int]:
    stats = {"total": 0}
    stats.update({state: 0 for state in MEDIA_STATES})
    stats["failed"] = 0
    return stats


def _empty_reconcile(uid: str | None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "uid": str(uid or ""),
        "posts": 0,
        "posts_with_text": 0,
        "posts_without_text": 0,
        "posts_need_text": 0,
        "posts_with_media": 0,
        "media_expected": 0,
        "pic_total_sum": 0,
        "media_rows": 0,
        "media_missing_url": 0,
        "downloaded": 0,
        "album": 0,
        "bed": 0,
        "thumb": 0,
        "vector": 0,
        "uploaded_bed": 0,
        "thumbnail": 0,
        "vectorized": 0,
        "pending": 0,
        "failed": 0,
    }
    return report


class ArchiveStore:
    """微博归档库（posts / media / backfill_cursor / meta）。"""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        media_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.db_path = Path(os.fspath(db_path))
        self.media_root = Path(os.fspath(media_root)) if media_root is not None else None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

        connection = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
        connection.row_factory = sqlite3.Row
        with self._lock:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.executescript(_SCHEMA_SQL)
            self._migrate_schema(connection)
            connection.commit()
            self._schema_version = self._read_schema_version(connection)
            if self._schema_version > _SCHEMA_VERSION:
                # 库是更高版本插件写的：保留其版本号不回写（回写会让降级后的插件
                # 把自己的旧结构"盖章"成新版本，之后再升级就永远不再迁移）。
                # 同时明确告警——继续跑等于用旧 SQL 打新表，缺列会在写入时才炸。
                logger.error(
                    "归档库 schema 版本(%s) 高于当前插件(%s)：可能是降级运行，"
                    "请升级插件或先备份后重建 archive.db（%s）",
                    self._schema_version,
                    _SCHEMA_VERSION,
                    self.db_path,
                )
            elif self._schema_version < _SCHEMA_VERSION:
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(_SCHEMA_VERSION),),
                )
                self._schema_version = _SCHEMA_VERSION
                connection.commit()
            self._conn = connection

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        """老库就地补列（幂等）：P5 起 posts.cleaned_text / media.thumb_url。"""
        post_columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(posts)").fetchall()
        }
        if "cleaned_text" not in post_columns:
            connection.execute("ALTER TABLE posts ADD COLUMN cleaned_text TEXT NOT NULL DEFAULT ''")
        # P2-4：text_full 标记「text 已经是长文全文」。老库升级后就地补列，
        # 已有长文行只能标记为"未知"（0），下一次补全再改写。
        if "text_full" not in post_columns:
            connection.execute("ALTER TABLE posts ADD COLUMN text_full INTEGER NOT NULL DEFAULT 0")
        media_columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(media)").fetchall()
        }
        if "thumb_url" not in media_columns:
            connection.execute("ALTER TABLE media ADD COLUMN thumb_url TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _read_schema_version(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            return 0
        value = _to_int(row["value"], 0)
        return value if value > 0 else 0

    @property
    def schema_version(self) -> int:
        """库结构版本；库内版本更高时保持原值（只读、不迁移）。"""
        return self._schema_version

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        """关闭连接；可重复调用，关闭后所有公开方法降级为安全空操作。"""
        with self._lock:
            connection, self._conn = self._conn, None
            if connection is None:
                return
            with contextlib.suppress(sqlite3.Error):
                connection.close()

    def _connection(self) -> sqlite3.Connection | None:
        return self._conn

    # ------------------------------------------------------------------
    # posts
    # ------------------------------------------------------------------

    def upsert_post(self, post: Mapping[str, Any]) -> str:
        """写入/补全一条帖子；ON CONFLICT 只覆盖非空新值，返回 mid。"""
        values = _post_values(post)
        mid = values["mid"]
        with self._lock:
            connection = self._connection()
            if connection is None:
                return mid
            row = connection.execute("SELECT * FROM posts WHERE mid = ?", (mid,)).fetchone()
            if row is None:
                inserted = dict(values)
                for column, default in _POST_INSERT_DEFAULTS.items():
                    if _is_blank(inserted.get(column)):
                        inserted[column] = default
                inserted["updated_at"] = _utc_iso()
                connection.execute(_POST_UPSERT_SQL, [inserted[column] for column in _POST_COLUMNS])
            else:
                merged: dict[str, Any] = {"mid": mid}
                merged_text, merged_text_full = _merge_text(
                    row["text"],
                    values.get("text"),
                    old_full=_as_flag(row["text_full"]),
                    new_full=_as_flag(values.get("text_full")),
                )
                for column in _POST_COLUMNS:
                    if column == "mid":
                        continue
                    old_value = row[column]
                    new_value = values.get(column, "")
                    if column == "created_at":
                        merged[column] = max(_to_int(old_value), _to_int(new_value))
                    elif column == "source":
                        # 首见来源保留：monitor 归档过的帖后来用 json 再导入也不改来源。
                        merged[column] = _as_text(old_value) or _as_text(new_value) or "monitor"
                    elif column == "text":
                        merged[column] = merged_text
                    elif column == "text_full":
                        merged[column] = merged_text_full
                    else:
                        merged[column] = _merge(old_value, new_value)
                merged["updated_at"] = _utc_iso()
                connection.execute(_POST_UPSERT_SQL, [merged[column] for column in _POST_COLUMNS])
            connection.commit()
        return mid

    def get_post(self, mid: str) -> dict | None:
        """按 mid 取一条帖子（全部列 + 解析后的 pics）。"""
        with self._lock:
            connection = self._connection()
            if connection is None:
                return None
            row = connection.execute("SELECT * FROM posts WHERE mid = ?", (str(mid),)).fetchone()
        return _post_row_to_dict(row) if row is not None else None

    def has_post(self, mid: str) -> bool:
        with self._lock:
            connection = self._connection()
            if connection is None:
                return False
            row = connection.execute("SELECT 1 FROM posts WHERE mid = ?", (str(mid),)).fetchone()
        return row is not None

    def count_posts(self, uid: str | None = None, *, source: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if uid:
            clauses.append("uid = ?")
            params.append(str(uid))
        if source:
            clauses.append("source = ?")
            params.append(str(source))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            connection = self._connection()
            if connection is None:
                return 0
            row = connection.execute(f"SELECT COUNT(*) FROM posts{where}", params).fetchone()
        return _to_int(row[0]) if row is not None else 0

    def list_posts(self, uid: str | None = None, *, limit: int = 20, offset: int = 0) -> list[dict]:
        """按 created_at DESC, mid DESC 列出帖子（含 pics）。"""
        where = " WHERE uid = ?" if uid else ""
        params: list[Any] = [str(uid)] if uid else []
        params.extend([max(0, _to_int(limit, 20)), max(0, _to_int(offset))])
        with self._lock:
            connection = self._connection()
            if connection is None:
                return []
            rows = connection.execute(
                f"SELECT * FROM posts{where} ORDER BY created_at DESC, mid DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [_post_row_to_dict(row) for row in rows]

    def iter_posts_keyset(
        self,
        uid: str | None = None,
        *,
        after: tuple[int, str] | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """按 (created_at, mid) 键集分页列出帖子，供全库扫描使用。

        ``list_posts`` 用的 ``LIMIT/OFFSET`` 在并发插入（监控侧一直在写新帖）
        时会让整页向后位移，导致漏扫——对账会因此报出"全部通过"的假结论。
        键集分页以「上一页最后一行的 (created_at, mid)」为锚点，插入新行
        只会出现在已扫过的区间之前，不会挤掉待扫区间。

        排序键与 ``list_posts`` 一致（created_at DESC, mid DESC），``after``
        传上一页最后一行的 (created_at, mid)；首屏传 None。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if uid:
            clauses.append("uid = ?")
            params.append(str(uid))
        if after is not None:
            anchor_at, anchor_mid = int(after[0]), str(after[1])
            # 行值比较的等价展开：同一 created_at 下按 mid 继续向后
            clauses.append("(created_at < ? OR (created_at = ? AND mid < ?))")
            params.extend([anchor_at, anchor_at, anchor_mid])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, _to_int(limit, 200)))
        with self._lock:
            connection = self._connection()
            if connection is None:
                return []
            rows = connection.execute(
                f"SELECT * FROM posts{where} ORDER BY created_at DESC, mid DESC LIMIT ?",
                params,
            ).fetchall()
        return [_post_row_to_dict(row) for row in rows]

    def list_posts_for_pipeline(
        self, uid: str | None = None, *, done_state: str = "vector", limit: int = 200
    ) -> list[dict]:
        """列出「还有图没跑到 done_state」的帖子（图床管线补跑用）。

        只回帖子关键列与进度计数：media_total 为该帖入库图片数，media_done
        为已达到 done_state 的数量；两者相等即整帖跑完。按 created_at DESC
        排序——新帖先跑，用户能尽快在新内容上看到效果。
        """
        rank = _MEDIA_STATE_RANK.get(str(done_state))
        if rank is None:
            raise ValueError(f"非法的媒体状态: {done_state}")
        where = " WHERE p.uid = ?" if uid else ""
        params: list[Any] = [rank]
        if uid:
            params.append(str(uid))
        params.append(max(1, _to_int(limit, 200)))
        with self._lock:
            connection = self._connection()
            if connection is None:
                return []
            rows = connection.execute(
                "SELECT p.mid AS mid, p.uid AS uid, p.author AS author,"
                " p.created_at AS created_at, p.pic_total AS pic_total,"
                " COUNT(m.pid) AS media_total,"
                " COALESCE(SUM(CASE WHEN m.state_rank >= ? THEN 1 ELSE 0 END), 0)"
                " AS media_done"
                " FROM posts p JOIN media m ON m.mid = p.mid"
                f"{where}"
                " GROUP BY p.mid HAVING media_done < COUNT(m.pid)"
                " ORDER BY p.created_at DESC, p.mid DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def iter_pending_text_posts(self, uid: str | None = None) -> list[dict]:
        """cleaned_at 为空（正文尚未补全/清理）的帖子。"""
        where = ["cleaned_at = ''"]
        params: list[Any] = []
        if uid:
            where.append("uid = ?")
            params.append(str(uid))
        with self._lock:
            connection = self._connection()
            if connection is None:
                return []
            rows = connection.execute(
                "SELECT * FROM posts WHERE "
                + " AND ".join(where)
                + " ORDER BY created_at DESC, mid DESC",
                params,
            ).fetchall()
        return [_post_row_to_dict(row) for row in rows]

    def mark_post_cleaned(self, mid: str, cleaned_text: str) -> None:
        """写入清洗结果并打 cleaned_at（``cleaned_text`` 是文案库的唯一正文来源）。"""
        now = _utc_iso()
        with self._lock:
            connection = self._connection()
            if connection is None:
                return
            connection.execute(
                "UPDATE posts SET cleaned_text = ?, cleaned_at = ?, updated_at = ? WHERE mid = ?",
                (str(cleaned_text), now, now, str(mid)),
            )
            connection.commit()

    def iter_uncleaned_posts(self, uid: str | None = None, limit: int = 0) -> list[dict]:
        """还没清洗过的帖子，按时间从旧到新。

        判据用 cleaned_at 而不是 cleaned_text：纯链接/纯表情帖清洗结果为空串，
        用文本判空会让它们每轮都被重洗，"待清洗"永远不归零。
        """
        where = ["cleaned_at = ''"]
        params: list[Any] = []
        if uid:
            where.append("uid = ?")
            params.append(str(uid))
        sql = (
            "SELECT mid, uid, author, created_at, text, text_raw, orig_text,"
            " is_long, is_retweet, source FROM posts WHERE "
            + " AND ".join(where)
            + " ORDER BY created_at ASC, mid ASC"
        )
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            connection = self._connection()
            if connection is None:
                return []
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def copy_accounts(self) -> list[dict]:
        """文案库里的账号维度统计：uid / author / total / cleaned / long。"""
        with self._lock:
            connection = self._connection()
            if connection is None:
                return []
            rows = connection.execute(
                "SELECT uid, MAX(author) AS author, COUNT(*) AS total,"
                " COALESCE(SUM(CASE WHEN cleaned_text <> '' THEN 1 ELSE 0 END), 0)"
                " AS cleaned, COALESCE(SUM(is_long), 0) AS long"
                " FROM posts GROUP BY uid ORDER BY total DESC, uid ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def copy_stats(self, uid: str | None = None) -> dict:
        """文案库总览：帖子总数 / 已清洗 / 长文 / 转发 / 带图帖子。"""
        where = ""
        params: list[Any] = []
        if uid:
            where = " WHERE uid = ?"
            params.append(str(uid))
        with self._lock:
            connection = self._connection()
            if connection is None:
                return {"total": 0, "cleaned": 0, "long": 0, "retweet": 0, "with_pics": 0}
            row = connection.execute(
                "SELECT COUNT(*) AS total,"
                " COALESCE(SUM(CASE WHEN cleaned_text <> '' THEN 1 ELSE 0 END), 0)"
                " AS cleaned, COALESCE(SUM(is_long), 0) AS long,"
                " COALESCE(SUM(is_retweet), 0) AS retweet,"
                " COALESCE(SUM(CASE WHEN pic_total > 0 THEN 1 ELSE 0 END), 0)"
                " AS with_pics FROM posts" + where,
                params,
            ).fetchone()
        # 显式取列：sqlite3.Row 迭代产出的是值不是键，不能写成 for key in row
        return {
            "total": _to_int(row["total"]),
            "cleaned": _to_int(row["cleaned"]),
            "long": _to_int(row["long"]),
            "retweet": _to_int(row["retweet"]),
            "with_pics": _to_int(row["with_pics"]),
        }

    def list_copy_posts(
        self,
        uid: str | None = None,
        *,
        keyword: str = "",
        include_retweets: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """文案列表（已清洗优先），供 ``文案列表`` / WebUI 使用。"""
        where: list[str] = []
        params: list[Any] = []
        if uid:
            where.append("uid = ?")
            params.append(str(uid))
        if not include_retweets:
            where.append("is_retweet = 0")
        if keyword:
            where.append("(cleaned_text LIKE ? OR text LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like])
        sql = (
            "SELECT mid, uid, author, created_at, link, is_long, is_retweet,"
            " pic_total, cleaned_at, cleaned_text, text FROM posts"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, mid DESC LIMIT ? OFFSET ?"
        params.extend([max(1, int(limit)), max(0, int(offset))])
        with self._lock:
            connection = self._connection()
            if connection is None:
                return []
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_copy_entries(self, uid: str | None = None) -> list[dict]:
        """签到用：每条帖子一行 + 首页图片（idx 最小且有 url），含缩略图地址。"""
        where = " WHERE p.cleaned_text <> ''"
        params: list[Any] = []
        if uid:
            where += " AND p.uid = ?"
            params.append(str(uid))
        sql = (
            "SELECT p.mid, p.uid, p.author, p.created_at, p.link, p.is_long,"
            " p.is_retweet, p.pic_total, p.cleaned_text, p.text,"
            " m.pid, m.url AS image_url, m.thumb_url AS thumb_url,"
            " m.local_path FROM posts p"
            " LEFT JOIN media m ON m.mid = p.mid AND m.kind = 'image' AND m.pid = ("
            "SELECT m2.pid FROM media m2 WHERE m2.mid = p.mid AND m2.kind = 'image'"
            " AND m2.url <> '' ORDER BY m2.idx ASC, m2.pid ASC LIMIT 1)"
            + where
            + " ORDER BY p.created_at DESC, p.mid DESC"
        )
        with self._lock:
            connection = self._connection()
            if connection is None:
                return []
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def copy_signature(self) -> str:
        """文案库指纹：签到侧缓存失效用（条数 + 最近更新时间 + 最新清洗时间）。"""
        with self._lock:
            connection = self._connection()
            if connection is None:
                return ""
            row = connection.execute(
                "SELECT COUNT(*) AS total, COALESCE(MAX(updated_at), '') AS updated,"
                " COALESCE(MAX(cleaned_at), '') AS cleaned FROM posts"
            ).fetchone()
        return f"{_to_int(row['total'])}|{row['updated']}|{row['cleaned']}"

    def set_post_time(self, mid: str, column: str, value: str) -> None:
        """写 pushed_at / cleaned_at / reconciled_at；非法列名抛 ValueError。"""
        if column not in _POST_TIME_COLUMNS:
            raise ValueError(f"非法的时间列: {column}")
        with self._lock:
            connection = self._connection()
            if connection is None:
                return
            connection.execute(
                f"UPDATE posts SET {column} = ?, updated_at = ? WHERE mid = ?",
                (str(value), _utc_iso(), str(mid)),
            )
            connection.commit()

    def set_pics(self, mid: str, pics: list[dict]) -> None:
        """重写一条帖子的图列表（pics_json + pic_total=len(pics)）。"""
        items = list(pics or [])
        with self._lock:
            connection = self._connection()
            if connection is None:
                return
            connection.execute(
                "UPDATE posts SET pics_json = ?, pic_total = ?, updated_at = ? WHERE mid = ?",
                (_dump_pics(items), len(items), _utc_iso(), str(mid)),
            )
            connection.commit()

    # ------------------------------------------------------------------
    # media
    # ------------------------------------------------------------------

    def upsert_media(self, mid: str, items: Sequence[Mapping[str, Any]]) -> int:
        """写入帖子下的图片行；冲突时只补 url/alt_url/ext/kind，返回新增条数。"""
        mid = str(mid)
        inserted = 0
        with self._lock:
            connection = self._connection()
            if connection is None:
                return 0
            for position, item in enumerate(items):
                pid = _as_text(item.get("pid")).strip()
                if not pid:
                    continue
                url = _as_text(item.get("url")).strip()
                alt_url = _as_text(item.get("alt_url")).strip()
                ext = _as_text(item.get("ext")).strip().lower()
                kind = _as_text(item.get("kind")).strip()
                idx = _to_int(item.get("idx"), position)
                row = connection.execute(
                    "SELECT 1 FROM media WHERE mid = ? AND pid = ?", (mid, pid)
                ).fetchone()
                if row is None:
                    now = _utc_iso()
                    connection.execute(
                        _MEDIA_INSERT_SQL,
                        [
                            mid,
                            pid,
                            idx,
                            url,
                            alt_url,
                            ext or "jpg",
                            kind or "image",
                            "",
                            0,
                            "pending",
                            0,
                            0,
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            now,
                        ],
                    )
                    inserted += 1
                else:
                    connection.execute(
                        "UPDATE media SET"
                        " url = CASE WHEN ? <> '' THEN ? ELSE url END,"
                        " alt_url = CASE WHEN ? <> '' THEN ? ELSE alt_url END,"
                        " ext = CASE WHEN ? <> '' THEN ? ELSE ext END,"
                        " kind = CASE WHEN ? <> '' THEN ? ELSE kind END"
                        " WHERE mid = ? AND pid = ?",
                        (
                            url,
                            url,
                            alt_url,
                            alt_url,
                            ext,
                            ext,
                            kind,
                            kind,
                            mid,
                            pid,
                        ),
                    )
            connection.commit()
        return inserted

    def list_media(self, mid: str) -> list[dict]:
        with self._lock:
            connection = self._connection()
            if connection is None:
                return []
            rows = connection.execute(
                "SELECT * FROM media WHERE mid = ? ORDER BY idx ASC, pid ASC", (str(mid),)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_media_bulk(self, mids: Sequence[str]) -> dict[str, list[dict]]:
        """批量取多帖的媒体行，替代对账时按帖的 N+1 查询（P2-3）。

        按 _BULK_MEDIA_CHUNK（500）个 mid 一批查询：SQLite 3.32 起绑定变量默认
        上限是 32766，但更老的版本默认只有 999、嵌入式绑定还可能更低，一条 WHERE
        mid IN (...) 塞进 5000 个 mid 在旧版本上会直接报 "too many SQL
        variables"。返回 {mid: [row, ...]}，同一 mid 内排序与 list_media 一致；
        没有媒体行的 mid 不出现在结果里（调用方按 get(mid) or [] 处理）。
        """
        wanted = [str(mid) for mid in mids if str(mid or "").strip()]
        result: dict[str, list[dict]] = {}
        if not wanted:
            return result
        with self._lock:
            connection = self._connection()
            if connection is None:
                return result
            for start in range(0, len(wanted), _BULK_MEDIA_CHUNK):
                chunk = wanted[start : start + _BULK_MEDIA_CHUNK]
                placeholders = ", ".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT * FROM media WHERE mid IN ({placeholders})"
                    " ORDER BY mid ASC, idx ASC, pid ASC",
                    chunk,
                ).fetchall()
                for row in rows:
                    result.setdefault(str(row["mid"]), []).append(dict(row))
        return result

    def get_media(self, mid: str, pid: str) -> dict | None:
        with self._lock:
            connection = self._connection()
            if connection is None:
                return None
            row = connection.execute(
                "SELECT * FROM media WHERE mid = ? AND pid = ?", (str(mid), str(pid))
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_media(
        self,
        mid: str,
        pid: str,
        state: str,
        *,
        local_path: str | None = None,
        size: int | None = None,
        error: str | None = None,
        thumb_url: str | None = None,
    ) -> None:
        """推进媒体状态；rank 只前进不后退，状态推进时写对应时间戳列。"""
        if state not in _MEDIA_STATE_RANK:
            raise ValueError(f"非法的媒体状态: {state}")
        new_rank = _MEDIA_STATE_RANK[state]
        with self._lock:
            connection = self._connection()
            if connection is None:
                return
            row = connection.execute(
                "SELECT state_rank FROM media WHERE mid = ? AND pid = ?",
                (str(mid), str(pid)),
            ).fetchone()
            if row is None:
                return
            old_rank = _to_int(row["state_rank"])
            assignments: list[str] = []
            params: list[Any] = []
            if new_rank > old_rank:
                assignments.extend(["state = ?", "state_rank = ?"])
                params.extend([state, new_rank])
                timestamp_column = STATE_AT_COLUMN.get(state)
                if timestamp_column:
                    assignments.append(f"{timestamp_column} = ?")
                    params.append(_utc_iso())
            if local_path is not None:
                assignments.append("local_path = ?")
                params.append(str(local_path))
            if size is not None:
                assignments.append("size = ?")
                params.append(_to_int(size))
            if error is not None:
                assignments.append("error = ?")
                params.append(str(error))
            if thumb_url is not None:
                assignments.append("thumb_url = ?")
                params.append(str(thumb_url))
            assignments.append("updated_at = ?")
            params.append(_utc_iso())
            params.extend([str(mid), str(pid)])
            connection.execute(
                f"UPDATE media SET {', '.join(assignments)} WHERE mid = ? AND pid = ?",
                params,
            )
            connection.commit()

    def bump_media_attempt(self, mid: str, pid: str, error: str = "") -> int:
        """attempts+1 并记录错误，返回新的 attempts；行不存在时返回 0。"""
        with self._lock:
            connection = self._connection()
            if connection is None:
                return 0
            row = connection.execute(
                "SELECT attempts FROM media WHERE mid = ? AND pid = ?",
                (str(mid), str(pid)),
            ).fetchone()
            if row is None:
                return 0
            attempts = _to_int(row["attempts"]) + 1
            connection.execute(
                "UPDATE media SET attempts = ?, error = ?, updated_at = ?"
                " WHERE mid = ? AND pid = ?",
                (attempts, str(error), _utc_iso(), str(mid), str(pid)),
            )
            connection.commit()
        return attempts

    def media_stats(self, uid: str | None = None) -> dict:
        """媒体状态计数（"达到或超过该 rank"），failed = pending 且 attempts>0。"""
        if uid:
            source = " FROM media m JOIN posts p ON p.mid = m.mid WHERE p.uid = ?"
            params: tuple[Any, ...] = (str(uid),)
        else:
            source = " FROM media m"
            params = ()
        with self._lock:
            connection = self._connection()
            if connection is None:
                return _empty_stats()
            row = connection.execute(
                "SELECT COUNT(*) AS total, "
                + _STATE_SEL
                + ", COALESCE(SUM(CASE WHEN m.state = 'pending' AND m.attempts > 0"
                " THEN 1 ELSE 0 END), 0) AS failed" + source,
                params,
            ).fetchone()
        stats = {"total": _to_int(row["total"])}
        for state in MEDIA_STATES:
            stats[state] = _to_int(row[f"{state}_count"])
        stats["failed"] = _to_int(row["failed"])
        return stats

    def media_file(self, mid: str, pid: str) -> Path | None:
        """本地已落盘的媒体文件；local_path 为空或文件不存在时返回 None。"""
        with self._lock:
            connection = self._connection()
            if connection is None:
                return None
            row = connection.execute(
                "SELECT local_path FROM media WHERE mid = ? AND pid = ?",
                (str(mid), str(pid)),
            ).fetchone()
        if row is None:
            return None
        local = _as_text(row["local_path"]).strip()
        if not local:
            return None
        path = Path(local)
        if not path.is_absolute() and self.media_root is not None:
            path = self.media_root / path
        try:
            return path if path.is_file() else None
        except OSError:
            return None

    # ------------------------------------------------------------------
    # 回填游标
    # ------------------------------------------------------------------

    def get_cursor(self, uid: str) -> dict | None:
        with self._lock:
            connection = self._connection()
            if connection is None:
                return None
            row = connection.execute(
                "SELECT * FROM backfill_cursor WHERE uid = ?", (str(uid),)
            ).fetchone()
        return dict(row) if row is not None else None

    def set_cursor(
        self,
        uid: str,
        next_page: int,
        oldest_created_at: int = 0,
        status: str = "running",
    ) -> None:
        """写回填游标；oldest_created_at=0 表示"不知道"，不覆盖已有值。"""
        now = _utc_iso()
        with self._lock:
            connection = self._connection()
            if connection is None:
                return
            connection.execute(
                "INSERT INTO backfill_cursor"
                " (uid, next_page, oldest_created_at, status, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(uid) DO UPDATE SET"
                " next_page = excluded.next_page,"
                " oldest_created_at = CASE WHEN excluded.oldest_created_at > 0"
                " THEN excluded.oldest_created_at ELSE backfill_cursor.oldest_created_at END,"
                " status = excluded.status,"
                " updated_at = excluded.updated_at",
                (str(uid), _to_int(next_page), _to_int(oldest_created_at), str(status), now),
            )
            connection.commit()

    def clear_cursor(self, uid: str) -> None:
        with self._lock:
            connection = self._connection()
            if connection is None:
                return
            connection.execute("DELETE FROM backfill_cursor WHERE uid = ?", (str(uid),))
            connection.commit()

    # ------------------------------------------------------------------
    # 对账聚合
    # ------------------------------------------------------------------

    def reconcile_counts(self, uid: str | None = None) -> dict:
        """对账用原始聚合（ArchiveMixin 据此组织第 6 节的缺口报告）。"""
        uid_text = str(uid or "")
        with self._lock:
            connection = self._connection()
            if connection is None:
                return _empty_reconcile(uid)
            post_where = " WHERE uid = ?" if uid else ""
            post_params: tuple[Any, ...] = (uid_text,) if uid else ()
            post_row = connection.execute(
                "SELECT COUNT(*) AS posts,"
                " COALESCE(SUM(CASE WHEN TRIM(text) <> '' THEN 1 ELSE 0 END), 0)"
                " AS posts_with_text,"
                # 待补正文＝还没被清洗流水线打上 cleaned_at 的帖子。判据必须用
                # cleaned_at 而不是 TRIM(text)：纯链接/纯表情帖清洗结果合法为空串，
                # 按 text 判会把它们永久算成"缺正文"并反复重试。
                " COALESCE(SUM(CASE WHEN cleaned_at = '' THEN 1 ELSE 0 END), 0)"
                " AS posts_need_text,"
                " COALESCE(SUM(CASE WHEN pic_total >= 0 THEN pic_total ELSE 0 END), 0)"
                " AS media_expected"
                f" FROM posts{post_where}",
                post_params,
            ).fetchone()
            if uid:
                media_from = " FROM media m JOIN posts p ON p.mid = m.mid WHERE p.uid = ?"
                media_params: tuple[Any, ...] = (uid_text,)
            else:
                media_from = " FROM media m"
                media_params = ()
            media_row = connection.execute(
                "SELECT COUNT(*) AS media_rows,"
                " COALESCE(SUM(CASE WHEN TRIM(m.url) = '' THEN 1 ELSE 0 END), 0)"
                " AS media_missing_url,"
                " COALESCE(SUM(CASE WHEN m.state = 'pending' THEN 1 ELSE 0 END), 0)"
                " AS pending,"
                " COALESCE(SUM(CASE WHEN m.state = 'pending' AND m.attempts > 0"
                " THEN 1 ELSE 0 END), 0) AS failed, " + _STATE_SEL + media_from,
                media_params,
            ).fetchone()
            with_media_row = connection.execute(
                "SELECT COUNT(DISTINCT m.mid) AS posts_with_media"
                " FROM media m JOIN posts p ON p.mid = m.mid" + (" WHERE p.uid = ?" if uid else ""),
                media_params,
            ).fetchone()

        posts = _to_int(post_row["posts"])
        posts_with_text = _to_int(post_row["posts_with_text"])
        media_expected = _to_int(post_row["media_expected"])
        report = _empty_reconcile(uid)
        report.update(
            {
                "posts": posts,
                "posts_with_text": posts_with_text,
                "posts_without_text": max(0, posts - posts_with_text),
                "posts_need_text": _to_int(post_row["posts_need_text"]),
                "posts_with_media": _to_int(with_media_row["posts_with_media"]),
                "media_expected": media_expected,
                "pic_total_sum": media_expected,
                "media_rows": _to_int(media_row["media_rows"]),
                "media_missing_url": _to_int(media_row["media_missing_url"]),
                "pending": _to_int(media_row["pending"]),
                "failed": _to_int(media_row["failed"]),
            }
        )
        for state in MEDIA_STATES:
            if state == "pending":
                # pending 只有"恰好待下载"一种含义，不能让 rank>=0 的总数覆盖它。
                continue
            report[state] = _to_int(media_row[f"{state}_count"])
        report["uploaded_bed"] = report["bed"]
        report["thumbnail"] = report["thumb"]
        report["vectorized"] = report["vector"]
        return report

    # ------------------------------------------------------------------
    # meta
    # ------------------------------------------------------------------

    def meta_get(self, key: str, default: str = "") -> str:
        with self._lock:
            connection = self._connection()
            if connection is None:
                return default
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (str(key),)).fetchone()
        return str(row["value"]) if row is not None else default

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            connection = self._connection()
            if connection is None:
                return
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )
            connection.commit()


__all__ = ["MEDIA_STATES", "STATE_AT_COLUMN", "ArchiveStore"]
