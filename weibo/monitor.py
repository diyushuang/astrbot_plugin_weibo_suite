"""轮询、基线、漏博翻页、帖子解析与增量检测。"""

from __future__ import annotations

from .runtime import *


class MonitorMixin:
    @staticmethod
    def _is_pinned(card: dict[str, Any], mb: dict[str, Any]) -> bool:
        for node in (card, mb):
            with contextlib.suppress(TypeError, ValueError):
                if int(node.get("mblogtype") or 0) == 2:
                    return True
            if node.get("isTop") or node.get("is_top"):
                return True
        title = mb.get("title")
        return isinstance(title, dict) and title.get("text") == "置顶"

    def _parse_post(self, mb: dict[str, Any], fallback_uid: str) -> dict[str, Any] | None:
        pid = str(mb.get("id") or mb.get("mid") or "").strip()
        if not pid:
            return None
        user = mb.get("user") or {}
        uid = str(user.get("id") or fallback_uid)
        bid = str(mb.get("bid") or pid)
        limit = self._int_cfg("text_max_length", 100)
        created_raw = str(mb.get("created_at") or "")

        own_text = _clean_text(str(mb.get("text") or ""), limit)
        own_raw = str(mb.get("text") or "")
        orig_name, orig_text, orig_id = "", "", ""
        orig_raw = ""
        orig_is_long = False
        is_retweet = "retweeted_status" in mb
        rt_mb: dict[str, Any] | None = None
        if is_retweet:
            rt_mb = mb.get("retweeted_status") or {}
            rt_user = rt_mb.get("user") or {}
            orig_name = str(rt_user.get("screen_name") or "")
            orig_raw = str(rt_mb.get("text") or "")
            orig_text = _clean_text(orig_raw, limit)
            orig_id = str(rt_mb.get("id") or rt_mb.get("mid") or "")
            orig_is_long = bool(rt_mb.get("isLongText"))
        # 视频帖标志（实测 m.weibo.cn：纯视频微博 page_info.type == "video" 且无
        # pics 数组）。只用作相册上传跳过时的日志说明，是否上传始终以提取到的
        # 图片列表为准，避免未来出现"图文视频混排"时误跳过混排里的真图。
        pi = mb.get("page_info") if isinstance(mb.get("page_info"), dict) else {}
        # 原微博的 page_info 只查容器不查值会漏判 None（转发纯文字帖就撞上），
        # 与上一行同口径：取不到就当空 dict
        rt_pi = (
            rt_mb.get("page_info")
            if isinstance(rt_mb, dict) and isinstance(rt_mb.get("page_info"), dict)
            else {}
        )
        is_video = str(pi.get("type") or "") == "video" or str(rt_pi.get("type") or "") == "video"
        # 接口自述的图片总数：超 9 图微博时间线卡片的 pics 只携带部分图片时，
        # pic_num 仍是全量数——识别侧据此补一次详情拿全量，见 _ensure_full_pics。
        # 转发帖的图挂在原微博上，两处取大即真实总数
        pic_total = 0
        for node in (mb, rt_mb):
            with contextlib.suppress(TypeError, ValueError):
                pic_total = max(pic_total, int((node or {}).get("pic_num") or 0))

        return {
            "id": pid,
            "uid": uid,
            "author": str(user.get("screen_name") or "") or uid,
            "link": f"https://weibo.com/{uid}/{bid}",
            "time": _format_time(created_raw),
            "created_ts": _parse_created_epoch(created_raw),
            "text": own_text,
            # 清洗前的接口原文：归档库的 `text_raw` 列与"原文重清洗"都要它，
            # 只存清洗后的 `text` 会让这两条链路永久拿不到数据源。
            "raw_text": own_raw,
            "is_retweet": is_retweet,
            "orig_name": orig_name,
            "orig_text": orig_text,
            "orig_raw_text": orig_raw,
            "orig_id": orig_id,
            "orig_is_long": orig_is_long,
            # 本条是否超长（全文按需拉取时用，见 _apply_full_text）
            "is_long": bool(mb.get("isLongText")),
            "pics": _extract_pics(mb, rt_mb),
            "pic_total": pic_total,
            "is_video": is_video,
            # 卡片原始结构的引用（只在本次入队流程里用，不写进 state.json）：
            # 视频直链优先从这里取，省一次详情请求，见 _apply_video
            "video_src": (
                ([mb] + ([rt_mb] if isinstance(rt_mb, dict) else [])) if is_video else []
            ),
        }

    def _post_body(self, post: dict[str, Any]) -> str:
        """模板 {weibo} 占位符的正文内容。"""
        translate = bool(self.config.get("weibo_emoji_translate", False))
        if post["is_retweet"]:
            parts: list[str] = []
            if post["text"]:
                parts.append(post["text"])
            who = post["orig_name"] or "原作者"
            orig = post["orig_text"] or "（原微博内容不可见）"
            parts.append(f"转发 @{who}：{orig}")
            body = "\n".join(parts)
        else:
            body = post["text"] or ""
        return translate_weibo_emojis(body) if translate else body

    def _keyword_hit(self, post: dict[str, Any]) -> str | None:
        """关键词过滤：命中屏蔽词或被白名单排除时返回原因，否则 None。"""
        text = f"{post['text']}\n{post['orig_text']}"
        for kw in self.config.get("filter_keywords") or []:
            kw = str(kw).strip()
            if kw and kw in text:
                return f"包含屏蔽词“{kw}”"
        whitelist = [
            str(k).strip() for k in (self.config.get("whitelist_keywords") or []) if str(k).strip()
        ]
        if whitelist and not any(kw in text for kw in whitelist):
            return "未包含任何白名单关键词"
        return None

    async def _poll_loop(self):
        logger.info(f"{PLUGIN_NAME} 轮询任务已启动")
        await asyncio.sleep(5)
        while True:
            interval = max(MIN_INTERVAL_SECONDS, self._int_cfg("poll_interval", 120))
            try:
                pushed = await self.check_all()
                if pushed:
                    logger.info(f"本轮共检测到 {pushed} 条新微博")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"{PLUGIN_NAME} 轮询异常: {e!r}")
            jitter = random.uniform(0, min(15, interval * 0.2))
            if time.time() < self._blocked_until:
                # 风控冷却期间用短周期轮询，冷却一结束立即恢复检查
                await asyncio.sleep(min(60, interval))
            else:
                await asyncio.sleep(interval + jitter)

    def _is_expired(self, created_ts: float, at_ts: float | None = None) -> bool:
        """时效过滤：超过 max_post_age_minutes 的微博视为过期。

        发布时间无法解析（created_ts=0）时宽松放行。
        """
        max_age = self._max_post_age()
        if max_age <= 0 or not created_ts:
            return False
        return (at_ts or time.time()) - created_ts > max_age

    async def check_all(self) -> int:
        """检查一轮所有账号并推送，返回本轮新检测到的微博数。"""
        if self._checking:
            return 0
        if time.time() < self._blocked_until:
            logger.info(f"{PLUGIN_NAME} 风控冷却中，本轮跳过")
            return 0
        self._checking = True
        try:
            self._sync_accounts_from_config()
            if not self._custom_cookies and (
                not self._visitor_cookies.get("SUB")
                or time.time() - self._visitor_ts > VISITOR_MAX_AGE
            ):
                await self._renew_visitor_quietly()
            new_count = 0
            # 是否产生了需要落盘的变化（新微博/基线/昵称修正/待推送队列变化）：
            # 无变化的常规轮次不写 state.json
            state_dirty = False
            entries = list(self.accounts.items())
            for pos, (uid, info) in enumerate(entries):
                if time.time() < self._blocked_until:
                    logger.warning(f"{PLUGIN_NAME} 风控冷却生效，本轮剩余账号跳过")
                    break
                if time.time() < info.get("next_retry_ts", 0):
                    continue
                try:
                    got, dirty = await self._check_account(uid)
                    new_count += got
                    state_dirty = state_dirty or dirty
                    info["fail_count"] = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    info["fail_count"] = info.get("fail_count", 0) + 1
                    fail = info["fail_count"]
                    backoff = min(600.0, 60.0 * 2 ** (fail - 1)) if fail >= 2 else 60.0
                    info["next_retry_ts"] = time.time() + backoff
                    if fail == 1:
                        # 只记首次失败，断网/风控持续期间不逐轮刷屏
                        self._record_event(
                            "error",
                            uid=uid,
                            name=self._account_name(uid),
                            detail=f"检查失败: {e}",
                        )
                    logger.warning(
                        f"检查账号 {uid} 失败（连续第 {fail} 次，{backoff:.0f}s 后重试）: {e}"
                    )
                # 多账号之间留随机间隔错开请求；最后一个账号之后没必要再等
                if pos < len(entries) - 1:
                    await asyncio.sleep(random.uniform(1.0, 3.0))
            # 推送可能把条目移出内存队列（已送达的不落盘就会重启重发），
            # 所以 _flush_pending 抛异常时同样要落盘；无变化的常规轮次
            # 仍保持"不脏不写"，不额外放大 state.json 的写次数。
            flushed = False
            save_needed = state_dirty
            try:
                flushed = await self._flush_pending()
            except asyncio.CancelledError:
                save_needed = True
                raise
            except Exception:
                save_needed = True
                raise
            finally:
                self.last_check_ts = time.time()
                if save_needed or flushed:
                    await self._save_state_async()
            return new_count
        finally:
            self._checking = False

    async def _check_account(self, uid: str) -> tuple[int, bool]:
        """检查单个账号，返回 (新检测到的微博数, 是否产生需要落盘的状态变化)。"""
        info = self.accounts[uid]
        # 已见 ID 按加入顺序保存，超出上限只裁最早记录的：去重用 set，但
        # list(set) 是哈希序，直接裁剪会随机丢 ID——单账号记录涨满 500 条后，
        # 每轮都有概率把刚推送过的新微博 ID 裁掉，下一轮再推一遍造成重复
        seen_list = [str(i) for i in (info.get("seen_ids") or [])][-SEEN_IDS_LIMIT:]
        seen: set[str] = set(seen_list)
        posts = await self._fetch_timeline(uid, seen if info.get("baseline_done") else None)
        dirty = False
        # 用接口数据修正昵称（添加时可能没拉到）
        if posts and info.get("name") == uid:
            info["name"] = posts[0]["author"]
            dirty = True
        if not info.get("baseline_done"):
            # 首次观察：只记录基线，不推送历史微博
            for post in posts:
                if post["id"] not in seen:
                    seen.add(post["id"])
                    seen_list.append(post["id"])
            info["seen_ids"] = seen_list[-SEEN_IDS_LIMIT:]
            info["baseline_done"] = True
            logger.info(f"账号 {uid} 基线已建立，共 {len(posts)} 条历史微博")
            return 0, True
        new_posts = [p for p in posts if p["id"] not in seen]
        if not new_posts:
            return 0, dirty
        include_rt = bool(self.config.get("include_retweets", True))
        show_full = bool(self.config.get("show_full_weibo_text", False))
        # 图片数上限：消息带图与相册上传共用（0/异常值回退默认）
        max_imgs = self._int_cfg("album_max_images", DEFAULT_MAX_IMAGES) or DEFAULT_MAX_IMAGES
        for post in reversed(new_posts):  # 旧→新依次入队
            if len(self.pending) >= PENDING_HARD_LIMIT:
                # 队列满时停下，别把剩余新微博标成已见：留到下一轮再处理。
                # （以前是标记后丢弃最新一条，积压时反而永久漏掉最新微博。）
                logger.warning("待推送队列已满，本轮停止检查，剩余微博留到下一轮")
                break
            seen.add(post["id"])
            seen_list.append(post["id"])
            # 每标记一条就写回 info：取消/异常打断时 terminate 落盘的 seen_ids
            # 必须包含已经入队的条目，否则重启后同一条会被再推一遍。
            info["seen_ids"] = seen_list[-SEEN_IDS_LIMIT:]
            if self._is_expired(post.get("created_ts", 0.0)):
                continue
            if post["is_retweet"] and not include_rt:
                continue
            if show_full:
                # 全文只补给确定要推的帖子：在时效/转发过滤之后、关键词过滤
                # 之前，保证关键词匹配看到的仍是全文
                await self._apply_full_text(post)
            hit = self._keyword_hit(post)
            if hit:
                logger.info(f"微博 {post['id']} {hit}，已跳过推送")
                continue
            # 超 9 图识别补全与视频详情按需拉取：都放在过滤之后，被过滤
            # 掉的帖子不花这些请求；补全在前，_apply_video 能复用它拉到的详情
            await self._ensure_full_pics(post)
            await self._apply_video(post)
            videos_enabled = self._videos_enabled()
            extracted = post.get("videos") or []
            # 增量归档：挂在「通过全部过滤、确定要推」之后，pics/videos 已补全。
            # 归档是旁路：任何异常只 warning，绝不阻塞推送；archive_post 内部只
            # 落库 + 入队，不再发网络请求，所以这里不会拖慢 _check_account。
            if self._archive_enabled() and self._archive_bool("auto_archive", True):
                try:
                    await self.archive_post(post)
                except Exception as e:  # 归档失败绝不阻塞推送
                    logger.warning(f"归档失败（不影响推送）: {e}")
            self.pending.append(
                {
                    "post_id": post["id"],
                    "uid": post["uid"],
                    "text": self._build_message(post),
                    # 关键词相册路由的匹配文本：正文+转发原文（用户改模板去掉
                    # {weibo} 时渲染消息里就不含原文了，必须留存原始正文）
                    "raw_text": f"{post['text']}\n{post['orig_text']}",
                    "created_ts": post.get("created_ts", 0.0),
                    "retries": 0,
                    "pics": (post.get("pics") or [])[:max_imgs],
                    "is_video": bool(post.get("is_video")),
                    "videos": extracted,
                    # 直链取到的时刻：签名一小时过窗，下载前据此决定要不要重取
                    "videos_ts": time.time(),
                    "video_files": [],
                    "video_attempts": 0,
                    "video_fail": "",
                    # 检测到视频但没提取到直链（直播/详情缺失）：直接降级提示，
                    # 否则 _video_ready 会一直等一个永远不会完成的下载
                    "video_note": (
                        VIDEO_FALLBACK_NOTE
                        if videos_enabled and post.get("is_video") and not extracted
                        else ""
                    ),
                }
            )
        info["seen_ids"] = seen_list[-SEEN_IDS_LIMIT:]
        return len(new_posts), True
