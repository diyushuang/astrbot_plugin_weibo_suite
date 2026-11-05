"""P6/P8 统一 WebUI：路由清单、接口适配与七页签静态门禁。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.views_backend import dashboard, settings_api, sns_api


class Resp:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code


class FakeRequest:
    def __init__(self, body: dict | None = None):
        self.body = body or {}

    async def json(self, default=None):
        return self.body if self.body else ({} if default is None else default)


class FakeStore:
    def __init__(self):
        self.cleaned: list[tuple[str, str]] = []

    def count_posts(self):
        return 3

    def media_stats(self, uid=None, *, max_attempts=None):
        return {
            "total": 4,
            "downloaded": 3,
            "bed": 2,
            "thumb": 2,
            "vector": 1,
            "failed": 1,
            "abandoned": 0,
        }

    def copy_stats(self):
        return {"total": 3, "cleaned": 2, "long": 1, "retweet": 0, "with_pics": 3}

    def copy_signature(self):
        return "3|now"

    def list_copy_entries(self, uid=None):
        return []


class FakeTrace:
    def __init__(self):
        self.config = {"checkin": {"mode": "weibo"}}
        self.library = type("Library", (), {"stats": lambda self: {"total": 9, "indexed": 8}})()
        self.vector = type(
            "Vector",
            (),
            {
                "enabled": True,
                "embed_model": "embed-v3",
                "collection": "weibo",
                "count": self._count,
            },
        )()
        self.repaired = 0

    async def _count(self):
        return 8

    def _repair_history_keys(self):
        self.repaired = 2
        return 2

    async def rescan(self, event):
        yield event.plain_result("扫描完成：新增 1")


class FakePlugin:
    def __init__(self, tmp_path: Path):
        self.config = {"pipeline": {}}
        self._data_dir = tmp_path
        self._trace_plugin = None
        self._store = FakeStore()
        self._backfill_last_stats = {"status": "done", "inserted": 2}
        self._backfill_task = None
        self._pipeline_worker_task = None
        self.events: list[str] = []
        self.saved = 0

    def archive_store(self):
        return self._store

    async def archive_query(self, uid="", *, limit=10, offset=0):
        return [
            {
                "mid": "1001",
                "uid": "42",
                "author": "博主",
                "created_at": 1700000000,
                "text": "归档正文",
                "cleaned_text": "清洗正文",
                "pic_total": 1,
            }
        ]

    def archive_backfill_running(self):
        return False

    def archive_backfill_stop(self):
        self.events.append("backfill-stop")

    async def archive_backfill(self, uid, *, since="", full=False):
        self.events.append(f"backfill:{uid}:{since}:{full}")
        return {"status": "done"}

    async def archive_reconcile(self, uid=""):
        return {"ok": True, "uid": uid, "gap_count": 0, "gaps": []}

    async def archive_import_json(self, path):
        self.events.append(f"archive-import:{Path(path).read_text(encoding='utf-8')}")
        return {"inserted": 1, "updated": 0}

    def pipeline_stats(self):
        return {
            "enabled": True,
            "mode": "plugin",
            "queue": 1,
            "pending": 0,
            "posts": 2,
            "media": {"total": 4, "vector": 3},
            "last_error": "http://secret.local/path failed",
        }

    async def pipeline_backfill(self, limit=200):
        self.events.append(f"pipeline:{limit}")
        return {"scanned": 2, "queued": 2}

    def _save_config(self):
        self.saved += 1

    def copylib_status(self):
        return {"ok": True, "enabled": True, "total": 3, "cleaned": 2}

    def copylib_list(self, uid="", keyword="", limit=10):
        return [{"mid": "1001", "uid": uid, "keyword": keyword, "text": "文案"}]

    async def copylib_import(self, path, uid=""):
        self.events.append(f"copylib-import:{Path(path).name}:{uid}")
        return {"ok": True, "records": 1, "imported": 1}

    def _record_event(self, kind, **kwargs):
        self.events.append(f"event:{kind}:{kwargs.get('detail', '')}")


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "_WEB_AVAILABLE", True)
    monkeypatch.setattr(dashboard, "json_response", lambda payload: Resp(payload))
    monkeypatch.setattr(
        dashboard,
        "error_response",
        lambda message, status_code=400: Resp({"status": "error", "message": message}, status_code),
    )
    plugin = FakePlugin(tmp_path)
    return dashboard.DashboardAPI(
        plugin,
        "astrbot_plugin_weibo_suite",
        "v0.7.0",
        lambda text: text,
        lambda text: text,
    )


def test_registered_routes_cover_all_tabs(monkeypatch):
    class Context:
        def __init__(self):
            self.routes = []

        def register_web_api(self, path, handler, methods, description):
            self.routes.append((path, handler.__name__, tuple(methods), description))

    class Plugin:
        context = Context()

    monkeypatch.setattr(dashboard, "_WEB_AVAILABLE", True)
    monkeypatch.setattr(sns_api, "_WEB_AVAILABLE", True)
    monkeypatch.setattr(settings_api, "_WEB_AVAILABLE", True)
    assert dashboard.register_dashboard(
        Plugin(), "suite", "v0.7.0", lambda text: text, lambda text: text
    )
    assert sns_api.register_sns_dashboard(Plugin(), "suite", "v0.7.0")
    assert settings_api.register_settings_dashboard(Plugin(), "suite", "v0.7.0")
    paths = {path.rsplit("/suite/", 1)[-1] for path, *_ in Plugin.context.routes}
    assert {
        "overview",
        "errors",
        "accounts",
        "archive/overview",
        "archive/list",
        "archive/backfill",
        "archive/reconcile",
        "archive/import",
        "pipeline/status",
        "pipeline/retry",
        "pipeline/naming",
        "copylib/overview",
        "copylib/list",
        "copylib/import",
        "trace/status",
        "trace/rescan",
        "trace/repair-history",
        "sns/overview",
        "sns/credentials",
        "sns/session",
        "sns/accounts",
        "sns/guard",
        "sns/check-now",
        "sns/preview",
        "settings",
        "settings/save",
    } <= paths
    # 原 35 条保持不变，新增设置页 2 条，合计 37 条。
    assert len(Plugin.context.routes) == 37


def test_dashboard_markup_has_all_tabs_and_actions():
    root = Path(__file__).resolve().parents[1] / "views" / "dashboard"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "style.css").read_text(encoding="utf-8")
    tabs = (
        "总览",
        "微博监控",
        "归档与图床",
        "文案库",
        "溯源与签到",
        "多平台订阅",
        "设置",
        "错误日志",
    )
    for name in tabs:
        assert f">{name}</button>" in html
    for endpoint in (
        "archive/overview",
        "archive/backfill",
        "archive/reconcile",
        "archive/import",
        "pipeline/status",
        "pipeline/retry",
        "pipeline/naming",
        "copylib/overview",
        "copylib/list",
        "copylib/import",
        "trace/status",
        "trace/rescan",
        "trace/repair-history",
        "sns/overview",
        "sns/credentials",
        "sns/session",
        "sns/accounts",
        "sns/guard",
        "sns/check-now",
        "sns/preview",
    ):
        assert endpoint in js
    assert "AstrBotPluginView" in js
    assert '[data-theme="dark"]' in css
    assert ".tab-panel.active" in css


async def test_archive_overview_and_list(api):
    overview = await api.archive_overview()
    assert overview.status_code == 200
    assert overview.payload["posts"] == 3
    assert overview.payload["recent"][0]["text"] == "清洗正文"
    assert overview.payload["backfill"]["stats"]["inserted"] == 2
    listing = await api.archive_list()
    assert listing.payload["items"][0]["mid"] == "1001"


async def test_backfill_start_and_stop(api, monkeypatch):
    monkeypatch.setattr(dashboard, "web_request", FakeRequest({"uid": "42", "since": "2025-01-01"}))
    started = await api.archive_backfill()
    assert started.payload["started"] is True
    task = api.p._backfill_task
    await task
    assert "backfill:42:2025-01-01:False" in api.p.events
    monkeypatch.setattr(dashboard, "web_request", FakeRequest({"action": "stop"}))
    stopped = await api.archive_backfill()
    assert stopped.payload == {"stopped": True}


async def test_pipeline_and_naming_roundtrip(api, monkeypatch):
    status = await api.pipeline_status()
    assert status.payload["status"]["queue"] == 1
    monkeypatch.setattr(
        dashboard,
        "web_request",
        FakeRequest(
            {
                "naming_directory": "{category}/@{account}/",
                "naming_file": "{date}-{title}.jpg",
                "categories": {"42": "日常"},
            }
        ),
    )
    saved = await api.pipeline_naming()
    assert saved.payload["categories"] == {"42": "日常"}
    assert api.p.config["pipeline"]["naming_file"] == "{date}-{title}.jpg"
    assert api.p.saved == 1


async def test_copylib_search_and_base64_import(api, monkeypatch, tmp_path):
    monkeypatch.setattr(
        dashboard,
        "web_request",
        FakeRequest(
            {
                "filename": "文案.csv",
                "uid": "42",
                "content_base64": base64.b64encode(b"a,b\n1,2\n").decode(),
            }
        ),
    )
    result = await api.copylib_import()
    assert result.status_code == 200
    assert result.payload["imported"] == 1
    assert any("copylib-import:" in item for item in api.p.events)
    monkeypatch.setattr(
        dashboard,
        "web_request",
        FakeRequest({"uid": "42", "keyword": "你好", "limit": 5}),
    )
    listed = await api.copylib_list()
    assert listed.payload["items"][0]["keyword"] == "你好"


async def test_archive_base64_import(api, monkeypatch):
    payload = json.dumps([{"id": "1"}]).encode()
    monkeypatch.setattr(
        dashboard,
        "web_request",
        FakeRequest(
            {
                "filename": "seed.json",
                "content_base64": base64.b64encode(payload).decode(),
            }
        ),
    )
    result = await api.archive_import()
    assert result.payload["inserted"] == 1
    assert any(item == 'archive-import:[{"id": "1"}]' for item in api.p.events)


async def test_trace_status_rescan_and_repair(api):
    api.p._trace_plugin = FakeTrace()
    status = await api.trace_status()
    assert status.payload["library"]["total"] == 9
    assert status.payload["vector"]["count"] == 8
    started = await api.trace_rescan()
    assert started.payload["started"] is True
    await api._trace_task
    assert "扫描完成：新增 1" in api._trace_job["messages"]
    repaired = await api.trace_repair_history()
    assert repaired.payload["cleaned"] == 2


async def test_upload_base64_precheck_rejects_before_decode(api, monkeypatch):
    """超大 base64 按长度预检直接拒绝，不先解进内存。"""
    monkeypatch.setattr(dashboard, "_UPLOAD_MAX_BYTES", 10)
    payload = base64.b64encode(b"x" * 20).decode()
    path, error, status = await api._read_upload({"filename": "big.csv", "content_base64": payload})
    assert path is None
    assert status == 413
    assert "64MB" in error


async def test_upload_rejects_unsupported_suffix(api):
    path, error, status = await api._read_upload(
        {"filename": "evil.exe", "content_base64": base64.b64encode(b"x").decode()}
    )
    assert (path, status) == (None, 400)
    assert "只接受" in error


class FakeUpload:
    """模拟 StarletteUploadFile：size 有值、read(n) 按块返回，并记录每次 read 的实参。

    记录 read 实参是本组用例的关键：旧实现的过错正是 `read(-1)` 一次性读全量，
    只断言「最终结果正确」是抓不到的（内存里读 32 字节和分 8 次读 32 字节
    返回值完全一样），必须断言**读取粒度**。
    """

    def __init__(self, data: bytes, filename: str = "big.csv", *, declared_size=None):
        self._data = data
        self._offset = 0
        self.filename = filename
        # declared_size 可以撒谎：真实场景里 Content-Length/size 不可信，
        # 预先声明的大小只是优化，落盘时仍必须自己累计校验。
        self.size = len(data) if declared_size is None else declared_size
        self.read_calls: list[int] = []

    async def read(self, size: int = -1):
        self.read_calls.append(size)
        if size is None or size < 0:
            chunk, self._offset = self._data[self._offset :], len(self._data)
            return chunk
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class FakeUploadRequest:
    def __init__(self, upload):
        self._upload = upload

    async def files(self):
        return {"file": self._upload}

    async def form(self):
        return {}


@pytest.fixture
def replace_spy(monkeypatch):
    """记录 os.replace 调用（转发给真实实现，不影响其他代码）。"""
    calls: list[tuple[str, str]] = []
    real_replace = dashboard.os.replace

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(dashboard.os, "replace", spy)
    return calls


async def test_upload_streams_in_chunks_and_aborts_on_limit(api, monkeypatch, replace_spy):
    """multipart 分支按 _UPLOAD_CHUNK_BYTES 分块读，累计超限立刻停并清理半成品。

    关键点：size 预检必须**不生效**（这里让 upload.size 撒谎成 4，小于上限 10），
    否则会像旧用例那样在 `storage.size` 预检处直接 413、`_store_stream` 压根没被
    调用——那时无论实现改成 `read(-1)` 还是删掉 .part 清理，用例都照样绿。
    """
    monkeypatch.setattr(dashboard, "_UPLOAD_MAX_BYTES", 10)
    monkeypatch.setattr(dashboard, "_UPLOAD_CHUNK_BYTES", 4)
    upload = FakeUpload(b"y" * 32, declared_size=4)
    monkeypatch.setattr(dashboard, "web_request", FakeUploadRequest(upload))

    path, error, status = await api._read_upload({"filename": "big.csv"})

    assert (path, status) == (None, 413)
    assert "64MB" in error
    # 读取粒度必须是分块上限，而不是 -1/None 一次性读
    assert upload.read_calls, "必须真的进入流式读取分支"
    assert set(upload.read_calls) == {4}, f"应按 4 字节分块读取，实际 {upload.read_calls}"
    assert len(upload.read_calls) > 1, "一次性把内容读完说明没有分块"
    # 累计到 12 字节就该中止，不该把 32 字节全读完（4 块 × 4 = 16 已超）
    assert upload._offset <= 16, f"超过上限后仍在继续读，已读 {upload._offset} 字节"
    assert replace_spy == [], "超限时不得把半成品改名为成品"
    leftovers = list((api.p._data_dir / "webui_uploads").glob(".upload-*.part"))
    assert leftovers == [], "超限时必须删掉半成品"
    assert list((api.p._data_dir / "webui_uploads").iterdir()) == [], "失败后不得留下任何文件"


async def test_upload_aborts_on_observed_oversize_even_when_size_lies(api, monkeypatch):
    """size 谎报成 1 字节也不能绕过上限：落盘时按实际累计字节数校验。"""
    monkeypatch.setattr(dashboard, "_UPLOAD_MAX_BYTES", 8)
    upload = FakeUpload(b"z" * 100, declared_size=1)
    monkeypatch.setattr(dashboard, "web_request", FakeUploadRequest(upload))

    path, error, status = await api._read_upload({"filename": "big.json"})

    assert (path, status) == (None, 413)
    assert "64MB" in error
    assert list((api.p._data_dir / "webui_uploads").iterdir()) == []


async def test_upload_streams_to_file_when_within_limit(api, monkeypatch, replace_spy):
    """正常路径：分块读完后原子改名，内容与写入字节完全一致，不留 .part。"""
    monkeypatch.setattr(dashboard, "_UPLOAD_CHUNK_BYTES", 4)
    payload = b"a,b\n1,2\n"
    upload = FakeUpload(payload, filename="small.csv")
    monkeypatch.setattr(dashboard, "web_request", FakeUploadRequest(upload))

    path, error, status = await api._read_upload({"filename": "small.csv"})

    assert (error, status) == ("", 0)
    assert path is not None
    assert path.read_bytes() == payload
    assert path.suffix == ".csv"
    assert set(upload.read_calls) == {4}, "落盘也应按分块上限读取"
    assert len(replace_spy) == 1, "成功时必须原子改名一次"
    assert replace_spy[0][1].endswith(".csv")
    assert not list((api.p._data_dir / "webui_uploads").glob("*.part"))
    assert list((api.p._data_dir / "webui_uploads").iterdir()) == [path]


class CheckPlugin:
    """check_now 需要的最小插件面：账号、冷却、去重标志、一轮检查。"""

    def __init__(self, tmp_path):
        self._data_dir = tmp_path
        self.config = {}
        self.accounts = ["42"]
        self._checking = False
        self._blocked_until = 0.0
        self._manual_check_task = None
        self.events = []
        self.started = 0
        self.release = None

    async def check_all(self):
        self.started += 1
        if self.release is not None:
            await self.release.wait()
        return 1

    def _record_event(self, kind, **kwargs):
        self.events.append(f"{kind}:{kwargs.get('detail', '')}")


async def test_check_now_task_is_cancelled_by_shutdown(tmp_path, monkeypatch):
    """手动检查必须进 _jobs：否则卸载时撤不掉，关库后还在写。"""
    import asyncio

    monkeypatch.setattr(dashboard, "_WEB_AVAILABLE", True)
    monkeypatch.setattr(dashboard, "json_response", lambda payload: Resp(payload))
    monkeypatch.setattr(
        dashboard, "error_response", lambda message, *, status_code=400: Resp({}, status_code)
    )
    plugin = CheckPlugin(tmp_path)
    plugin.release = asyncio.Event()
    api = dashboard.DashboardAPI(
        plugin, "astrbot_plugin_weibo_suite", "v0.7.0", lambda text: text, lambda text: text
    )

    first = await api.check_now()
    assert first.payload == {"started": True}
    task = plugin._manual_check_task
    assert task in api._jobs, "任务必须登记到 _jobs，否则 shutdown 取消不到"
    await asyncio.sleep(0)
    assert plugin.started == 1

    await api.shutdown()

    assert task.done(), "shutdown 之后任务必须已经结束"
    assert api._jobs == set()
    # 已经在等待的检查被撤掉后，可以再次发起
    again = await api.check_now()
    assert again.payload == {"started": True}
    await api.shutdown()


def test_prune_upload_dir_drops_stale_files(tmp_path):
    import os

    folder = tmp_path / "webui_uploads"
    folder.mkdir()
    stale = folder / "stale.csv"
    stale.write_text("x", encoding="utf-8")
    os.utime(stale, (0, 0))
    fresh = folder / "fresh.csv"
    fresh.write_text("x", encoding="utf-8")
    dashboard.DashboardAPI._prune_upload_dir(folder)
    assert not stale.exists()
    assert fresh.exists()


async def test_upload_rejects_unsafe_extension(api, monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "web_request",
        FakeRequest({"filename": "evil.exe", "content_base64": base64.b64encode(b"x").decode()}),
    )
    result = await api.copylib_import()
    assert result.status_code == 400
    assert "只接受" in result.payload["message"]


def test_safe_error_masks_url_and_ip(api):
    text = api._safe_error("failed http://127.0.0.1:8080/secret from 10.1.2.3")
    assert "127.0.0.1" not in text
    assert "10.1.2.3" not in text


def test_safe_error_masks_local_absolute_paths(api):
    # 异常串里的部署目录/用户名不该回传给浏览器
    posix = api._safe_error("FileNotFoundError: [Errno 2] '/home/alice/.astrbot/data/x.json'")
    assert "/home/alice" not in posix
    assert "<路径已隐藏>" in posix
    win = api._safe_error("PermissionError: [WinError 5] 'C:\\Users\\bob\\AstrBot\\data\\a.db'")
    assert "bob" not in win
    assert "<路径已隐藏>" in win
    # 相对路径与普通文本不该被误伤
    assert api._safe_error("archives/seed.json 解析失败") == "archives/seed.json 解析失败"


def test_safe_error_masks_url_ip_secret_and_local_path(api):
    """面板错误四个口径一次收齐：URL、裸 IP、裸凭据、本机绝对路径。"""
    text = (
        "request failed: http://internal.example.com/x from 10.1.2.3 "
        "client_secret=abcd1234efgh at 'C:\\Users\\bob\\AstrBot\\data\\a.db'"
    )
    safe = api._safe_error(text)

    assert "internal.example.com" not in safe
    assert "10.1.2.3" not in safe
    assert "abcd1234efgh" not in safe
    assert "bob" not in safe
    assert "client_secret=" in safe
    assert "<路径已隐藏>" in safe


def test_safe_error_masks_authorization_bearer_header(api):
    """面板回传的认证头必须整段消失：只遮 Bearer 会留下后面的令牌。"""
    token = "eyJhbGciOiJIUzI1NiJ9.abc.def"
    text = api._safe_error(f"请求失败 authorization: Bearer {token}")
    assert token not in text
    assert "Bearer" not in text
    assert "authorization: <凭据已隐藏>" in text


def test_safe_error_masks_cookie_header_value(api):
    """面板错误响应与 errors.log 用同一套 Cookie 头口径。"""
    safe = api._safe_error("请求失败 Cookie: session=abc123; theme=dark")
    assert "abc123" not in safe
    assert "session=<凭据已隐藏>" in safe
    assert "theme=dark" in safe


def test_safe_error_keeps_plain_text_and_does_not_cut_placeholder(api):
    assert api._safe_error("解析失败：缺少字段 title") == "解析失败：缺少字段 title"

    # 占位符横跨长度上限时从它前面截断，不能回传半个 <凭据已隐藏>
    noisy = "x" * 200 + " client_secret=abcd1234efgh"
    cut = api._safe_error(noisy, limit=220)
    assert cut.endswith("…")
    assert "<凭据已" not in cut
    assert "client_secret=" in cut
