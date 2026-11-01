"""配置读取与图床哈希索引后台刷新（状态 / 后台任务）。"""

from __future__ import annotations

import asyncio
import time

from astrbot.api import (
    logger,
)

try:
    from ..shared.common import (
        as_float,
        as_int,
        bool_or,
        is_blank,
        truthy,
    )
    from .hash_index import (
        REMOTE_HASH_PAYLOAD_KEYS,
    )

except ImportError:  # 兼容插件以独立模块方式加载
    from hash_index import (  # type: ignore[no-redef]
        REMOTE_HASH_PAYLOAD_KEYS,
    )

    from astrbot_plugin_weibo_suite.shared.common import (  # type: ignore[no-redef]
        as_float,
        as_int,
        bool_or,
        is_blank,
        truthy,
    )

try:
    from .constants import (
        _LAYOUT_VERSION,
        _NAPCAT_KEY_MAP,
        _QQ_KEY_MAP,
        _SCALAR_PATHS,
        BUCKET_LEGACY_FIELDS,
        BUCKET_MODES,
        BUCKET_REQUIRED,
    )
except ImportError:  # 兼容插件以独立模块方式加载
    from constants import (  # type: ignore[no-redef]
        _LAYOUT_VERSION,
        _NAPCAT_KEY_MAP,
        _QQ_KEY_MAP,
        _SCALAR_PATHS,
        BUCKET_LEGACY_FIELDS,
        BUCKET_MODES,
        BUCKET_REQUIRED,
    )


class StateMixin:
    """配置读取与图床哈希索引后台刷新（状态 / 后台任务）。"""

    # ------------------------------------------------------------------
    # 图床哈希索引（后台刷新）
    # ------------------------------------------------------------------

    def remote_hash_on(self) -> bool:
        """图床哈希索引开关（默认开；只需 Qdrant 可达，不依赖 embedding）。

        拉取只调用 Qdrant scroll，不碰向量化接口；因此启用判定用 qdrant_ready
        而不是 vector.enabled——只配 Qdrant、不配 embedding 的纯哈希部署同样
        应该能用图床哈希索引。
        """
        return self._bool_cfg("remote_hash_enabled", True) and bool(
            getattr(self.vector, "qdrant_ready", False)
        )

    def _remote_hash_ttl(self) -> int:
        """缓存最长有效期（秒）。缓存新鲜时启动/溯源都不重新拉取。"""
        return max(300, self._int_cfg("remote_hash_ttl", 21600))

    def _remote_hash_max_points(self) -> int:
        """拉取上限（0 = 不限）。用于先小样本验证链路，再放开全量。"""
        return max(0, self._int_cfg("remote_hash_max_points", 0))

    def _remote_hash_cache_fresh(self) -> bool:
        """本地缓存是否仍在 remote_hash_ttl 有效期内且确实有可用条目。

        仅有 built_at 不够：切换 hash_size 后旧缓存全部长度不符，load_cache
        仍会返回成功但 count 为 0；此时若按 TTL 判「新鲜」，图床哈希腿会静默
        空转直到 TTL 到期。要求 count > 0 才能在空缓存时触发重建。
        """
        built_at = float(getattr(self.remote_hash, "built_at", 0.0) or 0.0)
        return (
            built_at > 0
            and self.remote_hash.count > 0
            and (time.time() - built_at) < self._remote_hash_ttl()
        )

    def _maybe_start_remote_hash_refresh(self) -> None:
        """缓存过期或缺失时才启动刷新。

        此前 /溯源 每次都调 start_remote_hash_refresh，而任务完成后 done() 为真，
        于是刷新完的下一次溯源又会翻一遍全量 Qdrant（2.4 万点/47 页）。用 TTL
        收口后：缓存新鲜就不刷；过期后由重启或下一次 /溯源 触发一次重建。
        """
        if not self.remote_hash_on() or self._remote_hash_cache_fresh():
            return
        if time.time() < self._remote_hash_backoff_until:
            return
        self.start_remote_hash_refresh()

    async def _refresh_remote_hash(self) -> None:
        """从 Qdrant 拉全量 pHash 重建本地索引。异常不出后台任务。"""
        try:
            points = await self.vector.scroll_payloads(
                key="phash",
                # 必须显式带上展示字段：scroll 默认只取 key 一个字段，缺 image_url
                # 会让 HashIndex.build 把全部点判成「无直链」跳过（索引恒为 0 条）
                with_payload_keys=list(REMOTE_HASH_PAYLOAD_KEYS),
                page_size=512,
                max_points=self._remote_hash_max_points(),
            )
            count = await asyncio.to_thread(self.remote_hash.build, points)
            if count > 0:
                self._remote_hash_backoff_until = 0.0
            else:
                # 成功但 0 条（hash_size 变更后旧点全被跳过等）：也要退避，
                # 否则每次 /溯源 都会再翻一遍全量。
                self._remote_hash_backoff_until = time.time() + min(self._remote_hash_ttl(), 600.0)
            logger.info(
                f"图床哈希索引已刷新：{count} 条可用"
                f"（远端点位 {len(points) if isinstance(points, list) else 0}），"
                f"缓存于 {self.remote_hash.cache_path}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._remote_hash_backoff_until = time.time() + min(self._remote_hash_ttl(), 600.0)
            logger.warning(f"图床哈希索引刷新失败（沿用已有缓存）: {exc}")

    def start_remote_hash_refresh(self) -> None:
        """启动后台刷新任务（幂等）：已有一个在跑就不再起第二个。

        直接调用表示“此刻就刷”；常规入口应走 _maybe_start_remote_hash_refresh，
        由 remote_hash_ttl 决定要不要刷。

        启动即刷新 vs 按需刷新：刷新要翻 ~47 页（2.4 万点），耗时数秒到数十秒。
        放在启动后台跑，首次 /溯源 就能命中；若拖到首次查询再拉，用户会先吃到
        一次「未命中」——而「未命中」是个确定结论，比查不到更误导人。
        """
        if not self.remote_hash_on():
            return
        if self._remote_hash_task is not None and not self._remote_hash_task.done():
            return
        self._remote_hash_task = asyncio.create_task(self._refresh_remote_hash())

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _dict_cfg(self, key: str) -> dict:
        """取对象型配置；手工改坏配置（非 dict）时退化为空 dict 而不是抛错。"""
        value = self.config.get(key)
        return value if isinstance(value, dict) else {}

    def _ensure_group(self, name: str) -> dict:
        """取（必要时创建）对象型配置组，迁移与新键写入共用。"""
        group = self.config.get(name)
        if not isinstance(group, dict):
            group = {}
            self.config[name] = group
        return group

    def _cfg(self, key: str):
        """读标量配置：新布局（basic / limits）优先，回落旧顶层键。

        迁移未执行（配置被手改、或用户回滚过版本）时旧键仍能生效；两处都有值时
        以新键为准，避免"面板改了不生效"。
        """
        group, field = _SCALAR_PATHS.get(key, ("", key))
        if group:
            value = self._dict_cfg(group).get(field)
            if not is_blank(value):
                return value
        return self.config.get(key)

    def _int_cfg(self, key: str, default: int) -> int:
        return as_int(self._cfg(key), default)

    def _float_cfg(self, key: str, default: float) -> float:
        return as_float(self._cfg(key), default)

    def _bool_cfg(self, key: str, default: bool) -> bool:
        """布尔配置：未配置（缺键/空串/null）时取默认值，与数值配置口径一致。"""
        return bool_or(self._cfg(key), default)

    def _migrate_config_layout(self) -> None:
        """把旧布局搬进 basic / delivery / limits（一次性，带版本标记）。

        为什么不「删旧键」：AstrBot 加载插件配置时会按 schema 删除已不存在的键
        （AstrBotConfig.check_config_integrity 会丢掉 refer_conf 之外的键），键名
        一改，用户的旧值在插件初始化**之前**就已经被清成默认值了。所以旧键继续以
        「隐藏镜像」的形式留在 schema 里（condition 恒假，面板不显示），迁移只做
        搬移 + 打标记，之后不再覆盖——用户此后在面板改的新值不会被旧值盖回去。

        旧值优先于新键：新键在首次加载时已被 schema 默认值填满，此时「新键非空」
        不代表用户改过，只有旧键才是用户真实设置。
        """
        if str(self.config.get("config_layout") or "") == _LAYOUT_VERSION:
            return
        moved = False
        for legacy, (group, field) in _SCALAR_PATHS.items():
            value = self.config.get(legacy)
            if is_blank(value):
                continue
            self._ensure_group(group)[field] = value
            moved = True
        for legacy_group, key_map in (
            ("image_delivery", _NAPCAT_KEY_MAP),
            ("qq_upload", _QQ_KEY_MAP),
        ):
            legacy_items = self.config.get(legacy_group)
            if not isinstance(legacy_items, dict):
                continue
            target = self._ensure_group("delivery")
            for old_key, new_key in key_map.items():
                value = legacy_items.get(old_key)
                if not is_blank(value):
                    target[new_key] = value
                    moved = True
        self.config["config_layout"] = _LAYOUT_VERSION
        try:
            self.config.save_config()
        except Exception as exc:  # 保存失败不影响本次会话
            logger.warning(f"配置布局迁移写入失败（重启后会自动重试）: {exc}")
        if moved:
            logger.info("配置已迁移到新布局：basic / delivery / limits（旧键保留为隐藏镜像）")

    def _napcat_delivery_cfg(self) -> dict:
        """推送通道（NapCat）配置，键名沿用 delivery_settings 的入参约定。

        delivery.napcat_* 优先，逐项回落旧 image_delivery.*（迁移未执行时也能用）。
        """
        new = self._dict_cfg("delivery")
        old = self._dict_cfg("image_delivery")
        return {
            "mode": self._first_value(new.get("napcat_mode"), old.get("mode")),
            "max_side": self._first_value(new.get("napcat_max_side"), old.get("max_side")),
            "quality": self._first_value(new.get("napcat_quality"), old.get("quality")),
            "scaled_url_style": self._first_value(
                new.get("napcat_scaled_url_style"), old.get("scaled_url_style")
            ),
            "verify_scaled": self._first_value(
                new.get("napcat_verify_scaled"), old.get("verify_scaled")
            ),
            "target_kb": self._first_value(new.get("napcat_target_kb"), old.get("target_kb")),
            "webp": self._first_value(new.get("napcat_webp"), old.get("webp")),
            "onebot_image_timeout": self._first_value(
                new.get("napcat_timeout"), old.get("onebot_image_timeout")
            ),
        }

    def _qq_delivery_cfg(self) -> dict:
        """推送通道（QQ 官方）配置，键名沿用 qq_upload_settings 的入参约定。"""
        new = self._dict_cfg("delivery")
        old = self._dict_cfg("qq_upload")
        return {
            "mode": self._first_value(new.get("qq_mode"), old.get("mode")),
            "direct_max_mb": self._first_value(
                new.get("qq_native_max_mb"), old.get("direct_max_mb")
            ),
            "auto_prefer": self._first_value(new.get("qq_auto_prefer"), old.get("auto_prefer")),
            "url_max_mb": self._first_value(new.get("qq_url_max_mb"), old.get("url_max_mb")),
            "max_upload_mb": self._first_value(
                new.get("qq_max_upload_mb"), old.get("max_upload_mb")
            ),
            "on_failure": self._first_value(new.get("qq_on_failure"), old.get("on_failure")),
        }

    @staticmethod
    def _first_value(*values):
        """取第一个非空值（0 / False 是合法配置，不能按假值跳过）。"""
        for value in values:
            if not is_blank(value):
                return value
        return None

    def _migrate_bucket_config(self) -> None:
        """v1.2.x 升级迁移（幂等）：image_bed 组里的储存桶模式与字段搬到
        独立的 storage_bucket 组。

        v1.3.0 起 image_bed.mode 不再含 cloudflare_r2 / oracle_oci（二者
        移入 storage_bucket 组），老配置只在 image_bed 组里存在，启动时
        搬一次；storage_bucket 已有 mode 时只清理残留不覆盖用户新配置。
        """
        bed = self.config.get("image_bed")
        if not isinstance(bed, dict):
            return
        legacy_mode = str(bed.get("mode") or "").strip()
        if legacy_mode not in BUCKET_MODES:
            return
        bucket = self.config.get("storage_bucket")
        if not isinstance(bucket, dict):
            bucket = {}
            self.config["storage_bucket"] = bucket
        if not bucket.get("mode"):
            bucket["mode"] = legacy_mode
            for key in BUCKET_LEGACY_FIELDS:
                value = bed.get(key)
                if value not in (None, "", False):
                    bucket[key] = value
        # 旧下拉里已没有储存桶选项，归位 local；r2_*/oci_* 字段也不在
        # image_bed 组的 schema 里了，一并清掉避免残留
        bed["mode"] = "local"
        for key in BUCKET_LEGACY_FIELDS:
            bed.pop(key, None)
        try:
            self.config.save_config()
        except Exception as e:
            # 保存失败只影响持久化：本次会话已用迁移后的内存配置，
            # 重启后同一条迁移会再次执行（幂等）
            logger.warning(f"储存桶配置迁移写入失败（重启后会自动重试）: {e}")
        logger.info(
            f"已把 v1.2.x 图床配置中的储存桶设置（{legacy_mode}）迁移到 "
            "storage_bucket 组，image_bed 组回归图床模式 local。"
        )

    def _bucket_config(self) -> dict | None:
        """储存桶配置完整时返回可并入图床配置的 dict（含 mode），否则 None。

        完整 = mode 已选 + 必填项齐全 + 公开直链条件满足（R2 需
        r2_public_base_url，OCI 需公共桶或自定义公开地址二选一）：
        缺任一项上传都必然回退本地副本，优先于图床只会白费一次必败上传。
        """
        raw = self._dict_cfg("storage_bucket")
        mode = str(raw.get("mode") or "").strip()
        if mode not in BUCKET_MODES:
            return None
        missing = [f"storage_bucket.{k}" for k in BUCKET_REQUIRED[mode] if is_blank(raw.get(k))]
        if (
            mode == "oracle_oci"
            and not truthy(raw.get("oci_public_bucket"))
            and is_blank(raw.get("oci_public_base_url"))
        ):
            missing.append(
                "storage_bucket.oci_public_bucket / oci_public_base_url（公开直链二选一）"
            )
        if missing:
            logger.warning(
                f"储存桶 {mode} 配置不完整（缺 {'、'.join(missing)}），登记原图将改用图床设置。"
            )
            return None
        cfg = dict(raw)
        cfg["mode"] = mode
        return cfg

    def _effective_bed_cfg(self) -> dict:
        """合并储存桶与图床两组配置：储存桶配置完整时优先（覆盖 mode 与字段）。"""
        bed = dict(self._dict_cfg("image_bed"))
        bucket = self._bucket_config()
        if bucket is not None:
            bed.update(bucket)
        return bed

    def _bed_mode(self) -> str:
        """生效的存储模式（storage_bucket 优先合并后的图床配置）。"""
        return str(self._bed_cfg.get("mode") or "local")

    def _storage_line(self) -> str:
        """/溯源状态 的存储方式一行：标明生效来源（储存桶优先于图床）。"""
        bucket_mode = str(self._dict_cfg("storage_bucket").get("mode") or "").strip()
        bed_mode = str(self._dict_cfg("image_bed").get("mode") or "local")
        active = self._bed_mode()
        if bucket_mode in BUCKET_MODES:
            if active == bucket_mode:
                return f"· 存储方式：储存桶 {bucket_mode}（优先于图床 {bed_mode}）"
            return f"· 存储方式：图床 {active}（储存桶 {bucket_mode} 配置不完整，未生效）"
        return f"· 存储方式：图床 {active}"

    def _engine_choice(self) -> str:
        """当前检索引擎：hash | vector | auto（vector 优先，失败/未命中回退 hash）。"""
        choice = str(self._cfg("search_engine") or "auto").strip().lower()
        return choice if choice in ("hash", "vector", "auto") else "auto"

    def _vector_index_on_register(self) -> bool:
        value = self._dict_cfg("vector_search").get("vector_index_on_register")
        if is_blank(value):
            return True
        return truthy(value)

    def _scan_dirs(self) -> list:
        raw = self._cfg("scan_dirs")
        if isinstance(raw, str):  # 手工配置成单个路径字符串时按一个目录处理
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [d.strip() for d in raw if isinstance(d, str) and d.strip()]
