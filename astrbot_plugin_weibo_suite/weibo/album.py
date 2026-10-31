"""群相册下载、去重、实况转 GIF 与上传。"""

from __future__ import annotations

from .runtime import *


class AlbumMixin:
    def _album_tmp_root(self) -> Path:
        assert self._data_dir is not None
        return self._data_dir / ALBUM_TMP_DIR

    def _wipe_album_tmp(self):
        """清掉上次运行残留的下载临时文件（正常流程传完即删，这里只兜底）。"""
        shutil.rmtree(self._album_tmp_root(), ignore_errors=True)

    def _diag_write(self, msg: str) -> None:
        """诊断行的同步落盘实现，由 _diag 丢线程池执行（open/write/fsync 都是阻塞 syscall）。"""
        if self._diag_path is None:
            return
        try:
            self._diag_path.parent.mkdir(parents=True, exist_ok=True)
            if self._diag_path.exists() and self._diag_path.stat().st_size > DIAG_MAX_BYTES:
                lines = self._diag_path.read_text(encoding="utf-8").splitlines(True)
                self._diag_path.write_text("".join(lines[len(lines) // 2 :]), encoding="utf-8")
            with open(self._diag_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass  # 诊断本身不能变成新的故障点

    async def _diag(self, msg: str) -> None:
        """关键诊断行落到独立的追加式文件（写后立即 fsync 刷盘，超 512KB 留后半段）。

        整机被内存顶死之后，AstrBot 主日志往往来不及刷/没机会看；这份文件在
        重负载动作发生"之前"落盘，重启后读它就能还原死机前最后几批的体量与
        配置，不用复现（与 astrbot_plugin_weibo_album 同一套做法）。
        """
        await asyncio.to_thread(self._diag_write, msg)

    def _album_rules(self) -> dict[tuple[str, str], str]:
        return _parse_album_rules(self.config.get("album_rules"))

    def _album_keyword_rules(self) -> list[tuple[str | None, str, str, str]]:
        return _parse_keyword_rules(self.config.get("album_keyword_rules"))

    def _keyword_album_target(self, item: dict[str, Any]) -> tuple[str, str] | None:
        """关键词相册路由目标：博主规则优先于全局规则，各自按配置顺序第一条命中生效。

        匹配文本为入队时留存的正文+转发原文（旧待办条目回退渲染后消息），语义与
        关键词过滤一致。返回 (群号, 相册名或ID)；未配置或未命中返回 None——此时
        上传目标完全按默认相册绑定规则走，与未引入本功能时行为一致。
        """
        rules = self._album_keyword_rules()
        if not rules:
            return None
        text = str(item.get("raw_text") or item.get("text") or "")
        if not text:
            return None
        uid = str(item.get("uid") or "")
        # 第一轮只扫博主规则（uid 匹配的条目），第二轮只扫全局规则：
        # 保证博主规则优先于全局，与配置里的先后顺序无关
        for scope_uid, kw, gid, album in rules:
            if scope_uid is None or scope_uid != uid:
                continue
            if kw in text:
                return gid, album
        for scope_uid, kw, gid, album in rules:
            if scope_uid is None and kw in text:
                return gid, album
        return None

    @staticmethod
    def _parse_umo(umo: str) -> tuple[str, str, str]:
        """Compatibility wrapper around the shared UMO parser."""
        return parse_umo(umo)

    def _dispatch_album_uploads(self, item: dict[str, Any], sent_sessions: list[str]):
        """转发成功后按规则派发相册上传任务：每个命中规则的群一个异步任务。

        触发语义与"转发后上传"一致：只有该群真的推送成功才上传。任务不 await，
        不阻塞轮询循环；登记进 _album_tasks 供 terminate 撤销。
        """
        if not bool(self.config.get("album_enabled", True)):
            return
        pics = item.get("pics") or []
        uid = str(item.get("uid") or "")
        if not uid:
            return
        if not pics:
            # 群相册只收图片与实况图：没有可传内容（纯视频/纯文字微博）就不上传
            if item.get("is_video"):
                logger.info(f"微博 {item.get('post_id')} 检测到视频、无图片/实况图，跳过群相册上传")
            return
        # 关键词路由：博主规则优先于全局规则，命中则图片只传固定群相册、完全
        # 替代默认绑定；未配置/未命中走原有按群派发逻辑（行为与从前一致）。
        target = self._keyword_album_target(item)
        if target:
            gid, album = target
            platform = self._parse_umo(sent_sessions[0])[0] if sent_sessions else ""
            # session 仅作完成通知的落点；platform 未知时通知发不出去，上传不受影响
            session = f"{platform}:GroupMessage:{gid}" if platform else ""
            task = asyncio.create_task(
                self._album_upload_task(session, platform, gid, dict(item), album)
            )
            self._album_tasks.add(task)
            task.add_done_callback(self._bg_task_done(self._album_tasks, "相册上传"))
            logger.info(f"微博 {item['post_id']} 命中关键词路由，图片改传群 {gid} 相册「{album}」")
            return
        rules = self._album_rules()
        if not rules:
            return
        hit_any = False
        for session in sent_sessions:
            platform, msg_type, sid = self._parse_umo(session)
            if msg_type != "GroupMessage" or not sid:
                continue
            want = rules.get((uid, sid))
            if not want:
                continue
            hit_any = True
            task = asyncio.create_task(
                self._album_upload_task(session, platform, sid, dict(item), want)
            )
            self._album_tasks.add(task)
            task.add_done_callback(self._bg_task_done(self._album_tasks, "相册上传"))
        if hit_any:
            logger.info(f"微博 {item['post_id']} 命中相册绑定规则，开始自动上传图片")

    def _find_bot(self, platform_id: str):
        """按 umo 的 platform_id 找 aiocqhttp 适配器实例的 CQHttp bot。

        监控插件是轮询型，手里没有消息事件，只能从 platform_manager 拿适配器实例
        （aiocqhttp 适配器把 OneBot 客户端挂在 .bot 上）。匹配不到 id 时退而取
        第一个带 bot 的实例，兼容老版本 AstrBot 的元数据缺 id 的情况。

        **兼容性提醒**：``inst.bot`` 与 ``metadata.id`` 都是适配器的非公开属性，
        AstrBot 未把它们纳入插件 API 承诺；取用一律走 ``getattr`` 并容许缺失
        （连 platform_manager 本身也当可选处理），任何一个环节变了都只是"拿不到
        bot → 退回提示用户"，不会抛异常打断推送链路。实测于 AstrBot 4.28.2；
        升级 AstrBot 大版本后请重新确认这两个属性仍在（metadata.yaml 已把
        astrbot_version 上界收在 <5）。
        """
        pm = getattr(self.context, "platform_manager", None)
        get_insts = getattr(pm, "get_insts", None)
        if not callable(get_insts):
            return None
        for inst in get_insts():
            try:
                meta = getattr(inst, "metadata", None)
                if meta is None and hasattr(inst, "meta"):
                    meta = inst.meta()
                inst_id = str(getattr(meta, "id", "") or "")
            except Exception:
                continue
            if inst_id and platform_id and inst_id != platform_id:
                continue
            bot = getattr(inst, "bot", None)
            if bot is not None:
                return bot
        return None

    async def _album_client(self, platform_id: str) -> NapCatAlbum:
        """构造走 AstrBot 已有 OneBot 连接的相册客户端（地址与 token 全归适配器管）。"""
        bot = self._find_bot(platform_id)
        if bot is None:
            raise NapCatError(
                f"未找到平台 {platform_id or '(未知)'} 的 aiocqhttp 连接，无法上传群相册"
            )
        self_id = await self._bot_self_id(bot)

        async def caller(action: str, params: dict):
            if self_id:
                params["self_id"] = self_id
            return await bot.call_action(action, **params)

        try:
            preferred = str(await self.get_kv_data("payload:same_host", "") or "")
        except Exception:
            preferred = ""
        return NapCatAlbum(
            caller,
            preferred=preferred,
            same_host=bool(self.config.get("album_same_host", False)),
        )

    @staticmethod
    def _ledger_key(gid: str, album_id: str) -> str:
        return f"sent:{gid}:{album_id}"

    async def _sent_marks(self, gid: str, album_id: str) -> set[str]:
        """本插件往这个相册传过哪些图（按 pid 记，KV 台账）。

        base64 载荷会被 NapCat 改名成 randomUUID，相册里的文件名不再含微博 pid，
        只靠回读文件名去重会失效，所以自己记一份。
        """
        try:
            raw = await self.get_kv_data(self._ledger_key(gid, album_id), {}) or {}
        except Exception as e:
            logger.info(f"{PLUGIN_NAME} 读取上传台账失败，本次跳过台账去重: {e!r}")
            return set()
        now = time.time()
        out: set[str] = set()
        for m, ts in dict(raw).items():
            try:
                if now - float(ts) < LEDGER_TTL:
                    out.add(m)
            except (TypeError, ValueError):
                continue
        return out

    async def _remember(self, gid: str, album_id: str, marks: list[str]) -> None:
        if not marks:
            return
        key = self._ledger_key(gid, album_id)
        try:
            raw = dict(await self.get_kv_data(key, {}) or {})
        except Exception:
            raw = {}
        now = time.time()
        sent: dict[str, float] = {}
        for m, ts in raw.items():
            try:
                t = float(ts)
            except (TypeError, ValueError):
                continue
            if now - t < LEDGER_TTL:
                sent[m] = t
        sent.update(dict.fromkeys(marks, now))
        if len(sent) > LEDGER_MAX:
            sent = dict(sorted(sent.items(), key=lambda kv: kv[1])[-LEDGER_MAX:])
        try:
            await self.put_kv_data(key, sent)
        except Exception as e:
            # 台账写不进去只损失去重能力，不影响这次已经传上去的图
            logger.warning(f"{PLUGIN_NAME} 写入上传台账失败: {e!r}")

    async def _existing_names(self, nc: NapCatAlbum, gid: str, album_id: str) -> str:
        """相册里已有媒体的文件名合集；读不到就返回空串（去重只是优化，不能成为故障点）。"""
        try:
            media = await nc.list_media(gid, album_id)
        except NapCatError as e:
            logger.info(f"{PLUGIN_NAME} 读取相册已有媒体失败，本次跳过文件名去重: {e}")
            return ""
        return " ".join(pick(m, MEDIA_NAME_KEYS) for m in media).lower()

    async def _resolve_album_cached(self, nc: NapCatAlbum, gid: str, want: str) -> tuple[str, str]:
        """(群, 相册名/ID) -> (album_id, album_name)，命中缓存就不再翻相册列表。"""
        key = f"{gid}:{want}"
        hit = self._album_cache.get(key)
        now = time.time()
        if hit and now - hit[0] < ALBUM_RESOLVE_TTL:
            return hit[1], hit[2]
        album_id, album_name = await nc.resolve_album(gid, want)
        self._album_cache[key] = (now, album_id, album_name)
        return album_id, album_name

    def _group_lock(self, gid: str) -> asyncio.Lock:
        lock = self._album_locks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._album_locks[gid] = lock
        return lock

    def _up_gate(self) -> asyncio.Semaphore:
        """上传并发总闸（配置热改时重建，在途任务拿旧闸跑完本批）。"""
        conc = max(1, self._int_cfg("album_upload_concurrency", 3))
        if conc != self._up_conc or self._up_sem is None:
            self._up_sem = asyncio.Semaphore(conc)
            self._up_conc = conc
        if self._up_cond is None:
            # 字节预算 Condition 全程复用不重建：在途任务还挂着旧实例，
            # 重建会把 _up_bytes 计数清零，预算就失真了
            self._up_cond = asyncio.Condition()
        return self._up_sem

    def _dl_gate(self) -> asyncio.Semaphore:
        """下载并发总闸。"""
        conc = max(1, self._int_cfg("album_download_concurrency", 5))
        if conc != self._dl_conc or self._dl_sem is None:
            self._dl_sem = asyncio.Semaphore(conc)
            self._dl_conc = conc
        return self._dl_sem

    async def _validate_media_url(self, url: str):
        """媒体下载前校验：仅 http(s)、host 限微博系 CDN、DNS 不解析到内网/保留地址。"""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise WeiboFetchError(f"拒绝非 http(s) 协议的媒体地址: {parsed.scheme}")
        host = parsed.hostname or ""
        if not any(host == s.lstrip(".") or host.endswith(s) for s in MEDIA_ALLOWED_SUFFIXES):
            raise WeiboFetchError(f"媒体 host 不在白名单内: {host}")
        try:
            infos = await self._resolve_host(host)
        except OSError as e:
            raise WeiboFetchError(f"媒体域名解析失败: {host} ({e})") from e
        self._ensure_public_addrs(infos, f"媒体域名 {host} ")

    async def _stream_to_file(
        self,
        url: str,
        dest: Path,
        *,
        max_bytes: int | float = IMG_MAX_BYTES,
        timeout_total: float = 60.0,
    ):
        """流式下载到 dest（64KB 分块边下边写），超限中途抛错由调用方清理残件。

        图片与视频共用：视频体积大、下载慢，上限与总超时由调用方给。
        响应头就说清楚的体积直接判超限（MediaTooLarge），不等正文烧完带宽；
        收完还要比对 Content-Length，中途被掐断的半截文件不能当成品交出去。

        异常契约（调用方据此决定"换档/降级"还是"换轮重试"）：

        - ``MediaTooLarge``：体积超上限，**确定性**结论。响应头阶段（还未读正文）
          与流式读到上限处各抛一次，两条路径语义相同。换直链、隔多久重来都不会
          变成能下，只该换下一档或直接降级，别白烧带宽。
        - ``WeiboFetchError``：HTML 被当成媒体（多半风控）、下载被截断等。
        - ``aiohttp.ClientResponseError``：非 2xx。状态码是"该不该重取直链"的判据。
        - ``aiohttp.ClientError`` / ``asyncio.TimeoutError`` / ``OSError``：瞬时故障。

        调用方见 ``_download_video_entry``（视频，按上述分档处理）、
        ``_download_pic``（图片，只需兜住的 ``WeiboFetchError`` 一族）。
        """
        assert self._http is not None
        headers = {
            "User-Agent": self._ua or UA_POOL[0],
            # 微博媒体 CDN 校验 Referer（视频 CDN 无此头直接 403）
            "Referer": "https://weibo.com/",
        }
        timeout = aiohttp.ClientTimeout(total=timeout_total)
        got = 0
        async with self._http.get(url, headers=headers, timeout=timeout, proxy=self._proxy) as resp:
            resp.raise_for_status()
            ctype = str(resp.headers.get("Content-Type", "")).lower()
            if "text/html" in ctype:
                # 风控页与错误页照样回 200，体积也常常过 1KB：只按大小判成功的话，
                # HTML 会被存成 .jpg 一路传进相册，问题要到很远的地方才爆
                raise WeiboFetchError(f"拿到的是 HTML 页面（多半被风控拦了）: {url}")
            declared = _header_int(resp.headers.get("Content-Length"))
            if declared and declared > max_bytes:
                raise MediaTooLarge(
                    f"单个媒体 {declared / 1048576:g}MB 超过 {max_bytes / 1048576:g}MB 上限",
                    declared,
                )
            with dest.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(65536):
                    got += len(chunk)
                    if got > max_bytes:
                        limit_mb = (
                            int(max_bytes // 1048576)
                            if isinstance(max_bytes, int)
                            else max_bytes / 1048576
                        )
                        raise MediaTooLarge(f"单个媒体超过 {limit_mb:g}MB 上限", got)
                    fh.write(chunk)
            if declared and got < declared:
                raise WeiboFetchError(f"下载被截断：收到 {got}B / 应有 {declared}B")
        return got

    async def _download_pic(self, pic: dict[str, str], dest: Path) -> Path:
        """下载一张微博图：先取 /large/ 原图，失败退回接口给的原始地址。

        这里按 ``WeiboFetchError`` 一族统一兜住即可——超限的 ``MediaTooLarge``
        是它的子类，图片档位上"这张原图太大"和"这个地址下不动"的处理没区别，
        都是换下一个候选地址再试，不像视频那样需要区分确定性/瞬时性。
        """
        last = ""
        for url in (pic.get("url"), pic.get("alt_url")):
            if not url:
                continue
            part = dest.with_name(dest.name + ".part")
            try:
                await self._validate_media_url(url)
                await self._stream_to_file(url, part)
            except WeiboFetchError as e:
                last = str(e)
                part.unlink(missing_ok=True)
                continue
            size = part.stat().st_size if part.is_file() else 0
            if size > 1024:
                part.replace(dest)
                return dest
            last = f"HTTP 响应过小（{size}B）"
            part.unlink(missing_ok=True)
        raise WeiboFetchError(f"图片下载失败 {pic.get('pid') or pic.get('url')}: {last}")

    async def _to_gif(self, mp4: Path, gif: Path) -> bool:
        """实况图视频段转 GIF（限帧率压体积），失败返回 False 让上层回落封面静图。"""
        # ffmpeg 的 lanczos 缩放是实打实的满核负载，全进程最多同时跑 2 个转码
        async with self._cpu_sem:
            proc = await asyncio.create_subprocess_exec(
                self._ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(mp4),
                "-vf",
                "fps=10,scale=480:-2:flags=lanczos",
                "-loop",
                "0",
                str(gif),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=60)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return False
        return proc.returncode == 0 and gif.is_file() and gif.stat().st_size > 0

    async def _live_gif(self, pic: dict[str, str], folder: Path) -> Path | None:
        """下载实况图的视频段并转 GIF；任何一步失败返回 None，由上层回落封面。"""
        stem = _pic_stem(pic)
        mp4, gif = folder / f"{stem}.mp4", folder / f"{stem}.gif"
        try:
            await self._validate_media_url(pic.get("video_url") or "")
            await self._stream_to_file(str(pic.get("video_url") or ""), mp4)
        except WeiboFetchError:
            mp4.unlink(missing_ok=True)
            return None
        try:
            ok = await self._to_gif(mp4, gif)
        except (OSError, ValueError) as e:
            logger.info(f"{PLUGIN_NAME} 实况图转 GIF 失败，改用封面静图: {e!r}")
            ok = False
        mp4.unlink(missing_ok=True)
        if ok and gif.is_file() and gif.stat().st_size > GIF_MAX_BYTES:
            # 超大 GIF 走 base64 时在途内存会放大到百 MB 级，单张超载荷预算还会
            # 空闸直行——宁可回落封面静图也不让它把小服务器顶穿
            logger.info(f"{PLUGIN_NAME} 实况图 GIF 超 {GIF_MAX_BYTES // 1048576}MB，改用封面静图")
            ok = False
        if not ok:
            gif.unlink(missing_ok=True)  # 超限/失败的产物不留进批次目录
            return None
        return gif

    async def _download_pics(
        self, pics: list[dict[str, str]], folder: Path, post_id: str = ""
    ) -> list[tuple[dict[str, str], Path]]:
        """整批并发下载到批次目录（实况图优先转 GIF，失败回落封面静图）。

        post_id 为空或归档库未就绪时行为与旧版完全一致；命中归档本地文件时
        直接复用、不发下载请求（实况图例外：还要转 GIF）。
        """
        free_mb = (
            await asyncio.to_thread(lambda: shutil.disk_usage(self._album_tmp_root()).free)
        ) // 1048576
        if free_mb < DISK_FLOOR_MB:
            raise WeiboFetchError(
                f"临时目录所在磁盘仅剩 {free_mb}MB（低于 {DISK_FLOOR_MB}MB 保护线），已取消本批下载"
            )
        sem = self._dl_gate()
        want_gif = bool(self.config.get("album_live_gif", True)) and bool(self._ffmpeg)

        async def one(pic: dict[str, str]) -> tuple[dict[str, str], Path] | None:
            async with sem:
                # 本地优先：归档库里已落盘的原图直接复用。live 图要转 GIF，仍走
                # 原逻辑（封面静图也在归档里，但转 GIF 需要原视频段）。
                local = (
                    self.archive_local_media(post_id, str(pic.get("pid") or ""))
                    if post_id
                    else None
                )
                if local is not None and pic.get("kind") != "live":
                    logger.info(f"相册图片命中本地归档，跳过下载: {local}")
                    return pic, local
                if pic.get("kind") == "live" and want_gif and pic.get("video_url"):
                    gif = await self._live_gif(pic, folder)
                    if gif is not None:
                        return pic, gif
                ext = re.sub(r"[^0-9a-z]", "", (pic.get("ext") or "jpg").lower())[:5] or "jpg"
                path = folder / f"{_pic_stem(pic)}.{ext}"
                try:
                    await self._download_pic(pic, path)
                except WeiboFetchError as e:
                    logger.info(f"相册图片下载失败，跳过: {e}")
                    return None
                return pic, path

        results = await asyncio.gather(*(one(p) for p in pics), return_exceptions=True)
        files: list[tuple[dict[str, str], Path]] = []
        for r in results:
            if isinstance(r, BaseException):
                # 子任务被撤（热重载/关停）时也按跳过收下，别让 CancelledError
                # 从 gather 里穿出去把父任务一起带走
                logger.warning(f"相册图片下载异常，跳过: {r!r}")
            elif r is not None:
                files.append(r)
        return files

    def _batch_dir(self, gid: str, post_id: str) -> Path:
        """批次下载目录：目录名带群号与微博 id，群与群、批与批互不覆盖。"""
        base = (
            f"{BAD_NAME_RE.sub('', gid)[:24] or 'group'}_"
            f"{BAD_NAME_RE.sub('', post_id)[:24] or 'post'}_"
            f"{time.strftime('%Y%m%d-%H%M%S')}"
        )
        folder = self._album_tmp_root() / base
        seq = 1
        while folder.exists():
            seq += 1
            folder = self._album_tmp_root() / f"{base}_{seq}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    async def _upload_files(
        self,
        nc: NapCatAlbum,
        gid: str,
        album_id: str,
        album_name: str,
        files: list[tuple[dict[str, str], Path]],
    ) -> tuple[int, list[str], list[str]]:
        """并发上传一批本地文件，返回 (成功数, 失败明细, 成功 marks)。"""
        sem = self._up_gate()
        cond = self._up_cond
        # NapCat 传相册是"每张内部串行发 16KB 分片"，单张本身就慢，并发数就是吞吐；
        # 间隔只用来错开起点防撞频控，压得太狠就是纯等
        interval = self._float_cfg("album_upload_interval", 0.5)
        # 在途 base64 载荷字节预算：按 1.5 倍原图体积收紧（载荷在插件与 NapCat
        # 两侧各驻留一份编码串）；单张超预算的图在空闸时照常放行，不会死锁
        budget_mb = self._float_cfg("album_upload_payload_mb", 32.0)
        payload_budget = float("inf") if budget_mb <= 0 else max(0.5, budget_mb) * 1048576 / 1.5
        modes: dict[str, int] = {}

        async def push(pic: dict[str, str], path: Path):
            async with sem:
                size = path.stat().st_size
                async with cond:
                    while self._up_bytes and self._up_bytes + size > payload_budget:
                        await cond.wait()
                    self._up_bytes += size
                try:
                    try:
                        mode = await nc.upload_file(gid, album_id, album_name, path)
                    except (NapCatError, OSError) as e:
                        logger.warning(f"{PLUGIN_NAME} 相册上传失败 {_mark(pic)}: {e}")
                        return None, f"{_mark(pic)}：{e}"
                    if interval:
                        # 传完占着并发槽歇 interval 再让位：只错开起点的话，
                        # 槽位一空就放行，配置里的频控间隔会名存实亡
                        await asyncio.sleep(interval)
                    return mode or "ok", None
                finally:
                    async with cond:
                        self._up_bytes -= size
                        cond.notify_all()

        results: list[Any] = []
        # 同机但还没学到可用载荷时，第一张先单独探路：整批一起拿本地路径去撞 NapCat，
        # 两边不同机就是每张一条 ENOENT，协议端控制台刷一屏，插件这边每张都白跑一趟
        # 载荷降级。学到过（preferred 非空，或 modes[0] 已是 base64）就别再探了。
        probe = bool(nc.same_host) and nc.modes[0] == "path" and not nc.preferred and len(files) > 1
        if probe:
            try:
                results.append(await push(*files[0]))
            except Exception as e:  # 探路这张出意外也别拖垮整批
                logger.warning(f"{PLUGIN_NAME} 相册上传探路异常: {e!r}")
                results.append((None, f"{_mark(files[0][0])}：{e!r}"))
        rest = files[1:] if probe else files
        results += await asyncio.gather(
            *(push(p, path) for p, path in rest), return_exceptions=True
        )
        ok, fails, marks = 0, [], []
        for (pic, _), r in zip(files, results, strict=True):
            if isinstance(r, BaseException):
                fails.append(f"{_mark(pic)}：{r!r}")
            elif r[0]:
                ok += 1
                modes[r[0]] = modes.get(r[0], 0) + 1
                marks.append(_mark(pic))
            else:
                fails.append(r[1] or "未知错误")
        if nc.same_host and modes:
            # 同机探测学到的可用载荷方式记下来，下一批复用，别每批都撞一遍 ENOENT
            learned = max(modes, key=lambda k: modes[k])
            with contextlib.suppress(Exception):
                await self.put_kv_data("payload:same_host", learned)
        return ok, fails, marks

    async def _album_upload_task(
        self, session: str, platform: str, gid: str, item: dict[str, Any], want: str
    ):
        """单条微博 × 单个群的相册上传任务（转发成功后异步执行）。"""
        post_id = str(item.get("post_id") or "")
        pics = [p for p in (item.get("pics") or []) if isinstance(p, dict)]
        try:
            async with self._group_lock(gid):
                await self._upload_batch(session, platform, gid, post_id, want, pics)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._record_album(
                0,
                0,
                0,
                f"微博 {post_id} -> 群 {gid} 上传任务异常: {e}",
            )
            await self._diag(f"微博 {post_id} -> 群 {gid} 上传任务异常: {e!r}")
            logger.warning(f"{PLUGIN_NAME} 微博 {post_id} 图片上传群 {gid} 相册失败: {e!r}")

    async def _upload_batch(
        self,
        session: str,
        platform: str,
        gid: str,
        post_id: str,
        want: str,
        pics: list[dict[str, str]],
    ):
        """一批图片的完整上传流程：解析相册 -> 去重 -> 下载 -> 上传 -> 台账 -> 清理。"""
        nc = await self._album_client(platform)
        album_id, album_name = await self._resolve_album_cached(nc, gid, want)
        sent = await self._sent_marks(gid, album_id)
        # 相册文件名回读只在 same_host 下才做：base64 载荷会被 NapCat 改名成 randomUUID，
        # 相册文件名里不含微博 pid，比对必然不命中，而它每次要翻最多 8 页媒体列表
        # （一页一次协议端往返，串行）。自记台账才是这条链路可靠的去重依据。
        existing = ""
        if nc.same_host and any(_mark(pic) not in sent for pic in pics):
            cache_key = f"{gid}:{album_id}"
            cached = self._album_existing_cache.get(cache_key)
            now = time.time()
            if cached and now - cached[0] < ALBUM_NAME_SCAN_TTL:
                existing = cached[1]
            else:
                existing = await self._existing_names(nc, gid, album_id)
                self._album_existing_cache[cache_key] = (now, existing)

        def _dup(pic: dict[str, str]) -> bool:
            mark = _mark(pic)
            # 子串匹配是有意的：QQ 侧重名会把文件名改成 "<原名> (1)"，整词比对会漏
            return bool(mark) and (mark in sent or mark in existing)

        todo = [p for p in pics if not _dup(p)]
        dup = len(pics) - len(todo)
        if not todo:
            self._record_album(
                0,
                len(pics),
                0,
                f"微博 {post_id} -> 群 {gid} 相册「{album_name}」：{len(pics)} 张此前已传过，跳过",
            )
            logger.info(
                f"微博 {post_id} 的 {len(pics)} 张图此前已传过相册「{album_name}」"
                f"（群 {gid}），跳过"
            )
            return
        folder = self._batch_dir(gid, post_id)
        live_n = sum(1 for p in todo if p.get("kind") == "live")
        await self._diag(
            f"批次 {folder.name}：待下载 {len(todo)} 张（live {live_n} 张）"
            f"-> 群 {gid} 相册「{album_name}」"
        )
        try:
            dl_t0 = time.monotonic()
            files = await self._download_pics(todo, folder, post_id)
            dl_sec = time.monotonic() - dl_t0
            if not files:
                await self._diag(f"批次 {folder.name}：{len(todo)} 张全部下载失败")
                self._record_album(
                    0,
                    0,
                    len(todo),
                    f"微博 {post_id} -> 群 {gid} 相册「{album_name}」："
                    f"{len(todo)} 张图全部下载失败",
                )
                logger.warning(
                    f"微博 {post_id} -> 群 {gid} 相册「{album_name}」：{len(todo)} 张图全部下载失败"
                )
                return
            total_mb = sum(path.stat().st_size for _, path in files) / 1048576
            # 记在重负载动作之前：整批卡住或进程崩了的时候，只有这行能说明当时在传多大一批
            logger.info(
                f"微博 {post_id} -> 群 {gid} 相册「{album_name}」：{len(files)} 张已下载"
                f"（{total_mb:.1f}MB / {dl_sec:.1f}s），开始上传"
            )
            gif_n = sum(1 for _, path in files if path.suffix.lower() == ".gif")
            # 诊断行先于上传落盘刷盘：死机/被 OOM 杀掉后，看它就知道死机前在传
            # 多大一批、当时的并发与预算配置
            await self._diag(
                f"上传 {len(files)} 张（{total_mb:.1f}MB，GIF {gif_n} 张）"
                f"-> 群 {gid} 相册「{album_name}」"
                f"并发={self._int_cfg('album_upload_concurrency', 3)} "
                f"预算={self._float_cfg('album_upload_payload_mb', 32.0):g}MB"
            )
            up_t0 = time.monotonic()
            ok, fails, marks = await self._upload_files(nc, gid, album_id, album_name, files)
            up_sec = time.monotonic() - up_t0
            if marks:
                await self._remember(gid, album_id, marks)
            summary = (
                f"微博 {post_id} -> 群 {gid} 相册「{album_name}」：成功 {ok}/{len(files)}"
                f"，上传 {up_sec:.1f}s（{total_mb:.1f}MB，下载 {dl_sec:.1f}s）"
            )
            if dup:
                summary += f"（另有 {dup} 张已传过跳过）"
            if fails:
                summary += f"，失败 {len(fails)} 张"
            logger.info(summary)
            self._record_album(
                ok,
                dup,
                len(fails),
                summary,
            )
            await self._diag(f"上传结束：成功 {ok}/{len(files)} 张，失败 {len(fails)}")
            if fails:
                logger.warning(f"相册上传失败明细: {'; '.join(fails[:5])}")
            if ok and bool(self.config.get("album_notify", False)):
                text = f"📷 已将微博图片 {ok} 张上传到相册「{album_name}」"
                if dup:
                    text += f"（另有 {dup} 张此前已传过，跳过）"
                if fails:
                    text += f"，{len(fails)} 张失败"
                await self._notify(session, text)
        finally:
            # 无论提前返回还是异常，批次目录（含 .part 残件）都不留到下一轮。
            # 整批几十到几百 MB 的删除是阻塞调用，留在事件循环里能把整个 AstrBot
            # 钉住数秒（同批其他群的推送与轮询跟着一起卡），丢线程池去删。
            await asyncio.to_thread(shutil.rmtree, folder, True)

    async def _notify(self, session: str, text: str):
        try:
            await self.context.send_message(session, MessageChain().message(text))
        except Exception as e:
            logger.info(f"{PLUGIN_NAME} 相册上传通知发送失败: {e!r}")
