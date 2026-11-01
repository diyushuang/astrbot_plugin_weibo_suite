"""媒体解析与 SSRF 校验的生产链路端到端用例。

为什么会有这个文件
------------------
复审（变异测试）证明此前的测试有两处“假绿”：

1. `tests/conftest.py` 的 `Image.convert_to_file_path` 桩返回的是 **url 字符串**，
   而真实的 AstrBot 实现（astrbot/core/message/components.py 的 Image ->
   MediaResolver.to_path）永远先把图片内容落成本地文件再返回绝对路径。
   于是 `trace/resolve_mixin.py:172` 的 `os.path.isfile(path)` 恒为 False，
   「宿主已经把图片下载成本地文件」这条最常见的生产主路径，没有任何测试走过；
   反过来，「插件自己兜底下载」这条罕见路径却被反复覆盖。

2. 旧 `imgbed` fixture 把整个 `resolve_public_addrs` 换成“照常 getaddrinfo、
   跳过公网 IP 白名单”的替身，所有用到本地假图床的用例都在**绕过 SSRF 校验**，
   「拒绝私网 / 回环 / 云元数据地址」的规则从未在这些用例里真正执行。

本文件的两组用例分别钉住这两点：本地文件分支必须由 `is_tmp=False` 与
“`_download` 未被调用”共同证明；SSRF 必须由未被打桩的真实函数证明。

两档宿主模式
------------
本文件必须在两种环境下都绿，且断言完全一致、不为任何一档放宽：

- 桩模式（默认）：`conftest` 注入最小 astrbot 桩；
- 真宿主模式：`WEIBO_SUITE_REAL_ASTRBOT=1`，用真 `astrbot` 包。

因此这里**只造两种宿主都同义的输入**：`Image.fromFileSystem(本地文件)`（真包解析回
同一路径、桩直接返回该路径），以及 `Image(file=..., url=...)` + 打类补丁让宿主的
`convert_to_file_path` 失败。刻意不用 `Image.fromURL`——真包会真的发网络请求
（假 URL 必然 400），那测的是宿主网络，不是插件的分支选择。
"""

from __future__ import annotations

import os

import pytest

from astrbot_plugin_weibo_suite.shared import http as url_guard
from astrbot_plugin_weibo_suite.shared.http import GuardedHttpClient, UrlBlockedError
from astrbot_plugin_weibo_suite.trace.features import compute_features_with_mime

# ----------------------------------------------------------------------
# 1) 宿主已把图片落盘：必须走本地文件分支
# ----------------------------------------------------------------------


async def test_host_downloaded_image_uses_local_file_branch(make_plugin, monkeypatch):
    """AstrBot 已把图片存成本地文件时，插件必须直接用本地文件，不再自己下载。

    判别方式不是“某个函数被调用过”，而是三条互补的事实：
    - 解析结果的 path 就是宿主给的那个本地文件（不是 url 字符串）；
    - `is_tmp` 为 False —— 只有插件兜底下载出来的临时文件才是 True
      （`trace/resolve_mixin.py:168` 与 `:194` 两处就是靠这个标记区分的）；
    - `_download`（兜底下载）一次都没被调用。
    """
    from astrbot.api.message_components import Image

    from .conftest import _png_bytes, _write_image_file

    plugin = make_plugin()
    downloaded: list[str] = []

    async def spy_download(url):
        downloaded.append(url)
        raise AssertionError(f"宿主已提供本地文件，不该再兜底下载: {url}")

    monkeypatch.setattr(plugin, "_download", spy_download)

    # 用 fromFileSystem 表达“宿主已经把图片落成本地文件”，两种宿主下都成立：
    # 真包返回 file:// URI + path，convert_to_file_path() 解析回同一个本地路径；
    # 桩直接返回该路径。刻意不用 Image.fromURL：真包会真的发起网络下载（假 URL
    # 必然 400），那测的是宿主网络而不是插件的分支选择。
    local_path = _write_image_file(_png_bytes(20240601), ".png")
    seg = Image.fromFileSystem(local_path)
    returned = await seg.convert_to_file_path()
    assert os.path.isfile(returned), "宿主侧必须真的给出一个本地文件，否则测的又是 URL 分支"
    assert os.path.normcase(os.path.abspath(returned)) == os.path.normcase(
        os.path.abspath(local_path)
    )

    resolved = await plugin._resolve_local(
        seg, probe=lambda path: compute_features_with_mime(path, plugin.hash_size)
    )

    assert resolved.path == local_path and os.path.isfile(resolved.path)
    assert resolved.is_tmp is False, "本地文件分支不得被标成临时文件（那会被当成下载产物删掉）"
    assert resolved.features is not None, "本地分支必须当场完成一次解码，拿到感知特征"
    assert resolved.mime
    assert downloaded == []


async def test_fallback_download_branch_is_the_only_tmp_path(make_plugin, monkeypatch):
    """对照组：宿主没给本地文件时，才由插件下载，并标记 is_tmp=True。

    这条用例证明上面那个 `is_tmp is False` 的断言有判别力——两条分支的返回值
    确实不同，而不是恒为 False。
    """
    from astrbot.api.message_components import Image

    from .conftest import _png_bytes, _write_image_file

    plugin = make_plugin()
    downloaded: list[str] = []

    async def fake_download(url):
        downloaded.append(url)
        # 必须是**能解码的真实图片**：解析器会校验格式并算感知特征，
        # 随便返回一个文本文件只会走进“兜底下载内容不是有效图片”的失败分支。
        return _write_image_file(_png_bytes(20240607), ".png")

    monkeypatch.setattr(plugin, "_download", fake_download)

    async def host_download_failed():
        """宿主的 convert_to_file_path 拿不到本地文件（QQ 图床签名失效时的真实结果）。"""
        raise FileNotFoundError("宿主侧下载原图失败")

    url = "https://multimedia.nt.qq.com.cn/download?fileid=fallback"
    # 生产形态：OneBot 的 image 段同时带 file（文件名）与 url（可下载地址），
    # 宿主先尝试把它落成本地文件；这里让这一步失败，插件必须自己兜底下载。
    seg = Image(file="qq-image-id.jpg", url=url)
    # 真包 Image 是 pydantic 模型，实例上挂不了方法，只能按类打补丁（monkeypatch
    # 会在用例结束时还原）。补的是宿主的下载动作，不是插件逻辑，也不是桩语义。
    monkeypatch.setattr(type(seg), "convert_to_file_path", host_download_failed)
    resolved = await plugin._resolve_local(
        seg, probe=lambda path: compute_features_with_mime(path, plugin.hash_size)
    )

    assert downloaded == [url]
    assert resolved.is_tmp is True
    assert resolved.path != url
    assert resolved.features is not None


# ----------------------------------------------------------------------
# 2) SSRF：真实校验函数，不打桩
# ----------------------------------------------------------------------


def test_resolver_under_test_is_the_real_implementation():
    """确保本文件的 SSRF 断言跑在真实实现上（防止有人又加回全局桩）。

    除了当前函数身份，还要看 imgbed fixture 里被“转发”的那个原件：如果有人在
    import 阶段就把 `resolve_public_addrs` 换掉，`_REAL_RESOLVE` 会捕获到桩，
    fixture 再“转发”也只是转发到桩上，SSRF 校验等于没有。
    """
    from .conftest import _REAL_RESOLVE

    assert url_guard.resolve_public_addrs.__module__ == "astrbot_plugin_weibo_suite.shared.http"
    assert _REAL_RESOLVE.__module__ == "astrbot_plugin_weibo_suite.shared.http"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/random",
        "http://localhost/x",
        "http://10.0.0.1",
        "http://192.168.1.1/x",
        "http://172.16.0.1:7658/random",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
    ],
)
def test_private_and_loopback_targets_are_rejected_by_real_resolver(url):
    """私网 / 回环 / 云元数据地址必须由真实校验拒绝，而不是被 fixture 放行。"""
    with pytest.raises(UrlBlockedError):
        url_guard.resolve_public_addrs(url, set())


async def test_guarded_client_refuses_private_host_before_any_connection():
    """端到端：GuardedHttpClient 在建立连接前就拒绝私网目标（无网络也不影响）。"""
    client = GuardedHttpClient(set())
    try:
        with pytest.raises(UrlBlockedError):
            await client.request("GET", "http://127.0.0.1:9/x")
    finally:
        await client.close()


def test_loopback_is_allowed_only_when_explicitly_allowlisted():
    """正向对照：回环地址只有在调用方显式加入白名单时才放行。

    这正是 `imgbed` fixture 现在的做法（只把假图床那台回环主机并入白名单），
    也是生产里 `random_media.lan_base_url` 内网直连的合法豁免路径。
    """
    host, addrs = url_guard.resolve_public_addrs("http://127.0.0.1:8080/x", {"127.0.0.1"})
    assert host == "127.0.0.1"
    assert addrs and addrs[0]["host"] == "127.0.0.1"
