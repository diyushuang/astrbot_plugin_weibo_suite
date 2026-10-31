"""推送编排、@全体闸门、取消/重试、视频与富媒体投递。"""

from __future__ import annotations

from .runtime import *


class PusherMixin:
    def _build_message(self, post: dict[str, Any]) -> str:
        name = self.accounts.get(post["uid"], {}).get("name") or post["author"]
        msg = (
            self._message_format()
            .replace("{name}", name)
            .replace("{time}", post["time"])
            .replace("{weibo}", self._post_body(post))
            .replace("{link}", post["link"])
        )
        if bool(self.config.get("show_hashtags", False)):
            # 话题标签行：与关键词过滤同口径（正文+转发原文），追加在消息末尾
            tags = extract_hashtags(f"{post['text']}\n{post['orig_text']}")
            if tags:
                msg += "\n" + " ".join(f"#{t}#" for t in tags)
        return re.sub(r"\n{3,}", "\n\n", msg).strip()

    def preview_push(self) -> dict[str, Any]:
        """按当前配置渲染一条样例推送，供面板「推送预览」卡片展示。

        样例微博是虚构的：不进已见记录、不进待推送队列、不给任何会话发消息，
        但渲染路径与真实推送完全一致（_build_message），模板、话题标签行、
        表情翻译都按当前配置生效。@全体成员只给「会不会带上」的判定——机器人
        在目标群有没有管理员权限要到协议端发送时才知道。
        """
        sample_text = "今天上新啦～[笑cry]转发抽三位宝送周边 #上新抽奖# #粉丝福利#"
        # 样例博主跟着名单走：填了 at_all_uids 就按名单首位博主渲染，否则固定
        # uid 永远落在名单外，预览只会反复说「本样例未命中」，看不出配置效果。
        uids = self._at_all_uids()
        sample_uid = uids[0] if uids else "1234567890"
        sample = {
            "id": "preview",
            "uid": sample_uid,
            "author": self._account_name(sample_uid) if uids else "样例博主",
            "time": "10-11 12:30",
            "text": sample_text,
            "orig_name": "",
            "orig_text": "",
            "is_retweet": False,
            "link": f"https://weibo.com/{sample_uid}/example",
        }
        kws = self._at_all_keywords()
        return {
            "text": self._build_message(sample),
            "at_all": self._at_all_hit({"raw_text": sample_text, "uid": sample_uid}),
            "at_all_on": bool(self.config.get("at_all", False)),
            "at_all_keywords": kws,
            "at_all_uids": [self._account_name(u) for u in uids],
            "with_images": bool(self.config.get("message_with_images", True)),
            "with_videos": self._videos_enabled(),
        }

    async def _build_chain(self, item: dict[str, Any], *, with_video: bool = True) -> MessageChain:
        """构建推送消息链：文本 + 可选图片段 + 可选视频段（message_with_* 开启时）。

        with_video=False 供视频发送反复失败后的降级重发使用：只发图文并附
        说明，保证微博正文至少能到群里。条目上挂着 pic_rejected / video_rejected
        （对应段已被协议端拒收过）时不再带那一段，改附提示，否则会原样再撞一次。
        """
        if with_video and item.get("video_rejected"):
            with_video = False
        chain = MessageChain().message(item["text"])
        if bool(self.config.get("message_with_images", True)):
            if item.get("pic_rejected"):
                # 图片段已被协议端拒收过：这条起不再带图，否则会原样再撞一次
                chain.message("\n" + PIC_REJECTED_NOTE)
            else:
                for pic in item.get("pics") or []:
                    url = str(pic.get("url") or "")
                    if url:
                        # 轻量档：群里看够了，原图交给相册那条路
                        chain.chain.append(Image.fromURL(to_light(url)))
        if self._videos_enabled():
            note = str(item.get("video_note") or "")
            if with_video:
                sent_n, refused_n = 0, 0
                for path in item.get("video_files") or []:
                    p = Path(str(path))
                    if not p.is_file():
                        continue
                    seg = await self._video_segment(p)
                    if seg is None:
                        refused_n += 1
                    else:
                        chain.chain.append(seg)
                        sent_n += 1
                if refused_n:
                    # 有视频段被内嵌上限拒发：全被拒按"未能转发"提示，部分被拒
                    # 按"部分未能转发"提示，与下载侧的降级口径一致
                    note = note or (VIDEO_FALLBACK_NOTE if not sent_n else VIDEO_PARTIAL_NOTE)
            elif item.get("video_files"):
                # 视频段被摘掉时的说明：群文件兜底真的传成了就说"已改以群文件发送"
                # （视频还在群里拿得到），没传成才是"未能自动转发"
                note = note or self._video_file_note(item)
            if note:
                chain.message("\n" + note)
        return chain

    async def _send_with_timeout(
        self, session: str, chain: MessageChain, *, timeout_seconds: int | None = None
    ) -> bool:
        """带超时的主动消息发送，防止单个适配器卡死拖住整个轮询任务。"""
        send_timeout = (
            self._int_cfg("message_send_timeout", 60)
            if timeout_seconds is None
            else timeout_seconds
        )
        try:
            if send_timeout > 0:
                ok = await asyncio.wait_for(
                    self.context.send_message(session, chain),
                    timeout=send_timeout,
                )
            else:
                ok = await self.context.send_message(session, chain)
            if ok is False:
                raise RuntimeError("AstrBot 未找到匹配的消息平台")
            return True
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"发送超过 {send_timeout} 秒，平台是否已接收未知") from e

    def _send_timeout_for(self, item: dict[str, Any]) -> int:
        """按这条链实际携带的媒体计算发送超时（秒），0 表示不限制。

        message_send_timeout（60s）是按纯文本消息的经验定的：图片段以 URL 交给
        协议端自行抓取、视频段更要传完整媒体，协议端把全部媒体处理完才回回执，
        耗时随媒体量线性涨——一刀切会大量出现"群里已收到、面板判超时放弃"的
        倒挂。带视频沿用固定下限（视频总比九图慢）；带图按张数放大；两样都带
        按视频算（下限更高）。
        """
        send_timeout = self._int_cfg("message_send_timeout", 60)
        if not send_timeout:
            return 0
        if self._video_carried(item):
            return max(send_timeout, VIDEO_SEND_TIMEOUT_FLOOR)
        if self._pics_enabled(item):
            n = sum(1 for p in (item.get("pics") or []) if str(p.get("url") or ""))
            if n:
                send_timeout = max(
                    send_timeout, PIC_SEND_TIMEOUT_BASE + PIC_SEND_TIMEOUT_PER_PIC * n
                )
        return send_timeout

    @staticmethod
    def _is_media_rejected(exc: BaseException) -> bool:
        """异常是否为"富媒体被协议端拒收"（QQNT 尺寸/体积风控，NapCatQQ#1443）。

        这一类是协议端明确回了失败：消息确定没送达，且原样重发多少次都是同样
        的结果，只有把嫌疑段摘掉才发得出去。
        """
        return RICH_MEDIA_REJECT_MARKER in str(exc).lower()

    @staticmethod
    def _reject_digest(exc: BaseException) -> str:
        """把协议端的拒收回执压成一行可读摘要（面板、错误留存与 state.json 都用它）。

        aiocqhttp 的 ActionFailed 是一整段带换行的裸字符串（retcode + eventRet
        JSON），原样留档既读不出重点又把持久化文件撑大。只留能定性的三样：
        retcode、result、errMsg——现场判"体积超限 / NapCat 版本回归 / 风控"
        就靠这三个字段。抓不到这些字段（别的协议端实现）时退化成折叠空白后的原文。
        """
        raw = str(exc)
        bits = []
        if m := _REJECT_RETCODE_RE.search(raw):
            bits.append(f"retcode={m.group(1)}")
        if m := _REJECT_RESULT_RE.search(raw):
            bits.append(f"result={m.group(1)}")
        if m := _REJECT_ERRMSG_RE.search(raw):
            bits.append(f"errMsg={m.group(1)}")
        text = " ".join(bits) if bits else re.sub(r"\s+", " ", raw).strip()
        return text[:REJECT_DETAIL_MAX]

    @staticmethod
    def _reject_weight(item: dict[str, Any], seg: str) -> str:
        """被判罪段当时的真实体量：判断"是不是体积撑爆"靠它，而不是靠猜。

        视频报真实落盘的文件体积（发出去的就是它）；图片只有 URL、不落本地，
        报张数。拿不到就返回空串，不编数字。
        """
        if seg == "视频":
            sizes = []
            for f in item.get("video_files") or []:
                try:
                    sizes.append(Path(str(f)).stat().st_size)
                except OSError:
                    continue
            if not sizes:
                return ""
            total = sum(sizes) / 1048576
            return f"{len(sizes)} 段共 {total:.1f}MB" if len(sizes) > 1 else f"{total:.1f}MB"
        if seg == "图片":
            n = len(item.get("pics") or [])
            return f"{n} 张" if n else ""
        return ""

    def _note_reject(self, item: dict[str, Any], seg: str, exc: BaseException) -> str:
        """把一次拒收的现场记在条目上，返回给日志用的一行说明。

        记的是"协议端到底回了什么 + 当时体量多大 + 被拒过几次"：这三样凑起来
        才能把"富媒体确实超限"和"协议端自己的毛病（NapCat 版本回归/风控）"分开。
        条目随后就出队了，所以同时进最近动态（见 _reject_reason），面板里查得到。
        """
        detail = self._reject_digest(exc)
        weight = self._reject_weight(item, seg)
        item["reject_detail"] = detail
        item["reject_weight"] = weight
        item["reject_attempts"] = int(item.get("reject_attempts") or 0) + 1
        return f"回执 {detail}" + (f"，当时 {weight}" if weight else "")

    def _pics_enabled(self, item: dict[str, Any]) -> bool:
        """这条链当前会不会带图片段：开关开着、有图、且图片段还没被拒收过。"""
        return bool(
            item.get("pics")
            and not item.get("pic_rejected")
            and bool(self.config.get("message_with_images", True))
        )

    def _video_carried(self, item: dict[str, Any]) -> bool:
        """这条链当前会不会带视频段：开关开着、有已下好的文件、且视频段没被拒收过。"""
        return bool(
            self._videos_enabled()
            and not item.get("video_rejected")
            and any(Path(str(f)).is_file() for f in (item.get("video_files") or []) if f)
        )

    def _strip_rejected_segment(self, item: dict[str, Any]) -> str:
        """整条被拒收后摘掉下一个嫌疑段，返回段名；没有可摘的段时返回空串。

        先摘图片段：超长截图/超大原图是这类拒收的主因。摘了图片仍然被拒，说明
        凶手是视频段（那一次链里根本没有图），此时图片还没被证明有罪，摘视频段
        就把图片段放回去——放回后再撞一次拒收，才算两段都有罪。
        """
        if self._pics_enabled(item):
            item["pic_rejected"] = True
            return "图片"
        if self._video_carried(item):
            item["video_rejected"] = True
            item.pop("pic_rejected", None)
            return "视频"
        return ""

    def _reject_reason(self, item: dict[str, Any]) -> str:
        """面板/记账用的说明：拒收过哪些段、视频最终走了哪条路、回执是什么。"""
        parts = []
        if item.get("pic_rejected"):
            parts.append("图片段被协议端拒收，已去图改发正文")
        sent = [str(s) for s in (item.get("video_file_sessions") or [])]
        if item.get("video_rejected"):
            if item.get("video_file_first"):
                # video_prefer_file 是配置直选文件，不是被拒收：措辞不提"拒收"，
                # 否则面板会把用户的主动选择报成"协议端有问题"
                parts.append(self._video_file_summary(item) or "视频未随消息发送")
            else:
                # 视频的去向有两种，措辞要分开：改发文件成了（视频还在群里拿得到，
                # 临时文件还是群文件见 _video_file_summary）或只剩一句提示链接——
                # 不然面板会同时出现"已去掉视频"和"已发送"两句，读起来自相矛盾
                parts.append(
                    "视频段被协议端拒收，" + (self._video_file_summary(item) or "已去掉视频")
                )
        if not sent:
            err = str(item.get("video_file_fail") or "")
            if err:
                parts.append(f"文件兜底也失败：{err}")
        detail = str(item.get("reject_detail") or "")
        if not detail:
            return "，".join(parts)
        bits = [f"协议端回执 {detail}"]
        weight = str(item.get("reject_weight") or "")
        if weight:
            bits.append(f"当时 {weight}")
        n = int(item.get("reject_attempts") or 0)
        if n > 1:
            bits.append(f"共被拒 {n} 次")
        extra = "，".join(bits)
        head = "，".join(parts)
        return f"{head}；{extra}" if head else extra

    def _at_all_uids(self) -> list[str]:
        """@全体博主名单（去空白、保序去重）；空列表 = 不限博主。"""
        uids: list[str] = []
        for item in self.config.get("at_all_uids") or []:
            uid = str(item).strip()
            if uid and uid not in uids:
                uids.append(uid)
        return uids

    def _at_all_keywords(self) -> list[str]:
        """@全体的关键词限定（去空白、保序去重）；空列表 = 不限内容。"""
        kws: list[str] = []
        for item in self.config.get("at_all_keywords") or []:
            kw = str(item).strip()
            if kw and kw not in kws:
                kws.append(kw)
        return kws

    def _at_all_hit(self, item: dict[str, Any]) -> bool:
        """这条推送是否满足 @全体条件：总开关开、博主在名单内、且关键词命中。

        博主名单与关键词是两道各自独立的闸门，都要过：名单留空 = 不限博主，
        关键词留空 = 不限内容（判定口径不变，向后兼容只开关键词的用法）。
        匹配范围与屏蔽词一致（条目上的 raw_text：正文+转发原文），与模板渲染
        无关——用户改模板也不影响判定，误把「微博」这类模板头部的词填进关键词
        也不会让每条推送都命中。
        """
        if not bool(self.config.get("at_all", False)):
            return False
        uids = self._at_all_uids()
        if uids and str(item.get("uid") or "") not in uids:
            return False
        kws = self._at_all_keywords()
        if not kws:
            return True
        text = str(item.get("raw_text") or "")
        return any(kw in text for kw in kws)

    def _with_at_all(self, session: str, item: dict[str, Any], chain: MessageChain) -> MessageChain:
        """群聊且满足条件时返回链首带 AtAll 的消息链，否则原链返回。

        每次发送前按会话现做：消息链在多个会话间复用（含拒收摘段后的重建链），
        而 @全体只该进群聊、私聊会话不能被动沾上；包装生成新链对象，不改原链。
        """
        if not self._at_all_hit(item):
            return chain
        if self._parse_umo(session)[1] != "GroupMessage":
            return chain
        return MessageChain(chain=[AtAll(), *chain.chain])

    def _at_all_limit(self) -> int:
        """24 小时内最多 @ 全体几次（0 = 不限制）。"""
        return max(0, self._int_cfg("at_all_daily_limit", AT_ALL_QUOTA_DEFAULT))

    def _at_all_quota_used(self) -> int:
        """窗口内已经用掉的次数（顺带把过窗的时间戳丢掉）。"""
        now = time.time()
        self._at_all_sent = [ts for ts in self._at_all_sent if now - ts < AT_ALL_QUOTA_WINDOW][
            -AT_ALL_QUOTA_MAX_KEEP:
        ]
        return len(self._at_all_sent)

    def _at_all_mark_sent(self):
        """记账一次真正带 @全体 出群的推送（与 QQ 同口径：账号级、所有群共享）。"""
        self._at_all_sent.append(time.time())
        self._at_all_quota_used()
        self._save_state()

    async def _bot_self_id(self, bot) -> str:
        """AstrBot 这条连接上的登录账号 QQ 号（按 bot 实例缓存，拿不到返回空串）。"""
        self_id = self._self_ids.get(id(bot))
        if self_id is not None:
            return self_id
        try:
            info = await asyncio.wait_for(
                bot.call_action("get_login_info"), timeout=AT_ALL_API_TIMEOUT
            )
            self_id = str((info or {}).get("user_id") or "")
        except Exception as e:
            # 拿不到 self_id 只影响 @全体 的权限预检与相册的同机判定，不影响发送
            logger.info(f"{PLUGIN_NAME} 获取机器人登录信息失败: {e!r}")
            self_id = ""
        self._self_ids[id(bot)] = self_id
        return self_id

    async def _group_role(self, platform_id: str, gid: str) -> str:
        """机器人在这个群的角色：owner / admin / member；拿不到返回空串（未知）。

        两级查询：get_group_member_info 直查自己（便宜、精确）；协议端不认这个
        接口或回执里没有 role 时退到 get_group_member_list 自己翻。**成员列表
        为空 / 接口异常一律按"未知"返回**，绝不能当成"没有权限"——那会把本该
        正常的 @全体 直接关掉。结果按 AT_ALL_ROLE_TTL 缓存：角色一周也难得变一次，
        每条推送都查一遍纯属浪费一次往返。
        """
        key = f"{platform_id}:{gid}"
        now = time.time()
        cached = self._at_all_roles.get(key)
        if cached and now - cached[0] < AT_ALL_ROLE_TTL:
            return cached[1]
        if not str(gid).isdigit():
            return ""
        bot = self._find_bot(platform_id)
        if bot is None:
            return ""
        self_id = await self._bot_self_id(bot)
        if not self_id:
            return ""
        role = ""
        try:
            info = await asyncio.wait_for(
                bot.call_action("get_group_member_info", group_id=int(gid), user_id=int(self_id)),
                timeout=AT_ALL_API_TIMEOUT,
            )
            if isinstance(info, dict):
                role = str(info.get("role") or "")
        except Exception as e:
            logger.info(f"{PLUGIN_NAME} 查询机器人在群 {gid} 的角色失败，改查成员列表: {e!r}")
        if not role:
            members = None
            try:
                members = await asyncio.wait_for(
                    bot.call_action("get_group_member_list", group_id=int(gid)),
                    timeout=AT_ALL_API_TIMEOUT,
                )
            except Exception as e:
                logger.info(f"{PLUGIN_NAME} 获取群 {gid} 成员列表失败，@全体 权限按未知处理: {e!r}")
            for member in members or []:
                if isinstance(member, dict) and str(member.get("user_id") or "") == self_id:
                    role = str(member.get("role") or "")
                    break
        role = role.lower()
        if role in AT_ALL_ROLE_NAMES:
            self._at_all_roles[key] = (now, role)
            return role
        return ""

    async def _at_all_gate(self, session: str) -> tuple[bool, str]:
        """此刻这条会话能不能真的 @ 到全体：返回 (可以, 不可以的原因)。"""
        platform_id, mtype, gid = self._parse_umo(session)
        if mtype != "GroupMessage":
            return False, "私聊会话不带 @全体"
        limit = self._at_all_limit()
        if limit:
            used = self._at_all_quota_used()
            if used >= limit:
                return False, (
                    f"24 小时内已 @全体 {used} 次，达到上限 {limit} 次"
                    "（QQ 的次数按账号算、所有群共享）；"
                    "超级会员可把 at_all_daily_limit 调到 20，设 0 则不限制"
                )
        if not bool(self.config.get("at_all_check_permission", True)):
            return True, ""
        role = await self._group_role(platform_id, gid)
        if not role:
            # 拿不到角色（协议端没有成员接口、群号异常、成员列表为空）：放行。
            # 拦错了等于功能直接失效；放过去最多是协议端自己把这条发失败，
            # 与 v1.12.x 的行为一致，且失败后还有"去掉 @全体 补发正文"兜底
            return True, ""
        if role in ("owner", "admin"):
            return True, ""
        name = AT_ALL_ROLE_NAMES.get(role, role)
        return False, (
            f"机器人在本群是{name}，QQ 只允许群主/管理员 @全体；"
            "继续发只会得到一条纯文本「@全体成员」而没有任何提醒，已改为普通推送"
        )

    def _remember_at_all(self, state: str, detail: str, session: str = ""):
        """把 @全体 的实际结果落成面板可读的一句话（不写日志、不去重）。"""
        self._at_all_last = {
            "ts": time.time(),
            "session": session,
            "state": state,
            "detail": detail,
        }
        self._record_event("at_all", detail=(f"{session}：{detail}" if session else detail))

    def _note_at_all(self, state: str, detail: str, session: str = "", *, log: bool | None = None):
        """记一次 @全体 的结果；同一条原因 AT_ALL_NOTICE_TTL 内不重复记（防刷屏）。

        state 取值：ok（回读确认带上了）/ degraded（协议端把 at 降级成文本）/
        skipped（闸门没过，没发）/ failed（协议端直接拒收）/ unknown（回读不出结论）/
        sent（发出去了但没开回读确认，只能记个"已发出"）。
        每条推送都命中同一原因时，只留第一条——但 _at_all_last 每次都更新，
        面板看到的永远是最近一次的真实结果。
        """
        key = f"{session}|{state}|{detail}"
        now = time.time()
        # TTL 清理与去重同一次遍历完成，避免长跑时只增不减。
        self._at_all_notices = {
            notice_key: ts
            for notice_key, ts in self._at_all_notices.items()
            if now - ts < AT_ALL_NOTICE_TTL
        }
        if key in self._at_all_notices:
            return
        self._at_all_notices[key] = now
        self._remember_at_all(state, detail, session)
        if log is None:
            # 只有"确实没 @ 成"才算告警：ok 是期望结果，unknown 是回读不出结论
            # （协议端没历史接口），都不该在错误留存里堆成问题
            log = state in ("degraded", "failed", "skipped")
        if log:
            logger.warning(f"{PLUGIN_NAME} {detail}" + (f"（{session}）" if session else ""))
        else:
            logger.info(f"{PLUGIN_NAME} {detail}" + (f"（{session}）" if session else ""))

    @staticmethod
    def _msg_text(msg: dict[str, Any]) -> str:
        """群消息里的纯文本部分（回读时按正文认领自己刚发的那条）。"""
        segs = msg.get("message")
        if isinstance(segs, str):
            return segs
        parts = [
            str((seg.get("data") or {}).get("text") or "")
            for seg in segs or []
            if isinstance(seg, dict) and seg.get("type") == "text"
        ]
        return "".join(parts) or str(msg.get("raw_message") or "")

    @staticmethod
    def _msg_has_at_all(msg: dict[str, Any]) -> bool:
        """这条群消息里是否真的带 at 全体（CQ 码形态也认）。"""
        segs = msg.get("message")
        if isinstance(segs, str):
            return "qq=all" in segs.replace(" ", "")
        for seg in segs or []:
            if not isinstance(seg, dict) or seg.get("type") != "at":
                continue
            if str((seg.get("data") or {}).get("qq") or "").lower() == "all":
                return True
        return False

    async def _verify_at_all(self, session: str, item: dict[str, Any]) -> tuple[str, str]:
        """回读群消息，确认 at 段是不是真的落地了（返回 (state, 人话说明)）。

        判定只看"自己刚发的那条"：按正文前缀在群历史里认领，认领到了再看它的
        消息段里还有没有 at 全体——有 = ok，没有 = degraded（QQ 把 at 降级成
        文本，正是"群里只看到 @全体成员 字样却没人被提醒"的现场）。认领不到
        （协议端没有历史接口、落库延迟）返回 unknown：回读是诊断手段，不能反过来
        把一条已经送出去的推送判成失败。
        """
        platform_id, _mtype, gid = self._parse_umo(session)
        if not str(gid).isdigit():
            return "unknown", "会话里没有群号，无法回读确认"
        bot = self._find_bot(platform_id)
        if bot is None:
            return "unknown", "拿不到协议端连接，无法回读确认"
        self_id = await self._bot_self_id(bot)
        if not self_id:
            return "unknown", "拿不到机器人账号，无法回读确认"
        needle = str(item.get("text") or "")[:24].strip()
        for attempt in range(AT_ALL_VERIFY_TRIES):
            if attempt:
                await asyncio.sleep(AT_ALL_VERIFY_GAP)
            try:
                resp = await asyncio.wait_for(
                    bot.call_action(
                        "get_group_msg_history",
                        group_id=int(gid),
                        count=AT_ALL_VERIFY_COUNT,
                    ),
                    timeout=AT_ALL_API_TIMEOUT,
                )
            except Exception as e:
                logger.info(f"{PLUGIN_NAME} 回读群 {gid} 消息失败（不影响推送）: {e!r}")
                return "unknown", "协议端不支持回读群消息，@全体 是否生效未确认"
            msgs = resp.get("messages") if isinstance(resp, dict) else resp
            if not isinstance(msgs, list) or not msgs:
                continue
            hit = None
            for msg in reversed(msgs):
                if not isinstance(msg, dict):
                    continue
                if str((msg.get("sender") or {}).get("user_id") or "") != self_id:
                    continue
                if needle and needle not in self._msg_text(msg):
                    continue
                hit = msg
                break
            if hit is None:
                continue
            if self._msg_has_at_all(hit):
                return "ok", "回读确认群里那条消息带 @全体成员"
            return "degraded", (
                "回读发现 @全体 被协议端降级成了纯文本（群友不会收到提醒）："
                "多半是机器人不是群主/管理员，或本账号 24 小时的 @全体 次数已用完"
            )
        return "unknown", "回读没认领到刚发出的那条消息，@全体 是否生效未确认"

    async def _confirm_at_all(self, session: str, item: dict[str, Any]):
        """发送成功后确认 @全体 是否真的生效（at_all_verify 关闭时只记一笔发出）。"""
        if not bool(self.config.get("at_all_verify", True)):
            self._note_at_all("sent", "已带 @全体 发出（未开启回读确认）", session, log=False)
            return
        state, detail = await self._verify_at_all(session, item)
        item["at_all_state"] = state
        self._note_at_all(state, detail, session, log=(state != "ok"))

    async def _send_rich_media(
        self,
        session: str,
        item: dict[str, Any],
        chain: MessageChain,
        timeout_seconds: int,
    ) -> tuple[str, MessageChain]:
        """向单个会话发送条目，命中协议端富媒体拒收时当场摘段补发。

        返回 (结果, 最后一版实际发出去的链)，结果取 "sent"（送达）/ "unknown"
        （超时，结果未知，不重发以防群里重复）/ "failed"（确定没送达）。
        摘段补发能立刻做是因为协议端明确回了失败、消息确定没进群；返回的链给调用方
        复用，本轮后续会话不必再拿同一条注定被拒的链各撞一次。
        @全体成员在发送环节按会话包装进链首（_with_at_all）：群聊且满足条件才带，
        发送失败时去掉 @全体补发一次正文，不让机器人权限这类问题拖死整条推送。
        带 at 段之前先过 _at_all_gate（群主/管理员 + 24 小时频次），没过就不带，
        并在面板与日志里写明原因——QQ 对无权限的 at 段不会报错，只会把它降级成
        纯文本 "@全体成员"，那正是"发了却没有提醒"的现场；发出去之后再用
        _confirm_at_all 回读群消息，把"到底生效没有"落成一句话。
        """
        at_all_skipped = False
        at_all_allowed = False
        # 配置命中只说明"想 @";真能不能 @ 到全体由闸门判定（权限 + 频次），
        # 没过就不带 at 段——发出去也只会被 QQ 降级成一条纯文本 "@全体成员"。
        # 私聊会话根本不进闸门、也不记账：@全体 只对群聊有意义，把它记成
        # "skipped（未发送）"会让面板的"上次结果"被一条与群无关的推送刷掉，
        # 日志里还会每半小时冒一条并不存在的故障
        if self._at_all_hit(item) and self._parse_umo(session)[1] == "GroupMessage":
            at_all_allowed, why = await self._at_all_gate(session)
            if not at_all_allowed:
                self._note_at_all("skipped", why, session)
        while True:
            send_chain = chain
            at_all_wrapped = False
            try:
                if not at_all_skipped and at_all_allowed:
                    wrapped = self._with_at_all(session, item, chain)
                    if wrapped is not chain:
                        send_chain = wrapped
                        at_all_wrapped = True
                ok = await self._send_with_timeout(
                    session, send_chain, timeout_seconds=timeout_seconds
                )
                if ok:
                    if at_all_wrapped:
                        # 真带出门了才占频次；随后回读确认 at 段有没有被降级。
                        # 这两步都是纯诊断：任何异常都不能漏到下面的 except 里——
                        # 那会被当成"发送失败"，去掉 @ 段再发一次，群里就重复了
                        try:
                            self._at_all_mark_sent()
                            await self._confirm_at_all(session, item)
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.info(
                                f"{PLUGIN_NAME} 确认 @全体 投递结果出错"
                                f"（不影响已送达的推送）: {e!r}"
                            )
                    return "sent", chain
                return "failed", chain
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return "unknown", chain
            except Exception as e:
                seg = self._strip_rejected_segment(item) if self._is_media_rejected(e) else ""
                if not seg:
                    if at_all_wrapped and not at_all_skipped:
                        # @全体发不出去（机器人不是群管理员、撞了 QQ 每日 @全体
                        # 频次限制等）：去掉 AtAll 补发一次，正文保底出群。只
                        # 影响本会话本次尝试，不落任何标记——其它群/下一轮还会
                        # 按配置再试 @全体
                        at_all_skipped = True
                        logger.warning(
                            f"推送到 {session} 失败: {e!r}；@全体成员未送出，"
                            f"去掉 @全体补发微博 {item['post_id']} 的正文"
                        )
                        self._note_at_all(
                            "failed",
                            f"@全体 被协议端拒收（{self._reject_digest(e)}），"
                            f"已去掉 @全体补发微博 {item['post_id']} 的正文",
                            session,
                            log=False,
                        )
                        continue
                    logger.warning(f"推送到 {session} 失败: {e!r}")
                    return "failed", chain
                # 回执与体量一并留下：这条日志（错误留存面板）是现场唯一的证据，
                # 只说"视频段被拒收"看不出是体积问题还是协议端自己的毛病
                note = self._note_reject(item, seg, e)
                logger.warning(
                    f"推送到 {session} 失败：{seg}段被协议端拒收（{note}），"
                    f"已去掉{seg}重发微博 {item['post_id']} 的正文"
                )
                # 视频段被拒：QQ 视频消息这条路走不通了，改走群文件（另一条上传
                # 通道）把视频本身送进群，再重建链发正文——群里先拿到视频，再拿到
                # 微博正文，视频不会再因为协议端的富媒体通道坏掉而整条丢掉
                if seg == "视频":
                    await self._fallback_video_to_file(item)
                chain = await self._build_chain(item)

    def _pending_discard(self, item: dict[str, Any], *, drop_media: bool = True):
        """按对象身份从待推送队列移除一条（内容相同的两条互不误伤）。

        已下载的视频临时文件默认一并删除：条目离队的三种出口（已推送/放弃/过期）
        都不再需要它。面板手动取消时传 drop_media=False——条目只是暂时离开队列，
        撤销窗口内要能原样放回来重推，把媒体删了等于"恢复"后必然重下（直链多半
        也已过窗），所以那段时间文件留着，真正过了撤销窗口才删。
        """
        for i, it in enumerate(self.pending):
            if it is item:
                del self.pending[i]
                if drop_media:
                    self._drop_pending_media(item)
                return

    @staticmethod
    def _drop_pending_media(item: dict[str, Any]):
        """删掉条目已下载的视频临时文件（条目彻底离队时才调）。"""
        for f in item.get("video_files") or []:
            Path(str(f)).unlink(missing_ok=True)

    def _pending_find(self, post_id: str) -> dict[str, Any] | None:
        key = str(post_id or "")
        return next(
            (it for it in self.pending if str(it.get("post_id") or "") == key),
            None,
        )

    def _cancel_prune(self) -> bool:
        """清掉已过撤销窗口的取消条目（连同媒体临时文件），返回是否有变化。

        窗口外的条目没有任何办法再恢复，留着只会让 state.json 越积越大；媒体
        文件也要在这时候真正删掉——取消时特意留着的，就只有这一段窗口。
        """
        now = time.time()
        keep = [
            e
            for e in self.cancelled
            if now - float(e.get("cancelled_ts") or 0.0) <= PUSH_CANCEL_UNDO_SECONDS
        ]
        if len(keep) > PUSH_CANCEL_KEEP:
            keep = keep[-PUSH_CANCEL_KEEP:]
        keep_ids = {id(e) for e in keep}
        dropped = [e for e in self.cancelled if id(e) not in keep_ids]
        for entry in dropped:
            self._drop_pending_media(entry)
        self.cancelled = keep
        return bool(dropped)

    def _finalize_cancel(self, item: dict[str, Any]) -> bool:
        """把一条已标记取消的条目真正出队并放进保留区；已处理过则返回 False。

        正在发送中（_send_active）的条目要等它的会话循环先收尾再来这里，否则
        发送协程还在写 item / 往群里发，这边已经把条目搬走了。
        """
        if item.get("_cancel_done"):
            return False
        item["_cancel_done"] = True
        was_sending = bool(item.pop("_send_active", False)) or bool(item.get("last_send_ts"))
        self._pending_discard(item, drop_media=False)
        item.pop("_cancel_requested", None)
        item["cancelled_ts"] = time.time()
        item["cancel_was_sending"] = was_sending
        self.cancelled.append(item)
        self._cancel_prune()
        uid = str(item.get("uid") or "")
        name = self._account_name(uid)
        detail = (
            "面板手动取消推送（取消时该条正在发送，已交给协议端的那一次收不回来，"
            "群里可能已经收到；本条不再重发）"
            if was_sending
            else f"面板手动取消推送（本条不再重发；{PUSH_CANCEL_UNDO_SECONDS / 60:.0f} 分钟内"
            "可在面板恢复）"
        )
        self._record_event(
            "push_cancel", uid=uid, name=name, text=str(item.get("text") or ""), detail=detail
        )
        if was_sending:
            # 发送中取消是唯一"动作与群里结果可能不一致"的情形，日志留一条便于对账
            logger.warning(
                f"面板取消推送：微博 {item.get('post_id')} 取消时正在发送，"
                "该次发送无法撤回，群里可能已收到"
            )
        else:
            logger.info(f"面板取消推送：微博 {item.get('post_id')} 已出队，未发送")
        return True

    def cancel_pending(self, post_id: str) -> tuple[bool, str]:
        """面板「取消」：把一条待推送微博摘出队列，进可撤销保留区。

        正在发送的条目不能立刻搬走（发送协程还在用它），只打标记，由
        _flush_pending 在它的会话循环收尾后处置。
        """
        item = self._pending_find(post_id)
        if item is None:
            return False, "该条目已不在待推送队列（可能刚推送成功、已超时或被其它操作处理）"
        if item.get("_cancel_requested"):
            return False, "该条目已在取消中"
        item["_cancel_requested"] = True
        undo_min = PUSH_CANCEL_UNDO_SECONDS / 60
        if item.get("_send_active"):
            return (
                True,
                "已请求取消：该条正在发送，会在本次发送返回后立即取消（已发出的部分收不回）",
            )
        self._finalize_cancel(item)
        self._save_state()
        name = self._account_name(str(item.get("uid") or ""))
        return True, f"已取消推送：{name} 的这条微博不再重发，{undo_min:.0f} 分钟内可恢复"

    def restore_pending(self, post_id: str) -> tuple[bool, str]:
        """面板「恢复」：把撤销窗口内的取消条目放回队列尾部。

        保留取消时的重试进度（撤销不是重置）：取消多半正是发生在"重试快烧完"的
        时刻，若在这里清零，恢复就变成绕开重试上限的通道，一条永远发不出去的
        微博可以被无限重投。进度原样带回去，本轮收尾动作与没取消过完全一致。
        """
        key = str(post_id or "")
        entry = next((e for e in self.cancelled if str(e.get("post_id") or "") == key), None)
        if entry is None:
            return False, "该条目已不可恢复（超出撤销窗口已被清理，或本就未被取消）"
        self.cancelled = [e for e in self.cancelled if e is not entry]
        entry.pop("cancelled_ts", None)
        entry.pop("cancel_was_sending", None)
        entry.pop("_cancel_done", None)
        self.pending.append(entry)
        if len(self.pending) > PENDING_HARD_LIMIT:
            self.pending = self.pending[-PENDING_HARD_LIMIT:]
        uid = str(entry.get("uid") or "")
        name = self._account_name(uid)
        self._record_event(
            "push_restore",
            uid=uid,
            name=name,
            text=str(entry.get("text") or ""),
            detail=(
                f"面板恢复已取消的推送（重试进度保留在 {int(entry.get('retries') or 0)} 次，"
                "本轮按原口径继续处理）"
            ),
        )
        self._save_state()
        logger.info(f"面板恢复已取消的推送：微博 {key}")
        return True, f"已恢复推送：{name} 的这条微博回到队列，重试进度保持不变"

    def stop_push_round(self) -> tuple[bool, str]:
        """面板「停止本轮推送」：让正在进行的 _flush_pending 立即收尾。

        只作用于本轮：已处理的条目维持各自结局，未处理的原样留在队列里，且
        不消耗重试次数（叫停不是失败）。下一轮轮询会照常重试它们。
        """
        if not self._push_round_active:
            return (
                False,
                "当前没有正在进行的推送轮次；队列里的条目会在下一轮检查时重试，"
                "若想让某条不再重发请用该行的「取消」",
            )
        self._push_round_stop = True
        logger.warning("面板停止本轮推送：本轮剩余条目不再发送，留待下一轮检查")
        return True, "已停止本轮推送：本轮剩余条目保持原样，下一轮检查时继续"

    @staticmethod
    def push_cancel_undo_seconds() -> float:
        """取消后的可撤销窗口（秒）。面板要展示"还剩几分钟可恢复"，
        窗口长度只在这一处定义，避免面板按自己的常量估算出对不上的剩余时间。"""
        return PUSH_CANCEL_UNDO_SECONDS

    def _bg_task_done(self, tasks: set, label: str):
        """后台任务的统一收尾回调：出集合 + 未捕获异常记入错误留存。

        后台任务里漏接的异常以前只会随 Task 对象静默蒸发（除非 GC 时才打一条
        没上下文的日志），服务器出事后无从追查；这里兜住并走 logger.error，
        由错误留存 Handler 落盘。
        """

        def _cb(task: asyncio.Task) -> None:
            tasks.discard(task)
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error(f"{label}后台任务未捕获异常退出: {exc!r}")

        return _cb

    def _mark_sending(self, item: dict[str, Any]) -> None:
        """发送前打"已开始发送"时间戳；视频条目立即落盘。

        条目要等发送成功返回才出队，而这中间的窗口里进程崩溃/插件重载的话，
        state.json 里还留着它——不打戳，重启后会原样重发，群里就是两条一样
        的消息。视频条目的发送窗口可达分钟级且正是内存吃紧的时段，是重发
        事故的主体，所以只有它强制立即落盘；纯图文发送快，随下一次防抖落盘。
        打过戳的条目在重启/重载后按"结果未知"跳过重发（见 _load_state）。
        """
        if not item.get("last_send_ts"):
            item["last_send_ts"] = time.time()
            if item.get("video_files"):
                self._save_state_debounced()

    async def _flush_pending(self) -> bool:
        """把待推送队列发送到所有绑定的会话，返回本轮是否有需落盘的状态变化。

        已发送/放弃/过期的条目在处理到它时立即从内存队列移除：插件重载
        （WebUI 保存配置即触发）或进程崩溃打断批量推送时，已发出的微博不会
        因为还留在 state.json 的 pending 里而在重启后重发一遍。落盘按
        STATE_SAVE_DEBOUNCE 合并：state.json 是全量序列化，积压批量推送时
        逐条落盘是纯写放大——首批照常立即落盘，之后隔一段合并一次，崩溃
        安全窗口最多放宽一个合并间隔。

        面板可以在这中间介入（见「面板手动取消推送」一节）：
        · 单条取消：条目在条目边界被搬进"可撤销保留区"；正在发送的条目要等它
          的会话循环收尾（取消时已经发出去的会话收不回来，按真实结果记账）；
        · 停止本轮：循环在条目之间收尾，剩余条目原样留队，不消耗重试次数。

        max_push_per_round（0 不限）限制的是本轮真正发送出去的条数：博主密集
        发博或翻页补漏积压时防刷屏、降低触发 QQ 风控的概率。取消/过期/重试
        超限的善后与视频预下载都不占配额——只挡"新发送"，不挡队列治理。
        """
        max_retries = self._int_cfg("max_pending_retries", 5)
        delay = self._int_cfg("push_delay_seconds", 2)
        max_push = self._int_cfg("max_push_per_round", 0)
        pushed_this_round = 0
        sessions = self._sessions()
        # 快照：循环中会从 self.pending 移除已完成条目。面板取消的条目同样会从
        # self.pending 消失，这里靠 _cancel_done / _cancel_requested 两个标记认出
        # 它们，避免对同一条重复处置
        items = list(self.pending)
        total = len(items)
        changed = False
        last_save = float("-inf")
        # 过了撤销窗口的取消条目在这里清理（它们的媒体临时文件也是此时才真正删除）
        if self._cancel_prune():
            changed = True
        self._push_round_active = True
        self._push_round_stop = False
        try:
            for idx, item in enumerate(items):
                if self._push_round_stop:
                    logger.info(
                        f"本轮推送被面板停止，剩余 {total - idx} 条留待下一轮，不消耗重试次数"
                    )
                    changed = True
                    break
                if item.get("_cancel_done"):
                    continue  # 面板已取消并处置完毕，别再动它
                if item.get("_cancel_requested"):
                    # 走到这里说明它没在发送中（发送中的条目由下面的会话循环收尾），
                    # 直接搬进保留区
                    if self._finalize_cancel(item):
                        changed = True
                        self._save_state()
                    continue
                if self._is_expired(item.get("created_ts", 0.0)):
                    logger.info(f"待推送微博 {item['post_id']} 已超过时效上限，自动清除")
                    self._pending_discard(item)
                    changed = True
                    continue
                if not sessions:
                    # 未绑定推送目标：保留消息但不消耗重试次数
                    continue
                if item.get("retries", 0) >= max_retries:
                    retries = item.get("retries", 0)
                    uid, text = str(item.get("uid") or ""), str(item.get("text") or "")
                    logger.warning(
                        f"推送重试 {retries} 轮超限，微博 {item['post_id']} 停止整条重发"
                    )
                    delivered, unknown = await self._last_chance_flush(item, sessions)
                    # 群文件兜底就在上面这次调用里做的：记账要连它一起说清楚
                    file_note = self._video_file_summary(item)
                    tail = f"，{file_note}" if file_note else ""
                    if delivered:
                        # 降级链确实发出去了：记账必须跟着结果走，否则会留下
                        # "面板写着放弃、群里却有这条微博"的倒挂
                        self._record_push(
                            uid,
                            True,
                            text,
                            f"整条重试 {retries} 轮无果，去视频降级后推送到 "
                            f"{len(delivered)} 个会话{tail}",
                        )
                    elif unknown:
                        self._record_push(
                            uid,
                            False,
                            text,
                            f"整条重试 {retries} 轮无果，去视频降级仍发送超时、"
                            f"结果未知未重发{tail}；如群里没收到请点原帖链接",
                        )
                    else:
                        self._record_push(
                            uid, False, text, f"重试 {retries} 轮仍失败，放弃推送{tail}"
                        )
                    if delivered or unknown:
                        self._dispatch_album_uploads(item, delivered + unknown)
                        # 降级补发同样是真实出群的消息：计入本轮推送配额
                        pushed_this_round += 1
                    self._pending_discard(item)
                    changed = True
                    continue
                if not self._video_ready(item):
                    # 视频还在后台下载：本轮跳过，下轮再发。不消耗重试次数——
                    # 下载不是失败，烧完重试配额会把已经下好的视频一起弄丢
                    self._ensure_video_prepare(item)
                    continue
                if max_push > 0 and pushed_this_round >= max_push:
                    # 单轮推送上限：放在视频就绪检查之后——预下载不占配额照常
                    # 进行，只挡真正的发送；剩余条目原样留队，不消耗重试次数
                    logger.info(
                        f"本轮已推送 {pushed_this_round} 条，达到 max_push_per_round"
                        f"（{max_push}）上限，剩余 {total - idx} 条留待下一轮"
                    )
                    break
                sent_sessions: list[str] = []
                timed_out_sessions: list[str] = []
                if (
                    self._video_prefer_file()
                    and self._video_file_fallback_enabled()
                    and self._video_carried(item)
                ):
                    # 文件优先（video_prefer_file）：视频消息这条路在 rich media
                    # transfer failed 常态化的协议端上每条都要白撞一次拒收，配置
                    # 直选文件就跳过它，先走文件服务把视频送进全部目标群，正文按
                    # "已去视频"的链发送。video_rejected 让链构建、超时计算与
                    # 拒收降级同路；video_file_first 留证措辞用——是配置直选，
                    # 不是被拒收，面板与记账不能写成"协议端拒收"
                    item["video_file_first"] = True
                    item["video_rejected"] = True
                    await self._fallback_video_to_file(item)
                chain = await self._build_chain(item)
                send_timeout = self._send_timeout_for(item)
                self._mark_sending(item)
                # 打上"正在发送"标记：面板此刻点取消不能直接把条目搬走（发送协程
                # 还在用它、还在往群里发），只能打 _cancel_requested，由下面收尾
                item["_send_active"] = True
                try:
                    for session in sessions:
                        if item.get("_cancel_requested"):
                            # 正在发送时被取消：不再往后面的会话发（已发出的收不回）
                            break
                        status, chain = await self._send_rich_media(
                            session, item, chain, send_timeout
                        )
                        if status == "sent":
                            sent_sessions.append(session)
                        elif status == "unknown":
                            # 超时不代表没发出去：大载荷最常见的就是"实际已送达但判超时"
                            # （九图、视频都撞过），帧早已交出去，协议端慢慢传而已。
                            # 结果未知时该会话不再重发——盲重发就是群里同一条消息出现
                            # 两遍；其余会话继续逐个尝试，互不连坐。
                            timed_out_sessions.append(session)
                            logger.warning(
                                f"推送到 {session} 超过 {send_timeout} 秒未返回，结果未知："
                                f"微博 {item['post_id']} 对该会话不再重发（防重复）"
                            )
                finally:
                    item.pop("_send_active", None)
                cancel_late = bool(item.get("_cancel_requested"))
                if cancel_late and not sent_sessions and not timed_out_sessions:
                    # 一条都没有出去（也没有结果未知的会话）：取消成立，进可撤销保留区。
                    # 有 sent/timed_out 的场合不能进保留区——恢复会把它再发给全部
                    # 会话，群里就重复了，所以那种情况落到下面的正常记账里
                    self._finalize_cancel(item)
                    changed = True
                    self._save_state()
                    continue
                if cancel_late:
                    item.pop("_cancel_requested", None)
                    item["_cancel_done"] = True
                if sent_sessions:
                    self._pending_discard(item)
                    pushed_this_round += 1
                    logger.info(f"已推送微博 {item['post_id']}")
                    # 摘段标记可能来自本轮的补发，也可能来自更早的轮次（摘段后
                    # 下一轮才发出去），记账口径一致
                    reason = self._reject_reason(item)
                    self._record_push(
                        str(item.get("uid") or ""),
                        True,
                        str(item.get("text") or ""),
                        f"推送到 {len(sent_sessions)} 个会话"
                        + (
                            f"，另 {len(timed_out_sessions)} 个会话超时结果未知未重发"
                            if timed_out_sessions
                            else ""
                        )
                        + (f"，{reason}" if reason else "")
                        + ("；面板取消来晚了一步，已送达的部分无法撤回" if cancel_late else ""),
                    )
                    # 超时的会话多半其实已送达，相册上传有自己的台账去重，一并派发
                    self._dispatch_album_uploads(item, sent_sessions + timed_out_sessions)
                    changed = True
                    now_mono = time.monotonic()
                    if now_mono - last_save >= STATE_SAVE_DEBOUNCE:
                        self._save_state()
                        last_save = now_mono
                elif timed_out_sessions:
                    self._pending_discard(item)
                    # 结果未知多半是实际已送达：按占用了发送配额计，防大载荷
                    # 超时后本轮又源源不断发出后续条目
                    pushed_this_round += 1
                    self._record_push(
                        str(item.get("uid") or ""),
                        False,
                        str(item.get("text") or ""),
                        f"发送超时（{send_timeout}s）结果未知，未重发以防重复；"
                        "如群里没收到请点原帖链接",
                    )
                    self._dispatch_album_uploads(item, timed_out_sessions)
                    changed = True
                else:
                    item["retries"] = item.get("retries", 0) + 1
                    changed = True
                # 多条之间留间隔防刷屏；最后一条之后无需再等
                if delay and idx < total - 1:
                    await asyncio.sleep(delay)
            if not sessions and len(self.pending) > PENDING_HARD_LIMIT:
                self.pending = self.pending[-PENDING_HARD_LIMIT:]
                changed = True
            return changed
        finally:
            # 标志必须收干净：异常退出时若还留着 active，面板会一直以为"本轮在跑"，
            # 「停止本轮」永远指向一个不存在的轮次
            self._push_round_active = False
            self._push_round_stop = False

    async def _last_chance_flush(
        self, item: dict[str, Any], sessions: list[str]
    ) -> tuple[list[str], list[str]]:
        """重试超限放弃前的最后一搏：带视频的条目去掉视频段（正文+图+降级提示）
        再各发一次，返回 (送达会话, 超时结果未知会话)。

        带毒的视频段（超 QQ 硬限、协议端不支持等）会把微博正文一起拖死——整条
        消息在每一轮都失败，一条都出不去。视频段已经被拒收过（video_rejected）时
        这里没有可摘的了，直接跳过，不再把同样的链多发一遍。
        """
        delivered: list[str] = []
        unknown: list[str] = []
        if not sessions or not self._video_carried(item):
            return delivered, unknown
        # 视频消息整条发不出去（反复超时/协议端拒绝）时，先把视频改传群文件：
        # 视频消息这条路已经证明走不通，群文件是另一条上传通道，让"至少能在群里
        # 拿到视频"这条底线成立，再发正文降级链
        await self._fallback_video_to_file(item)
        self._mark_sending(item)
        try:
            chain = await self._build_chain(item, with_video=False)
        except Exception as e:
            logger.warning(f"微博 {item['post_id']} 降级消息构建失败: {e!r}")
            return delivered, unknown
        for session in sessions:
            try:
                if await self._send_with_timeout(
                    session, chain, timeout_seconds=self._send_timeout_for(item)
                ):
                    delivered.append(session)
                    logger.info(
                        f"微博 {item['post_id']} 视频段多次发送失败，已去视频降级重发到 {session}"
                    )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                unknown.append(session)
                logger.warning(f"微博 {item['post_id']} 降级重发到 {session} 超时，结果未知")
            except Exception as e:
                logger.warning(f"微博 {item['post_id']} 降级重发到 {session} 仍失败: {e!r}")
        return delivered, unknown

    def _videos_enabled(self) -> bool:
        return bool(self.config.get("message_with_videos", True))

    def _video_tmp_root(self) -> Path:
        assert self._data_dir is not None
        return self._data_dir / VIDEO_TMP_DIR

    def _wipe_video_tmp(self):
        """清掉上次运行残留的视频临时文件（正常流程发完即删，这里只兜底）。"""
        shutil.rmtree(self._video_tmp_root(), ignore_errors=True)

    @staticmethod
    def _callback_api_base() -> str:
        if astrbot_config is None:
            return ""
        try:
            return str(astrbot_config.get("callback_api_base") or "").strip()
        except Exception:
            return ""

    def _video_file_fallback_enabled(self) -> bool:
        return bool(self.config.get("video_file_fallback", True))

    def _video_prefer_file(self) -> bool:
        """视频是否跳过 QQ 视频消息、直接以文件发送（video_prefer_file，默认开）。

        默认开：本插件面对的部署现场里富媒体通道对视频消息并不可靠——4~5MB
        的小视频也稳定回 `rich media transfer failed`（NapCat 版本回归、账号
        风控都可能），每条视频都要白撞一次拒收才轮到文件兜底，不如发送前直接
        走文件服务（落点由 video_file_temp 决定）。依赖 video_file_fallback
        开启——关着文件兜底就没有可走的路，本项不生效，仍按视频消息发送。
        富媒体通道可靠、想要"点开就播"视频消息的部署把本项关掉。
        """
        return bool(self.config.get("video_prefer_file", True))

    def _video_file_temp(self) -> bool:
        """兜底上传是否走"临时（在线）文件"而不是落进群文件永久空间。

        默认 True：临时文件不占群文件的 10GB / 1500 个配额，代价是成员要手动接收
        （未接收/未转存的话服务器中转只留一段时间，QQ 未公开精确期限）。想让视频
        在群文件里长期留存、可在线播放的部署把它关掉即可（那就要吃配额，空间满
        时 QQ 会把文件降级成临时件，NapCat 还可能直接报错，见 README）。
        """
        return bool(self.config.get("video_file_temp", True))

    @staticmethod
    def _video_file_name(post_id: str, idx: int, total: int) -> str:
        """文件名：带微博 id，群里一眼能对上原帖，重名也不会互相覆盖。

        落进群文件时它按名字展示，作为文件消息时它也是成员看到的文件名；用
        post_id 而不是博主昵称/正文：昵称与正文可能带斜杠、冒号这类在文件系统里
        非法的字符，也更容易撞重名。
        """
        return f"微博_{post_id}{'' if total <= 1 else f'_{idx}'}.mp4"

    @staticmethod
    def _video_payload(raw: bytes) -> str:
        """Compatibility wrapper around the shared NapCat base64 encoder."""
        return b64_payload(raw)

    async def _upload_video_files(self, session: str, item: dict[str, Any]) -> str:
        """把本条微博的视频改传成文件发进群，返回空串表示成功，否则返回失败原因。

        QQ 的文件走的是**文件服务**，与图片/视频消息用的富媒体通道不是一条：
        协议端回 `rich media transfer failed` 时富媒体那条路走不通，这条路往往
        还通——这是"视频消息发不出去"时唯一还能把视频本身送进群的路子。

        落点由 video_file_temp 决定（NapCat 的 `upload_file` 开关）：
        - 开（默认）：`upload_file=false` ⇒ 只发一条文件消息、**不落进群文件永久
          空间**（在线/临时文件，不占 10GB 配额，成员需接收/转存，过期即失效）；
        - 关：不带该参数（NapCat 默认 true）⇒ 落进群文件永久空间，可在线播放、
          长期留存，但吃配额。

        载荷用 base64 内嵌而不是本地路径：插件写不出 NapCat 那台机器上的路径
        （分离部署是常态，裸路径过去就是 ENOENT），base64 与相册上传同一条路，
        不依赖任何路径映射（NapCat 收到 base64 会自己落一个临时文件再走文件服务）。
        体积按 video_inline_max_mb 封顶——这条路固定走 base64，同样有"文件体积
        5~6 倍瞬时内存"的问题，小内存服务器不能被兜底反噬（超帽就如实报因，群里
        退回提示链接，正文不受影响）。读进来的原始字节编码完立刻释放，别让同一段
        视频在内存里同时留两份。
        """
        platform, msg_type, gid = self._parse_umo(session)
        if msg_type != "GroupMessage" or not gid:
            return "目标不是群会话，文件无处可发"
        files = [Path(str(f)) for f in (item.get("video_files") or []) if f]
        files = [p for p in files if p.is_file()]
        if not files:
            return "本地视频文件已不在（临时目录被清过或已发完删除）"
        cap_mb = self._int_cfg("video_inline_max_mb", VIDEO_INLINE_MB_DEFAULT)
        try:
            nc = await self._album_client(platform)
        except NapCatError as e:
            return f"拿不到协议端连接：{e}"
        post_id = str(item.get("post_id") or "")
        total = len(files)
        for idx, path in enumerate(files, 1):
            if cap_mb > 0:
                try:
                    size_mb = (await asyncio.to_thread(path.stat)).st_size / 1048576
                except OSError as e:
                    return f"读取视频大小失败：{e}"
                if size_mb > cap_mb:
                    return (
                        f"视频 {size_mb:.0f}MB 超过 base64 内嵌上限 {cap_mb}MB，"
                        "文件兜底跳过（这条路固定走 base64 内嵌）"
                    )
            try:
                raw = await asyncio.to_thread(path.read_bytes)
            except OSError as e:
                return f"读取视频失败：{e}"
            payload = await asyncio.to_thread(self._video_payload, raw)
            del raw  # 载荷一路上还要被 JSON 序列化再复制几份，原始字节不留
            # NapCat 的 upload_file=false ⇒ 只发文件消息、不落进群文件永久空间
            # （临时/在线文件，不占配额）；想让视频长期留在群文件里就不带这个参数，
            # 走 NapCat 的默认行为（true）。旧版 NapCat 不认识该字段时会忽略它、
            # 退回"进群文件"，属于安全降级
            extra = {"upload_file": False} if self._video_file_temp() else {}
            try:
                await asyncio.wait_for(
                    nc.call(
                        "upload_group_file",
                        idempotent=False,
                        group_id=str(gid),
                        file=payload,
                        name=self._video_file_name(post_id, idx, total),
                        **extra,
                    ),
                    timeout=VIDEO_FILE_UPLOAD_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return f"上传超过 {VIDEO_FILE_UPLOAD_TIMEOUT:.0f} 秒未返回，结果未知"
            except Exception as e:
                return f"{e}"
        return ""

    async def _fallback_video_to_file(self, item: dict[str, Any]) -> None:
        """视频段被协议端拒收后，把视频改以文件发出去；覆盖所有推送目标，每个群一次。

        必须一次覆盖**全部**目标群：摘掉视频段后的链会被本轮后续会话复用，后续
        会话不会再撞一次拒收——只给"第一个拒收的群"传文件，其余群就只能收到正文。
        结果记在条目上（video_file_sessions / video_file_fail）供面板与记账用；
        任何失败都不上抛：兜底失败不该把正文一起拖死，正文该照常送达。
        """
        if not self._video_file_fallback_enabled():
            return
        done = [str(s) for s in (item.get("video_file_sessions") or [])]
        for session in self._sessions():
            if session in done:
                continue
            err = await self._upload_video_files(session, item)
            if err:
                item["video_file_fail"] = err
                logger.warning(f"微博 {item.get('post_id')} 视频改发文件失败（{session}）：{err}")
                continue
            done.append(session)
            item["video_file_sessions"] = done
            item.pop("video_file_fail", None)
            kind = "临时文件" if self._video_file_temp() else "群文件"
            logger.warning(
                f"微博 {item.get('post_id')} 视频消息被协议端拒收，已改以{kind}发送到 {session}"
            )

    def _video_file_note(self, item: dict[str, Any]) -> str:
        """这条链尾该附哪句视频说明：全部目标群都送到位了才说"已改以文件发送"。

        推送目标可能有多个群，而链只有一条（所有会话共用）——只要有一个群没传成
        就退回保守的"请点原帖链接"，不谎报（哪个群失败在日志与面板里都能看到）。
        说明本身按落点分：临时文件不会进群文件列表，不能让成员去群文件里找。
        """
        sent = {str(s) for s in (item.get("video_file_sessions") or [])}
        targets = set(self._sessions())
        if sent and targets and targets <= sent:
            if item.get("video_file_first"):
                # 配置直选文件：没有"改以"的过程，措辞与拒收降级分开
                return (
                    VIDEO_FILE_FIRST_TEMP_NOTE
                    if self._video_file_temp()
                    else VIDEO_FILE_FIRST_SENT_NOTE
                )
            return VIDEO_FILE_TEMP_NOTE if self._video_file_temp() else VIDEO_FILE_SENT_NOTE
        return VIDEO_FALLBACK_NOTE

    def _video_file_summary(self, item: dict[str, Any]) -> str:
        """文件兜底结果的一句话摘要（记账与面板共用）。

        成功了就要说清楚"视频在群里拿得到、以什么形态拿到"（临时文件 vs 群文件），
        别让面板和群里一个说失败一个说成功；失败了也要带原因，否则用户不知道该去
        查权限还是查协议端。
        """
        sent = [str(s) for s in (item.get("video_file_sessions") or [])]
        if sent:
            kind = "临时文件" if self._video_file_temp() else "群文件"
            head = "视频以" if item.get("video_file_first") else "视频已改以"
            return f"{head}{kind}发送（{len(sent)} 个群）"
        err = str(item.get("video_file_fail") or "")
        if err:
            return f"文件兜底也失败：{err}"
        return ""

    def _video_inline_cap_mb(self) -> int:
        """base64 内嵌发送的体积上限（MB），0 表示不限制。

        配置了 callback_api_base 时走本地路径注册回调，没有内存放大，不受此限。
        """
        if self._callback_api_base():
            return 0
        return self._int_cfg("video_inline_max_mb", VIDEO_INLINE_MB_DEFAULT)

    def _video_download_cap_mb(self) -> int:
        """视频下载体积上限（MB），0 表示不限制。

        内嵌发送时不能下到 video_max_mb 才发现发不出去：下载帽取发送帽与
        video_max_mb 中更小的正数。超帽的视频在下载阶段就降级为提示链接，
        不浪费带宽也不占磁盘。
        """
        caps = [
            c
            for c in (
                self._int_cfg("video_max_mb", VIDEO_MAX_MB_DEFAULT),
                self._video_inline_cap_mb(),
            )
            if c > 0
        ]
        return min(caps) if caps else 0

    async def _video_segment(self, p: Path) -> Video | None:
        """构造视频段：默认 base64 内嵌，配置了 callback_api_base 才走本地路径。

        aiocqhttp 出站时 Image/Record 会被 AstrBot 自动转成 base64 内嵌，Video
        却把 file:// 路径原样透传——NapCat 与 AstrBot 分容器部署（互相看不到
        对方文件系统，Docker 分容器是常态）时，协议端打开这个路径必然 ENOENT，
        整条消息（含正文和图片）一起失败。base64 内嵌与图片同一条路，不依赖
        任何路径映射或额外配置；代价是发送瞬间内存放大约 1.33 倍，体积已被
        video_max_mb 封顶。配了 callback_api_base 时 to_dict() 会把非 http 的
        file 值交给 register_file 注册成回调下载地址，那条路只认本地文件，
        base64 串会炸，所以保持 fromFileSystem 让 AstrBot 自己去注册。

        超出内嵌上限（video_inline_max_mb）时返回 None，由 _build_chain 附降级
        提示而不是硬发：正常流程下载帽已拦住超大视频，这里兜的是"发送中途
        调小上限/旧状态文件残留大文件"的场合——硬发的内存尖峰会顶死小服务器。
        """
        if self._callback_api_base():
            return Video.fromFileSystem(path=str(p))
        cap_mb = self._video_inline_cap_mb()
        if cap_mb > 0:
            try:
                size_mb = p.stat().st_size / 1048576
            except OSError:
                size_mb = 0.0
            if size_mb > cap_mb:
                logger.warning(
                    f"视频 {p.name} {size_mb:.0f}MB 超过 base64 内嵌上限 {cap_mb}MB，"
                    "跳过视频段改发降级提示（防止内存尖峰顶死服务器）"
                )
                return None
        try:
            raw = await asyncio.to_thread(p.read_bytes)
            raw_b64 = await asyncio.to_thread(b64_payload, raw, prefix=False)
            del raw
        except OSError as e:
            # 读不动（文件刚被清掉 / 权限问题）：退回路径模式，不比旧行为差
            logger.warning(f"视频 {p.name} 读取失败，回退本地路径发送: {e!r}")
            return Video.fromFileSystem(path=str(p))
        return Video.fromBase64(raw_b64)

    def _video_ready(self, item: dict[str, Any]) -> bool:
        """条目的视频是否已就绪可发送：未开启 / 无视频 / 已下载 / 已降级 都算就绪。

        已下载的文件在磁盘上消失（重启清空 video_tmp）时清掉旧结论回到待下载，
        由后台任务重新下载——直链可能已过期，再失败就重新降级提示链接。
        """
        if not self._videos_enabled():
            return True
        files = [str(f) for f in item.get("video_files") or [] if f]
        if files:
            alive = [f for f in files if Path(f).is_file()]
            if len(alive) != len(files):
                item["video_files"] = alive
                files = alive
            if files:
                return True
            # 文件全部失效：清掉上一次的降级结论，按候选直链重新下载
            item["video_note"] = ""
        # 下载已有定论（全失败/部分失败都记了提示）就不再等后台任务
        if item.get("video_note"):
            return True
        # 没有本地文件也没有定论：还有候选直链就等下载，否则按无视频处理
        return not (item.get("videos") or [])

    def _ensure_video_prepare(self, item: dict[str, Any]):
        """为条目派发视频下载任务（同一微博同时只有一个，任务不阻塞轮询循环）。"""
        post_id = str(item.get("post_id") or "")
        if post_id in self._video_preparing:
            return
        self._video_preparing.add(post_id)
        task = asyncio.create_task(self._video_prepare(item))
        self._video_tasks.add(task)
        task.add_done_callback(self._bg_task_done(self._video_tasks, "视频下载"))

    async def _refresh_video_urls(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        """重新拉一次详情换一批新直链，写回条目并刷新 videos_ts。

        微博视频直链的签名实测一小时过窗，跨轮次重试或重启后条目里挂着的
        都是旧链，拿来下载必然 403。只在真要下载时花这一次请求。
        """
        pid = str(item.get("post_id") or "")
        prefer_hd = str(self.config.get("video_quality") or "hd") != "sd"
        cap_mb = self._video_download_cap_mb()
        max_bytes = cap_mb * 1048576 if cap_mb > 0 else 0
        detail = await self._fetch_status_detail(pid)
        if detail is None:
            return []
        seen: set[str] = set()
        videos = self._videos_from_mblog(detail, prefer_hd, seen, max_bytes)
        rt = detail.get("retweeted_status")
        if isinstance(rt, dict) and not videos:
            videos = self._videos_from_mblog(rt, prefer_hd, seen, max_bytes)
        cap = max(1, self._int_cfg("max_videos_per_post", 2))
        videos = videos[:cap]
        item["videos"] = videos
        item["videos_ts"] = time.time()
        return videos

    async def _download_video_entry(
        self,
        item: dict[str, Any],
        entry: dict[str, Any],
        idx: int,
        dest: Path,
        max_bytes: int | float,
    ) -> tuple[str, str, str]:
        """下载一个视频段，返回 (落盘路径, 最后一条失败原因, 失败性质)。

        候选按清晰度顺序逐个试，每个候选前先过出网白名单校验。响应头就声明
        超上限的档不读正文（MediaTooLarge），换下一档——42MB 的视频在上限 30MB
        时不必先烧完 42MB 带宽再丢。
        失败性质给上层决定下一步："stale"=直链失效（签名过窗，或 CDN 对同一连接
        的高频请求瞬时拒绝——实测同一条链稍后再请求就 200），重取直链再试有意义；
        "oversize"=所有候选都装不进上限，重取多少次都不会变，当场定论；
        ""=成功，或"换轮再试但不必重取直链"的瞬时故障。
        """
        post_id = str(item.get("post_id") or "")
        urls = [str(u) for u in (entry.get("urls") or []) if u]
        last = ""
        stale = False
        oversize = 0
        for url in urls:
            dest.unlink(missing_ok=True)
            try:
                await self._validate_media_url(url)
                await self._stream_to_file(
                    url, dest, max_bytes=max_bytes, timeout_total=VIDEO_DL_TIMEOUT
                )
                size = dest.stat().st_size if dest.is_file() else 0
                if size < 1024:
                    last = f"下载产物过小（{size}B）"
                else:
                    return str(dest), "", ""
            except asyncio.CancelledError:
                dest.unlink(missing_ok=True)
                raise
            except MediaTooLarge as e:
                last = str(e)
                oversize += 1
            except aiohttp.ClientResponseError as e:
                # raise_for_status 的非 2xx：状态码是"该不该重取直链"的判据
                if e.status in (401, 403, 404, 410):
                    stale = True
                last = f"HTTP {e.status}"
            except (
                WeiboFetchError,
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
            ) as e:
                last = f"{type(e).__name__}: {e}"[:160]
            dest.unlink(missing_ok=True)
            logger.info(f"微博 {post_id} 视频 {idx} 候选直链不可用，换下一候选: {last}")
        if oversize and oversize == len(urls):
            # 每个候选都是"装不下"，不是"下不动"：再等下一轮也不会有别的档
            return "", last or "无可用直链", "oversize"
        return "", last or "无可用直链", "stale" if stale else ""

    async def _video_prepare(self, item: dict[str, Any]) -> None:
        """后台下载条目视频到 video_tmp：成功填 video_files，失败按轮次收敛。

        与相册下载共用 _dl_gate 并发闸（微博媒体下载总并发不因视频新增一路）；
        下载前过磁盘保护线。条目里的直链过签名窗口就先重取，一轮没下成也不判死：
        每轮之间隔一个轮询间隔，最多 VIDEO_FETCH_MAX_ATTEMPTS 轮，耗尽（或彻底
        取不到档）才落"未能自动转发"提示，失败原因记在 video_fail 上供日志/面板看。
        下载结束回写 state.json，重启后不必重下；若条目已被丢弃（过期/放弃/重发），
        产物不留死角。
        """
        post_id = str(item.get("post_id") or "")
        entries = [v for v in (item.get("videos") or []) if isinstance(v, dict)]
        expected = len(entries)
        files: list[str] = []
        reasons: list[str] = []
        kinds: list[str] = []  # 每段失败的性质（oversize / stale / other）
        try:
            # 下载帽取 video_max_mb 与内嵌发送帽的较小正数：内嵌发送时下到
            # 95MB 才在发送阶段拒发，等于白烧一遍带宽还留了个发不出去的大文件
            cap_mb = self._video_download_cap_mb()
            max_bytes: int | float = cap_mb * 1048576 if cap_mb > 0 else float("inf")
            root = self._video_tmp_root()
            root.mkdir(parents=True, exist_ok=True)
            free_mb = (await asyncio.to_thread(lambda: shutil.disk_usage(root).free)) // 1048576
            if free_mb < DISK_FLOOR_MB:
                raise WeiboFetchError(
                    f"临时目录所在磁盘仅剩 {free_mb}MB"
                    f"（低于 {DISK_FLOOR_MB}MB 保护线），已取消视频下载"
                )
            if time.time() - float(
                item.get("videos_ts") or 0.0
            ) > VIDEO_URL_FRESH_SECONDS and await self._refresh_video_urls(item):
                entries = [v for v in (item.get("videos") or []) if isinstance(v, dict)]
                expected = max(expected, len(entries))
            stem_base = BAD_NAME_RE.sub("", post_id)[:48] or "video"
            gate = self._dl_gate()
            for idx, entry in enumerate(entries):
                dest = root / f"{stem_base}_{idx}.mp4"
                # 并发闸按"一档"为单位持有：一条大视频占着下载额度是设计如此
                # （微博媒体下载总并发不因视频新增一路），但重取直链也在闸内，
                # 不让一次 API 请求另开一路并发
                async with gate:
                    path, reason, outcome = await self._download_video_entry(
                        item, entry, idx, dest, max_bytes
                    )
                    if not path and outcome == "stale":
                        # 直链失效：换一批新链再试同一档（重取失败或新链也不行就收手）
                        fresh = await self._refresh_video_urls(item)
                        nxt = fresh[min(idx, len(fresh) - 1)] if fresh else None
                        if nxt is not None:
                            path, reason, outcome = await self._download_video_entry(
                                item, nxt, idx, dest, max_bytes
                            )
                if path:
                    files.append(path)
                else:
                    reasons.append(reason)
                    kinds.append(outcome or "other")
                    dest.unlink(missing_ok=True)
        except asyncio.CancelledError:
            for f in files:
                Path(f).unlink(missing_ok=True)
            raise
        except Exception as e:
            logger.warning(f"微博 {post_id} 视频下载异常: {e!r}")
            reasons.append(f"{type(e).__name__}: {e}"[:160])
            kinds.append("other")
        finally:
            self._video_preparing.discard(post_id)
            if all(queued is not item for queued in self.pending):
                # 条目已从队列消失（过期/放弃）：刚下载的文件一并清掉
                for f in files:
                    Path(f).unlink(missing_ok=True)
            else:
                item["video_files"] = files
                self._settle_video_attempt(item, expected, files, reasons, kinds)
                self._save_state_debounced()

    def _settle_video_attempt(
        self,
        item: dict[str, Any],
        expected: int,
        files: list[str],
        reasons: list[str],
        kinds: list[str] | None = None,
    ) -> None:
        """给这一轮的视频下载定结论：全数到手、还值得再试、还是彻底放弃。"""
        if expected and len(files) >= expected:
            item["video_fail"] = ""
            return
        kinds = kinds or []
        # 每一段都是"装不下"（超上限）：这是确定性结论，重取直链、多等几轮都不会
        # 变好，当场就给降级提示，不要空等 VIDEO_FETCH_MAX_ATTEMPTS 个轮询周期
        permanent = bool(kinds) and all(k == "oversize" for k in kinds)
        reason = (reasons[-1] if reasons else "无可用直链")[:200]
        item["video_fail"] = reason
        if files:
            # 部分段已到手：不会再有第二轮，缺的那段当场定成"部分未能转发"
            item["video_note"] = VIDEO_PARTIAL_NOTE
        elif not (item.get("videos") or []):
            # 重取后一档都不剩（详情接口失效、档位被撤）：再等也不会有链
            item["video_note"] = VIDEO_FALLBACK_NOTE
        else:
            attempts = int(item.get("video_attempts") or 0) + 1
            item["video_attempts"] = attempts
            if attempts < VIDEO_FETCH_MAX_ATTEMPTS and not permanent:
                # 还没到判死轮数：不写 note，_video_ready 保持"未就绪"，
                # 下一轮询周期会带着重取的直链再试一次
                return
            item["video_note"] = VIDEO_FALLBACK_NOTE
        logger.warning(
            f"微博 {item.get('post_id')} 视频未能转发（已试 "
            f"{int(item.get('video_attempts') or 1)} 轮，{len(files)}/{expected} 段到手）："
            f"{reason}"
        )
        self._record_event(
            "video_fail",
            uid=str(item.get("uid") or ""),
            text=str(item.get("text") or ""),
            detail=f"视频 {len(files)}/{expected} 段到手：{reason}",
        )
