"""设置页：179 键全覆盖、默认值回显、类型校验、secret 不回显与落盘。

这组用例守三件事：

1. `_conf_schema.json` 的 15 个可见分组、179 个键，每一个都在设置目录里，
   都有控件、短标签和可写标记；没有重复、没有漏项。
2. 空配置时 GET 回显 schema 默认值；把默认值原样 POST 回去能写入所有分组，
   再 GET 仍然一致。
3. secret 字段的响应里没有原值；提交 `__KEEP__` 保持不变，提交空串才清除。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrbot_plugin_weibo_suite.main import WeiboSuitePlugin
from astrbot_plugin_weibo_suite.trace.features import phash_hex_len
from astrbot_plugin_weibo_suite.views_backend import settings_api, settings_schema

from .conftest import _Context


class Resp:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code


class FakeRequest:
    def __init__(self, body: dict | None = None):
        self.body = body or {}

    async def json(self, default=None):
        return self.body if self.body else ({} if default is None else default)


class FakeConfig(dict):
    def __init__(self):
        super().__init__()
        self.saved = 0

    def save_config(self):
        self.saved += 1


class FakePlugin:
    def __init__(self):
        self.config = FakeConfig()
        self.saved = 0
        self.synced = 0
        self.reinit_calls = 0
        self.reinit_error = ""

    def _sync_config_groups(self):
        self.synced += 1
        group = self.config.get("monitor")
        if isinstance(group, dict) and "poll_interval" in group:
            self.config["poll_interval"] = group["poll_interval"]

    def _save_config(self):
        self.saved += 1
        self.config.save_config()

    async def reinit_trace_runtime(self):
        self.reinit_calls += 1
        if self.reinit_error:
            raise RuntimeError(self.reinit_error)
        return {"hash_size": 16}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings_api, "_WEB_AVAILABLE", True)
    monkeypatch.setattr(settings_api, "json_response", lambda payload: Resp(payload))
    monkeypatch.setattr(
        settings_api,
        "error_response",
        lambda message, status_code=400: Resp({"status": "error", "message": message}, status_code),
    )
    return settings_api.SettingsAPI(FakePlugin(), "astrbot_plugin_weibo_suite", "v0.9.2")


def _visible_keys() -> set[str]:
    schema = settings_schema.load_schema()
    groups = settings_schema.visible_schema_groups(schema)
    return {f"{group}.{name}" for group, node in groups.items() for name in node["items"]}


def _catalog_fields() -> list[dict]:
    catalog = settings_schema.build_catalog()
    return [field for _page, _group, field in settings_schema.iter_fields(catalog)]


def test_catalog_covers_every_visible_schema_key_exactly_once():
    catalog = settings_schema.build_catalog()
    fields = _catalog_fields()
    keys = [field["key"] for field in fields]

    assert len(keys) == len(set(keys)) == 179, "设置目录有重复键或漏项"
    assert set(keys) == _visible_keys(), "设置目录与 schema 的可见键不一致"
    assert catalog["counts"] == {
        "pages": 5,
        "groups": 15,
        "keys": 179,
        "writable": 179,
        "secrets": 16,
    }

    for field in fields:
        assert field["label"], f"{field['key']} 缺少中文短标签"
        assert field["description"], f"{field['key']} 缺少说明"
        assert field["widget"] in {
            "switch",
            "select",
            "number",
            "password",
            "text",
            "textarea",
            "list",
            "json",
        }, f"{field['key']} 控件类型未知: {field['widget']}"
        assert field["writable"] is True
        if field["type"] in ("int", "float") and not field["options"]:
            assert field["min"] is not None and field["max"] is not None, (
                f"{field['key']} 数字项缺少范围"
            )
            assert field["unit"], f"{field['key']} 数字项缺少单位"
        if field["options"]:
            assert field["options"], f"{field['key']} 枚举项没有可选项"


def test_every_visible_group_is_assigned_to_one_page():
    catalog = settings_schema.build_catalog()
    groups = [group["id"] for page in catalog["pages"] for group in page["groups"]]
    assert sorted(groups) == sorted(settings_schema.visible_schema_groups())
    assert len(groups) == len(set(groups)) == 15


async def test_defaults_are_echoed_and_written_back(api, monkeypatch):
    first = await api.get_settings()
    assert first.status_code == 200
    values = first.payload["values"]
    secrets = first.payload["secrets"]

    non_secret = {
        field["key"]: field["default"] for field in _catalog_fields() if not field["secret"]
    }
    assert values == non_secret, "空配置时没有按 schema 默认值回显"
    assert len(secrets) == 16
    assert all(item == {"configured": False} for item in secrets.values())
    assert not set(values) & set(secrets)

    monkeypatch.setattr(settings_api, "web_request", FakeRequest({"values": non_secret}))
    saved = await api.save_settings()
    assert saved.status_code == 200
    assert saved.payload["saved"] == len(non_secret)
    assert api.p.saved == 1
    assert api.p.synced == 1

    config = api.p.config
    for key, expected in non_secret.items():
        group, field = key.split(".", 1)
        assert config[group][field] == expected, f"{key} 没有按默认值写回"

    again = await api.get_settings()
    assert again.payload["values"] == non_secret, "写入后回显与默认值不一致"


async def test_secret_fields_never_return_raw_value(api, monkeypatch):
    api.p.config["monitor"] = {
        "proxy": "http://user:pass@127.0.0.1:7890",
        "custom_cookie": "SUB=secret-cookie",
    }
    response = await api.get_settings()
    value_blob = json.dumps(
        {"values": response.payload["values"], "secrets": response.payload["secrets"]},
        ensure_ascii=False,
    )
    assert "user:pass@" not in value_blob
    assert "secret-cookie" not in value_blob
    assert response.payload["secrets"]["monitor.proxy"] == {"configured": True}
    assert response.payload["secrets"]["monitor.custom_cookie"] == {"configured": True}
    assert "monitor.proxy" not in response.payload["values"]

    monkeypatch.setattr(
        settings_api,
        "web_request",
        FakeRequest({"values": {"monitor.proxy": settings_api.SECRET_KEEP}}),
    )
    kept = await api.save_settings()
    assert kept.status_code == 200
    assert api.p.config["monitor"]["proxy"] == "http://user:pass@127.0.0.1:7890"

    monkeypatch.setattr(
        settings_api,
        "web_request",
        FakeRequest({"values": {"monitor.proxy": ""}}),
    )
    cleared = await api.save_settings()
    assert cleared.status_code == 200
    assert api.p.config["monitor"]["proxy"] == ""
    assert api.p.config["monitor"]["custom_cookie"] == "SUB=secret-cookie"

    monkeypatch.setattr(
        settings_api,
        "web_request",
        FakeRequest({"values": {"monitor.proxy": "http://127.0.0.1:1080"}}),
    )
    updated = await api.save_settings()
    assert updated.status_code == 200
    assert api.p.config["monitor"]["proxy"] == "http://127.0.0.1:1080"


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("monitor.poll_interval", 10, "不能小于"),
        ("monitor.poll_interval", "abc", "需要整数"),
        ("monitor.poll_interval", True, "需要整数"),
        ("monitor.include_retweets", "yes", "需要布尔值"),
        ("basic.search_engine", "bogus", "只能是"),
        ("basic.hash_size", 6, "只能是"),
        ("image_bed.extra_fields", [1, 2], "需要 JSON 对象"),
        ("image_bed.extra_fields", "{bad", "JSON 解析失败"),
        ("archive.since", 123, "需要字符串"),
        ("unknown.key", 1, "不在面板目录"),
    ],
)
async def test_invalid_values_are_rejected_without_writing(api, monkeypatch, key, value, fragment):
    before = copy.deepcopy(dict(api.p.config))
    monkeypatch.setattr(settings_api, "web_request", FakeRequest({"values": {key: value}}))
    response = await api.save_settings()
    assert response.status_code == 400
    assert fragment in response.payload["message"]
    assert dict(api.p.config) == before
    assert api.p.saved == 0


async def test_flat_mirror_is_synced_for_weibo_keys(api, monkeypatch):
    monkeypatch.setattr(
        settings_api,
        "web_request",
        FakeRequest({"values": {"monitor.poll_interval": 300}}),
    )
    response = await api.save_settings()
    assert response.status_code == 200
    assert api.p.config["monitor"]["poll_interval"] == 300
    assert api.p.config["poll_interval"] == 300, "设置页写入没有同步到微博域平铺镜像键"
    assert api.p.synced == 1


async def test_save_failure_rolls_memory_back(api, monkeypatch):
    api.p.config["monitor"] = {"poll_interval": 120}

    def boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(
        settings_api, "web_request", FakeRequest({"values": {"monitor.poll_interval": 300}})
    )
    monkeypatch.setattr(api.p, "_save_config", boom)
    response = await api.save_settings()
    assert response.status_code == 500
    assert api.p.config["monitor"]["poll_interval"] == 120
    assert api.p.config["poll_interval"] == 120, "落盘失败后平铺镜像没有回滚"


async def test_save_failure_removes_flat_key_that_only_existed_in_memory(api, monkeypatch):
    """分组原本不存在时，回滚不能把 sync 写进平铺键的新值留在内存里。"""
    assert "monitor" not in api.p.config
    assert "poll_interval" not in api.p.config

    def boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.p, "_save_config", boom)
    monkeypatch.setattr(
        settings_api, "web_request", FakeRequest({"values": {"monitor.poll_interval": 300}})
    )
    response = await api.save_settings()

    assert response.status_code == 500
    assert "monitor" not in api.p.config
    assert "poll_interval" not in api.p.config


async def test_save_rebuilds_trace_runtime_after_success(api, monkeypatch):
    monkeypatch.setattr(
        settings_api, "web_request", FakeRequest({"values": {"monitor.poll_interval": 300}})
    )
    response = await api.save_settings()

    assert response.status_code == 200
    assert response.payload["saved"] == 1
    assert response.payload["reinit_ok"] is True
    assert api.p.reinit_calls == 1


async def test_save_succeeds_when_trace_reinit_fails(api, monkeypatch):
    """重建失败只降级为告警：磁盘已保存，响应要带标记而不是 500。"""
    api.p.reinit_error = "向量引擎初始化失败"
    monkeypatch.setattr(
        settings_api, "web_request", FakeRequest({"values": {"monitor.poll_interval": 300}})
    )
    response = await api.save_settings()

    assert response.status_code == 200
    assert response.payload["saved"] == 1
    assert response.payload["reinit_ok"] is False
    assert "重建运行期配置失败" in response.payload["reinit_error"]
    assert api.p.config["monitor"]["poll_interval"] == 300
    assert api.p.reinit_calls == 1


async def test_trace_reinit_rebuilds_bed_vector_and_hash_index(make_plugin):
    """改面板配置后，溯源域的图床/向量/哈希快照要跟着换，旧对象不被打断。"""
    plugin = make_plugin()
    old_bed, old_vector, old_library = plugin.bed, plugin.vector, plugin.library
    assert plugin.hash_size == 16

    plugin.config["basic"] = {
        "hash_size": 8,
        "hash_similarity_threshold": 0.9,
        "top_n": 2,
        "max_images_per_query": 5,
    }
    result = await plugin.reinit_runtime_settings()

    assert result["hash_size"] == 8
    assert result["hash_size_changed"] is True
    assert plugin.hash_size == 8
    assert plugin.hash_threshold == pytest.approx(0.9)
    assert plugin.top_n == 2
    assert plugin.max_images_per_query == 5
    assert plugin.bed is not old_bed
    assert plugin.vector is not old_vector
    assert plugin.library is not old_library
    assert plugin.library.expected_phash_hex_len == phash_hex_len(8)
    assert plugin.remote_hash.expected_hex_len == phash_hex_len(8)
    # 旧图库只是被替换引用、不被 close：在跑命令/管线仍能继续读完
    assert old_library.expected_phash_hex_len == phash_hex_len(16)
    assert isinstance(old_library.count(), int)
    old_library.close()


def test_frontend_contains_settings_page_and_every_widget():
    root = Path(__file__).resolve().parents[1] / "views" / "dashboard"
    js = (root / "settings.js").read_text(encoding="utf-8")
    html = (root / "index.html").read_text(encoding="utf-8")
    css = (root / "style.css").read_text(encoding="utf-8")
    app = (root / "app.js").read_text(encoding="utf-8")

    assert 'data-tab="settings"' in html
    assert 'id="settings-card"' in html
    assert 'apiGet("settings")' in js
    assert 'apiPost("settings/save"' in js
    for widget in ("switch", "select", "number", "password", "textarea", "json", "list"):
        assert f'"{widget}"' in js, f"设置页缺少 {widget} 控件分支"
    assert "settingsHasUnsaved" in js
    assert "settingsConfirmLeave" in js
    assert "SECRET_KEEP" in js
    assert "reinit_ok === false" in js
    assert "beforeunload" in js
    assert "initSettings" in app
    assert "max-width: 1580px" in css
    assert "@media (max-width: 1080px)" in css


async def test_settings_save_works_on_real_weibo_plugin(make_weibo_plugin, monkeypatch):
    """用真实 WeiboMonitorPlugin 验证分组写回 + 平铺镜像 + 监控名单同步。"""
    plugin = make_weibo_plugin()
    monkeypatch.setattr(settings_api, "_WEB_AVAILABLE", True)
    monkeypatch.setattr(settings_api, "json_response", lambda payload: Resp(payload))
    monkeypatch.setattr(
        settings_api,
        "error_response",
        lambda message, status_code=400: Resp({"status": "error", "message": message}, status_code),
    )
    api = settings_api.SettingsAPI(plugin, "astrbot_plugin_weibo_suite", "v0.9.2")
    monkeypatch.setattr(
        settings_api,
        "web_request",
        FakeRequest(
            {
                "values": {
                    "monitor.poll_interval": 600,
                    "push.message_format": "测试模板 {name}",
                    "monitor.monitored_uids": ["123456"],
                }
            }
        ),
    )
    response = await api.save_settings()
    assert response.status_code == 200
    assert plugin.config["monitor"]["poll_interval"] == 600
    assert plugin.config["poll_interval"] == 600
    assert plugin.config["message_format"] == "测试模板 {name}"
    assert plugin.accounts["123456"]["name"] == "123456"


async def test_new_install_without_config_can_save_and_reinit(data_dir, monkeypatch):
    """新装（宿主只传 context）：设置页保存要真落盘，重建要读到新值。

    之前 config 缺失时微博域与溯源域各建一个 `{}`，设置页的 `_save_config`
    找不到 `save_config()` 只能静默返回成功，磁盘上什么都没写；重建再读同一份
    空快照，面板改的值永远不生效。
    """
    plugin = WeiboSuitePlugin(_Context())
    assert plugin.config["monitor"]["poll_interval"] == 120, "新装没有按 schema 默认值生成配置"
    assert plugin._trace_plugin.config is plugin.config, "两个域必须共用同一份配置对象"

    monkeypatch.setattr(settings_api, "_WEB_AVAILABLE", True)
    monkeypatch.setattr(settings_api, "json_response", lambda payload: Resp(payload))
    monkeypatch.setattr(
        settings_api,
        "error_response",
        lambda message, status_code=400: Resp({"status": "error", "message": message}, status_code),
    )
    api = settings_api.SettingsAPI(plugin, "astrbot_plugin_weibo_suite", "v0.9.2")
    monkeypatch.setattr(
        settings_api,
        "web_request",
        FakeRequest({"values": {"monitor.poll_interval": 300, "basic.hash_size": 8}}),
    )
    response = await api.save_settings()

    assert response.status_code == 200
    assert response.payload["saved"] == 2
    assert response.payload["reinit_ok"] is True

    # 运行期读到新值；溯源域重建后也不是旧快照
    assert plugin._int_cfg("poll_interval", 120) == 300
    assert plugin.config["poll_interval"] == 300, "保存没有同步到微博域平铺镜像键"
    assert plugin._trace_plugin.hash_size == 8

    # 真落盘：配置对象绑定的宿主配置路径里确实写进了新值
    target = getattr(plugin.config, "_config_path", None) or plugin.config.config_path
    saved = json.loads(Path(target).read_text(encoding="utf-8-sig"))
    assert saved["monitor"]["poll_interval"] == 300
    assert saved["basic"]["hash_size"] == 8


async def test_save_failure_keeps_runtime_account_state(make_weibo_plugin, monkeypatch):
    """落盘失败回滚时，不能把被移除账号的运行态重置成"新账号"。

    曾经的实现先在内存里按新名单同步 self.accounts，再落盘；落盘失败后虽然
    把配置回滚了，但账号条目已被 pop 再重建，seen_ids 与 baseline_done 丢失，
    下一轮会把历史微博当基线重新吞掉。
    """
    plugin = make_weibo_plugin()
    monkeypatch.setattr(settings_api, "_WEB_AVAILABLE", True)
    monkeypatch.setattr(settings_api, "json_response", lambda payload: Resp(payload))
    monkeypatch.setattr(
        settings_api,
        "error_response",
        lambda message, status_code=400: Resp({"status": "error", "message": message}, status_code),
    )
    api = settings_api.SettingsAPI(plugin, "astrbot_plugin_weibo_suite", "v0.9.2")
    plugin.accounts["123456"] = {
        "name": "示例博主",
        "seen_ids": ["1001"],
        "baseline_done": True,
        "fail_count": 2,
        "next_retry_ts": 123.0,
    }
    plugin.config["monitor"] = {"monitored_uids": ["123456"]}
    plugin.config["monitored_uids"] = ["123456"]

    def boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(plugin, "_save_config", boom)
    monkeypatch.setattr(
        settings_api,
        "web_request",
        FakeRequest({"values": {"monitor.monitored_uids": []}}),
    )
    response = await api.save_settings()
    assert response.status_code == 500
    assert plugin.config["monitor"]["monitored_uids"] == ["123456"]
    assert plugin.config["monitored_uids"] == ["123456"]
    assert plugin.accounts["123456"]["seen_ids"] == ["1001"]
    assert plugin.accounts["123456"]["baseline_done"] is True


def test_generated_settings_map_lists_every_key():
    mapping = (Path(__file__).resolve().parents[1] / "docs" / "SETTINGS_MAP.md").read_text(
        encoding="utf-8"
    )
    rows = [line for line in mapping.splitlines() if line.startswith("| ") and "`" in line]
    assert len(rows) == 179, f"映射表应有 179 行配置项，实际 {len(rows)}"
    for key in _visible_keys():
        assert f"`{key}`" in mapping, f"映射表缺少 {key}"


def test_register_settings_routes(monkeypatch):
    class Context:
        def __init__(self):
            self.routes = []

        def register_web_api(self, path, handler, methods, description):
            self.routes.append((path, handler.__name__, tuple(methods), description))

    class Plugin:
        context = Context()

    monkeypatch.setattr(settings_api, "_WEB_AVAILABLE", True)
    assert settings_api.register_settings_dashboard(Plugin(), "suite", "v0.9.2")
    paths = {path.rsplit("/suite/", 1)[-1] for path, *_ in Plugin.context.routes}
    assert paths == {"settings", "settings/save"}
    assert len(Plugin.context.routes) == 2


async def test_catalog_error_only_breaks_settings_page(monkeypatch):
    """schema 缺标签/重复归类不能让开面板失败，只让设置页返回 500。

    build_catalog() 只在 GET/POST 设置接口里调用，注册路由与其它 7 个页签不碰它。
    """

    class Context:
        def __init__(self):
            self.routes = []

        def register_web_api(self, path, handler, methods, description):
            self.routes.append((path, handler.__name__, tuple(methods), description))

    class Plugin:
        def __init__(self):
            self.context = Context()
            self.config = {}

    monkeypatch.setattr(settings_api, "_WEB_AVAILABLE", True)
    monkeypatch.setattr(settings_api, "json_response", lambda payload: Resp(payload))
    monkeypatch.setattr(
        settings_api,
        "error_response",
        lambda message, status_code=400: Resp({"status": "error", "message": message}, status_code),
    )

    def broken():
        raise ValueError("配置键缺少面板元数据（短标签）")

    monkeypatch.setattr(settings_api, "build_catalog", broken)
    plugin = Plugin()
    assert settings_api.register_settings_dashboard(plugin, "suite", "v0.9.2")
    assert len(plugin.context.routes) == 2

    api = settings_api.SettingsAPI(plugin, "suite", "v0.9.2")
    read = await api.get_settings()
    assert read.status_code == 500
    assert "设置目录读取失败" in read.payload["message"]
    saved = await api.save_settings()
    assert saved.status_code == 500
    assert "设置目录读取失败" in saved.payload["message"]
