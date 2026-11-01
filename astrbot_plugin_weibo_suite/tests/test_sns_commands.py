"""P8 聊天指令：`订阅` 指令组的分发与文案契约。

指令层是用户唯一能碰到的地方，测的是"说人话能不能走通"：
别名（抖音/dy）要认、缺参数要给用法、不认识的子命令要回用法而不是静默、
写操作成功后要明确告诉用户"基线只记录不推送"。
"""

from __future__ import annotations

import asyncio

import pytest

from astrbot_plugin_weibo_suite.sns import service as sns_service
from astrbot_plugin_weibo_suite.sns.commands import SnsCommandsMixin
from astrbot_plugin_weibo_suite.sns.types import FetchResult, SnsPost


class FakeConfig(dict):
    def save_config(self):
        return None


class FakePlugin(SnsCommandsMixin):
    """指令层只需要 `config` / `_data_dir` / `_sessions` / `deliver_sns_payload`。

    刻意**不**实现 `sns_service()`：插件类上真正的 getter 会惰性构造服务，
    这里的 `self.sns` 就是那份实例，走 `_sns()` 的回落分支即可。
    """

    def __init__(self, tmp_path, config=None):
        self.config = FakeConfig(config or {})
        self._data_dir = str(tmp_path)
        self.delivered = []
        self.sns = sns_service.SnsService(self)

    def _sessions(self):
        return ["aiocqhttp:GroupMessage:123"]

    async def deliver_sns_payload(self, payload, session):
        self.delivered.append((payload, session))
        return "sent"


class FakeResult:
    def __init__(self, text):
        self.text = text


class Event:
    """AstrBot 的 `AstrMessageEvent` 最小替身。

    `is_admin()` 是真实 API（`astrbot.core.platform.astr_message_event` 上确实有），
    根命令组「订阅」没有 ADMIN 装饰器，所以指令层必须自己查——桩里不能省掉它，
    否则越权那条路径在单测里根本走不到。
    """

    def __init__(self, message_str, *, admin=True):
        self.message_str = message_str
        self.origin = "aiocqhttp:GroupMessage:123"
        self.admin = admin

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return FakeResult(text)


def run(agen):
    """把一个 async generator 的所有产出收集成文本列表。"""

    async def collect():
        return [item.text async for item in agen]

    return asyncio.run(collect())


@pytest.fixture
def plugin(tmp_path):
    return FakePlugin(tmp_path)


def test_help_mentions_every_command(plugin):
    text = "\n".join(run(plugin.sns_help(Event("订阅 帮助"))))
    for keyword in (
        "订阅 抖音",
        "订阅 小红书",
        "订阅 IG",
        "订阅 列表",
        "订阅 删除",
        "订阅 检测",
        "订阅 登录",
        "订阅 演练",
    ):
        assert keyword in text
    assert "保号" in text or "风控" in text


def test_group_dispatcher_routes_platform_alias(plugin):
    out = run(plugin.sns_group(Event("订阅 dy https://www.douyin.com/user/MS4wLjABAAAAtest")))
    assert any("已订阅" in line for line in out)
    # 基线只记录不推送这句话必须出现，否则用户会以为丢了消息
    assert any("基线" in line for line in out)
    assert len(plugin.sns.state.accounts) == 1


def test_group_dispatcher_reports_usage_when_target_missing(plugin):
    out = run(plugin.sns_group(Event("订阅 抖音")))
    assert out and "用法" in out[0]
    out2 = run(plugin.sns_group(Event("订阅")))
    assert out2 and "订阅 抖音" in out2[0]


def test_unknown_subcommand_falls_back_to_usage(plugin):
    out = run(plugin.sns_group(Event("订阅 胡说八道")))
    assert "不认识的用法" in out[0]
    assert "订阅 抖音" in out[0]


def test_add_rejects_unrecognizable_input(plugin):
    out = run(plugin.sns_group(Event("订阅 抖音 随便一句话")))
    assert "没能" in "".join(out)
    assert plugin.sns.state.accounts == {}


def test_rss_subscription(plugin, monkeypatch):
    # 真实 DNS 是外部依赖：离线/内网 CI 会解析失败，那不是被测逻辑的问题。
    # 只桩掉"解析结果"，SSRF 判定本身仍走 add_feed -> _feed_url_error 真代码。
    from astrbot_plugin_weibo_suite import shared
    from astrbot_plugin_weibo_suite.shared import http as shared_http

    monkeypatch.setattr(shared_http, "resolve_public_addrs", lambda url, allowed=None: (url, []))
    assert shared.http is shared_http  # 别名存在，patch 点就是 add_feed 里 import 的那个模块
    out = run(plugin.sns_group(Event("订阅 rss https://example.com/feed.xml 我的源")))
    assert any("RSS" in line for line in out)
    account = next(iter(plugin.sns.state.accounts.values()))
    assert account.platform == "rss"
    assert account.name == "我的源"


def test_remove_by_link(plugin):
    """`订阅 删除 ...` 由 main.py 的子命令处理，这里直接测它的实现。"""
    run(plugin.sns_group(Event("订阅 抖音 https://www.douyin.com/user/MS4wLjABAAAAtest")))
    out = run(
        plugin.sns_remove(Event("订阅 删除 抖音 https://www.douyin.com/user/MS4wLjABAAAAtest"))
    )
    assert any("已取消" in line for line in out)
    assert plugin.sns.state.accounts == {}
    out2 = run(
        plugin.sns_remove(Event("订阅 删除 抖音 https://www.douyin.com/user/MS4wLjABAAAAtest"))
    )
    assert any("没有这一项" in line for line in out2)


def test_list_and_status(plugin):
    assert any("还没有订阅" in line for line in run(plugin.sns_list(Event("订阅 列表"))))
    run(plugin.sns_group(Event("订阅 抖音 https://www.douyin.com/user/MS4wLjABAAAAtest")))
    listed = "\n".join(run(plugin.sns_list(Event("订阅 列表"))))
    assert "共 1 个订阅" in listed
    assert "待建基线" in listed
    status = "\n".join(run(plugin.sns_status(Event("订阅 状态"))))
    assert "抖音" in status


def test_check_without_subscription_tells_user_what_to_do(plugin):
    out = run(plugin.sns_check(Event("订阅 检测")))
    assert any("还没有任何订阅" in line for line in out)


def test_check_reports_baseline_and_new_posts(plugin, monkeypatch):
    run(plugin.sns_group(Event("订阅 抖音 https://www.douyin.com/user/MS4wLjABAAAAtest")))
    account = next(iter(plugin.sns.state.accounts.values()))

    async def fake_fetch(_account):
        return FetchResult.success(
            [SnsPost(platform="douyin", account_id=account.account_id, post_id="1")]
        )

    monkeypatch.setattr(plugin.sns, "_fetch", fake_fetch)
    first = "\n".join(run(plugin.sns_check(Event("订阅 检测"))))
    assert "基线" in first

    async def fake_fetch_two(_account):
        return FetchResult.success(
            [
                SnsPost(platform="douyin", account_id=account.account_id, post_id="1"),
                SnsPost(
                    platform="douyin",
                    account_id=account.account_id,
                    post_id="2",
                    text="新内容",
                ),
            ]
        )

    monkeypatch.setattr(plugin.sns, "_fetch", fake_fetch_two)
    second = "\n".join(run(plugin.sns_check(Event("订阅 检测 抖音"))))
    assert "新增 1 条" in second
    assert len(plugin.delivered) == 1


def test_check_reports_risk_signal_instead_of_silence(plugin, monkeypatch):
    run(plugin.sns_group(Event("订阅 抖音 https://www.douyin.com/user/MS4wLjABAAAAtest")))
    account = next(iter(plugin.sns.state.accounts.values()))
    account.baseline_done = True

    async def fake_fetch(_account):
        return FetchResult.failure("douyin.empty_silent", "空响应")

    monkeypatch.setattr(plugin.sns, "_fetch", fake_fetch)
    out = "\n".join(run(plugin.sns_check(Event("订阅 检测"))))
    assert "空" in out or "风控" in out


def test_login_without_platform_explains_persistent_window(plugin):
    out = "\n".join(run(plugin.sns_login(Event("订阅 登录"))))
    assert "持久化" in out
    assert "Cookie 留在浏览器" in out


def test_login_reports_browser_unavailable_gracefully(plugin, monkeypatch):
    async def boom(_platform):
        raise sns_service.BrowserUnavailable("没有可用浏览器")

    monkeypatch.setattr(plugin.sns, "open_login", boom)
    out = run(plugin.sns_login(Event("订阅登录 抖音")))
    assert out and "没有可用浏览器" in out[0]


def test_drill_prints_posts_and_never_pushes(plugin, monkeypatch):
    run(plugin.sns_group(Event("订阅 抖音 https://www.douyin.com/user/MS4wLjABAAAAtest")))
    account = next(iter(plugin.sns.state.accounts.values()))
    account.baseline_done = True

    async def fake_fetch(_account):
        return FetchResult.success(
            [
                SnsPost(
                    platform="douyin", account_id=account.account_id, post_id="1", title="第一条"
                ),
                SnsPost(
                    platform="douyin", account_id=account.account_id, post_id="2", title="第二条"
                ),
            ],
            nickname="示例抖音博主",
        )

    monkeypatch.setattr(plugin.sns, "_fetch", fake_fetch)
    out = "\n".join(run(plugin.sns_drill(Event("订阅 演练 抖音"))))
    assert "演练通过" in out
    assert "第一条" in out
    assert "不推送" in out
    assert plugin.delivered == []


def test_drill_reports_failure_reason(plugin, monkeypatch):
    run(plugin.sns_group(Event("订阅 抖音 https://www.douyin.com/user/MS4wLjABAAAAtest")))

    async def fake_fetch(_account):
        return FetchResult.failure("xhs.verify_box", "小红书风控校验")

    monkeypatch.setattr(plugin.sns, "_fetch", fake_fetch)
    out = "\n".join(run(plugin.sns_drill(Event("订阅 演练"))))
    assert "演练失败" in out
    assert "风控" in out or "校验" in out


# ------------------------------------------------- 越权与"服务未就绪"回归


@pytest.mark.parametrize(
    "text",
    [
        "订阅 抖音 https://www.douyin.com/user/MS4wLjABAAAAtest",
        "订阅 rss https://example.com/feed.xml",
    ],
)
def test_non_admin_cannot_add_subscription(plugin, monkeypatch, text):
    """P1-2 回归：根命令组「订阅」没有 ADMIN 装饰器，普通群成员能直接走到这里。

    修复前这段代码只做参数解析就落库，等于任何群友都能替整个群加订阅（还能
    借 RSS 分支让插件去请求任意 http(s) 地址 = SSRF）。现在必须先过 is_admin。
    """
    from astrbot_plugin_weibo_suite.shared import http as shared_http

    monkeypatch.setattr(shared_http, "resolve_public_addrs", lambda url, allowed=None: (url, []))
    out = run(plugin.sns_group(Event(text, admin=False)))
    assert out and "仅管理员" in out[0]
    assert plugin.sns.state.accounts == {}
    assert plugin.delivered == []


def test_non_admin_still_sees_usage_text(plugin):
    """缺参数是"用法提示"，不是写操作，不该被权限拦成"仅管理员"。"""
    out = run(plugin.sns_group(Event("订阅 抖音", admin=False)))
    assert out and "用法" in out[0]
    # 只敲「订阅」= 缺全部参数，回完整用法；同样不该被拦成「仅管理员」
    bare = run(plugin.sns_group(Event("订阅", admin=False)))
    assert bare and "多平台订阅" in bare[0]
    # 但真正要写库的路径必须拒绝
    denied = run(
        plugin.sns_group(Event("订阅 抖音 https://www.douyin.com/user/MS4wLjAB", admin=False))
    )
    assert denied and "仅管理员" in denied[0]
    assert plugin.sns.state.accounts == {}


@pytest.mark.parametrize(
    "text",
    [
        "订阅 删除 抖音 https://www.douyin.com/user/MS4wLjABAAAAtest",
        "订阅 检测",
        "订阅 登录 抖音",
        "订阅 演练",
        "订阅 列表",
        "订阅 状态",
        "订阅 帮助",
    ],
)
def test_root_handler_silently_delegates_subcommands(plugin, text):
    """P1-2 回归：子命令一律交给 main.py 里带 ADMIN 的实现。

    修复前 `sns_group` 里有一张 dict 影子分派表，把删除/检测/登录/演练又实现
    了一遍且不带权限检查——main.py 上那排 @ADMIN 形同虚设。现在根 handler 必须
    对它们零产出、零副作用。
    """
    run(plugin.sns_group(Event("订阅 抖音 https://www.douyin.com/user/MS4wLjABAAAAtest")))
    before = set(plugin.sns.state.accounts)
    assert run(plugin.sns_group(Event(text, admin=True))) == []
    assert set(plugin.sns.state.accounts) == before, "子命令在根 handler 里产生了副作用"


def test_commands_degrade_when_service_missing(plugin):
    """P2-4 回归：`sns.enabled=false` 时服务没构造，指令不能抛 AttributeError。"""
    plugin.sns = None
    cases = [
        (plugin.sns_list, "订阅 列表"),
        (plugin.sns_status, "订阅 状态"),
        (plugin.sns_check, "订阅 检测"),
        (plugin.sns_drill, "订阅 演练"),
        (plugin.sns_remove, "订阅 删除 抖音 x"),
        (plugin.sns_login, "订阅 登录 抖音"),
    ]
    for handler, text in cases:
        out = run(handler(Event(text)))
        assert out and "未就绪" in out[0], f"{handler.__name__} 没给出可读的降级提示：{out}"


def test_usage_text_covers_platform_aliases():
    from astrbot_plugin_weibo_suite.sns.commands import USAGE

    assert "订阅 抖音" in USAGE
    assert "IG" in USAGE
    assert "保号" in USAGE  # 用法里必须带上保号提示，否则用户不知道有守卫
