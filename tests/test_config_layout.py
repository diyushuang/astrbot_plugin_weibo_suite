"""配置布局迁移与回落：旧布局 → basic / delivery / limits。

1.7.14 把散落的同类项合并成三个组。关键约束：AstrBot 加载插件配置时会按 schema
删除已不存在的键，所以旧键不能直接删名——它们以「隐藏镜像」留在 schema 里，
迁移只做搬移 + 打版本标记。这里锁定四件事：

1. 旧布局（顶层散项 + image_delivery / qq_upload 两组）的值被搬进新组，不丢；
2. 搬迁只做一次（有版本标记），此后用户改的新值不会被旧值盖回去；
3. 新键优先于旧键（`_cfg` 运行时回落）；
4. 迁移被绕过（配置被手改回旧布局）时旧键仍能生效。
"""

from __future__ import annotations

import pytest

from astrbot_plugin_weibo_suite.trace.main import _LAYOUT_VERSION


def test_migration_moves_legacy_scalars_into_groups(make_plugin):
    plugin = make_plugin(
        top_n=5,
        similarity_threshold=0.9,
        max_download_mb=30,
        remote_hash_ttl=600,
    )
    cfg = plugin.config

    assert cfg["basic"]["top_n"] == 5
    assert cfg["basic"]["hash_similarity_threshold"] == 0.9
    assert cfg["limits"]["max_download_mb"] == 30
    assert cfg["limits"]["remote_hash_ttl"] == 600
    assert cfg["config_layout"] == _LAYOUT_VERSION


def test_migration_merges_both_delivery_groups(make_plugin):
    plugin = make_plugin(
        image_delivery={"mode": "url", "max_side": 1280, "quality": 70},
        qq_upload={"mode": "bytes", "direct_max_mb": 5, "on_failure": "notify"},
    )
    delivery = plugin.config["delivery"]

    assert delivery["napcat_mode"] == "url"
    assert delivery["napcat_max_side"] == 1280
    assert delivery["napcat_quality"] == 70
    assert delivery["qq_mode"] == "bytes"
    assert delivery["qq_native_max_mb"] == 5
    assert delivery["qq_on_failure"] == "notify"


def test_migrated_values_reach_the_readers(make_plugin):
    """搬完家还要真的被读到：两条推送通道各自解析出新值。"""
    plugin = make_plugin(
        image_delivery={"mode": "url", "max_side": 1280, "quality": 70},
        qq_upload={"mode": "url", "url_max_mb": 30},
    )

    mode, max_side, quality, _style = plugin._delivery_config()
    assert (mode, max_side, quality) == ("url", 1280, 70)

    qq_mode, _native, url_max, _prefer, _upload, _failure = plugin._qq_upload_config()
    assert qq_mode == "url"
    assert url_max == 30 * 1024 * 1024


def test_legacy_values_win_on_first_migration(make_plugin):
    """首次迁移以旧值为准：新键此时可能只是 schema 默认值填的。"""
    plugin = make_plugin(
        delivery={"napcat_mode": "auto"},
        image_delivery={"mode": "url"},
    )

    assert plugin.config["delivery"]["napcat_mode"] == "url"
    assert plugin._delivery_config()[0] == "url"


def test_migration_runs_only_once(make_plugin):
    """有版本标记后不再搬移：面板改过的新值不会被旧值盖回去。"""
    plugin = make_plugin(image_delivery={"mode": "url"})
    assert plugin.config["delivery"]["napcat_mode"] == "url"

    # 模拟用户在面板里改成 native，再重启（= 再跑一次迁移）
    plugin.config["delivery"]["napcat_mode"] = "native"
    plugin._migrate_config_layout()

    assert plugin.config["delivery"]["napcat_mode"] == "native"
    assert plugin._delivery_config()[0] == "native"


def test_cfg_falls_back_to_legacy_when_group_missing(make_plugin):
    """迁移被绕过时（配置被手改回旧布局）旧键仍生效。"""
    plugin = make_plugin(top_n=7)
    plugin.config.pop("basic", None)
    plugin.config["top_n"] = 7

    assert plugin._int_cfg("top_n", 3) == 7


def test_first_value_keeps_zero_and_false(make_plugin):
    """0 / False 是合法配置，不能被当成"未配置"跳过。"""
    plugin = make_plugin()

    assert plugin._first_value(0, 5) == 0
    assert plugin._first_value(False, True) is False
    assert plugin._first_value(None, "", 5) == 5


@pytest.mark.parametrize(
    ("legacy", "group", "field"),
    [
        ("search_engine", "basic", "search_engine"),
        ("scan_dirs", "basic", "scan_dirs"),
        ("ai_verify", "basic", "ai_verify"),
        ("register_admin_only", "basic", "register_admin_only"),
        ("remote_hash_enabled", "limits", "remote_hash_enabled"),
        ("remote_hash_max_points", "limits", "remote_hash_max_points"),
    ],
)
def test_scalar_paths_cover_every_moved_key(legacy, group, field):
    """每个被搬走的顶层键都在映射表里挂到了正确的组和字段。"""
    from astrbot_plugin_weibo_suite.trace.main import _SCALAR_PATHS

    assert _SCALAR_PATHS[legacy] == (group, field)


def test_sns_schema_group_covers_every_runtime_key():
    """面板可写的 sns 键与 schema 分组必须一一对应。

    AstrBot 加载配置时会按 schema 删掉未声明的键：schema 少了，运行期写进去的
    值下次启动就消失（表现为"改了没生效"）；schema 多了，面板/文档会暴露出
    代码根本不读的假开关。所以两边必须严格对齐。
    """
    import json
    from pathlib import Path

    from astrbot_plugin_weibo_suite.sns import constants as C

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert "sns" in schema, "schema 里缺 sns 分组，运行期写入的订阅配置会被删掉"
    declared = set(schema["sns"].get("items") or {})
    assert declared == set(C.CFG_KEYS), (
        f"schema 多出来的键：{sorted(declared - set(C.CFG_KEYS))}；"
        f"schema 缺少的键：{sorted(set(C.CFG_KEYS) - declared)}"
    )


def test_sns_schema_secrets_are_marked_secret():
    """Cookie 三项必须标 secret，避免面板/日志明文回显。"""
    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    items = schema["sns"]["items"]

    for key in ("douyin_cookie", "xhs_cookie", "instagram_cookie"):
        assert items[key].get("secret") is True, f"{key} 必须标注 secret"


# AstrBot 文档列出的全部 schema 类型；写错一个字母，宿主在载入插件时就抛
# TypeError，同样是"整个插件装不上"。
_SUPPORTED_SCHEMA_TYPES = {
    "string",
    "text",
    "int",
    "float",
    "bool",
    "object",
    "list",
    "dict",
    "template_list",
    "file",
}


def _schema():
    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _plugin_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


def _walk(node, path=""):
    """遍历 schema，产出 (路径, 节点)，只下钻 items / templates 这类子 schema。"""
    for key, value in node.items():
        if not isinstance(value, dict) or "type" not in value:
            continue
        yield f"{path}.{key}" if path else key, value
        if isinstance(value.get("items"), dict):
            yield from _walk(value["items"], f"{path}.{key}" if path else key)
        templates = value.get("templates")
        if isinstance(templates, dict):
            for name, template in templates.items():
                if isinstance(template, dict) and isinstance(template.get("items"), dict):
                    yield from _walk(
                        template["items"],
                        f"{path}.{key}.templates.{name}" if path else f"{key}.templates.{name}",
                    )


def test_schema_types_are_supported():
    bad = [
        f"{path}={node['type']}"
        for path, node in _walk(_schema())
        if node["type"] not in _SUPPORTED_SCHEMA_TYPES
    ]
    assert not bad, f"schema 里出现宿主不认识的 type：{bad}"


def test_object_nodes_declare_items():
    """`type: object` 必须带 `items`，否则宿主载入插件时直接失败。

    AstrBot 的 `AstrBotConfig._config_schema_to_default_config` 对每个 object
    节点都走 `_parse_schema(v["items"], conf[k])`，缺键就是 `KeyError('items')`；
    PluginManager 把它包成「加载插件「…」时出现问题，原因：'items'。」并判安装
    失败——报错信息里既没有字段名也没有文件名，极难反查。v0.9.0 就是
    `quote_api_headers`、`image_delivery`、`qq_upload` 三处写成了无 items 的
    object，AstrBot 4.29.1 上装不上。自由键值映射请用 `type: dict`。
    """
    bad = [
        path for path, node in _walk(_schema()) if node["type"] == "object" and "items" not in node
    ]
    assert not bad, f"这些 object 节点缺 items（应补 items 或改成 type: dict）：{bad}"


def test_schema_to_default_config_matches_host_algorithm():
    """复刻宿主生成默认配置的算法，确保 schema 能真的走完一遍。

    真机上是 `_config_schema_to_default_config` 抛异常导致安装失败，这里把同一
    段逻辑独立实现一遍：任何 schema 结构错误（未知 type、object 缺 items）都会
    在这条用例里炸出来，而不是等到用户装插件时才发现。
    """
    # 宿主的 DEFAULT_VALUE_MAP：类型 → 缺省默认值
    value_map = {
        "string": "",
        "text": "",
        "int": 0,
        "float": 0.0,
        "bool": False,
        "list": [],
        "dict": {},
        "object": {},
        "template_list": [],
        "file": [],
    }

    def parse(schema: dict, conf: dict) -> None:
        for key, node in schema.items():
            if node["type"] not in value_map:
                raise TypeError(f"不受支持的配置类型 {node['type']}")
            default = node["default"] if "default" in node else value_map[node["type"]]
            if node["type"] == "object":
                conf[key] = {}
                parse(node["items"], conf[key])
            else:
                conf[key] = default

    conf: dict = {}
    parse(_schema(), conf)
    assert conf["checkin"]["quote_api_headers"] == {}
    assert conf["image_delivery"] == {}
    assert conf["qq_upload"] == {}

    # 插件侧按同一算法复刻的默认值（config 缺失时用来建可落盘的配置对象）必须
    # 与宿主结果一致，否则"新装默认值"会和面板显示/宿主实际值分叉。
    from astrbot_plugin_weibo_suite.weibo.plugin import _schema_default_config

    assert _schema_default_config(_schema()) == conf


def test_documented_config_key_count_matches_schema():
    """README 与配置手册里的「180 个配置键」必须与 schema 同步。

    配置手册自己声明「以 `_conf_schema.json` 当前内容为准，改 schema 时这份计数
    要一起改」——既然规则写出来了，就交给用例守。计数口径与手册一致：每个配置
    项算一个键，`object` 分组容器本身也算一个，其子键另计。
    """
    import re

    schema = _schema()

    def count(node: dict) -> int:
        total = 0
        for value in node.values():
            if not isinstance(value, dict) or "type" not in value:
                continue
            total += 1
            if value["type"] == "object" and isinstance(value.get("items"), dict):
                total += count(value["items"])
        return total

    total = sum(
        count(node.get("items") or {}) for node in schema.values() if node.get("type") == "object"
    )

    readme = (_plugin_root() / "README.md").read_text(encoding="utf-8")
    assert f"{total} 个配置键" in readme, f"README 的键数与 schema 不符（实际 {total}）"

    doc = (_plugin_root() / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    assert f"合计 {total} 个分组键" in doc, f"配置手册的合计与 schema 不符（实际 {total}）"

    # 分组表逐行对齐，避免总数蒙对、分项漂移
    for name, expected in re.findall(r"^\| `([a-z_]+)` \| (\d+) \|", doc, re.M):
        if name not in schema or schema[name].get("type") != "object":
            continue
        actual = count(schema[name].get("items") or {})
        assert int(expected) == actual, (
            f"配置手册 {name} 组写 {expected} 个键，schema 实际 {actual} 个"
        )
