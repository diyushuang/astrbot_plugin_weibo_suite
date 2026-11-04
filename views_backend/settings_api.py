"""设置页后端：读取 schema 目录、回显默认值、批量校验并写回配置。

安全边界：

- 只接受目录里登记的 180 个分组键。未知键一律 400，避免面板变成任意写入口。
- `secret: true` 的字段从不回传原值，只回 `configured` 布尔；前端未修改时提交
  `__KEEP__` 哨兵，服务端跳过该键。显式清空提交空字符串。
- 校验全部通过后才落盘；落盘失败会把内存里的分组回滚，绝不留"内存改了、磁盘
  没改"的半截状态。
"""

from __future__ import annotations

import contextlib
import copy
import json
import math
import re
import time
from typing import Any

from astrbot.api import logger

from .settings_schema import build_catalog, iter_fields

try:
    from astrbot.api.web import error_response, json_response
    from astrbot.api.web import request as web_request

    _WEB_AVAILABLE = True
except ImportError:
    try:
        from quart import jsonify

        class web_request:
            @staticmethod
            async def json(default: Any = None) -> Any:
                from quart import request

                data = await request.get_json(silent=True)
                if isinstance(data, dict):
                    return data
                return {} if default is None else default

        def json_response(payload: dict):
            return jsonify(payload)

        def error_response(message: str, *, status_code: int = 400):
            resp = jsonify({"status": "error", "message": message})
            resp.status_code = status_code
            return resp

        _WEB_AVAILABLE = True
    except ImportError:
        _WEB_AVAILABLE = False

        def json_response(payload: dict):
            raise RuntimeError("AstrBot WebUI 不可用（缺少 quart），控制面板未启用")

        def error_response(message: str, *, status_code: int = 400):
            raise RuntimeError("AstrBot WebUI 不可用（缺少 quart），控制面板未启用")

        class web_request:
            @staticmethod
            async def json(default: Any = None) -> Any:
                return {} if default is None else default


# 前端"未修改的 secret 字段"哨兵。不能用空串表示保持不变，否则无法表达"清除"。
SECRET_KEEP = "__KEEP__"

_INT_RE = re.compile(r"^-?\d+$")


def _safe_error(exc: Exception) -> str:
    """错误文本回传前去掉绝对路径，和 dashboard.py 的口径一致。"""
    text = str(exc)
    text = re.sub(
        r"(?<![\w:\\])[A-Za-z]:\\(?:[^\\\s'\"<>|,;:]+\\)*[^\\\s'\"<>|,;:]*", "<路径已隐藏>", text
    )
    text = re.sub(r"(?<![\w:/])/(?:[\w.-]+/)+[\w.-]*", "<路径已隐藏>", text)
    return text or exc.__class__.__name__


def _display_value(field: dict[str, Any], raw: Any) -> Any:
    """把配置值整理成前端控件能安全消费的形状；类型不符时回落默认值。"""
    default = field.get("default")
    node_type = field.get("type")
    if node_type == "bool":
        return raw if isinstance(raw, bool) else default
    if node_type == "int":
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else default
    if node_type == "float":
        return raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else default
    if node_type == "list":
        return copy.deepcopy(raw) if isinstance(raw, list) else []
    if node_type in ("object", "dict"):
        return copy.deepcopy(raw) if isinstance(raw, dict) else {}
    if node_type in ("string", "text"):
        return raw if isinstance(raw, str) else default
    return raw


def _coerce_int(raw: Any) -> int:
    if isinstance(raw, bool):
        raise ValueError("需要整数")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str) and _INT_RE.match(raw.strip()):
        return int(raw.strip())
    raise ValueError("需要整数")


def _coerce_float(raw: Any) -> float:
    if isinstance(raw, bool):
        raise ValueError("需要数字")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise ValueError("需要数字") from exc
    else:
        raise ValueError("需要数字")
    if not math.isfinite(value):
        raise ValueError("需要有限数字")
    return value


def _coerce_string(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("需要字符串")
    return raw


def _coerce_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        items: list[Any] = raw.splitlines()
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise ValueError("需要列表")
    out: list[str] = []
    for item in items:
        if isinstance(item, (dict, list, tuple)):
            raise ValueError("列表项必须是字符串")
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _coerce_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 解析失败：{exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ValueError("需要 JSON 对象")
    return raw


def _validate(field: dict[str, Any], raw: Any) -> Any:
    """按 schema 类型 + 面板范围校验，返回归一化后的值。"""
    node_type = field.get("type")
    label = field.get("label") or field.get("key")
    if field.get("secret"):
        if not isinstance(raw, str):
            raise ValueError(f"{label} 需要字符串")
        return raw
    if node_type == "bool":
        if not isinstance(raw, bool):
            raise ValueError(f"{label} 需要布尔值")
        return raw
    if node_type == "int":
        value = _coerce_int(raw)
    elif node_type == "float":
        value = _coerce_float(raw)
    elif node_type in ("string", "text"):
        value = _coerce_string(raw)
    elif node_type == "list":
        value = _coerce_list(raw)
    elif node_type in ("object", "dict"):
        value = _coerce_dict(raw)
    else:
        raise ValueError(f"{label} 的类型 {node_type!r} 不支持面板写入")

    options = field.get("options") or []
    if options:
        allowed = [entry.get("value") for entry in options]
        if value not in allowed:
            names = " / ".join(str(item) for item in allowed)
            raise ValueError(f"{label} 只能是 {names}")

    if node_type in ("int", "float"):
        low, high = field.get("min"), field.get("max")
        unit = field.get("unit") or ""
        if low is not None and value < low:
            raise ValueError(f"{label} 不能小于 {low}{unit}")
        if high is not None and value > high:
            raise ValueError(f"{label} 不能大于 {high}{unit}")
    return value


class SettingsAPI:
    """设置页的 2 条接口。"""

    def __init__(self, plugin: Any, plugin_name: str, version: str):
        self.p = plugin
        self.name = plugin_name
        self.version = version

    def _catalog(self) -> dict[str, Any]:
        return build_catalog()

    @staticmethod
    def _groups(config: Any) -> dict[str, Any]:
        return config if isinstance(config, dict) else {}

    def _snapshot(self, catalog: dict[str, Any]) -> dict[str, Any]:
        config = self._groups(getattr(self.p, "config", {}))
        values: dict[str, Any] = {}
        secrets: dict[str, dict[str, Any]] = {}
        for _page, _group, field in iter_fields(catalog):
            group_cfg = config.get(field["group"])
            group_cfg = group_cfg if isinstance(group_cfg, dict) else {}
            raw = group_cfg.get(field["field"], field.get("default"))
            if field.get("secret"):
                # schema 里有 secret 字段带非空默认值（image_bed.auth_header 默认
                # "Authorization"）；等于默认值不算"用户配置过"，否则空配置也会
                # 显示成已配置。
                text = str(raw or "").strip()
                secrets[field["key"]] = {
                    "configured": bool(text) and text != str(field.get("default") or "").strip()
                }
                continue
            values[field["key"]] = _display_value(field, raw)
        return {
            "status": "ok",
            "version": self.version,
            "generated_ts": time.time(),
            "counts": catalog["counts"],
            "pages": catalog["pages"],
            "values": values,
            "secrets": secrets,
        }

    async def get_settings(self):
        try:
            return json_response(self._snapshot(self._catalog()))
        except Exception as exc:
            return error_response(f"设置目录读取失败：{_safe_error(exc)}", status_code=500)

    async def save_settings(self):
        try:
            catalog = self._catalog()
        except Exception as exc:
            return error_response(f"设置目录读取失败：{_safe_error(exc)}", status_code=500)
        fields = {field["key"]: field for _page, _group, field in iter_fields(catalog)}
        try:
            body = await web_request.json(default={})
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象")
        raw_values = body.get("values")
        if not isinstance(raw_values, dict):
            return error_response("缺少 values 对象")
        if not raw_values:
            return error_response("没有需要保存的配置项")

        unknown = sorted(set(raw_values) - set(fields))
        if unknown:
            head = "、".join(unknown[:5])
            more = f" 等 {len(unknown)} 项" if len(unknown) > 5 else ""
            return error_response(f"这些配置键不在面板目录里，拒绝写入：{head}{more}")

        staged: dict[str, Any] = {}
        for key, raw in raw_values.items():
            field = fields[key]
            if field.get("secret") and raw == SECRET_KEEP:
                continue
            try:
                staged[key] = _validate(field, raw)
            except ValueError as exc:
                return error_response(f"{field['label']}：{exc}")

        config = self._groups(getattr(self.p, "config", {}))
        touched: dict[str, Any] = {}
        flat_touched: dict[str, tuple[bool, Any]] = {}
        for key, value in staged.items():
            group = fields[key]["group"]
            if group not in touched:
                current = config.get(group)
                touched[group] = copy.deepcopy(current) if isinstance(current, dict) else None
            # 平铺镜像键也要快照：分组原本不存在时 previous 为 None，回滚只能
            # pop 分组，而 sync 已经把新值写进了平铺键且再没有源可覆盖。
            flat = fields[key]["field"]
            if flat not in flat_touched:
                exists = isinstance(config, dict) and flat in config
                flat_touched[flat] = (exists, copy.deepcopy(config.get(flat)))
            group_cfg = config.get(group)
            if not isinstance(group_cfg, dict):
                group_cfg = {}
                config[group] = group_cfg
            group_cfg[fields[key]["field"]] = value

        # 微博域业务代码读的是平铺镜像键；把分组写回后同步一次，避免"面板改了
        # 不生效"。方法由入口插件提供，views_backend 不反向导入业务实现。
        sync = getattr(self.p, "_sync_config_groups", None)
        if callable(sync):
            try:
                sync()
            except Exception as exc:
                # 平铺镜像同步失败不阻断分组落盘（重启时还会再同步一次），但必须
                # 留下告警：否则面板显示"已保存"，微博域却读到的还是旧值。
                logger.warning(f"设置页写入后同步平铺镜像失败: {_safe_error(exc)}")

        saver = getattr(self.p, "_save_config", None)
        try:
            if callable(saver):
                saver()
            else:
                raw_saver = getattr(self.p.config, "save_config", None)
                if callable(raw_saver):
                    raw_saver()
        except Exception as exc:
            for group, previous in touched.items():
                if previous is None:
                    config.pop(group, None)
                else:
                    config[group] = previous
            for flat, (existed, previous) in flat_touched.items():
                if existed:
                    config[flat] = previous
                else:
                    config.pop(flat, None)
            # 镜像同步发生在落盘之前；回滚分组与平铺键后再同步一次，确保
            # 运行时读到的值与磁盘上的旧配置一致。
            if callable(sync):
                with contextlib.suppress(Exception):
                    sync()
            return error_response(f"配置落盘失败：{_safe_error(exc)}", status_code=500)

        # 监控名单在运行期是 self.accounts 字典，启动时才按配置同步一次。
        # 设置页直接改名单后补一次同步，避免"保存成功但下一轮不检查新账号"。
        # 必须放在落盘成功之后：落盘失败时若已同步过，回滚只能把账号加回来，
        # 却找不回被移除账号的 seen_ids / baseline_done（等于把它当新账号重建
        # 基线，中间这段时间的新微博会被吞掉）。
        if "monitor.monitored_uids" in staged:
            sync_accounts = getattr(self.p, "_sync_accounts_from_config", None)
            if callable(sync_accounts):
                try:
                    sync_accounts()
                except Exception as exc:
                    logger.warning(f"设置页写入后同步监控名单失败: {_safe_error(exc)}")

        snapshot = self._snapshot(catalog)
        payload = {"saved": len(staged), **snapshot}
        # 落盘、镜像、账号同步都成功后再重建溯源域运行期快照（图床/向量/hash
        # 尺寸），让面板改的配置立刻生效，不依赖宿主是否热重载。重建失败只降级
        # 为告警：磁盘已经保存成功，重启插件即可恢复。
        reinit = getattr(self.p, "reinit_trace_runtime", None)
        if callable(reinit):
            try:
                await reinit()
                payload["reinit_ok"] = True
            except Exception as exc:
                payload["reinit_ok"] = False
                payload["reinit_error"] = f"重建运行期配置失败：{_safe_error(exc)}"
                logger.warning(
                    f"设置已保存，但重建溯源域运行期配置失败（重启插件可生效）: {_safe_error(exc)}"
                )
        return json_response(payload)


def register_settings_dashboard(plugin, plugin_name: str, version: str) -> bool:
    """注册设置页 2 条路由；Web API 不可用时返回 False。"""
    context = getattr(plugin, "context", None)
    register = getattr(context, "register_web_api", None)
    if not callable(register) or not _WEB_AVAILABLE:
        return False
    api = SettingsAPI(plugin, plugin_name, version)
    routes = (
        (f"/{plugin_name}/settings", api.get_settings, ["GET"], "设置页目录与当前值"),
        (f"/{plugin_name}/settings/save", api.save_settings, ["POST"], "批量保存设置"),
    )
    ok = False
    for route, handler, methods, desc in routes:
        try:
            register(route, handler, methods, desc)
            ok = True
        except Exception as exc:
            try:
                from astrbot.api import logger

                logger.warning(f"{plugin_name} 注册面板接口 {route} 失败: {exc!r}")
            except Exception:
                pass
    return ok


__all__ = ["SECRET_KEEP", "SettingsAPI", "register_settings_dashboard"]
