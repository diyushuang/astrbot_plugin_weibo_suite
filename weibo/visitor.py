"""游客身份、请求节流与风控自愈。"""

from __future__ import annotations

from .runtime import *


class VisitorMixin:
    async def _resolve_host(self, host: str) -> list[tuple[Any, ...]]:
        """带 TTL 缓存的 getaddrinfo（仅 TCP 记录），供出网前的 SSRF 校验使用。

        白名单 host 只有寥寥几个且极少变更；aiohttp 连接时还会自己再解析一次，
        那份由连接器的 ttl_dns_cache 管，这里只兜校验自己的。解析失败不记负
        缓存，下一次请求照常重试真解析。
        """
        now = time.monotonic()
        hit = self._dns_cache.get(host)
        if hit:
            infos, expires = hit
            if now < expires:
                return infos
            self._dns_cache.pop(host, None)
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        self._dns_cache[host] = (infos, now + DNS_CACHE_TTL)
        return infos

    @staticmethod
    def _ensure_public_addrs(infos: list[tuple[Any, ...]], label: str) -> None:
        """解析结果（新解析或缓存复用）里出现内网/保留地址就拦截。"""
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_loopback
                or ip.is_private
                or ip.is_reserved
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_unspecified
            ):
                raise WeiboFetchError(f"{label}解析到受限地址，已拦截: {ip}")

    async def _validate_url(self, url: str):
        """出网前校验：仅 http/https、host 白名单、DNS 不解析到内网/保留地址。"""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise WeiboFetchError(f"拒绝非 http(s) 协议: {parsed.scheme}")
        host = parsed.hostname or ""
        if host not in WEIBO_ALLOWED_HOSTS:
            raise WeiboFetchError(f"目标 host 不在白名单内: {host}")
        try:
            infos = await self._resolve_host(host)
        except OSError as e:
            raise WeiboFetchError(f"域名解析失败: {host} ({e})") from e
        self._ensure_public_addrs(infos, f"域名 {host} ")

    async def _pace(self):
        """全局请求间隔：任意两次微博请求之间至少间隔 REQUEST_GAP 内的随机秒数。

        加锁串行，保证轮询任务与指令处理并发时同样全局生效。锚点统一用
        「上一次请求放行的时刻」，因此间隔按 **请求发起时间** 计算——调用方
        必须保证每次出网前都过一遍这里，重试路径也不例外（`api._get_text`
        的退避若短于 `REQUEST_GAP` 下限，就等于绕过了限速）。
        """
        async with self._pace_lock:
            gap = random.uniform(*REQUEST_GAP)
            wait = self._last_request_mono + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_mono = time.monotonic()

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._ua or UA_POOL[0],
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    def _mark_risk_blocked(self, e: Exception):
        """IP 级风控：全局冷却并按连续触发次数升级（10 → 30 → 60 分钟）。

        冷却期内重复触发不叠加升级（同一轮请求的多次 432 只算一次）；
        距上次触发超过 RISK_ESCALATION_RESET 后梯度重置。
        """
        now = time.time()
        if now < self._blocked_until:
            return
        if now - self._last_risk_ts > RISK_ESCALATION_RESET:
            self._risk_level = 0
        self._last_risk_ts = now
        level = self._risk_level
        cooldown = RISK_COOLDOWN_STEPS[min(level, len(RISK_COOLDOWN_STEPS) - 1)]
        self._risk_level = min(level + 1, len(RISK_COOLDOWN_STEPS) - 1)
        self._blocked_until = now + cooldown
        self._record_event(
            "risk",
            detail=(f"触发微博风控，全局冷却 {cooldown // 60} 分钟（连续第 {level + 1} 次触发）"),
        )
        logger.warning(
            f"{PLUGIN_NAME} 触发微博风控，全局冷却 {cooldown // 60} 分钟"
            f"（连续第 {level + 1} 次触发）: {e}"
        )

    async def _renew_quietly_force(self):
        try:
            await self._renew_visitor(force=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"{PLUGIN_NAME} 换新游客身份失败: {e}")

    async def _renew_visitor_quietly(self):
        try:
            await self._renew_visitor()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"{PLUGIN_NAME} 更新游客身份失败: {e}")

    async def _warmup_quietly(self):
        """启动预热：带现有身份访问一次主页，刷新 _T_WM/XSRF-TOKEN 等指纹 cookie。"""
        try:
            cookies: dict[str, str] = {}
            await self._get_text(WEIBO_HOME_URL, {}, self._base_headers(), collect=cookies)
            if cookies:
                self._visitor_cookies.update(cookies)
                await self._save_state_async()
                logger.info(f"{PLUGIN_NAME} 启动预热完成（{sorted(cookies)}）")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info(f"{PLUGIN_NAME} 启动预热失败（不影响运行）: {e}")

    async def _renew_visitor(self, force: bool = False):
        """向微博游客系统换取新的匿名游客身份（SUB/SUBP，种在 .weibo.cn 域）。

        节流策略：每小时最多 RENEW_MAX_PER_HOUR 次尝试；
        失败后指数退避；非强制模式下受 RENEW_COOLDOWN 冷却限制。
        加锁串行，避免指令处理与轮询任务并发触发双重换新。
        """
        async with self._renew_lock:
            if self._http is None:
                raise WeiboFetchError("HTTP 会话未初始化")
            now = time.time()
            recent = [t for t in self._renew_times if now - t < 3600.0]
            self._renew_times = recent
            if len(recent) >= RENEW_MAX_PER_HOUR:
                raise WeiboFetchError(
                    f"游客身份续期已达每小时上限（{RENEW_MAX_PER_HOUR} 次），稍后自动重试"
                )
            if now < self._renew_fail_until:
                raise WeiboFetchError(
                    "上次续期失败，退避中："
                    + datetime.fromtimestamp(self._renew_fail_until, tz=_TZ_CN).strftime("%H:%M:%S")
                    + " 后自动重试"
                )
            if (
                not force
                and self._visitor_cookies.get("SUB")
                and now - self._last_renew_ts < RENEW_COOLDOWN
            ):
                return  # 冷却期内且现有身份尚在，直接复用
            self._renew_times.append(now)
            self._last_renew_ts = now
            try:
                await self._do_renew()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._renew_fail_count += 1
                backoff = min(3600.0, RENEW_FAIL_BACKOFF * (2 ** (self._renew_fail_count - 1)))
                self._renew_fail_until = time.time() + backoff
                raise
            self._renew_fail_count = 0
            self._renew_fail_until = 0.0
            await self._save_state_async()

    async def _do_renew(self):
        assert self._http is not None
        logger.info(f"{PLUGIN_NAME} 正在获取微博游客身份…")
        # UA 与 cookie 必须成套：先写 self._ua 会让失败的续期留下"旧身份+新 UA"
        ua = random.choice(UA_POOL)
        cookies: dict[str, str] = {}
        common = self._base_headers()
        common["User-Agent"] = ua
        visitor_headers = dict(common)
        visitor_headers["Referer"] = WEIBO_VISITOR_REFERER

        # 1) 预热移动端主页，收集 _T_WM / XSRF-TOKEN / MLOGIN 等游客 cookie
        await self._get_text(WEIBO_HOME_URL, {}, common, collect=cookies)

        # 2) genvisitor 获取 tid
        fp = json.dumps(
            {
                "os": "1",
                "browser": "Chrome136,136,0,0",
                "fonts": "undefined",
                "screenInfo": "1920*1080*30",
                "plugins": "",
            },
            separators=(",", ":"),
        )
        body = await self._get_text(
            WEIBO_GENVISITOR_URL,
            {"cb": "gen_callback", "fp": quote(fp, safe="")},
            visitor_headers,
        )
        gen = jsonp_data(body) or {}
        if gen.get("retcode") != 20000000:
            raise WeiboFetchError(f"genvisitor 失败: {gen.get('retcode')} {gen.get('msg')}")
        tid = str((gen.get("data") or {}).get("tid") or "")
        confidence = (gen.get("data") or {}).get("confidence", 90)
        if not tid:
            raise WeiboFetchError("genvisitor 未返回 tid")

        # 3) incarnate 换取游客 SUB/SUBP（响应 Set-Cookie 种在 .weibo.cn 域）
        inc_params = {
            "a": "incarnate",
            "t": tid,
            "w": "2",
            "c": str(confidence),
            "gc": "",
            "cb": "cross_domain",
            "from": "weibo",
            "_rand": f"{random.random():.17f}",
        }
        await self._get_text(WEIBO_INCARNATE_URL, inc_params, visitor_headers, collect=cookies)

        if "SUB" not in cookies:
            raise WeiboAuthError("游客身份获取失败（未拿到 SUB）")
        cookies.setdefault("MLOGIN", "0")
        # 全部请求成功后才整体切换身份：失败分支不污染现有 cookie/UA 组合
        self._ua = ua
        self._visitor_cookies = cookies
        self._visitor_ts = time.time()
        logger.info(f"{PLUGIN_NAME} 游客身份已更新（{sorted(cookies)}）")
