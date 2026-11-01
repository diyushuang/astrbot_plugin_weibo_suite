"""P6/P8 统一 WebUI：路由清单、接口适配与七页签静态门禁。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.views_backend import dashboard, sns_api


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

    def media_stats(self):
        return {"total": 4, "downloaded": 3, "bed": 2, "thumb": 2, "vector": 1, "failed": 1}

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
    assert dashboard.register_dashboard(
        Plugin(), "suite", "v0.7.0", lambda text: text, lambda text: text
    )
    assert sns_api.register_sns_dashboard(Plugin(), "suite", "v0.7.0")
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
    } <= paths
    assert len(Plugin.context.routes) == 35


def test_dashboard_markup_has_all_tabs_and_actions():
    root = Path(__file__).resolve().parents[1] / "views" / "dashboard"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "style.css").read_text(encoding="utf-8")
    tabs = ("总览", "微博监控", "归档与图床", "文案库", "溯源与签到", "多平台订阅", "错误日志")
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
    path, error = await api._read_upload({"filename": "big.csv", "content_base64": payload})
    assert path is None
    assert "64MB" in error


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
