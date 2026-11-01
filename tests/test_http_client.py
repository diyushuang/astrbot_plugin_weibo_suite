"""验证 GuardedHttpClient 的会话复用与调用透传。

同一实例内复用同一个 aiohttp 会话，prepare 钩子与超时按调用透传，
关闭时释放会话。
"""

import aiohttp

from astrbot_plugin_weibo_suite.shared import http as http_client
from astrbot_plugin_weibo_suite.shared.http import GuardedHttpClient


class FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


async def test_guarded_http_client_reuses_one_session(monkeypatch):
    sessions = []
    prepares = []
    calls = []

    async def fake_guarded_request(session, method, url, *, prepare=None, **kwargs):
        sessions.append(session)
        prepares.append(prepare)
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(http_client, "guarded_request", fake_guarded_request)
    client = GuardedHttpClient()
    timeout = aiohttp.ClientTimeout(total=3)

    async with await client.request(
        "GET", "https://example.com", prepare=lambda _u, _m, _c: {"x": 1}, timeout=timeout
    ):
        pass
    async with await client.request("GET", "https://example.com/2"):
        pass

    assert len(sessions) == 2
    assert sessions[0] is sessions[1]
    kwargs = prepares[0]("https://example.com", "GET", False)
    assert kwargs == {"x": 1, "timeout": timeout}
    # 调用方没给 timeout 时必须注入默认超时（短连接上限，防挂死）；
    # 显式给的 timeout 不被默认值覆盖（上面第一条断言）。
    assert prepares[1]("https://example.com/2", "GET", False) == {
        "timeout": http_client.DEFAULT_REQUEST_TIMEOUT
    }
    # prepare 回调自己给出 timeout 时优先（大文件下载/慢接口放宽不被打回默认值）
    async with await client.request(
        "GET",
        "https://example.com/3",
        prepare=lambda _u, _m, _c: {"timeout": timeout},
    ):
        pass
    assert prepares[2]("https://example.com/3", "GET", False) == {"timeout": timeout}

    await client.close()
