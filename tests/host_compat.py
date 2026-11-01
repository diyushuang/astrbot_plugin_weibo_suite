"""真宿主（真 AstrBot）与测试桩之间的取数兼容层。

背景：`tests/conftest.py` 默认注入一套最小 astrbot 桩，让没装 astrbot 的机器也能
跑全部用例。桩是**替身**，它在三处比真包"宽容"，用例于是悄悄长在了桩的语义上：

1. `_MessageChain` 实现了 `__iter__`，而真 AstrBot 的 `MessageChain`
   （astrbot/core/message/message_event_result.py）是 dataclass，只有 `chain: list`
   字段，**不可迭代**。生产代码自己也只写 `chain.chain`（见 weibo/pusher.py:1835）。
2. `_Image.fromURL(u)` 把地址存进 `.url`，真 `Image.fromURL` 存进 `.file`
   （`.url` 留空），真包自己的取值口径就是 `self.url or self.file`
   （components.py 的 `convert_to_file_path`）。
3. 桩的 `filter.command` 装饰器把指令名写回函数属性 `command_name`；真 AstrBot 4.29
   的装饰器原样返回函数，指令名只存在于 `CommandFilter` 实例上
   （astrbot/core/star/filter/command.py），通过 `star_handlers_registry` 里的
   `StarHandlerMetadata.event_filters` 才取得到。

把 `WEIBO_SUITE_REAL_ASTRBOT=1` 打开后（CI 的 real-astrbot job 就是这么跑的），
上述差异会让这些用例变红。下面的三个取数函数把"从消息链/图片组件/指令处理器里
取数"这件事统一成**两种模式下同一套写法**，用例因此不再依赖桩的特有语义。

刻意不做的事：不把桩改得和真包一模一样。桩的存在意义是让没装 astrbot 的机器能跑
测试，改桩会牵连大量与之无关的用例；而且 `Image.fromURL` 之类的差异属于真包内部
实现细节，用例不该锁死在某一种字段布局上——断言"组件携带的地址是哪一个"才是
用例真正关心的事。
"""

from __future__ import annotations

__all__ = [
    "chain_segments",
    "command_alias_of",
    "command_name_of",
    "image_ref",
    "real_host",
]


def real_host() -> bool:
    """当前是否跑在真 AstrBot 上（conftest 的门是否打开且真包可导入）。"""
    from . import conftest

    return bool(getattr(conftest, "_REAL_HOST", False))


def chain_segments(chain) -> list:
    """把一条消息链拆成组件列表。

    真 `MessageChain` 只能通过 `.chain` 取组件（它没有 `__iter__`），桩两种都行。
    """
    inner = getattr(chain, "chain", None)
    if inner is not None:
        return list(inner)
    return list(chain)


def image_ref(seg) -> str:
    """取图片组件携带的地址/引用。

    真 AstrBot 的 `fromURL` 存在 `.file`、`fromFileSystem` 存在 `.file`+`.path`，
    桩存在 `.url`/`.path`；真包内部统一按 `url or file` 取值，这里照抄该口径。
    """
    for attr in ("url", "file", "path"):
        value = getattr(seg, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def command_name_of(func) -> str | None:
    """取某个处理器函数注册的指令名。

    桩模式下装饰器直接写在函数属性上；真宿主下要去 `star_handlers_registry` 里
    找到 `handler is func` 的那条元数据，再从它的 `event_filters` 里取 `CommandFilter`。
    """
    direct = getattr(func, "command_name", None)
    if isinstance(direct, str) and direct:
        return direct

    try:
        from astrbot.core.star.star_handler import star_handlers_registry
    except Exception:  # 桩模式下没有这个模块
        return None

    for handler_md in star_handlers_registry:
        if getattr(handler_md, "handler", None) is not func:
            continue
        for event_filter in getattr(handler_md, "event_filters", ()) or ():
            name = getattr(event_filter, "command_name", None)
            if isinstance(name, str) and name:
                return name
    return None


def command_alias_of(func) -> set[str]:
    """取某个处理器函数注册的指令别名集合。

    与 `command_name_of` 同源：桩把别名写回 `func.command_alias`（conftest 里两处
    装饰器替身），真 AstrBot 4.29 存成 `CommandFilter.alias`
    （astrbot/core/star/filter/command.py:44），函数上什么都没有。没有别名时返回
    空集合——真包内部同样用 `alias if alias else set()` 归一成空集合，所以
    断言 `== set()` 与 `== {""}` 不会互相混淆。
    """
    direct = getattr(func, "command_alias", None)
    if isinstance(direct, (set, frozenset)) and direct:
        return set(direct)

    try:
        from astrbot.core.star.star_handler import star_handlers_registry
    except Exception:  # 桩模式下没有这个模块
        return set()

    for handler_md in star_handlers_registry:
        if getattr(handler_md, "handler", None) is not func:
            continue
        for event_filter in getattr(handler_md, "event_filters", ()) or ():
            alias = getattr(event_filter, "alias", None)
            if isinstance(alias, (set, frozenset)):
                return set(alias)
    return set()
