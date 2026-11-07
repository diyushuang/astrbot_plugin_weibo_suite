"""登录态与凭据的磁盘口径。

两条完全不同的取数路径，凭据处理方式也不同：

- **M1（默认，浏览器）**：Cookie 只活在 Playwright 持久化上下文里，本模块只
  记录"这个平台上次登录成功是什么时候"，**不读、不写、不落盘任何 Cookie**。
  这是保号的关键：Cookie 一旦被导出成文本，就等于把可以无限重放的凭证复制到
  了插件数据目录，泄漏面从"一个浏览器 profile"扩大到"任何能读文件的东西"。
- **M2（HTTP + 导出 Cookie）**：Cookie **只从配置项读**（`<platform>_cookie`），
  插件不读、也不写任何凭据文件；面板只显示 `已配置 / 长度 / 前 6 位…`，日志里
  一律走 `redact_cookie()`。

`parse_cookie_string()` / `redact_cookie()` 都是纯函数，测试直接断言字符串。
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

from . import constants as C

# 平台登录态的粗判：M1 用页面特征，M2 用 Cookie 里必须存在的字段
REQUIRED_COOKIE_KEYS: dict[str, tuple[str, ...]] = {
    # 抖音的 web 接口签名需要 uifid；没有它接口必 403（实测 ArgusSecurityPlugin）
    "douyin": ("ttwid",),
    "xhs": ("a1", "web_session"),
    "instagram": ("sessionid",),
}


def parse_cookie_string(raw: str) -> dict[str, str]:
    """把浏览器导出的 `k=v; k2=v2` 解析成 dict；重复键以后出现的为准。

    容忍换行、多余空格、`Cookie:` 前缀、以及尾随分号——用户是从开发者工具里
    复制的，格式不可能完全一致。解析不了的片段直接跳过，不抛异常。
    """
    text = str(raw or "").strip()
    if not text:
        return {}
    if text.lower().startswith("cookie:"):
        text = text[7:]
    text = text.replace("\r", ";").replace("\n", ";")
    result: dict[str, str] = {}
    for chunk in text.split(";"):
        piece = chunk.strip()
        if not piece or "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        key = key.strip()
        if not key:
            continue
        result[key] = value.strip()
    return result


def redact_cookie(raw: str, *, keep: int = 6) -> str:
    """脱敏展示：只留长度与前几位，绝不回显完整值。"""
    text = str(raw or "")
    if not text:
        return ""
    head = text[:keep]
    return f"{head}…（共 {len(text)} 字符）"


def identity_status(cookie: str) -> dict[str, Any]:
    """一份 Cookie 的脱敏状态（面板展示用）。"""
    jar = parse_cookie_string(cookie)
    return {
        "configured": bool(jar),
        "keys": len(jar),
        "preview": redact_cookie(cookie) if jar else "",
    }


def missing_required_keys(platform: str, cookie: str) -> list[str]:
    jar = parse_cookie_string(cookie)
    return [k for k in REQUIRED_COOKIE_KEYS.get(platform, ()) if k not in jar]


class IdentityStore:
    """`<data>/sns/` 下的登录态文件与浏览器 profile 目录（不落任何 Cookie 文本）。"""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / C.SNS_DIR_NAME
        self._session_cache: dict[str, Any] | None = None

    # ------------------------------------------------------------ 路径

    @property
    def sessions_path(self) -> Path:
        return self.root / "sessions.json"

    @property
    def profile_root(self) -> Path:
        """浏览器持久化 profile 的根目录：每个平台一个独立子目录。"""
        return self.root / C.SNS_BROWSER_DIR

    def profile_dir(self, platform: str) -> Path:
        return self.profile_root / platform

    # ------------------------------------------------------------ 登录态（M1）

    def _sessions(self) -> dict[str, Any]:
        if self._session_cache is None:
            try:
                data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            self._session_cache = data if isinstance(data, dict) else {}
        return self._session_cache

    def login_state(self, platform: str) -> dict[str, Any]:
        row = self._sessions().get(platform)
        if not isinstance(row, dict):
            return {"logged_in": False, "last_login_ts": 0.0, "note": ""}
        return {
            "logged_in": bool(row.get("logged_in")),
            "last_login_ts": float(row.get("last_login_ts") or 0),
            "note": str(row.get("note") or ""),
        }

    def mark_login(self, platform: str, logged_in: bool, *, note: str = "") -> None:
        sessions = self._sessions()
        row = sessions.get(platform)
        row = row if isinstance(row, dict) else {}
        row["logged_in"] = bool(logged_in)
        if logged_in:
            row["last_login_ts"] = time.time()
        row["note"] = str(note)[:120]
        sessions[platform] = row
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            tmp = self.sessions_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.sessions_path)
        except Exception:
            with contextlib.suppress(OSError):
                self.sessions_path.with_suffix(".json.tmp").unlink(missing_ok=True)

    def summary(self, platform: str, cookie_from_config: str = "") -> dict[str, Any]:
        """面板用的一份完整状态：M1 登录态 + M2 凭据是否就绪。"""
        cookie = str(cookie_from_config or "")
        state = self.login_state(platform)
        info = identity_status(cookie)
        missing = missing_required_keys(platform, cookie)
        return {
            "platform": platform,
            "label": C.PLATFORM_LABEL.get(platform, platform),
            "logged_in": state["logged_in"],
            "last_login_ts": state["last_login_ts"],
            "note": state["note"],
            "cookie_configured": info["configured"],
            "cookie_keys": info["keys"],
            "cookie_preview": info["preview"],
            "missing_keys": missing,
            # 只回"目录建了没"的布尔：本机绝对路径一旦进 JSON 就会经
            # `/sns/overview` 回传到浏览器。
            "has_profile": self.profile_dir(platform).is_dir(),
        }
