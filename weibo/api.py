"""微博 API、长文、详情、图片补全与时间线读取。"""

from __future__ import annotations

from .runtime import *


class ApiMixin:
    async def _get_text(
        self,
        url: str,
        params: dict[str, Any],
        headers: dict[str, str],
        collect: dict[str, str] | None = None,
    ) -> str:
        await self._validate_url(url)
        assert self._http is not None
        timeout = aiohttp.ClientTimeout(total=15)
        last_err: Exception | None = None
        for attempt in range(3):
            # 每次尝试都要重新排队限速：1.5s/3s 的退避短于 REQUEST_GAP 下限，
            # 直接重发就是"被限流后立刻再打"，最容易把风控推到 IP 级。
            await self._pace()
            try:
                async with self._http.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=timeout,
                    proxy=self._proxy,
                ) as resp:
                    if resp.status in (418, 432):
                        raise WeiboRiskError(f"HTTP {resp.status}，触发微博 IP 级风控")
                    resp.raise_for_status()
                    if collect is not None:
                        self._merge_cookies(collect, resp)
                    return await resp.text()
            except (WeiboRiskError, WeiboAuthError):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = e
                if attempt < 2:
                    await asyncio.sleep(1.5 * (2**attempt))
        raise WeiboFetchError(f"请求失败: {last_err}")

    async def _api_get(
        self, url: str, params: dict[str, Any], referer: str | None = None
    ) -> dict[str, Any]:
        """GET m.weibo.cn JSON 接口。

        ok=-100（游客身份过期）→ 换新身份重试一次；
        HTTP 418/432（IP 级风控）→ 记录全局冷却并换新身份，重试一次；
        冷却期内直接快速失败，避免冷却期间继续向微博出网。
        """
        if time.time() < self._blocked_until:
            raise WeiboFetchError(
                f"微博风控冷却中（剩余 {int(self._blocked_until - time.time())} 秒）"
            )
        for attempt in (1, 2):
            try:
                data = await self._api_get_once(url, params, referer)
                # 成功即解除风控冷却
                self._risk_level = 0
                self._blocked_until = 0.0
                return data
            except WeiboRiskError as e:
                # 风控刚触发就进入 IP 级冷却：继续换身份/重试只会加重风控。
                # 原实现冷却生效后立刻再打 homepage + genvisitor + 重发请求。
                self._mark_risk_blocked(e)
                raise
            except WeiboAuthError as e:
                if attempt == 2:
                    raise
                logger.info(f"游客身份失效（{e}），正在自动换新…")
                await self._renew_visitor(force=True)

    async def _api_get_once(
        self, url: str, params: dict[str, Any], referer: str | None
    ) -> dict[str, Any]:
        if self._http is None:
            raise WeiboFetchError("HTTP 会话未初始化")
        headers = self._base_headers()
        headers.update(API_HEADERS)
        headers["Referer"] = referer or "https://m.weibo.cn/"
        if self._custom_cookies:
            # 自定义 Cookie 模式：直接使用用户身份，不走游客续期
            cookies = self._custom_cookies
        else:
            if not self._visitor_cookies.get("SUB"):
                await self._renew_visitor()
            cookies = self._visitor_cookies
            xsrf = cookies.get("XSRF-TOKEN", "")
            if xsrf:
                headers["X-Xsrf-Token"] = xsrf
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        body = await self._get_text(url, params, headers)
        if body.lstrip().startswith("<"):
            # 风控拦截页与错误页照样回 200：JSON 接口拿到 HTML 说明这个 IP
            # 已经被墙了，按风控处理进入全局冷却，而不是按账号级失败继续撞墙
            # （与媒体下载对 HTML 200 的判定口径一致）
            raise WeiboRiskError("JSON 接口返回了 HTML 页面（疑似被风控拦截）")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise WeiboFetchError("接口返回的不是 JSON") from e
        if not isinstance(data, dict):
            raise WeiboFetchError("接口返回异常")
        if data.get("ok") == -100:
            if self._custom_cookies:
                # 自定义 Cookie 失效时换新游客身份没有意义，直接报错由调用方回退
                raise WeiboFetchError("自定义 Cookie 已失效（接口要求登录）")
            raise WeiboAuthError("接口要求登录（ok=-100）")
        if data.get("ok") != 1:
            raise WeiboFetchError(f"接口返回异常 ok={data.get('ok')}")
        return data

    @staticmethod
    def _parse_cookie_string(raw: str) -> dict[str, str]:
        """Compatibility wrapper around the shared cookie parser."""
        return parse_cookie_string(raw)

    @staticmethod
    def _merge_cookies(target: dict[str, str], resp: aiohttp.ClientResponse):
        """Compatibility wrapper around the shared Set-Cookie merger."""
        merge_response_cookies(target, resp)

    async def _apply_full_text(self, post: dict[str, Any]):
        """show_full_weibo_text 开启时拉取超长微博全文，替换摘要。

        只对确定要推送的新微博调用：旧微博反正进不了推送，拉全文是白花请求。
        覆盖外层微博与转发原微博两处，每条最多 2 次请求；任一获取失败安全回退
        摘要。已实测 /statuses/extend 游客身份可用（ok=1，data.longTextContent
        为含 HTML 的全文）。
        """
        if post["id"] and post.get("is_long"):
            full = await self._fetch_long_text(post["id"])
            if full:
                post["text"] = full
        if post["is_retweet"] and post.get("orig_id") and post.get("orig_is_long"):
            full = await self._fetch_long_text(str(post["orig_id"]))
            if full:
                post["orig_text"] = full

    async def _fetch_long_text(self, status_id: str) -> str | None:
        """拉取超长微博全文，失败返回 None（调用方回退摘要）。"""
        try:
            data = await self._api_get(
                WEIBO_EXTEND_URL,
                {"id": status_id},
                referer=f"https://m.weibo.cn/detail/{status_id}",
            )
            long_text = (data.get("data") or {}).get("longTextContent")
            if isinstance(long_text, str) and long_text.strip():
                # 全文用独立的安全上限，不受摘要 text_max_length 影响
                return _clean_text(long_text, FULL_TEXT_MAX_CHARS)
            logger.info(f"长微博 {status_id} 全文响应缺少正文，使用摘要")
        except asyncio.CancelledError:
            raise
        except WeiboFetchError as e:
            logger.info(f"获取长微博 {status_id} 全文失败，使用摘要: {e}")
        return None

    @staticmethod
    def _video_nodes(
        pi: Any, prefer_hd: bool, seen_urls: set[str], max_bytes: int | float = 0
    ) -> list[dict[str, Any]]:
        """从 page_info 提取一个视频的直链候选列表；直播中或无 media_info 时返回空。"""
        out: list[dict[str, Any]] = []
        if not isinstance(pi, dict) or str(pi.get("type") or "") != "video":
            return out
        if int(pi.get("live_status") or 0) == 1:
            logger.info("检测到直播中的视频，暂不支持转发（可点原帖链接观看）")
            return out
        mi = pi.get("media_info")
        if not isinstance(mi, dict):
            return out
        urls = [u for u in _video_candidates(mi, prefer_hd, max_bytes) if u not in seen_urls]
        seen_urls.update(urls)
        if urls:
            out.append({"urls": urls})
        return out

    def _videos_from_mblog(
        self,
        src: dict[str, Any],
        prefer_hd: bool,
        seen_urls: set[str],
        max_bytes: int | float = 0,
    ) -> list[dict[str, Any]]:
        """从一个微博结构（时间线卡片或 statuses/show 的 data）收集视频直链。

        覆盖 page_info 与图文混排 mix_media_info 两种挂法，seen_urls 跨结构去重。
        """
        out = self._video_nodes(src.get("page_info"), prefer_hd, seen_urls, max_bytes)
        _, medias = _mix_media_nodes(src)
        for mi in medias:
            urls = [u for u in _video_candidates(mi, prefer_hd, max_bytes) if u not in seen_urls]
            seen_urls.update(urls)
            if urls:
                out.append({"urls": urls})
        return out

    def _merge_mix_pics(self, post: dict[str, Any], src: dict[str, Any]) -> None:
        """混排帖的图可能只出现在 mix_media_info 而不在 mblog.pics 里，并入图片列表。"""
        pic_nodes, _ = _mix_media_nodes(src)
        if not pic_nodes:
            return
        merged = list(post.get("pics") or [])
        have = {str(p.get("pid") or "") for p in merged}
        for p in _extract_pics({"pics": pic_nodes}):
            pid = str(p.get("pid") or "")
            if pid not in have:
                merged.append(p)
                have.add(pid)
        post["pics"] = merged

    async def _fetch_status_detail(self, pid: str, what: str = "视频") -> dict[str, Any] | None:
        """拉 statuses/show 详情，失败或结构异常返回 None（不抛，调用方各自降级）。

        视频直链与超 9 图补全共用这一个详情接口：视频路径回退「请点原帖
        链接」提示，图片路径按时间线已识别到的图继续（见 _ensure_full_pics）。
        """
        try:
            data = await self._api_get(
                WEIBO_SHOW_URL,
                {"id": pid},
                referer=f"https://m.weibo.cn/detail/{pid}",
            )
        except asyncio.CancelledError:
            raise
        except WeiboFetchError as e:
            logger.info(f"获取微博 {pid} {what}详情失败: {e}")
            return None
        detail = data.get("data")
        return detail if isinstance(detail, dict) else None

    async def _ensure_full_pics(self, post: dict[str, Any]) -> None:
        """超 9 图微博的识别补全：时间线卡片的 pics 不齐时补拉详情核对。

        线上出现过 18 图微博只识别到 9 张：时间线卡片对超 9 图帖的 pics
        只带部分图片（真机实测见过 9/18 与 0/18 两种形态），全量数要靠
        卡片自述的 pic_num。缺图判定走两条路：

        - 主信号：pic_num > 已识别数——字段在且可信时最准（转发帖的图挂
          在原微博上，_parse_post 已对两处取大）；
        - 兜底：pic_num 缺失/非法（pic_total=0）但恰好识别到 9 张——9 张
          正是卡片截断的形态，补一次详情核对。真九宫格（pic_num=9 与张数
          一致）走主信号就返回，不花这次请求；代价只是 pic_num 缺失的
          九宫格帖多一次详情请求，换"任何数量都不漏识别"。

        补到的是 statuses/show 详情的完整 pics（转发帖挂在 retweeted_status
        上）。安全边界：

        - 只在图片确实会被用到（消息带图或相册上传任一开启）时才补，
          两个开关都关着补图纯属浪费请求；
        - 补拉失败、补全后仍不足自述数：warning 进错误留存（面板可查，
          线上"只传 9 张"这类漏图不再只能靠人对日志才发现），推送按已
          识别的继续，图片补全不反过来挡推送；
        - 拉到的详情挂到 post["_show_detail"]，_apply_video 的兜底详情
          直接复用它，同一帖不花第二次请求（条目入队时该字段被丢弃）。
        """
        if not (
            bool(self.config.get("message_with_images", True))
            or bool(self.config.get("album_enabled", True))
        ):
            return
        total = int(post.get("pic_total") or 0)
        have = post.get("pics") or []
        # pic_num 不可知时按"卡片只带前 9 张"的截断形态兜底核对一次
        need = total > len(have) if total > 0 else len(have) == 9
        if not need:
            return
        pid = str(post.get("id") or "")
        if not pid:
            return
        detail = await self._fetch_status_detail(pid, "完整图片")
        if detail is None:
            logger.warning(
                f"微博 {pid} 图片识别可能不全：已识别 {len(have)} 张"
                + (f"（接口自述 {total} 张）" if total else "")
                + "，补拉详情失败，按已识别的继续转发；"
                "如群相册缺图请点原帖核对，下轮重推也会带上已识别的图"
            )
            return
        post["_show_detail"] = detail
        rt = detail.get("retweeted_status")
        full = _extract_pics(detail, rt if isinstance(rt, dict) else None)
        merged = list(have)
        known = {str(p.get("pid") or "") for p in merged}
        for p in full:
            key = str(p.get("pid") or "")
            if key and key not in known:
                merged.append(p)
                known.add(key)
        # pic_ids 权威列表兜底：受限/审核帖的 pics 数组可能比 pic_ids 少带
        # 详情条目（真机实测 11 个 id 只给 9 条），缺的按图床地址构造补入
        built = _fill_pics_from_ids(detail, rt if isinstance(rt, dict) else None, merged, known)
        if len(merged) > len(have):
            logger.info(
                f"微博 {pid} 时间线只识别到 {len(have)} 张图，详情补全为 "
                f"{len(merged)} 张"
                + (f"（自述 {total}）" if total else "")
                + (f"，含 pic_ids 构造 {built} 张" if built else "")
            )
            post["pics"] = merged
        if total > len(merged):
            # 补全后仍不足自述总数（详情接口也没带全）：漏图风险显性化，
            # 与补全成功的信息分开报，两者可同时出现（并进了一部分但没补满）
            logger.warning(
                f"微博 {pid} 图片识别仍不足：接口自述 {total} 张，"
                f"现在只有 {len(merged)} 张（详情接口也没带全），"
                "可能漏图，建议点原帖核对"
            )

    async def _apply_video(self, post: dict[str, Any]) -> None:
        """提取视频直链到 post["videos"]，混排里的图片一并并入 post["pics"]。

        先用时间线卡片自带的 media_info（实测 m.weibo.cn 的 getIndex 已经把
        stream_url/stream_url_hd 全给到了，12/12 样本与详情接口一致），只有
        卡片里取不到直链才补一次 statuses/show——省下的是每次 2~5 秒限速的
        微博请求，也是少一个可能被风控/要登录的失败点。覆盖纯视频帖、图文
        混排与转发原微博；直播中的视频取不到直链。任何失败都安全降级：
        videos 保持为空，推送时自动回落"请点原帖链接"提示。
        """
        if not post.get("is_video") or not self._videos_enabled():
            return
        pid = str(post["id"])
        prefer_hd = str(self.config.get("video_quality") or "hd") != "sd"
        # 体积帽换算成字节传给候选排序：内嵌发送时优先挑体积适配的档位
        cap_mb = self._video_download_cap_mb()
        max_bytes = cap_mb * 1048576 if cap_mb > 0 else 0

        videos: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        consulted: list[dict[str, Any]] = []
        for node in post.get("video_src") or []:
            # 外层优先，取不到才轮到转发原文（_parse_post 已按此顺序放好）
            videos.extend(self._videos_from_mblog(node, prefer_hd, seen_urls, max_bytes))
            consulted.append(node)
            if videos:
                break
        if not videos:
            if _only_live_video(post.get("video_src")):
                logger.info(f"微博 {pid} 的视频正在直播，无可下载直链，推送时提示打开原帖")
                return
            # _ensure_full_pics 补图拉过的详情挂在这里，直接复用：同一帖
            # 不为视频再花一次 2~5 秒限速的详情请求（pop 取走，只消费一次）
            detail = post.pop("_show_detail", None) or await self._fetch_status_detail(pid)
            if detail is not None:
                videos = self._videos_from_mblog(detail, prefer_hd, seen_urls, max_bytes)
                consulted.append(detail)
                rt = detail.get("retweeted_status")
                if isinstance(rt, dict) and not videos:
                    # 外层没有视频才看转发原微博（转发时原视频挂在 rt 的结构里）
                    videos = self._videos_from_mblog(rt, prefer_hd, seen_urls, max_bytes)
                    consulted.append(rt)

        cap = max(1, self._int_cfg("max_videos_per_post", 2))
        post["videos"] = videos[:cap]
        for node in consulted:
            self._merge_mix_pics(post, node)

    async def _fetch_timeline_page(self, uid: str, page: int) -> list[dict[str, Any]]:
        """拉取用户时间线的指定页，返回解析后的帖子列表（新→旧）。"""
        data = await self._api_get(
            WEIBO_INDEX_URL,
            {
                "type": "uid",
                "value": uid,
                "containerid": f"107603{uid}",
                "page": page,
                "count": 20,
            },
            referer=f"https://m.weibo.cn/u/{uid}",
        )
        payload = data.get("data")
        if not isinstance(payload, dict):
            return []
        cards = payload.get("cards") or []
        posts: list[dict[str, Any]] = []
        for card in cards:
            # 接口对已删除/隐藏的微博会在 cards 里留 null 槽位，条目不保证是对象
            if not isinstance(card, dict):
                continue
            if card.get("card_type") != 9:
                continue
            mb = card.get("mblog")
            if not isinstance(mb, dict):
                continue
            if self._is_pinned(card, mb):
                continue
            post = self._parse_post(mb, uid)
            if post:
                posts.append(post)
        return posts

    async def _fetch_timeline(
        self, uid: str, known_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """拉取用户最新微博，返回解析后的帖子列表（新→旧，跨页按 id 去重）。

        默认只拉第一页；整页都是未见过的微博说明两次检查之间可能积压了超过
        一页（轮询间隔较长或博主发博密集），向后翻页补漏，最多共
        TIMELINE_MAX_PAGES 页——否则更早的新微博会滑出第一页，永远漏掉。
        known_ids 为 None（建基线轮）时不翻页，历史微博反正不推送。
        """
        posts: list[dict[str, Any]] = []
        got: set[str] = set()
        for page in range(1, TIMELINE_MAX_PAGES + 1):
            batch = await self._fetch_timeline_page(uid, page)
            for post in batch:
                if post["id"] not in got:
                    got.add(post["id"])
                    posts.append(post)
            # 本页没有帖子，或页面里出现了已见过的微博（新旧边界已落在本页内）：
            # 更早的页全是旧微博，停止翻页
            if not batch or known_ids is None or any(p["id"] in known_ids for p in batch):
                break
        return posts

    async def _fetch_nickname(self, uid: str) -> str | None:
        """通过用户资料容器拉取昵称，失败返回 None。"""
        try:
            data = await self._api_get(
                WEIBO_INDEX_URL,
                {"type": "uid", "value": uid, "containerid": f"100505{uid}"},
                referer=f"https://m.weibo.cn/u/{uid}",
            )
            info = (data.get("data") or {}).get("userInfo") or {}
            name = str(info.get("screen_name") or "").strip()
            if name:
                return name
        except WeiboFetchError as e:
            logger.info(f"拉取用户 {uid} 昵称失败（不影响添加）: {e}")
        return None
