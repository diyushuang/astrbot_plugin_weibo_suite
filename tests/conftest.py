"""测试期 astrbot 宿主装配：默认用最小桩，装了真包时可切到真宿主。

默认行为：在 collection 之前把最小 astrbot 包注入 sys.modules，使
`import astrbot_plugin_weibo_suite.trace.main` 在**没装 astrbot 的机器**上也可行，
从而让测试能真实驱动命令处理器层（`async for ... in plugin.<命令方法>(event)`）。

真宿主模式：设 `WEIBO_SUITE_REAL_ASTRBOT=1`（CI 的 real-astrbot job 就是这么跑的）
后不再整体替换 sys.modules["astrbot"]——真 AstrBot 提供什么就用什么，只把它确实
缺少的名字补成桩（4.29 缺 `astrbot.api.message_components.Message`，补了什么记在
`_FILLED_STUBS` 里）。这样 `views_backend/dashboard.py` 的 astrbot.api.web 导入、
`main.py` 的 star_map 清理才会走真实分支，而不是永远走 ImportError 兜底。

两种模式下取"消息链组件 / 图片地址 / 指令名"请用 tests/host_compat.py，不要直接
依赖某一种宿主的字段布局。

桩只覆盖 main.py 实际用到的符号；不修改任何生产代码。为避免 ruff E402
（模块级 import 必须在文件顶部），main 的导入放在 make_plugin fixture 内部。
"""

from __future__ import annotations

import atexit
import io
import logging
import os
import shutil
import sys
import tempfile
import types
from urllib.parse import urlparse

import pytest
from aiohttp import web

from astrbot_plugin_weibo_suite.shared import http as url_guard

# ----------------------------------------------------------------------
# 1) 最小 astrbot 桩（模块级注入，先于任何 main 导入执行）
# ----------------------------------------------------------------------


# 真实宿主门：设 WEIBO_SUITE_REAL_ASTRBOT=1 时，凡是真 AstrBot 提供的模块一律
# 不装桩，让插件内部拿到真实现。默认关闭，因为没装 astrbot 的机器也要能跑全部
# 用例；但只要装了真 AstrBot（CI 的 real-astrbot job 就是），桩就会把真包整体
# 遮住，代价是：
#   * views_backend/dashboard.py 的 `from astrbot.api.web import ...` 永远走
#     ImportError 兜底，跑的是另一份实现（真机 500 就是这么漏出去的）；
#   * main.py 的 star_map 清理永远走 ImportError 分支，逻辑零覆盖；
#   * 装饰器 / permission / 消息组件的真实语义一概看不见（真实 MessageChain
#     根本不可迭代，桩却给了 __iter__）。
# 门打开后 conftest 补齐真模块缺失的名字（如 4.29 的
# astrbot.api.message_components 没有 Message），补齐记录在 _FILLED_STUBS 里，
# 便于一眼看出"真宿主模式下还有哪些是替身"。
_REAL_HOST = os.environ.get("WEIBO_SUITE_REAL_ASTRBOT") == "1"

if _REAL_HOST:
    # 真 astrbot 导入时会在 get_astrbot_root() 下建 data/；不指走就会在仓库根
    # 留一个未跟踪的 data/（.gitignore 已兜底，但最好根本别建）。
    #
    # 用 `if not ...` 而不是 setdefault：setdefault 只在**键不存在**时生效，
    # 而 astrbot/core/utils/astrbot_path.py 的 get_astrbot_root() 是
    # `os.environ.get("ASTRBOT_ROOT") or os.getcwd()`——空串会回落 cwd。
    # 用户/CI 把 ASTRBOT_ROOT 显式设成空串（很常见的"没配置"写法）时，
    # setdefault 不会覆盖，data/ 就照旧建到仓库根。
    if not os.environ.get("ASTRBOT_ROOT"):
        os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="weibo_suite_root_")
    atexit.register(shutil.rmtree, os.environ["ASTRBOT_ROOT"], True)

# 真宿主模式下被补成替身的 (模块名, 属性名)，便于排查
_FILLED_STUBS: list[tuple[str, str]] = []


def _import_real(name: str):
    """真 AstrBot 提供该模块时返回它；否则 None（含未开门的情况）。"""
    if not _REAL_HOST:
        return None
    import importlib

    try:
        return importlib.import_module(name)
    except Exception:  # 真包缺失/依赖不全，退回桩
        return None


def _install(name: str, **attrs):
    """注册一个模块到 sys.modules，并挂到父模块的同名属性上。

    真实宿主模式下优先用真模块，只补它确实没有的名字。
    """
    real = _import_real(name)
    if real is not None:
        for key, value in attrs.items():
            if hasattr(real, key):
                continue
            try:  # filter 这类以子模块形式存在的名字
                sub = _import_real(f"{name}.{key}")
                if sub is None:
                    raise ModuleNotFoundError(f"{name}.{key}")
                setattr(real, key, sub)
                continue
            except Exception:
                pass
            setattr(real, key, value)
            _FILLED_STUBS.append((name, key))
        sys.modules[name] = real
        parent, _, child = name.rpartition(".")
        if parent and parent in sys.modules:
            setattr(sys.modules[parent], child, real)
        return real
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent, _, child = name.rpartition(".")
    if parent and parent in sys.modules:
        setattr(sys.modules[parent], child, module)
    return module


_IMAGE_TMP = {"dir": None, "seq": 0}


def _image_tmp_dir() -> str:
    """进程级临时目录：存放 Image 替身“已落盘”的图片（session 内复用）。"""
    if _IMAGE_TMP["dir"] is None:
        path = tempfile.mkdtemp(prefix="weibo_suite_img_")
        _IMAGE_TMP["dir"] = path
        atexit.register(shutil.rmtree, path, True)
    return _IMAGE_TMP["dir"]


def _png_bytes(seed: int) -> bytes:
    """生成一张确定的、非纯色的 64x64 PNG（纯色图会被 is_degenerate_phash 判退化）。"""
    import numpy as np
    from PIL import Image as PILImage

    noise = np.random.default_rng(seed).integers(0, 256, (64, 64), dtype=np.uint8)
    buffer = io.BytesIO()
    PILImage.fromarray(noise, mode="L").save(buffer, format="PNG")
    return buffer.getvalue()


def _write_image_file(data: bytes, suffix: str) -> str:
    _IMAGE_TMP["seq"] += 1
    path = os.path.join(_image_tmp_dir(), f"img_{_IMAGE_TMP['seq']}{suffix}")
    with open(path, "wb") as handle:
        handle.write(data)
    return path


class _Image:
    """Image 组件替身：构造即把参数存到自身属性，便于断言回传内容。

    ``convert_to_file_path`` 必须返回**真实存在的本地文件**：AstrBot 的真实实现
    （astrbot/core/message/components.py 的 Image.convert_to_file_path →
    MediaResolver.to_path）永远先把图片内容落成本地文件、再返回绝对路径，网络
    URL 也是先下载。旧替身直接返回 url 字符串，于是
    `trace/resolve_mixin.py:172` 的 `os.path.isfile(path)` 恒为 False——
    “宿主已经把图片下载成本地文件”这条最常见的生产主路径，从来没被任何测试走过。

    需要验证插件**自带**兜底下载分支（resolve_mixin.py:187 起）的用例，请在该用例
    内部用 `force_url_passthrough(monkeypatch)` 显式退回旧语义，而不是把默认行为改回去。
    """

    def __init__(self, *, url=None, file=None, path=None):
        self.url = url
        self.file = file
        self.path = path
        self._converted_path = None

    @classmethod
    def fromURL(cls, url):
        return cls(url=url)

    @classmethod
    def fromBytes(cls, data):
        return cls(file=data)

    @classmethod
    def fromFileSystem(cls, path):
        return cls(path=path)

    async def convert_to_file_path(self):
        if self._converted_path is not None:
            return self._converted_path

        for candidate in (self.path, self.file):
            if isinstance(candidate, str) and candidate and os.path.isfile(candidate):
                self._converted_path = candidate
                return candidate

        # 有本地路径字段但文件不存在：原样返回，让插件照旧走自己的兜底/失败分支
        for candidate in (self.path, self.file):
            if isinstance(candidate, str) and candidate:
                self._converted_path = candidate
                return candidate

        if isinstance(self.file, (bytes, bytearray)):
            # 真实 fromBytes 也是把原始字节落盘；内容不合法时下游必须报解码失败
            self._converted_path = _write_image_file(bytes(self.file), ".bin")
            return self._converted_path

        # 只有 url：宿主会把它下载成本地文件，这里用一张真实 PNG 代表该结果
        _IMAGE_TMP["seq"] += 1
        self._converted_path = _write_image_file(_png_bytes(_IMAGE_TMP["seq"]), ".png")
        return self._converted_path


def force_url_passthrough(monkeypatch):
    """把 Image.convert_to_file_path 退回“返回 url 字符串”的旧语义。

    只给需要验证插件兜底下载分支（resolve_mixin.py:187 起：宿主没给本地文件时
    自己下载）的用例使用。真实 AstrBot 不会返回 url，所以这是**测试侧**的显式豁免。
    """

    async def _passthrough(seg):
        return seg.path or seg.file or seg.url

    monkeypatch.setattr(_Image, "convert_to_file_path", _passthrough)


class _Reply:
    def __init__(self, chain=None):
        self.chain = chain or []


class _Plain:
    def __init__(self, text=""):
        self.text = text


class _At:
    def __init__(self, qq=None):
        self.qq = qq


class _AtAll:
    def __init__(self, **kwargs):
        self.qq = "all"
        self.__dict__.update(kwargs)


class _Video:
    def __init__(self, *, url=None):
        self.url = url

    @classmethod
    def fromURL(cls, url):
        return cls(url=url)

    @classmethod
    def fromFileSystem(cls, path, **kwargs):
        return cls(url=None, **kwargs)

    @classmethod
    def fromBase64(cls, data, **kwargs):
        return cls(url=None, **kwargs)


class _Message:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _MessageChain:
    def __init__(self, chain=None, type_=None):
        self.chain = list(chain or [])
        self.type = type_

    def message(self, text):
        self.chain.append(_Plain(text))
        return self

    def __iter__(self):
        return iter(self.chain)


class _File:
    """File 组件替身：name + 本地路径，get_file 直接返回路径。"""

    def __init__(self, name="", file="", url=""):
        self.name = name
        self.file = file
        self.url = url

    async def get_file(self, allow_return_url=False):
        if allow_return_url and self.url:
            return self.url
        return self.file


def _command(name, alias=None):
    """模拟 filter.command：装饰后原样返回函数，并记录命令名/别名便于断言。"""
    normalized_alias = set(alias or ())

    def decorator(func):
        func.command_name = name
        func.command_alias = normalized_alias
        return func

    return decorator


def _llm_tool(name):
    def decorator(func):
        func.llm_tool_name = name
        return func

    return decorator


def _permission_type(permission):
    def decorator(func):
        func.permission_required = permission
        return func

    return decorator


class _CommandGroupNode:
    def __init__(self, name, func=None):
        self.name = name
        self.func = func

    def __getattr__(self, item):
        def factory(*args, **kwargs):
            def decorator(func):
                func.command_group_name = self.name
                func.command_name = kwargs.get("name") or (args[0] if args else item)
                func.command_alias = set(kwargs.get("alias") or ())
                return func

            return decorator

        return factory


def _command_group(name):
    def decorator(func):
        return _CommandGroupNode(name, func)

    return decorator


class _PermissionType:
    ADMIN = "admin"


class _Filter:
    PermissionType = _PermissionType
    command = staticmethod(_command)
    command_group = staticmethod(_command_group)
    llm_tool = staticmethod(_llm_tool)
    permission_type = staticmethod(_permission_type)


class _AstrMessageEvent:
    pass


class _Context:
    def __init__(self):
        self.config = {}

    def get_config(self):
        return self.config

    def get_using_provider(self):
        return None

    async def get_using_provider_async(self):
        return None


class _Star:
    def __init__(self, context):
        self.context = context
        self.kv = {}

    async def get_kv_data(self, key, default=None):
        return self.kv.get(key, default)

    async def put_kv_data(self, key, value):
        self.kv[key] = value

    async def delete_kv_data(self, key):
        self.kv.pop(key, None)


class _StarTools:
    @staticmethod
    def get_data_dir(name):
        from pathlib import Path

        path = Path(_DATA_DIR["path"]) / "plugin_data" / name
        path.mkdir(parents=True, exist_ok=True)
        return path


class _AstrBotConfig(dict):
    def save_config(self):
        return None


def _register(*_args, **_kwargs):
    def decorator(cls):
        return cls

    return decorator


# 插件数据目录由 fixture 注入；__init__ 会用它落库/建临时目录
_DATA_DIR = {"path": None}


def _get_astrbot_data_path():
    return _DATA_DIR["path"]


_install("astrbot")
_install("astrbot.api", logger=logging.getLogger("astrbot"), AstrBotConfig=_AstrBotConfig)
_install(
    "astrbot.api.event",
    AstrMessageEvent=_AstrMessageEvent,
    MessageChain=_MessageChain,
    filter=_Filter,
)
_install(
    "astrbot.api.message_components",
    Image=_Image,
    Reply=_Reply,
    Plain=_Plain,
    At=_At,
    AtAll=_AtAll,
    Video=_Video,
    File=_File,
    Message=_Message,
)
_install(
    "astrbot.api.star",
    Context=_Context,
    Star=_Star,
    StarTools=_StarTools,
    register=_register,
)
_install("astrbot.core")
_install("astrbot.core.utils")
_install("astrbot.core.utils.astrbot_path", get_astrbot_data_path=_get_astrbot_data_path)


# ----------------------------------------------------------------------
# 2) 事件 / 结果 / 配置替身
# ----------------------------------------------------------------------


class FakeResult:
    """plain_result / chain_result 的返回值：可直接断言 kind / text / chain。"""

    def __init__(self, kind, text=None, chain=None):
        self.kind = kind
        self.text = text
        self.chain = chain


class FakeEvent:
    """AstrMessageEvent 替身：只实现插件真正调用的方法。"""

    def __init__(
        self,
        message_str="",
        *,
        origin="session-1",
        platform="telegram",
        sender_id="10001",
        group_id=None,
        bot=None,
        self_id="9999",
        chain=None,
    ):
        self.message_str = message_str
        self.unified_msg_origin = origin
        self.platform = platform
        self.sender_id = sender_id
        self.group_id = group_id
        self.bot = bot
        self.stopped = False
        self.sent = []
        # message_obj.message 供 _extract_images 遍历
        self.message_obj = types.SimpleNamespace(self_id=self_id, message=list(chain or []))

    def get_platform_name(self):
        return self.platform

    def get_sender_id(self):
        return self.sender_id

    def get_group_id(self):
        return self.group_id

    def get_sender_name(self):
        return "测试用户"

    def stop_event(self):
        self.stopped = True

    def is_admin(self):
        return True

    def plain_result(self, text):
        return FakeResult("plain", text=text)

    def chain_result(self, chain):
        return FakeResult("chain", chain=list(chain))

    async def send(self, result):
        """AstrBot 的主动发送接口；/溯源重扫 用它汇报开始与进度。"""
        self.sent.append(result)


class FakeConfig(dict):
    """AstrBotConfig 替身：dict 语义 + save_config() 无操作。"""

    def save_config(self):
        return None


# ----------------------------------------------------------------------
# 3) 共享 fixture
# ----------------------------------------------------------------------


@pytest.fixture
def make_event():
    """返回 FakeEvent 类本身，测试里 make_event("/随机图", origin="g1") 构造事件。"""
    return FakeEvent


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """把插件数据目录指向 tmp_path，隔离每个用例的库/临时文件。

    桩模式下 `astrbot.api.star.StarTools` 是 conftest 自己的 `_StarTools`，它读
    `_DATA_DIR`，所以设字典就够了。真宿主模式下 StarTools 是真包实现：
    `get_data_dir()` 读的是**进程级**的 `ASTRBOT_ROOT/data/plugin_data/<name>`，
    只设 `_DATA_DIR` 等于没人读——所有 `make_plugin` 用例会共享同一份
    plugin_data，库/哈希缓存/remote_phash.json 跨用例累积，症状是「单跑绿、
    全量红」（例如 test_hash_refresh 里 count==0 的断言实得前面用例留下的 2）。
    所以真宿主下必须把它也指到 tmp_path。monkeypatch 负责还原。
    """
    _DATA_DIR["path"] = str(tmp_path)
    # 插件配置文件的落盘位置也要隔离：`config is None` 时会按 schema 默认值造
    # 一份可落盘的配置对象，真宿主下它默认写进程级 ASTRBOT_ROOT/data/config，
    # 不隔离就会把"新装"用例互相污染（前一条写过 300，后一条读到的就不是默认值）。
    import astrbot.core.utils.astrbot_path as host_path

    monkeypatch.setattr(
        host_path, "get_astrbot_config_path", lambda: str(tmp_path / "config"), raising=False
    )
    if _REAL_HOST:
        from astrbot.api.star import StarTools

        def _tmp_data_dir(name):
            path = tmp_path / "plugin_data" / name
            path.mkdir(parents=True, exist_ok=True)
            return path

        monkeypatch.setattr(StarTools, "get_data_dir", staticmethod(_tmp_data_dir))
    return tmp_path


@pytest.fixture
async def make_plugin(data_dir):
    """返回工厂 make_plugin(**config) -> ImageTracePlugin；用例结束统一关会话。"""
    from astrbot_plugin_weibo_suite.trace.main import ImageTracePlugin

    created = []

    def factory(**overrides):
        plugin = ImageTracePlugin(_Context(), FakeConfig(overrides))
        created.append(plugin)
        return plugin

    yield factory
    for plugin in created:
        if plugin._http is not None:
            await plugin._http.close()
    # 内网直连白名单是模块级状态（生产由 terminate 清理），测试插件不走
    # terminate，这里显式复位，避免泄漏到其它测试文件
    url_guard.set_allowed_private_hosts(())


def _jpeg_bytes() -> bytes:
    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.effect_noise((64, 64), 32).convert("RGB").save(buffer, format="JPEG")
    return buffer.getvalue()


class MockImgBed:
    """本地 ImgBed 替身的句柄：暴露 base_url 与已收到的 /random 查询参数。"""

    def __init__(self, base_url, state):
        self.base_url = base_url
        self._state = state

    @property
    def random_queries(self):
        return self._state["random_queries"]


@pytest.fixture
def make_weibo_plugin(data_dir):
    """构造真实拆包后的微博监控类；不调用 initialize，避免测试联网。"""
    from astrbot_plugin_weibo_suite.weibo.plugin import WeiboMonitorPlugin

    def factory(**overrides):
        return WeiboMonitorPlugin(_Context(), FakeConfig(overrides))

    return factory


_REAL_RESOLVE = url_guard.resolve_public_addrs


def _loopback_allowlisted_resolve(url, allowed_private_hosts=None):
    """只在“被测假图床所在的那台回环主机”上加白名单，其余一律走真实校验。

    历史上这里把整个 resolve_public_addrs 换成“照常 getaddrinfo、跳过公网 IP
    白名单”的替身，于是所有用到 imgbed fixture 的用例都在**绕过 SSRF 校验**，
    「拒绝私网/环回」的规则在这 40+ 条用例里从未真正执行过。

    现在改为直接调用真实函数，仅当目标主机是回环地址时把该主机并入调用方的
    白名单（这正是生产里 random_media.lan_base_url 内网直连的合法豁免路径），
    因此地址判定、端口校验、私网拒绝全部由真实实现执行。
    """

    host = (urlparse(url).hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        allow = set(allowed_private_hosts or ())
        allow.add(host)
        return _REAL_RESOLVE(url, allow)
    return _REAL_RESOLVE(url, allowed_private_hosts)


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture
async def imgbed(monkeypatch):
    """在本地起一个假 ImgBed：/random 返回 JSON 直链，/file/ 返回真实 JPEG。

    为了让指向本机的请求能过 SSRF 校验，本 fixture 临时把 **回环主机** 并入
    白名单（见 _loopback_allowlisted_resolve）；不是回环主机的地址仍然由真实
    resolve_public_addrs 判定，私网/元数据地址照旧拒绝。
    """

    monkeypatch.setattr(url_guard, "resolve_public_addrs", _loopback_allowlisted_resolve)

    state = {"random_queries": []}
    image_bytes = _jpeg_bytes()

    async def random_handler(request):
        state["random_queries"].append(dict(request.query))
        name = "v.mp4" if request.query.get("content") == "video" else "x.jpg"
        return web.json_response({"url": f"/file/{name}"})

    async def file_handler(_request):
        return web.Response(body=image_bytes, content_type="image/jpeg")

    app = web.Application()
    app.router.add_get("/random", random_handler)
    app.router.add_get("/file/{name}", file_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield MockImgBed(f"http://127.0.0.1:{port}", state)
    finally:
        await runner.cleanup()
