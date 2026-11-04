"""真实 AstrBot 加载冒烟（CI 门禁，本地缺 astrbot 时 skip）。

现有单测大量 monkeypatch 了 `astrbot.api`（conftest 里 _Context/_Star/_StarTools
全是替身），因此"签名对不对""插件类能不能被真实宿主构造"这类问题在单测里
永远发现不了——P1-1 的 `error_response(..., 409)` 就是这么漏到真机上的。

这里在**子进程**里 import 真实 astrbot，做三件事：
1. 用 inspect 断言 `astrbot.api.web.error_response` / `json_response` 的
   status_code 确实是 keyword-only（面板代码必须按关键字传）；
2. 真实构造 `WeiboSuitePlugin(context)`——宿主的"只传 context"路径
   （新装、配置被删），以及带 config 的路径；
3. 未 initialize 就 terminate 必须安全（宿主加载失败后可能直接丢弃实例）。

子进程 cwd 指向 tmp_path，任何误写盘的文件都不会落进仓库。
本地没装 astrbot 时 skip；CI 设 `REAL_ASTRBOT_REQUIRED=1` 时**不允许** skip。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

REQUIRED = os.environ.get("REAL_ASTRBOT_REQUIRED") == "1"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _probe_astrbot() -> bool:
    """在**子进程**里探测真实 astrbot。

    不能在本进程用 importlib.util.find_spec：tests/conftest.py 会在 collection
    前把自制的 `astrbot` 桩注入 sys.modules，桩没有 __spec__，find_spec 直接抛
    ValueError；即使不抛也只会探到桩，永远测不出真宿主。
    """
    code = (
        "import astrbot, sys;"
        "print(getattr(astrbot, '__file__', '') or '');"
        "sys.exit(0 if astrbot.__spec__ is not None else 3)"
    )
    # cwd 必须是干净的空目录：TEMP 根下若有同名 .py（本机就有一个 platform.py）
    # 会抢先被 import，导致探测/冒烟拿到完全无关的模块。
    try:
        with tempfile.TemporaryDirectory() as clean_cwd:
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=clean_cwd,
                capture_output=True,
                timeout=120,
            )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


HAVE_ASTRBOT = _probe_astrbot()

SMOKE = textwrap.dedent(
    """
    import asyncio
    import inspect
    import os

    import astrbot
    from astrbot.api import web as web_api

    sig = inspect.signature(web_api.error_response)
    param = sig.parameters.get("status_code")
    assert param is not None, "error_response 没有 status_code 参数"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        f"status_code 不是 keyword-only：{param.kind}，面板按位置传会 TypeError"
    )
    sig2 = inspect.signature(web_api.json_response)
    assert sig2.parameters["status_code"].kind is inspect.Parameter.KEYWORD_ONLY

    # 运行期契约（不只是签名）：error_response 真的按关键字返回对应状态码，
    # 且面板模块拿到的是**真实实现**而不是 quart 兜底那份
    import json

    from astrbot_plugin_weibo_suite.main import WeiboSuitePlugin
    from astrbot_plugin_weibo_suite.views_backend import dashboard

    assert dashboard.error_response is web_api.error_response, (
        "面板的 error_response 不是 astrbot.api.web 的实现（走了 quart 兜底），"
        "真实宿主下错误分支会 500"
    )
    resp = dashboard.error_response("上传文件超过 64MB", status_code=413)
    assert resp.status_code == 413, resp.status_code
    payload = json.loads(resp.body)
    assert payload["message"] == "上传文件超过 64MB", payload
    assert payload["status"] == "error", payload
    ok = dashboard.json_response({"items": []})
    assert ok.status_code == 200
    assert json.loads(ok.body) == {"items": []}

    class Ctx:
        def get_config(self):
            return {}

        async def get_using_provider_async(self):
            return None

    # 1) 宿主的"只传 context"路径：新装 / 配置文件被删时走这条
    plugin = WeiboSuitePlugin(Ctx())
    # 缺失配置时必须按 _conf_schema.json 默认值造一份可落盘的对象，不能退回
    # {}（否则设置页 _save_config 没有 save_config() 可调，保存静默不落盘）。
    assert plugin.config["monitor"]["poll_interval"] == 120, plugin.config
    assert plugin._trace_plugin.config is plugin.config, "两个域必须共用同一份配置对象"
    assert callable(getattr(plugin.config, "save_config", None))
    # 真宿主下它就是 AstrBotConfig，构造时已按 schema 把默认配置写到 config_path
    assert os.path.isfile(plugin.config.config_path), plugin.config.config_path
    assert plugin._weibo_initialized is False
    assert plugin._trace_initialized is False

    # 2) 带 config 的路径：必须保持原对象引用（面板 save_config 回写通路）
    class Cfg(dict):
        def save_config(self):
            pass

    cfg = Cfg({"pipeline": {}})
    plugin2 = WeiboSuitePlugin(Ctx(), cfg)
    assert plugin2.config is cfg, "config 必须是原对象，不能 dict(config) 拷贝"

    # 3) 未 initialize 就 terminate 必须安全（加载失败后宿主可能直接丢弃实例）
    asyncio.run(plugin.terminate())

    # 4) main.py 的 star_map 清理在单测里零覆盖：conftest 注入的 astrbot 桩不提供
    #    astrbot.core.star.star，导入必然走 ImportError 兜底分支。真宿主导入 main
    #    就会执行清理，这里断言"AstrBot 插件列表里只留 main 一条"。
    from astrbot.core.star.star import star_map, star_registry

    pkg = WeiboSuitePlugin.__module__.rsplit(".", 1)[0]
    entries = {k: v for k, v in star_map.items() if k == pkg or k.startswith(pkg + ".")}
    assert entries, f"star_map 里没有本包条目：{sorted(star_map)}"
    assert list(entries) == [pkg + ".main"], (
        f"main.py 的 star_map 清理没有生效，残留条目：{sorted(entries)}"
    )
    assert entries[pkg + ".main"] in star_registry, "main 的元数据必须留在 star_registry 里"

    print("REAL_ASTRBOT_OK", getattr(astrbot, "__version__", "unknown"))
    """
)


def _run_smoke(tmp_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", SMOKE],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


@pytest.mark.skipif(
    not HAVE_ASTRBOT and not REQUIRED,
    reason="本机未安装 astrbot（CI 用 REAL_ASTRBOT_REQUIRED=1 强制真跑）",
)
def test_plugin_loads_against_real_astrbot(tmp_path):
    if REQUIRED and not HAVE_ASTRBOT:
        pytest.fail(
            "REAL_ASTRBOT_REQUIRED=1 但当前环境没有 astrbot："
            "CI 必须安装 AstrBot 后再跑本用例，否则真实加载从未被验证"
        )
    result = _run_smoke(tmp_path)
    assert result.returncode == 0, (
        "真实 AstrBot 加载失败：\nSTDOUT:\n" + result.stdout + "\nSTDERR:\n" + result.stderr
    )
    assert "REAL_ASTRBOT_OK" in result.stdout
