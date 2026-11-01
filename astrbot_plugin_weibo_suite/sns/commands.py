"""「订阅」指令族的业务实现（真正的 `@filter` 装饰器只在 `main.py` 里）。

拆开的原因和微博域一致：AstrBot 按 `handler.__module__ == 插件模块路径` 绑定
处理器，把装饰器写进子模块会让处理器"消失"。这里只提供方法，由 `main.py` 的
薄壳逐条转发。

指令统一在群聊里用，写操作限管理员；读操作（列表/状态）谁都能看——
让群友能看到"到底订了什么"，比藏起来更少误会。

**权限必须在这一层再确认一遍**：`main.py` 的根命令组 `订阅` 没有 ADMIN 装饰器
（它还要接「订阅 抖音 <链接>」这种带空格的写法），所以凡是会改状态的路径都得
自己查一次 `event.is_admin()`，否则普通群成员就能替整个群加订阅（P1-2）。
"""

from __future__ import annotations

import inspect
import logging
import time

from . import constants as C
from .browser import BrowserUnavailable

logger = logging.getLogger("astrbot_plugin_weibo_suite.sns")

# 由 `main.py` 里真正的子命令处理（带 ADMIN 装饰器）。根 handler 遇到它们
# 必须静默返回：在这里再实现一遍就等于绕过了那些 ADMIN 检查。
_DELEGATED_HEADS = frozenset(
    {
        "列表",
        "删除",
        "检测",
        "登录",
        "状态",
        "演练",
        "帮助",
        "list",
        "remove",
        "check",
        "login",
        "status",
        "drill",
        "help",
    }
)

_SERVICE_OFF = "多平台订阅服务未就绪（检查 sns.enabled 与插件数据目录）"


async def _is_admin(event) -> bool:
    """同步/异步两种 `event.is_admin()` 都能判；拿不到就按"非管理员"处理。"""
    checker = getattr(event, "is_admin", None)
    if not callable(checker):
        return False
    try:
        result = checker()
    except Exception:
        return False
    if inspect.isawaitable(result):
        try:
            return bool(await result)
        except Exception:
            return False
    return bool(result)


USAGE = """📡 多平台订阅（抖音 / 小红书 / Instagram）
订阅 抖音 <主页链接或 sec_uid>     订阅抖音博主
订阅 小红书 <主页链接或 24 位 id>  订阅小红书博主
订阅 IG <用户名或主页链接>         订阅 Instagram 博主
订阅 列表                          查看全部订阅
订阅 删除 <平台> <链接或 id>       取消订阅（管理员）
订阅 检测 [平台]                   立刻检查一次（管理员）
订阅 状态                          看登录态、健康度、冷却与预估延迟
订阅 演练 [平台]                   取一次数但不推送（试通不通）
订阅 登录 <平台>                   打开浏览器窗口让你手动登录
订阅 RSS <地址> [备注]             订阅任意 RSS 源（备选通道）

保号提示：默认走持久化浏览器取数，Cookie 不出浏览器；平台风控命中后会自动
冷却并暂停，不会自动重登、也不会替你重试。"""


def _norm_platform(text: str) -> str:
    raw = str(text or "").strip().lower()
    return C.PLATFORM_ALIAS.get(raw, "")


def _tail(event, *names: str) -> list[str]:
    """剥掉指令组与子命令本身，只留用户真正给的参数。

    AstrBot 的命令组有两种写法：`订阅 登录 抖音`（空格分开）和
    `订阅登录 抖音`（粘在一起）。两种都必须解析到同样的参数，
    否则 `订阅 删除 抖音 xxx` 会被当成「平台叫删除」。
    """
    parts = (event.message_str or "").strip().split()
    if parts and parts[0].startswith("订阅"):
        glued = parts[0][2:]
        parts = ([glued] if glued else []) + parts[1:]
    lowered = {str(n).lower() for n in names}
    if parts and parts[0].lower() in lowered:
        parts = parts[1:]
    return parts


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "—"
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "—"


def _fmt_ago(ts: float) -> str:
    if not ts:
        return "从未"
    delta = max(0.0, time.time() - float(ts))
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{delta / 3600:.1f} 小时前"
    return f"{delta / 86400:.1f} 天前"


class SnsCommandsMixin:
    """混入插件类；依赖 `self.sns`（`SnsService`）。"""

    def _sns(self):
        """取订阅服务；拿不到就返回 None，绝不让 AttributeError 冒到群里。

        `sns.enabled=false` 时插件不会在启动阶段构造服务，`self.sns` 就是 None；
        而"关闭轮询但仍想管理订阅"是完全合理的用法，所以这里走惰性 getter
        （`sns_service()`），它会构造实例并把状态读进来。
        """
        getter = getattr(self, "sns_service", None)
        if callable(getter):
            try:
                service = getter()
            except Exception as exc:
                logger.warning(f"[sns] 构造订阅服务失败：{exc.__class__.__name__}: {exc}")
                service = None
            if service is not None:
                return service
        return getattr(self, "sns", None)

    # ------------------------------------------------------------ 入口

    async def sns_help(self, event):
        """查看多平台订阅指令用法"""
        yield event.plain_result(USAGE)

    async def sns_group(self, event):
        """`订阅 <平台> <链接>` 这种"带空格"的写法（只负责新增订阅）。

        子命令一律交给 `main.py` 里带 ADMIN 装饰器的实现；本处理器只有
        "新增订阅"这一条写路径，且**自己再查一次管理员**。
        """
        parts = (event.message_str or "").strip().split()
        args = parts[1:] if len(parts) > 1 else []
        if not args:
            yield event.plain_result(USAGE)
            return
        head = args[0]
        if head in _DELEGATED_HEADS or head.lower() in _DELEGATED_HEADS:
            # 静默返回：交给 main.py 的真子命令。这里若"顺手实现一下"，
            # 就等于给删除/检测/登录/演练开了一个不需要管理员的后门。
            return
        platform = _norm_platform(head)
        is_feed = head.lower() in ("rss", "订阅源")
        if not platform and not is_feed:
            yield event.plain_result(f"不认识的用法：{head}\n\n{USAGE}")
            return
        # 先做参数校验再查权限：缺参数纯属"不会用"，回用法比回"仅管理员"有用，
        # 也不泄露任何信息（下面两个分支都不碰存储）。
        target = " ".join(args[1:]).strip() if platform else ""
        if platform and not target:
            label = C.PLATFORM_LABEL.get(platform, platform)
            yield event.plain_result(f"用法：订阅 {head} <{label}主页链接或账号 id>")
            return
        if is_feed and not args[1:]:
            yield event.plain_result("用法：订阅 RSS <地址> [备注]")
            return
        if not await _is_admin(event):
            yield event.plain_result("仅管理员可以添加订阅")
            return
        service = self._sns()
        if service is None:
            yield event.plain_result(_SERVICE_OFF)
            return
        if platform:
            async for item in self._sns_add(event, platform, target):
                yield item
            return
        rest = args[1:]
        # 地址只取第一个 token：整串塞进 url 会让备注变成地址的一部分
        url = rest[0]
        name = " ".join(rest[1:]).strip()
        async for item in self._sns_add_feed(event, url, name):
            yield item

    # ------------------------------------------------------------ 写操作

    async def sns_add(self, event):
        """订阅一个博主（`订阅抖音 <链接>`）"""
        args = _tail(
            event,
            "抖音",
            "douyin",
            "dy",
            "小红书",
            "xhs",
            "红书",
            "ig",
            "ins",
            "instagram",
        )
        if len(args) < 2:
            yield event.plain_result("用法：订阅抖音 <主页链接或 sec_uid>")
            return
        platform = _norm_platform(args[0]) or "douyin"
        async for item in self._sns_add(event, platform, " ".join(args[1:])):
            yield item

    async def _sns_add(self, event, platform: str, target: str):
        service = self._sns()
        if service is None:
            yield event.plain_result(_SERVICE_OFF)
            return
        ok, message, _account = service.add_account(platform, target)
        if ok:
            platform_label = C.PLATFORM_LABEL.get(platform, platform)
            yield event.plain_result(
                f"{message}\n"
                f"已排队做一次基线抓取"
                f"（{platform_label}节奏约 {service.base_interval(platform)}s 一轮），"
                f"基线只记录不推送；之后的新内容才会进群。"
            )
        else:
            yield event.plain_result(message)

    async def sns_rss(self, event):
        """订阅任意 RSS 源（备选通道）"""
        args = _tail(event, "rss", "订阅源")
        if not args:
            yield event.plain_result("用法：订阅 RSS <地址> [备注]")
            return
        async for item in self._sns_add_feed(event, args[0], " ".join(args[1:])):
            yield item

    async def _sns_add_feed(self, event, url: str, name: str):
        service = self._sns()
        if service is None:
            yield event.plain_result(_SERVICE_OFF)
            return
        # 成功与否 add_feed 都已经给了能直接读的一句话（含 SSRF 拒绝的原因）
        _ok, message, _account = await service.add_feed(url, name)
        yield event.plain_result(message)

    async def sns_remove(self, event):
        """取消订阅（管理员）"""
        args = _tail(event, "删除")
        if len(args) < 2:
            yield event.plain_result(
                "用法：订阅 删除 <平台> <链接或 id>（或 订阅删除 <平台> <id>）"
            )
            return
        platform = _norm_platform(args[0])
        if not platform:
            yield event.plain_result(f"不认识的平台：{args[0]}")
            return
        service = self._sns()
        if service is None:
            yield event.plain_result(_SERVICE_OFF)
            return
        _ok, message = service.remove_account(platform, " ".join(args[1:]))
        yield event.plain_result(message)

    async def sns_check(self, event):
        """立刻检查一次（管理员）"""
        args = _tail(event, "检测")
        platform = _norm_platform(args[0]) if args else ""
        if args and not platform:
            yield event.plain_result(f"不认识的平台：{args[0]}")
            return
        service = self._sns()
        if service is None:
            yield event.plain_result(_SERVICE_OFF)
            return
        if not service.state.accounts:
            yield event.plain_result("还没有任何订阅，先「订阅 抖音 <链接>」加一个")
            return
        result = await service.check_all(platform, force=True)
        if not result.get("ok"):
            yield event.plain_result(f"检查未执行：{result.get('error')}")
            return
        failed = int(result.get("failed") or 0)
        lines = [
            f"检查完成：本轮 {result.get('checked', 0)} 个订阅，推送 {result.get('pushed', 0)} 条"
            + (f"，{failed} 个失败" if failed else "")
        ]
        if result.get("note"):
            lines.append(str(result["note"]))
        for row in result.get("detail") or []:
            if not isinstance(row, dict):
                continue
            if row.get("baseline"):
                lines.append(f"· {row['key']}：已建立基线（不推送历史）")
            elif row.get("ok"):
                lines.append(
                    f"· {row['key']}：新增 {row.get('new', 0)} 条"
                    + (f"，已推 {row.get('sent', 0)} 条" if row.get("sent") else "")
                )
            else:
                lines.append(f"· {row['key']}：{row.get('label') or row.get('signal')}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 读操作

    async def sns_list(self, event):
        """查看全部订阅"""
        service = self._sns()
        if service is None:
            yield event.plain_result(_SERVICE_OFF)
            return
        accounts = sorted(service.state.accounts.values(), key=lambda a: (a.platform, a.label))
        if not accounts:
            yield event.plain_result("还没有订阅。用「订阅 抖音 <链接>」开始。")
            return
        lines = [f"共 {len(accounts)} 个订阅："]
        for account in accounts:
            label = C.PLATFORM_LABEL.get(account.platform, account.platform)
            state = []
            if account.paused:
                state.append("已暂停")
            elif account.cooling():
                state.append("冷却中")
            if not account.baseline_done:
                state.append("待建基线")
            suffix = f"（{'、'.join(state)}）" if state else ""
            lines.append(
                f"· [{label}] {account.label}{suffix}\n"
                f"  上次检查 {_fmt_ago(account.last_check_ts)} · "
                f"下次 {_fmt_ts(account.next_check_ts)} · 已推 {account.push_ok} 条"
            )
        yield event.plain_result("\n".join(lines))

    async def sns_status(self, event):
        """看登录态、健康度、冷却与预估延迟"""
        service = self._sns()
        if service is None:
            yield event.plain_result(_SERVICE_OFF)
            return
        yield event.plain_result(service.status_text())

    async def sns_login(self, event):
        """打开浏览器窗口让你手动登录（管理员）"""
        args = _tail(event, "登录")
        if not args:
            yield event.plain_result(
                "用法：订阅 登录 <抖音|小红书|IG>\n"
                "打开的是持久化窗口，登录一次以后都不用再登；Cookie 留在浏览器里。"
            )
            return
        platform = _norm_platform(args[0])
        if not platform:
            yield event.plain_result(f"不认识的平台：{args[0]}")
            return
        service = self._sns()
        if service is None:
            yield event.plain_result(_SERVICE_OFF)
            return
        try:
            ok, message = await service.open_login(platform)
        except BrowserUnavailable as exc:
            yield event.plain_result(f"打开登录窗口失败：{exc}")
            return
        if ok:
            # 窗口刚打开 ≠ 用户已登录：这里不能再替用户"宣布"登录成功
            # （否则状态里会显示已登录，下一轮才发现 cookie 根本用不了）。
            yield event.plain_result(
                f"{message}\n请在弹出的窗口里完成登录（扫码/账号密码均可），"
                f"登录后回到群里发「订阅 演练 {args[0]}」验证是否连通。"
            )
        else:
            yield event.plain_result(message)

    async def sns_drill(self, event):
        """取一次数但不推送（验证登录态是否可用）"""
        args = _tail(event, "演练")
        platform = _norm_platform(args[0]) if args else ""
        service = self._sns()
        if service is None:
            yield event.plain_result(_SERVICE_OFF)
            return
        result = await service.drill(platform)
        if not result.get("ok"):
            yield event.plain_result(
                f"演练失败：{result.get('error') or result.get('label') or result.get('signal')}"
            )
            return
        lines = [
            f"演练通过：{result['name']}"
            f"（{C.PLATFORM_LABEL.get(result['platform'], result['platform'])}）",
            f"拿到 {result['posts']} 条，耗时 {result['elapsed']}s",
        ]
        if result.get("nickname"):
            lines.append(f"昵称：{result['nickname']}")
        for title in result.get("preview") or []:
            lines.append(f"· {title}")
        lines.append("（演练不推送、不记已读、不影响冷却）")
        yield event.plain_result("\n".join(lines))
