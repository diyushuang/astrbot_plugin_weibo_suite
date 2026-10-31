"""「微博」23 条管理指令。"""

from __future__ import annotations

from .runtime import *

# 归档查询每页条数（archive_query 的 limit 默认也是 10，这里显式固化分页口径）
ARCHIVE_QUERY_PAGE_SIZE = 10


def _cmd_args(event: AstrMessageEvent) -> list[str]:
    """返回指令名之后的参数词（与框架一致用空白正则分词）。"""
    tokens = re.split(r"\s+", (event.message_str or "").strip())
    return tokens[1:] if len(tokens) > 1 else []


def _parse_backfill_args(args: list[str], uid_text: str) -> tuple[str, str, bool]:
    """解析 微博回填 的 <uid> [--since 日期] [--full]。"""
    uid = _extract_uid(uid_text)
    since = ""
    full = False
    for idx, tok in enumerate(args):
        if tok in {"--full", "-f"}:
            full = True
        elif tok.startswith("--since="):
            since = tok.split("=", 1)[1].strip()
        elif tok == "--since" and idx + 1 < len(args):
            since = args[idx + 1].strip()
    if not uid:
        for tok in args:
            if tok.startswith("-"):
                continue
            uid = _extract_uid(tok)
            if uid:
                break
    return uid or "", since, full


def _import_path(event: AstrMessageEvent, path_text: str) -> str:
    """取 微博归档导入 的剩余文本（路径可能含空格，不能只取第一个 token）。"""
    raw = (event.message_str or "").strip()
    tokens = re.split(r"\s+", raw)
    path = raw[len(tokens[0]) :].strip() if len(tokens) > 1 else path_text.strip()
    return path.strip().strip('"').strip("'")


class CommandsMixin:
    @filter.command_group("微博")
    async def weibo(self, event: AstrMessageEvent):
        """微博监控转发指令组"""
        yield event.plain_result(USAGE)

    @weibo.command("帮助", alias={"help", "用法"})
    async def weibo_help(self, event: AstrMessageEvent):
        """查看微博监控指令用法"""
        yield event.plain_result(USAGE)

    @weibo.command("绑定")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_bind(self, event: AstrMessageEvent):
        """把当前群聊绑定为微博推送目标（管理员）"""
        if not event.get_group_id():
            yield event.plain_result("请在要接收推送的群聊中使用该指令")
            return
        umo = event.unified_msg_origin
        sessions = self._sessions()
        if umo in sessions:
            yield event.plain_result("本群已在推送列表中，无需重复绑定")
            return
        sessions.append(umo)
        self.config["push_sessions"] = sessions
        self._save_config()
        logger.info(f"{PLUGIN_NAME} 新增推送目标: {umo}")
        yield event.plain_result("绑定成功，本群将接收微博更新推送")

    @weibo.command("解绑")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_unbind(self, event: AstrMessageEvent):
        """解除当前群聊的微博推送（管理员）"""
        umo = event.unified_msg_origin
        sessions = self._sessions()
        if umo not in sessions:
            yield event.plain_result("本群尚未绑定推送")
            return
        sessions.remove(umo)
        self.config["push_sessions"] = sessions
        self._save_config()
        logger.info(f"{PLUGIN_NAME} 移除推送目标: {umo}")
        yield event.plain_result("解绑成功，本群不再接收推送")

    @weibo.command("添加")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_add(self, event: AstrMessageEvent, uid_text: str = ""):
        """添加监控的微博账号，参数为 uid 或主页链接（管理员）"""
        uid = _extract_uid(uid_text)
        if not uid:
            yield event.plain_result(
                "用法：微博添加 <uid或主页链接>\n"
                "例如：微博添加 1234567890 或 微博添加 https://weibo.com/u/1234567890"
            )
            return
        self._sync_accounts_from_config()
        if uid in self.accounts:
            yield event.plain_result(f"账号 {uid} 已在监控列表中")
            return
        yield event.plain_result("正在验证账号…")
        nickname = await self._fetch_nickname(uid)
        self.accounts[uid] = {
            "name": nickname or uid,
            "seen_ids": [],
            "baseline_done": False,
            "fail_count": 0,
            "next_retry_ts": 0.0,
        }
        self.config["monitored_uids"] = list(self.accounts.keys())
        self._save_config()
        self._save_state()
        name = self.accounts[uid]["name"]
        extra = "" if nickname else "（未能获取昵称，下轮检查会自动补齐，请留意确认 uid 正确）"
        yield event.plain_result(
            f"已添加监控：{name}（{uid}）{extra}\n"
            "下一轮检查将建立基线，历史微博不会推送，仅推送之后的新微博。"
        )

    @weibo.command("删除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_remove(self, event: AstrMessageEvent, uid_text: str = ""):
        """取消监控某个微博账号（管理员）"""
        uid = _extract_uid(uid_text)
        self._sync_accounts_from_config()
        if not uid or uid not in self.accounts:
            yield event.plain_result(f"账号 {uid or '(空)'} 不在监控列表中，请用 微博列表 查看")
            return
        name = self.accounts.pop(uid)["name"]
        self.config["monitored_uids"] = list(self.accounts.keys())
        self._save_config()
        self._save_state()
        yield event.plain_result(f"已取消监控：{name}（{uid}）")

    @weibo.command("列表")
    async def weibo_list(self, event: AstrMessageEvent):
        """查看当前监控的微博账号"""
        self._sync_accounts_from_config()
        if not self.accounts:
            yield event.plain_result("尚未监控任何账号，管理员可用 微博添加 <uid> 添加")
            return
        lines = ["当前监控的微博账号："]
        for idx, (uid, info) in enumerate(self.accounts.items(), 1):
            lines.append(f"{idx}. {info['name']}（{uid}）")
        sessions = self._sessions()
        lines.append(f"推送目标：{len(sessions)} 个（群内发送 微博绑定 添加）")
        rules = self._album_rules()
        lines.append(f"相册绑定：{len(rules)} 条（微博相册列表 查看）")
        kw_rules = self._album_keyword_rules()
        kw_b = sum(1 for u, *_ in kw_rules if u is not None)
        lines.append(
            f"关键词路由：博主 {kw_b} 条 / 全局 {len(kw_rules) - kw_b} 条"
            + ("" if kw_rules else "（未配置时按默认相册绑定上传）")
        )
        yield event.plain_result("\n".join(lines))

    @weibo.command("相册绑定")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_album_bind(self, event: AstrMessageEvent, uid_text: str = ""):
        """把某博主的微博图片自动上传到本群指定相册（管理员）。

        用法：微博相册绑定 <uid或链接> <相册名或ID>
        """
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("请在目标群聊中使用该指令（图片将上传到本群相册）")
            return
        uid = _extract_uid(uid_text)
        # 相册名可能含空格。GreedyStr 只存在于框架内部模块（astrbot.core）、未纳入
        # astrbot.api 公开接口，这里按框架同一套分词规则（re.split(r"\s+")）手动取
        # uid 之后的全部剩余参数，语义与 GreedyStr 的 " ".join(剩余参数) 一致
        album_name = ""
        if uid_text:
            tokens = re.split(r"\s+", (event.message_str or "").strip())
            with contextlib.suppress(ValueError):
                album_name = " ".join(tokens[tokens.index(uid_text) + 1 :])
        if not uid or not album_name:
            yield event.plain_result(
                "用法：微博相册绑定 <uid或主页链接> <相册名或ID>\n"
                "例如：微博相册绑定 1234567890 美食图集\n"
                "相册可用 微博列相册 查看名称与 ID"
            )
            return
        self._sync_accounts_from_config()
        if uid not in self.accounts:
            yield event.plain_result(
                f"账号 {uid} 不在监控列表中，请先用 微博添加 添加，或用 微博列表 查看"
            )
            return
        rules = self._album_rules()
        rules[(uid, str(gid))] = album_name
        self.config["album_rules"] = [album_rule_token(u, g, a) for (u, g), a in rules.items()]
        self._save_config()
        name = self.accounts[uid]["name"]
        tip = (
            ""
            if event.unified_msg_origin in self._sessions()
            else "\n注意：本群尚未 微博绑定，推送不成功不会触发图片上传"
        )
        logger.info(f"{PLUGIN_NAME} 相册绑定: {uid} -> 群 {gid} 相册「{album_name}」")
        yield event.plain_result(
            f"已绑定：{name}（{uid}）的微博图片将自动上传到本群相册「{album_name}」{tip}"
        )

    @weibo.command("相册移除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_album_unbind(self, event: AstrMessageEvent, uid_text: str = ""):
        """解除博主的相册绑定（管理员）：群内使用解除本群，私聊使用解除全部"""
        uid = _extract_uid(uid_text)
        if not uid:
            yield event.plain_result("用法：微博相册移除 <uid>")
            return
        gid = event.get_group_id()
        rules = self._album_rules()
        targets = [(u, g) for (u, g) in rules if u == uid and (not gid or g == str(gid))]
        if not targets:
            yield event.plain_result(f"账号 {uid} 没有相册绑定记录（微博相册列表 查看）")
            return
        for key in targets:
            rules.pop(key, None)
        self.config["album_rules"] = [album_rule_token(u, g, a) for (u, g), a in rules.items()]
        self._save_config()
        logger.info(f"{PLUGIN_NAME} 相册解绑: {uid} -> {targets}")
        yield event.plain_result(
            f"已解除 {len(targets)} 条绑定（群：{'、'.join(g for _, g in targets)}）"
        )

    @weibo.command("相册列表")
    async def weibo_album_rules_cmd(self, event: AstrMessageEvent):
        """查看所有博主的相册绑定规则"""
        rules = self._album_rules()
        if not rules:
            yield event.plain_result("暂无绑定规则。在目标群内用 微博相册绑定 <uid> <相册名> 绑定")
            return
        bound_gids = {self._parse_umo(s)[2] for s in self._sessions()}
        lines = ["博主相册绑定规则："]
        for idx, ((uid, gid), album) in enumerate(rules.items(), 1):
            name = self.accounts.get(uid, {}).get("name") or uid
            tip = "" if gid in bound_gids else "（该群未绑定推送，不会触发上传）"
            lines.append(f"{idx}. {name}（{uid}）→ 群 {gid} 相册「{album}」{tip}")
        yield event.plain_result("\n".join(lines))

    @weibo.command("列相册")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_album_list(self, event: AstrMessageEvent):
        """查看本群的相册列表（名称与 ID，管理员）"""
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("请在群聊中使用该指令")
            return
        platform, _, _ = self._parse_umo(event.unified_msg_origin)
        yield event.plain_result("正在获取本群相册列表…")
        try:
            nc = await self._album_client(platform)
            albums = await nc.list_albums(str(gid))
        except NapCatError as e:
            yield event.plain_result(f"获取相册列表失败：{e}")
            return
        if not albums:
            yield event.plain_result("本群没有相册（或机器人没权限），请先在 QQ 里创建相册")
            return
        lines = ["本群相册："]
        for idx, a in enumerate(albums, 1):
            aid = pick(a, ALBUM_LIST_ITEM_ID_KEYS)
            name = pick(a, ALBUM_LIST_ITEM_NAME_KEYS, "?")
            lines.append(f"{idx}. {name}（ID: {aid}）")
        yield event.plain_result("\n".join(lines))

    @weibo.command("关键词相册")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_keyword_album(self, event: AstrMessageEvent, kw: str = ""):
        """全局关键词路由（管理员）：命中关键词的微博图片只传固定群相册。

        微博关键词相册 <关键词> <群号> <相册名或ID>；群内使用可省略群号
        """
        if not kw:
            yield event.plain_result(
                "用法：微博关键词相册 <关键词> <群号> <相册名或ID>\n"
                "群内使用可省略群号：微博关键词相册 <关键词> <相册名或ID>\n"
                "例如：微博关键词相册 福利 1234567890 福利图集\n"
                "关键词不可含冒号；命中后图片只传该相册，替代相册绑定规则"
            )
            return
        gid = event.get_group_id()
        # 相册名可能含空格：以关键词在原始分词中定位，取其后的全部剩余参数
        # （与 微博相册绑定 的处理方式一致）
        tokens = re.split(r"\s+", (event.message_str or "").strip())
        rest: list[str] = []
        with contextlib.suppress(ValueError):
            rest = tokens[tokens.index(kw) + 1 :]
        if rest and rest[0].isdigit():
            target_gid, album = rest[0], " ".join(rest[1:])
        elif gid:
            target_gid, album = str(gid), " ".join(rest)
        else:
            yield event.plain_result(
                "私聊使用必须写明群号：微博关键词相册 <关键词> <群号> <相册名或ID>"
            )
            return
        if not album:
            yield event.plain_result(
                "请补上相册名或ID：微博关键词相册 <关键词> <群号> <相册名或ID>\n"
                "相册可用 微博列相册 查看名称与 ID"
            )
            return
        rules = self._album_keyword_rules()
        overwritten = _keyword_rules_upsert(rules, None, kw, target_gid, album)
        self.config["album_keyword_rules"] = _keyword_rules_to_cfg(rules)
        self._save_config()
        logger.info(f"{PLUGIN_NAME} 关键词路由(全局): 「{kw}」 -> 群 {target_gid} 相册「{album}」")
        tip = "（已覆盖同关键词同群的原规则）" if overwritten else ""
        yield event.plain_result(
            f"已设置：正文命中「{kw}」的微博图片将只上传到群 {target_gid} "
            f"相册「{album}」{tip}\n"
            "仅对该博主生效请用 微博博主关键词相册（博主规则优先）"
        )

    @weibo.command("关键词相册移除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_keyword_album_remove(self, event: AstrMessageEvent, kw: str = ""):
        """删除同名全局关键词路由规则（管理员）：微博关键词相册移除 <关键词>"""
        if not kw:
            yield event.plain_result("用法：微博关键词相册移除 <关键词>")
            return
        rules = self._album_keyword_rules()
        removed = [(g, a) for u, k, g, a in rules if u is None and k == kw]
        if not removed:
            yield event.plain_result(
                f"没有关键词「{kw}」的全局规则（微博关键词相册列表 查看；"
                "博主规则用 微博博主关键词相册移除 删除）"
            )
            return
        rules = [r for r in rules if not (r[0] is None and r[1] == kw)]
        self.config["album_keyword_rules"] = _keyword_rules_to_cfg(rules)
        self._save_config()
        logger.info(f"{PLUGIN_NAME} 关键词路由移除(全局): 「{kw}」 -> {removed}")
        yield event.plain_result(
            f"已删除 {len(removed)} 条全局规则："
            + "、".join(f"群 {g} 相册「{a}」" for g, a in removed)
        )

    @weibo.command("博主关键词相册")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_blogger_keyword_album(
        self, event: AstrMessageEvent, uid_text: str = "", kw: str = ""
    ):
        """博主关键词路由（管理员）：只对该博主生效，优先于全局关键词规则。

        微博博主关键词相册 <uid或链接> <关键词> <群号> <相册名或ID>；群内可省略群号
        """
        uid = _extract_uid(uid_text)
        if not uid or not kw:
            yield event.plain_result(
                "用法：微博博主关键词相册 <uid或链接> <关键词> <群号> <相册名或ID>\n"
                "群内使用可省略群号：微博博主关键词相册 <uid> <关键词> <相册名或ID>\n"
                "例如：微博博主关键词相册 1234567890 福利 9876543210 福利图集"
            )
            return
        self._sync_accounts_from_config()
        if uid not in self.accounts:
            yield event.plain_result(
                f"账号 {uid} 不在监控列表中，请先用 微博添加 添加，或用 微博列表 查看"
            )
            return
        gid = event.get_group_id()
        tokens = re.split(r"\s+", (event.message_str or "").strip())
        rest: list[str] = []
        with contextlib.suppress(ValueError):
            rest = tokens[tokens.index(kw) + 1 :]
        if rest and rest[0].isdigit():
            target_gid, album = rest[0], " ".join(rest[1:])
        elif gid:
            target_gid, album = str(gid), " ".join(rest)
        else:
            yield event.plain_result(
                "私聊使用必须写明群号：微博博主关键词相册 <uid> <关键词> <群号> <相册名或ID>"
            )
            return
        if not album:
            yield event.plain_result(
                "请补上相册名或ID：微博博主关键词相册 <uid> <关键词> <群号> <相册名或ID>"
            )
            return
        rules = self._album_keyword_rules()
        overwritten = _keyword_rules_upsert(rules, uid, kw, target_gid, album)
        self.config["album_keyword_rules"] = _keyword_rules_to_cfg(rules)
        self._save_config()
        name = self.accounts[uid]["name"]
        logger.info(
            f"{PLUGIN_NAME} 关键词路由(博主 {uid}): 「{kw}」 -> 群 {target_gid} 相册「{album}」"
        )
        tip = "（已覆盖同关键词同群的原规则）" if overwritten else ""
        yield event.plain_result(
            f"已设置：{name}（{uid}）的微博正文命中「{kw}」时，图片将只上传到"
            f"群 {target_gid} 相册「{album}」{tip}"
        )

    @weibo.command("博主关键词相册移除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_blogger_keyword_album_remove(
        self, event: AstrMessageEvent, uid_text: str = "", kw: str = ""
    ):
        """删除指定博主的关键词路由规则（管理员）：微博博主关键词相册移除 <uid> <关键词>"""
        uid = _extract_uid(uid_text)
        if not uid or not kw:
            yield event.plain_result("用法：微博博主关键词相册移除 <uid> <关键词>")
            return
        rules = self._album_keyword_rules()
        removed = [(g, a) for u, k, g, a in rules if u == uid and k == kw]
        if not removed:
            yield event.plain_result(
                f"账号 {uid} 没有关键词「{kw}」的规则（微博关键词相册列表 查看）"
            )
            return
        rules = [r for r in rules if not (r[0] == uid and r[1] == kw)]
        self.config["album_keyword_rules"] = _keyword_rules_to_cfg(rules)
        self._save_config()
        logger.info(f"{PLUGIN_NAME} 关键词路由移除(博主 {uid}): 「{kw}」 -> {removed}")
        yield event.plain_result(
            f"已删除 {len(removed)} 条规则：" + "、".join(f"群 {g} 相册「{a}」" for g, a in removed)
        )

    @weibo.command("关键词相册列表")
    async def weibo_keyword_album_list(self, event: AstrMessageEvent):
        """查看关键词相册路由规则（博主规则优先于全局规则）"""
        rules = self._album_keyword_rules()
        if not rules:
            yield event.plain_result(
                "未配置关键词路由：图片按默认相册绑定上传（微博相册列表 查看）。\n"
                "设置全局规则：微博关键词相册 <关键词> <群号> <相册名或ID>\n"
                "设置博主规则：微博博主关键词相册 <uid> <关键词> <群号> <相册名或ID>"
            )
            return
        blogger = [(u, k, g, a) for u, k, g, a in rules if u is not None]
        glob = [(k, g, a) for u, k, g, a in rules if u is None]
        lines = ["关键词相册路由规则（博主规则优先于全局，第一条命中生效）："]
        if blogger:
            lines.append("博主规则：")
            for idx, (uid, kw, gid, album) in enumerate(blogger, 1):
                name = self.accounts.get(uid, {}).get("name") or uid
                lines.append(f"{idx}. {name}（{uid}）命中「{kw}」→ 群 {gid} 相册「{album}」")
        if glob:
            lines.append("全局规则：")
            for idx, (kw, gid, album) in enumerate(glob, 1):
                lines.append(f"{idx}. 命中「{kw}」→ 群 {gid} 相册「{album}」")
        yield event.plain_result("\n".join(lines))

    @weibo.command("检测")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_check(self, event: AstrMessageEvent):
        """立即检查一轮微博更新（管理员）"""
        if self._checking:
            yield event.plain_result("已有检查任务在进行中，请稍候再试")
            return
        if not self.accounts:
            yield event.plain_result("尚未监控任何账号，请先用 微博添加 添加")
            return
        if time.time() < self._blocked_until:
            remain = cooldown_remaining_minutes(self._blocked_until)
            yield event.plain_result(f"微博风控冷却中（约 {remain} 分钟后自动恢复），请稍后再试")
            return
        yield event.plain_result("正在检查微博更新…")
        new_count = await self.check_all()
        if new_count:
            yield event.plain_result(f"检查完成，本轮检测到 {new_count} 条新微博")
        else:
            yield event.plain_result("检查完成，暂无新微博")

    @weibo.command("状态")
    async def weibo_status(self, event: AstrMessageEvent):
        """查看插件运行状态"""
        lines = ["微博监控运行状态："]
        last = (
            datetime.fromtimestamp(self.last_check_ts, tz=_TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
            if self.last_check_ts
            else "尚未检查"
        )
        lines.append(f"上次检查：{last}")
        lines.append(f"监控账号：{len(self.accounts)} 个")
        lines.append(f"推送目标：{len(self._sessions())} 个")
        lines.append(f"待推送/重试：{len(self.pending)} 条")
        store = self.archive_store()
        if store is None:
            lines.append("归档：未启用")
        else:
            try:
                posts_n = store.count_posts()
                media = store.media_stats()
                lines.append(
                    f"归档：{posts_n} 帖 / {int(media.get('total') or 0)} 图"
                    f"（已落盘 {int(media.get('downloaded') or 0)}）"
                )
            except Exception as e:  # 归档统计坏了不能拖垮状态指令
                logger.warning(f"读取归档统计失败: {e!r}")
                lines.append("归档：统计读取失败")
        kw_rules = self._album_keyword_rules()
        kw_b = sum(1 for u, *_ in kw_rules if u is not None)
        lines.append(
            f"关键词路由：博主 {kw_b} 条 / 全局 {len(kw_rules) - kw_b} 条"
            + ("" if kw_rules else "（未配置时按默认相册绑定上传）")
        )
        if time.time() < self._blocked_until:
            remain = cooldown_remaining_minutes(self._blocked_until)
            lines.append(f"风控状态：冷却中（约 {remain} 分钟后恢复）")
        else:
            lines.append("风控状态：正常")
        if self._visitor_ts:
            age = (time.time() - self._visitor_ts) / 3600
            lines.append(f"游客身份：{age:.1f} 小时前更新")
        else:
            lines.append("游客身份：未获取")
        if self._custom_cookies:
            lines.append(f"身份模式：自定义 Cookie（{sorted(self._custom_cookies)}）")
        else:
            lines.append("身份模式：免 Cookie 游客身份")
        lines.append(f"出网方式：{'代理 ' + _redact_proxy(self._proxy) if self._proxy else '直连'}")
        if self._web_ready:
            lines.append("控制面板：AstrBot 管理面板 - 插件 - 本插件详情页打开")
        else:
            lines.append("控制面板：当前 AstrBot 版本不支持插件页面，未启用")
        yield event.plain_result("\n".join(lines))

    # ---------------- 归档（P3） ----------------

    @weibo.command("回填")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_backfill(self, event: AstrMessageEvent, uid_text: str = ""):
        """全量回填某博主的全部历史微博到归档库（管理员）"""
        uid, since, full = _parse_backfill_args(_cmd_args(event), uid_text)
        if not uid:
            yield event.plain_result(
                "用法：微博回填 <uid> [--since YYYY-MM-DD] [--full]\n"
                "例如：微博回填 1234567890 --since 2024-01-01"
            )
            return
        # archive_backfill 自己也会拒绝并发，这里先挡一次：直接给出人话提示，
        # 而不是让指令看起来"开始了"却立刻失败
        if self.archive_backfill_running():
            yield event.plain_result(
                "已有回填任务在跑（微博回填状态 查看进度，微博回填停止 可中止）"
            )
            return
        self._backfill_stop = False

        async def _run() -> None:
            # 后台任务：任何异常都只能记日志，否则会变成无人 await 的 task 异常
            try:
                await self.archive_backfill(uid, since=since, full=full)
            except Exception as e:
                logger.warning(f"回填任务异常: {e!r}")

        self._backfill_task = asyncio.create_task(_run())
        yield event.plain_result(
            f"已开始回填 {uid}"
            + (f"（since {since}）" if since else "")
            + (f"（full {'开' if full else '关'}）" if full else "")
            + "；微博回填状态 查看进度，微博回填停止 可中止"
        )

    @weibo.command("回填状态")
    async def weibo_backfill_status(self, event: AstrMessageEvent):
        """查看回填进度、最近一次统计与游标"""
        running = self.archive_backfill_running()
        lines = ["微博回填状态：", f"任务：{'进行中' if running else '空闲'}"]
        stats = getattr(self, "_backfill_last_stats", None)
        if stats:
            formatted = self.format_backfill(stats)
            if formatted:
                lines.append(formatted.rstrip())
        else:
            lines.append("最近统计：暂无（微博回填 <uid> 开始）")
        cursor_lines: list[str] = []
        store = self.archive_store()
        if store is not None:
            for uid in list(self.accounts):
                try:
                    cur = store.get_cursor(uid)
                except Exception as e:
                    logger.warning(f"读取回填游标失败: {e!r}")
                    continue
                if not cur:
                    continue
                cursor_lines.append(
                    f"{self._account_name(uid)}（{uid}）{cur.get('status', 'running')} "
                    f"下一页 {int(cur.get('next_page') or 1)}"
                )
        lines.append("游标：" + ("；".join(cursor_lines) if cursor_lines else "无"))
        yield event.plain_result("\n".join(lines))

    @weibo.command("回填停止")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_backfill_stop(self, event: AstrMessageEvent):
        """请求停止正在跑的回填（管理员，游标保留可续跑）"""
        self.archive_backfill_stop()
        yield event.plain_result("已请求停止回填：当前页处理完即退出，游标保留可续跑")

    @weibo.command("归档导入")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_archive_import(self, event: AstrMessageEvent, path_text: str = ""):
        """把 JSON 种子文件导入归档库（管理员）"""
        path = _import_path(event, path_text)
        if not path:
            yield event.plain_result("用法：微博归档导入 <文件路径>")
            return
        yield event.plain_result("正在导入归档…")
        stats = await self.archive_import_json(path)
        if stats.get("error"):
            yield event.plain_result(f"导入失败：{stats['error']}")
            return
        yield event.plain_result(self.format_import(stats))

    @weibo.command("归档查询")
    async def weibo_archive_query_cmd(self, event: AstrMessageEvent, uid_text: str = ""):
        """查看归档库里的微博（每页 10 条）"""
        args = _cmd_args(event)
        uid = _extract_uid(uid_text) or ""
        page = 1
        rest = args
        if uid and args and _extract_uid(args[0]) == uid:
            rest = args[1:]
        elif not uid:
            for idx, tok in enumerate(args):
                cand = _extract_uid(tok)
                if cand:
                    uid = cand
                    rest = args[idx + 1 :]
                    break
        for tok in rest:
            if tok.isdigit():
                page = max(1, min(int(tok), 9999))
                break
        offset = (page - 1) * ARCHIVE_QUERY_PAGE_SIZE
        rows = await self.archive_query(uid, limit=ARCHIVE_QUERY_PAGE_SIZE, offset=offset)
        if not rows:
            yield event.plain_result(f"归档暂无微博（{uid or '全部账号'}，第 {page} 页）")
            return
        lines = [f"微博归档（{uid or '全部账号'}，第 {page} 页，{len(rows)} 条）："]
        for row in rows:
            created = int(row.get("created_at") or 0)
            when = (
                datetime.fromtimestamp(created, tz=_TZ_CN).strftime("%Y-%m-%d %H:%M")
                if created
                else (str(row.get("created_raw") or "") or "时间未知")
            )
            text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
            snippet = text[:40] + ("…" if len(text) > 40 else "")
            pics = row.get("pics") or []
            author = row.get("author") or row.get("uid") or "未知"
            lines.append(f"{when} @{author} {snippet or '（无正文）'}（{len(pics)} 图）")
        yield event.plain_result("\n".join(lines))

    @weibo.command("对账")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_archive_reconcile(self, event: AstrMessageEvent, uid_text: str = ""):
        """核对归档完整性并列出缺口（管理员）"""
        args = _cmd_args(event)
        uid = _extract_uid(uid_text) or ""
        if not uid:
            for tok in args:
                uid = _extract_uid(tok) or ""
                if uid:
                    break
        report = await self.archive_reconcile(uid)
        yield event.plain_result(self.format_reconcile(report))

    @weibo.command("管线状态")
    async def weibo_pipeline_status(self, event: AstrMessageEvent):
        """查看图床/向量管线的队列与六态进度"""
        yield event.plain_result(self.format_pipeline_status())

    @weibo.command("管线补跑")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weibo_pipeline_backfill(self, event: AstrMessageEvent):
        """把归档库里没跑完图床/向量的帖子重新入队（管理员）"""
        stats = await self.pipeline_backfill()
        error = str((stats or {}).get("error") or "")
        if error:
            yield event.plain_result(f"管线补跑失败：{error}")
            return
        scanned = int((stats or {}).get("scanned") or 0)
        queued = int((stats or {}).get("queued") or 0)
        yield event.plain_result(f"管线补跑完成：已扫描 {scanned} 帖，入队 {queued} 帖")

