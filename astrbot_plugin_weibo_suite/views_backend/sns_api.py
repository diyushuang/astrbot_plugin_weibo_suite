"""「多平台订阅」页签的后端接口（抖音 / 小红书 / Instagram）。

7 条路由，与 `views_backend/dashboard.py` 的 28 条合起来是 35 条。分开一个文件
而不是往 dashboard.py 里塞，理由是两件事的失败面完全不同：微博域的面板接口出错
不该让订阅域失联，反过来也一样。

安全性上有一条硬约束：**Cookie 永不出现在任何响应里**。这里能返回的只有
`identity.summary()` 那份脱敏摘要（是否已配置、字段名、前缀 + 长度）。
"""

from __future__ import annotations

import time
from typing import Any

from ..sns import constants as C

try:  # 与新/旧 AstrBot 的 Web 层对接方式保持一致（同 dashboard.py）
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

        def error_response(message: str, status_code: int = 400):
            resp = jsonify({"status": "error", "message": message})
            resp.status_code = status_code
            return resp

        _WEB_AVAILABLE = True
    except ImportError:
        _WEB_AVAILABLE = False

        def json_response(payload: dict):
            raise RuntimeError("AstrBot WebUI 不可用（缺少 quart），控制面板未启用")

        def error_response(message: str, status_code: int = 400):
            raise RuntimeError("AstrBot WebUI 不可用（缺少 quart），控制面板未启用")

        class web_request:
            @staticmethod
            async def json(default: Any = None) -> Any:
                return {} if default is None else default


def _safe(value: Any, limit: int = 240) -> str:
    text = str(value or "").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class SnsAPI:
    """订阅域的面板接口集合。"""

    def __init__(self, plugin: Any, plugin_name: str, version: str):
        self.p = plugin
        self.name = plugin_name
        self.version = version

    # ------------------------------------------------------------ 内部

    def service(self) -> Any:
        """只取插件注入好的实例。

        views_backend 不 import、也不自己 `new` 业务实现（CONTRIBUTING.md 的
        分层约束）；更重要的是，缺 getter 时若在这里再建一个 `SnsService`，
        面板与聊天指令就会各持一个实例、同时往同一个 state 文件写，互相覆盖。
        拿不到就返回 None，由调用方回一条"服务未就绪"的非 2xx。
        """
        getter = getattr(self.p, "sns_service", None)
        if callable(getter):
            return getter()
        return getattr(self.p, "sns", None)

    def _require_service(self) -> tuple[Any, Any]:
        """返回 (service, None) 或 (None, 非 2xx 响应)。"""
        service = self.service()
        if service is None:
            return None, error_response("SNS 服务未就绪", 503)
        return service, None

    async def _body(self) -> dict[str, Any]:
        try:
            data = await web_request.json(default={})
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _platform(text: Any) -> str:
        raw = str(text or "").strip().lower()
        return C.PLATFORM_ALIAS.get(raw, raw if raw in C.PLATFORMS else "")

    def _set_cfg(self, key: str, value: Any) -> None:
        """写进 `sns` 分组并落盘（组是唯一定义面，见 sns_cfg 的说明）。"""
        config = self.p.config
        group = config.get(C.CFG_GROUP)
        if not isinstance(group, dict):
            group = {}
            config[C.CFG_GROUP] = group
        group[key] = value
        saver = getattr(config, "save_config", None)
        if callable(saver):
            saver()

    # ------------------------------------------------------------ 读

    async def overview(self):
        """订阅总览：账号、平台、登录态、守卫参数、浏览器状态与延迟估算。"""
        service, err = self._require_service()
        if err is not None:
            return err
        try:
            snap = service.snapshot()
        except Exception as exc:
            return error_response(_safe(exc), 500)
        return json_response({"status": "ok", **snap})

    async def credentials(self):
        """登录态摘要（脱敏：只回是否配置、字段名、前缀与长度）。"""
        service, err = self._require_service()
        if err is not None:
            return err
        rows = []
        for platform in C.PLATFORMS:
            summary = service.identity.summary(
                platform, service.cfg(f"{platform}_cookie", "") or ""
            )
            rows.append(
                {
                    "platform": platform,
                    "label": C.PLATFORM_LABEL.get(platform, platform),
                    "logged_in": bool(summary.get("logged_in")),
                    "last_login_ts": summary.get("last_login_ts") or 0,
                    "note": _safe(summary.get("note")),
                    "cookie_configured": bool(summary.get("cookie_configured")),
                    # 计数而非字段名列表：面板不需要（也不该）知道用户存了哪些键
                    "cookie_keys": int(summary.get("cookie_keys") or 0),
                    "cookie_preview": summary.get("cookie_preview") or "",
                    "missing_keys": list(summary.get("missing_keys") or []),
                    "profile_dir": summary.get("profile_dir") or "",
                }
            )
        return json_response(
            {
                "status": "ok",
                "fetch_mode": service.fetch_mode,
                "rows": rows,
                "modes": [
                    {
                        "id": "browser",
                        "label": "浏览器取数（推荐）",
                        "desc": "复用本机 Edge，登录态与 Cookie 都留在浏览器里",
                        "risk": "低",
                    },
                    {
                        "id": "http",
                        "label": "Cookie + 纯 HTTP",
                        "desc": "需要把 Cookie 交给插件；指纹不是浏览器，风控概率明显更高",
                        "risk": "高",
                    },
                    {
                        "id": "rss",
                        "label": "RSS 兜底",
                        "desc": "订阅任意 RSS 源，插件只读 feed，不碰平台登录态",
                        "risk": "低",
                    },
                ],
            }
        )

    # ------------------------------------------------------------ 写

    async def session(self):
        """登录窗口：`{action: "open"}` 打开持久化窗口；`{action:"mark"}` 记一次登录。"""
        body = await self._body()
        action = str(body.get("action") or "open").strip().lower()
        platform = self._platform(body.get("platform"))
        if not platform:
            return error_response("未知平台", 400)
        service, err = self._require_service()
        if err is not None:
            return err
        if action == "mark":
            logged_in = bool(body.get("logged_in", True))
            service.identity.mark_login(
                platform, logged_in, note=_safe(body.get("note") or "面板确认")
            )
            return json_response({"status": "ok", "platform": platform, "logged_in": logged_in})
        ok, message = await service.open_login(platform)
        # 窗口打开 ≠ 已登录：这里绝不能替用户宣布登录成功，否则面板会先显示
        # 已登录，下一轮检查才发现 cookie 根本用不了。真伪以下一轮检查为准。
        if not ok:
            return error_response(message, 409)
        return json_response(
            {
                "status": "ok",
                "message": f"{message}（请在弹出的窗口里完成登录，下一轮检查会自动验证）",
            }
        )

    async def accounts(self):
        """订阅增删：`{action:"add"|"remove"|"toggle", platform, target|key}`。"""
        body = await self._body()
        action = str(body.get("action") or "").strip().lower()
        service, err = self._require_service()
        if err is not None:
            return err
        if action == "add":
            platform = self._platform(body.get("platform"))
            if not platform:
                return error_response("未知平台", 400)
            target = str(body.get("target") or "").strip()
            if not target:
                return error_response("缺少账号链接或 id", 400)
            ok, message, _account = service.add_account(platform, target)
            if not ok:
                return error_response(message, 409)
            return json_response({"status": "ok", "message": message})
        if action == "remove":
            platform = self._platform(body.get("platform"))
            target = str(body.get("key") or body.get("target") or "").strip()
            if not platform or not target:
                return error_response("缺少平台或账号", 400)
            ok, message = service.remove_account(platform, target)
            if not ok:
                return error_response(message, 409)
            return json_response({"status": "ok", "message": message})
        if action == "toggle":
            key = str(body.get("key") or "").strip()
            account = service.state.get(key)
            if account is None:
                return error_response("没有这个订阅", 404)
            account.enabled = not account.enabled
            if account.enabled:
                account.next_check_ts = time.time()
            service.state.save()
            return json_response({"status": "ok", "key": key, "enabled": account.enabled})
        return error_response(f"不支持的操作：{action or '（空）'}", 400)

    async def guard(self):
        """守卫参数调整（冷却基数/上限、健康度阈值、间隔、开关）。"""
        body = await self._body()
        if not body:
            service, err = self._require_service()
            if err is not None:
                return err
            return json_response(
                {
                    "status": "ok",
                    "guard": {
                        "base_cooldown": service.guard_config().base_cooldown,
                        "cooldown_cap": service.guard_config().cooldown_cap,
                        "health_pause": service.guard_config().health_pause,
                        "health_degrade": service.guard_config().health_degrade,
                    },
                    "latency": service.latency_estimate(),
                }
            )
        service, err = self._require_service()
        if err is not None:
            return err
        allowed = {
            "enabled": bool,
            "fetch_mode": str,
            "aggressive": bool,
            "night_slowdown": bool,
            "browser_headed": bool,
            "cooldown_base_seconds": int,
            "cooldown_cap_seconds": int,
            "health_pause_threshold": float,
            "health_degrade_threshold": float,
            "warmup_minutes": int,
            "protect_mode_hours": int,
            "base_interval_douyin": int,
            "base_interval_xhs": int,
            "base_interval_instagram": int,
            "message_format": str,
            "message_with_images": bool,
            "max_images": int,
            "text_max_length": int,
            "at_all": bool,
        }
        changed: list[str] = []
        for key, caster in allowed.items():
            if key not in body:
                continue
            raw = body[key]
            try:
                if caster is bool:
                    value: Any = (
                        raw
                        if isinstance(raw, bool)
                        else str(raw).lower()
                        in (
                            "1",
                            "true",
                            "yes",
                            "on",
                        )
                    )
                else:
                    value = caster(raw)
            except (TypeError, ValueError):
                return error_response(f"{key} 的值不合法：{raw!r}", 400)
            if key == "fetch_mode" and value not in ("browser", "http", "rss"):
                return error_response("fetch_mode 只能是 browser / http / rss", 400)
            self._set_cfg(key, value)
            changed.append(key)
        if not changed:
            return error_response("没有可更新的字段", 400)
        return json_response({"status": "ok", "changed": changed})

    async def check_now(self):
        """立刻跑一轮（平台为空则跑全部到点的订阅）。"""
        body = await self._body()
        platform = self._platform(body.get("platform")) if body.get("platform") else ""
        service, err = self._require_service()
        if err is not None:
            return err
        try:
            result = await service.check_all(platform)
        except Exception as exc:
            return error_response(_safe(exc), 500)
        return json_response({"status": "ok", **result})

    async def preview(self):
        """按当前模板渲染一条示例，不发出去（用于确认排版）。"""
        body = await self._body()
        service, err = self._require_service()
        if err is not None:
            return err
        platform = self._platform(body.get("platform")) or "douyin"
        group = service.cfg("message_format") or (
            "📢 {platform}更新\n【{name}】{time}\n{text}\n🔗 {link}"
        )
        sample = {
            "platform": C.PLATFORM_LABEL.get(platform, platform),
            "name": "示例博主",
            "time": time.strftime("%m-%d %H:%M"),
            "text": "这是一条示例更新正文，用来确认模板里的字段替换是否正常。",
            "link": "https://www.douyin.com/video/0000000000000000000",
        }
        try:
            text = str(group).format(**sample)
        except (KeyError, IndexError, ValueError) as exc:
            return json_response(
                {
                    "status": "error",
                    "message": f"模板占位符不合法：{exc}",
                    "text": "",
                }
            )
        return json_response(
            {
                "status": "ok",
                "text": text,
                "sessions": service.sessions(),
                "at_all": bool(service.cfg("at_all", False)),
            }
        )


def register_sns_dashboard(plugin, plugin_name: str, version: str) -> bool:
    """把订阅域的 7 条路由挂到 Dashboard；Web API 不可用时返回 False。"""
    context = getattr(plugin, "context", None)
    register = getattr(context, "register_web_api", None)
    if not callable(register) or not _WEB_AVAILABLE:
        return False
    api = SnsAPI(plugin, plugin_name, version)
    routes = (
        (f"/{plugin_name}/sns/overview", api.overview, ["GET"], "多平台订阅总览"),
        (f"/{plugin_name}/sns/credentials", api.credentials, ["GET"], "各平台登录态摘要"),
        (f"/{plugin_name}/sns/session", api.session, ["POST"], "打开登录窗口/标记已登录"),
        (f"/{plugin_name}/sns/accounts", api.accounts, ["POST"], "订阅增删与启停"),
        (f"/{plugin_name}/sns/guard", api.guard, ["GET", "POST"], "风控守卫参数"),
        (f"/{plugin_name}/sns/check-now", api.check_now, ["POST"], "立刻检查一轮"),
        (f"/{plugin_name}/sns/preview", api.preview, ["POST"], "推送模板预览"),
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
