"""本地 mock 宿主：静态托管 views/dashboard，注入最小 AstrBot 桥接。

用途：面板视觉回归与性能复测（配套 measure.js）。不依赖 AstrBot 运行时。

  python scripts/perf_dashboard/mock_server.py 8765 [dashboard_dir]
  http://127.0.0.1:8765/                  → 面板（浅色）
  http://127.0.0.1:8765/?theme=dark       → 面板（深色）
  http://127.0.0.1:8765/#monitor          → 指定页签
  http://127.0.0.1:8765/?badtime=1        → 非法/缺失时间的边界态
  http://127.0.0.1:8765/?fail=pipeline/status#archive
                                          → 指定接口返回 404，观察失败徽标

基线版本（HEAD 的 views/dashboard）可先导出到临时目录，再用第二个参数指过去：
  git archive HEAD views/dashboard | tar -x -C <临时目录>
"""

from __future__ import annotations

import json
import mimetypes
import random
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[2]
DASH = REPO / "views" / "dashboard"
LOG = Path(tempfile.gettempdir()) / "weibo_perf_requests.log"

sys.path.insert(0, str(REPO))
from views_backend.settings_schema import build_catalog  # noqa: E402

CATALOG = build_catalog()

BRIDGE = r"""
(function () {
  const dark = /(theme=dark)/.test(location.search);
  const fail = (location.search.match(/fail=([^&]+)/) || [])[1] || "";
  const badtime = /(badtime=1)/.test(location.search);
  function make(method) {
    return async function (endpoint, body) {
      const query = [];
      if (fail) query.push("fail=" + encodeURIComponent(fail));
      if (badtime) query.push("badtime=1");
      const url = "/api/" + String(endpoint).replace(/^\/+/, "") +
        (query.length ? "?" + query.join("&") : "");
      const resp = await fetch(url, {
        method: method,
        headers: { "Content-Type": "application/json" },
        body: method === "POST" ? JSON.stringify(body || {}) : undefined,
      });
      const text = await resp.text();
      if (!resp.ok) throw new Error(text || ("HTTP " + resp.status));
      return text ? JSON.parse(text) : {};
    };
  }
  const stub = {
    ready: async () => ({ isDark: dark }),
    onContext: () => {},
    apiGet: make("GET"),
    apiPost: make("POST"),
  };
  window.AstrBotPluginView = stub;
  window.AstrBotPluginPage = stub;
})();
"""

random.seed(20261107)
NOW = time.time()
NAMES = [
    "微博管理员",
    "视觉志",
    "冷笑话精选",
    "思想聚焦",
    "人民日报",
    "每日壁纸",
    "摄影日记",
    "科技美学",
    "设计青年",
    "城市画报",
    "深夜食堂",
    "猫咪研究所",
]
ACCOUNTS = [{"uid": str(1700000000 + i * 37), "name": n} for i, n in enumerate(NAMES)]


def iso_ts(offset: float) -> float:
    return NOW - offset


def build_overview() -> dict:
    accounts = []
    for i, acc in enumerate(ACCOUNTS):
        accounts.append(
            {
                "uid": acc["uid"],
                "name": acc["name"],
                "seen_count": 120 + i * 33,
                "baseline_done": i != 10,
                "fail_count": 2 if i == 4 else 0,
                "next_retry_ts": iso_ts(-30) if i == 4 else 0,
                "push_ok": 88 - i * 5,
                "at_all": i < 4,
            }
        )
    pending = []
    for i in range(100):
        acc = ACCOUNTS[i % len(ACCOUNTS)]
        pending.append(
            {
                "post_id": str(5200000000000000 + i),
                "uid": acc["uid"],
                "name": acc["name"],
                "text": (
                    f"这是一条待推送的微博正文样例 #{i + 1}："
                    "城市傍晚的天色与人群，随手记录的第七十二个瞬间。"
                ),
                "created_ts": iso_ts(600 + i * 90),
                "retries": 1 if i % 17 == 0 else 0,
                "video": i % 11 == 0,
                "video_fail": "",
                "video_note": False,
                "reject": "",
                "sending": False,
            }
        )
    cancelled = [
        {
            "post_id": str(999000000 + i),
            "uid": ACCOUNTS[i]["uid"],
            "name": ACCOUNTS[i]["name"],
            "text": f"这条推送被手动取消，仍在可撤销窗口内 #{i}。",
            "cancelled_ts": iso_ts(120 + i * 60),
            "retries": 0,
            "video": False,
            "was_sending": i == 0,
            "remain_seconds": 1800 - (120 + i * 60),
        }
        for i in range(6)
    ]
    album_rules = [
        {
            "uid": ACCOUNTS[i]["uid"],
            "name": ACCOUNTS[i]["name"],
            "gid": str(700100 + i),
            "album": f"{ACCOUNTS[i]['name']}图集",
        }
        for i in range(8)
    ]
    keyword_rules = [
        {
            "uid": ACCOUNTS[i % 8]["uid"] if i % 3 else "",
            "name": ACCOUNTS[i % 8]["name"] if i % 3 else "全局",
            "keyword": kw,
            "gid": str(700100 + i % 8),
            "album": f"{ACCOUNTS[i % 8]['name']}图集",
        }
        for i, kw in enumerate(
            [
                "壁纸",
                "摄影",
                "合集",
                "高清",
                "原图",
                "风景",
                "人像",
                "夜景",
                "街拍",
                "胶片",
                "构图",
                "调色",
            ]
        )
    ]
    activity = []
    kinds = ["push", "push_fail", "album", "risk", "error", "at_all", "push_cancel", "info"]
    for i in range(50):
        kind = kinds[i % len(kinds)]
        activity.append(
            {
                "ts": iso_ts(60 + i * 240),
                "kind": kind,
                "name": ACCOUNTS[i % len(ACCOUNTS)]["name"],
                "detail": "12 张图片" if kind == "album" else "第 3 轮",
                "text": f"事件样例正文 #{i}：博主发布了一条包含图片的动态。",
            }
        )
    activity.reverse()
    return {
        "version": "v0.10.0",
        "status": {
            "poll_running": True,
            "checking": False,
            "last_check_ts": iso_ts(45),
            "next_check_ts": iso_ts(-15),
            "poll_interval": 60,
            "pending_count": 100,
            "pending_retrying": 6,
            "push_round_active": True,
            "push_round_stopping": False,
            "cancelled_count": 6,
            "cancel_undo_seconds": 1800,
            "risk_blocked": False,
            "risk_level": 0,
            "risk_until_ts": 0,
            "identity_mode": "visitor",
            "identity_cookie_keys": [],
            "visitor_ts": iso_ts(900),
            "renew_fail_count": 0,
            "proxy": "",
            "ffmpeg": True,
            "album_enabled": True,
            "bg_tasks": {"video_download": 2, "video_preparing": 1, "album_upload": 0},
            "mem": {"total_mb": 8192, "available_mb": 3012},
            "disk": {"total_mb": 488281, "free_mb": 310744},
        },
        "accounts": accounts,
        "at_all_settings": {
            "at_all": True,
            "keywords": ["壁纸", "高清壁纸"],
            "quota_used": 4,
            "quota_limit": 10,
            "last": {"ts": iso_ts(3600), "result": "ok", "detail": "本轮 @ 到 3 个群"},
            "bloggers": [
                {"uid": a["uid"], "name": a["name"], "monitored": True} for a in ACCOUNTS[:4]
            ],
        },
        "sessions": [
            "aiocqhttp:GroupMessage:700100001",
            "aiocqhttp:GroupMessage:700100002",
            "aiocqhttp:GroupMessage:700100003",
            "aiocqhttp:GroupMessage:700100004",
            "aiocqhttp:FriendMessage:900001",
        ],
        "album_rules": album_rules,
        "keyword_rules": keyword_rules,
        "pending": pending,
        "cancelled": cancelled,
        "stats": {
            "push_ok": 3841,
            "push_fail": 27,
            "album_uploaded": 12093,
            "album_skipped": 4421,
            "album_failed": 88,
            "daily": {
                f"2026-11-{(7 - i):02d}": {"push_ok": v}
                for i, v in enumerate([52, 61, 44, 70, 33, 58, 66])
            },
            "by_uid": {
                a["uid"]: {"name": a["name"], "push_ok": 640 - i * 45}
                for i, a in enumerate(ACCOUNTS[:8])
            },
        },
        "activity": activity,
    }


def build_overview_bad_time() -> dict:
    """边界态：时间字段缺失 / 非法，验证前端不会渲染 NaN。"""
    data = build_overview()
    data["status"]["last_check_ts"] = None
    data["status"]["next_check_ts"] = "not-a-number"
    data["status"]["visitor_ts"] = None
    for item in data["activity"]:
        item["ts"] = random.choice(["abc", None, -1, ""])
    return data


def build_archive_bad_time() -> dict:
    """边界态：归档任务缺耗时字段，验证不显示「用时 0s」。"""
    data = build_archive()
    data["backfill"]["stats"].pop("elapsed", None)
    data["backfill"]["stats"]["updated_ts"] = None
    return data


def build_archive() -> dict:
    rows = []
    for i in range(60):
        rows.append(
            {
                "mid": 5100000000000000 + i,
                "uid": ACCOUNTS[i % len(ACCOUNTS)]["uid"],
                "author": ACCOUNTS[i % len(ACCOUNTS)]["name"],
                "created_at": iso_ts(3600 + i * 1800),
                "text": f"归档正文样例 #{i}：午后光线穿过窗棂，落在桌面上的第九张照片。",
                "pic_total": i % 7,
            }
        )
    return {
        "enabled": True,
        "posts": 20184,
        "media": {
            "total": 86420,
            "downloaded": 84120,
            "bed": 79311,
            "thumb": 79002,
            "vector": 76880,
            "failed": 214,
            "abandoned": 31,
        },
        "copylib": {"total": 20184, "cleaned": 19902},
        "recent": rows,
        "backfill": {
            "running": False,
            "stats": {"status": "完成", "inserted": 1842, "existing": 231, "elapsed": 96},
        },
    }


def build_errors() -> dict:
    store = []
    for i in range(50):
        store.append(
            {
                "ts": iso_ts(120 + i * 600),
                "level": "WARNING" if i % 4 == 0 else "ERROR",
                "src": ["push", "album", "sns", "poll"][i % 4],
                "msg": f"错误样例 #{i}：请求超时，已自动重试（第 {i % 3 + 1} 次）。",
                "restored": i % 9 == 0,
            }
        )
    return {"errors": store}


def build_sns() -> dict:
    accounts = []
    for i in range(9):
        accounts.append(
            {
                "platform": ["douyin", "xhs", "instagram"][i % 3],
                "account_id": f"acc_{i:02d}",
                "name": f"{['抖音', '小红书', 'Instagram'][i % 3]}账号 {i + 1}",
                "enabled": i != 7,
                "paused": i == 7,
                "paused_reason": "连续风控",
                "cooling": False,
                "cooldown_until": 0,
                "baseline_done": i != 8,
                "health": 0.92 - i * 0.02,
                "last_error": "签名校验失败" if i == 7 else "",
                "push_ok": 120 - i * 9,
            }
        )
    return {
        "enabled": True,
        "fetch_mode": "browser",
        "aggressive": False,
        "stats": {"risk": 3, "check": 1842},
        "browser": {"available": True},
        "last_round": {"ts": iso_ts(240), "fetched": 42},
        "accounts": accounts,
        "platforms": [
            {
                "platform": "douyin",
                "label": "抖音",
                "accounts": 3,
                "base_interval": 180,
                "login": {"logged_in": True, "cookie_configured": True},
                "paused_until": 0,
            },
            {
                "platform": "xhs",
                "label": "小红书",
                "accounts": 3,
                "base_interval": 240,
                "login": {"logged_in": False, "cookie_configured": True},
                "paused_until": 0,
            },
            {
                "platform": "instagram",
                "label": "Instagram",
                "accounts": 3,
                "base_interval": 300,
                "login": {"logged_in": False, "cookie_configured": False},
                "paused_until": 0,
            },
        ],
        "guard": {
            "trip_hits": 3,
            "trip_window": 600,
            "base_cooldown": 300,
            "cooldown_cap": 7200,
            "health_pause": 0.4,
            "health_degrade": 0.7,
            "protect_mode_hours": 24,
            "night_slowdown": True,
        },
        "latency": {
            "douyin": {"accounts": 3, "interval": 180, "p50": 90, "p95": 178},
            "xhs": {"accounts": 3, "interval": 240, "p50": 121, "p95": 238},
            "instagram": {},
        },
        "events": [
            {
                "ts": iso_ts(300 + i * 900),
                "kind": ["risk", "error", "push", "info", "push_fail", "baseline"][i % 6],
                "name": accounts[i % len(accounts)]["name"],
                "detail": "第 2 页",
                "text": f"订阅事件样例 #{i}。",
            }
            for i in range(20)
        ],
    }


def build_settings() -> dict:
    pages = json.loads(json.dumps(CATALOG["pages"]))
    values = {}
    secrets = {}

    def walk():
        for page in pages:
            for group in page.get("groups", []):
                for field in group.get("fields", []):
                    key = field["key"]
                    if field.get("secret"):
                        secrets[key] = True
                        continue
                    values[key] = field.get("default")

    walk()
    for page in pages:
        for group in page.get("groups", []):
            for field in group.get("fields", []):
                if field["key"] in values:
                    values[field["key"]] = field.get("default")
    return {
        "version": "v0.10.0",
        "generated_ts": NOW,
        "counts": CATALOG["counts"],
        "pages": pages,
        "values": values,
        "secrets": sorted(secrets),
    }


def build_copylib_list() -> dict:
    return {
        "items": [
            {
                "mid": 5000000000000000 + i,
                "uid": ACCOUNTS[i % len(ACCOUNTS)]["uid"],
                "author": ACCOUNTS[i % len(ACCOUNTS)]["name"],
                "created_at": iso_ts(7200 + i * 900),
                "text": f"文案库样例 #{i}：把日常的碎片装订成册，翻页的时候才发现已经写了这么久。",
                "cleaned": i % 5 != 0,
            }
            for i in range(40)
        ]
    }


ROUTES = {
    "overview": build_overview,
    "errors": build_errors,
    "preview-push": lambda: {
        "at_all_on": True,
        "at_all": True,
        "with_images": True,
        "with_videos": False,
        "at_all_uids": [a["uid"] for a in ACCOUNTS[:3]],
        "at_all_keywords": ["壁纸", "高清"],
        "text": (
            "【视觉志】刚刚发布了新微博：\n城市傍晚的天色与人群，"
            "随手记录的第七十二个瞬间。\n\n"
            "🔗 https://m.weibo.cn/detail/5200000000000000\n@全体成员"
        ),
    },
    "archive/overview": build_archive,
    "pipeline/status": lambda: {
        "status": {
            "enabled": True,
            "mode": "增量",
            "queue": 12,
            "pending": 4,
            "posts": 128,
            "media": {"pending": 210, "done": 1480, "failed": 9},
            "last_error": "",
        }
    },
    "pipeline/naming": lambda: {
        "naming_directory": "weibo_archive",
        "naming_file": "{date}-{uid}-{mid}",
        "categories": {"摄影": "photo", "壁纸": "wallpaper"},
    },
    "copylib/overview": lambda: {
        "enabled": True,
        "total": 20184,
        "cleaned": 19902,
        "long": 3120,
        "retweet": 1841,
        "with_pics": 12093,
    },
    "copylib/list": build_copylib_list,
    "trace/status": lambda: {
        "checkin_mode": "weibo",
        "library": {"total": 4831, "indexed": 4780},
        "vector": {"count": 4780, "enabled": True, "model": "text-embedding-3-small"},
        "rescan": {"running": False},
    },
    "sns/overview": build_sns,
    "sns/credentials": lambda: [
        {
            "platform": "douyin",
            "label": "抖音",
            "logged_in": True,
            "cookie_configured": True,
            "cookie_preview": "tt_webid=***8f2a",
            "missing_keys": [],
            "profile_ready": True,
        },
        {
            "platform": "xhs",
            "label": "小红书",
            "logged_in": False,
            "cookie_configured": True,
            "cookie_preview": "web_session=***c71",
            "missing_keys": ["a1"],
            "profile_ready": True,
        },
        {
            "platform": "instagram",
            "label": "Instagram",
            "logged_in": False,
            "cookie_configured": False,
            "cookie_preview": "",
            "missing_keys": ["sessionid", "csrftoken"],
            "profile_ready": False,
        },
    ],
    "settings": build_settings,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _log(self, path: str):
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.time():.3f}\t{self.command}\t{path}\n")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        fail = (query.get("fail") or [""])[0]
        bad_time = (query.get("badtime") or [""])[0] == "1"
        if path.startswith("/api/"):
            name = path[len("/api/") :]
            self._log(f"api:{name}")
            if fail and name == fail:
                self._send(404, b'{"message":"simulated failure"}', "application/json")
                return
            if name == "overview" and bad_time:
                payload = json.dumps(build_overview_bad_time(), ensure_ascii=False).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
                return
            if name == "archive/overview" and bad_time:
                payload = json.dumps(build_archive_bad_time(), ensure_ascii=False).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
                return
            builder = ROUTES.get(name)
            if builder is None:
                self._send(404, b'{"message":"unknown endpoint"}', "application/json")
                return
            payload = json.dumps(builder(), ensure_ascii=False).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
            return

        if path in ("/", "/index.html"):
            self._log("page:index")
            html = (DASH / "index.html").read_text(encoding="utf-8")
            html = html.replace(
                '<script type="module" src="./app.js">',
                '<script src="./mock-bridge.js"></script>\n  <script type="module" src="./app.js">',
                1,
            )
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/mock-bridge.js":
            self._send(200, BRIDGE.encode("utf-8"), "text/javascript; charset=utf-8")
            return

        for base in (DASH, REPO / "views" / "dashboard"):
            target = (base / path.lstrip("/")).resolve()
            if base.resolve() in target.parents and target.is_file():
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self._send(200, target.read_bytes(), ctype)
                return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        parsed = urlparse(self.path)
        name = parsed.path[len("/api/") :]
        self._log(f"api:{name}")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        if name == "settings/save":
            self._send(
                200,
                json.dumps({"ok": True, "saved": len(body.get("values", body) or {})}).encode(),
                "application/json",
            )
            return
        self._send(200, json.dumps({"ok": True}).encode(), "application/json")


class Server(ThreadingHTTPServer):
    """浏览器正常断开（关页签、截图后退出）不应打印一屏栈。"""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    LOG.unlink(missing_ok=True)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    if len(sys.argv) > 2:
        global DASH
        DASH = Path(sys.argv[2]).resolve()
    server = Server(("127.0.0.1", port), Handler)
    print(f"preview on http://127.0.0.1:{port}/", flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
