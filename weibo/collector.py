"""重点博主图集采集：账号解析、原创判定与归档触发。

collector 只负责「哪些新微博必须进图床」这一层决策，不复制图床/缩略图/向量
逻辑：命中后交给 archive_post，后续仍走 pipeline 的既有流程。

纯函数（collector_flags / collector_post_allowed）不碰网络与数据库，便于单测；
CollectorMixin 只做编排，任何异常都收敛成日志，绝不阻断原推送链路。
"""

from __future__ import annotations

from .pipeline_naming import (
    CollectorFlags,
    NamingContext,
    collector_accounts,
    collector_alias,
    collector_flags,
    collector_post_allowed,
    date_label,
    merged_category_map,
    naming_templates,
    render_directory,
    resolve_category,
)
from .runtime import *


class CollectorMixin:
    """把重点博主的新帖旁路送进归档/管线；不改变普通账号的行为。"""

    def _collector_flags_ref(self) -> CollectorFlags:
        return collector_flags(getattr(self, "config", None))

    def collector_uids(self) -> set[str]:
        return set(collector_accounts(getattr(self, "config", None)))

    def collector_is_account(self, uid: str) -> bool:
        return str(uid or "").strip() in self.collector_uids()

    def collector_account_alias(self, uid: str) -> str:
        return collector_alias(getattr(self, "config", None), uid)

    def collector_post_matches(self, post) -> bool:
        """只按 uid/配图/原创判定，不触发任何请求。"""
        if not isinstance(post, dict):
            return False
        flags = self._collector_flags_ref()
        if not flags.enabled:
            return False
        uid = str(post.get("uid") or "").strip()
        if uid not in collector_accounts(getattr(self, "config", None)):
            return False
        return collector_post_allowed(
            flags,
            is_retweet=bool(post.get("is_retweet")),
            orig_uid=str(post.get("orig_uid") or ""),
            orig_name=str(post.get("orig_name") or ""),
            pic_count=len(post.get("pics") or []),
        )

    def _collector_dependency_ok(self) -> bool:
        """归档总开关与自动归档是否允许采集；缺依赖只告警一次。"""
        enabled_fn = getattr(self, "_archive_enabled", None)
        bool_fn = getattr(self, "_archive_bool", None)
        if not callable(enabled_fn) or not callable(bool_fn):
            return False
        if not enabled_fn():
            self._collector_warn_once("collector 已启用，但 archive.enabled=false，采集不会生效")
            return False
        if not bool_fn("auto_archive", True):
            self._collector_warn_once(
                "collector 已启用，但 archive.auto_archive=false，采集不会自动归档"
            )
            return False
        return True

    def _collector_warn_once(self, message: str) -> None:
        if getattr(self, "_collector_dependency_warned", False):
            return
        self._collector_dependency_warned = True
        logger.warning(message)

    async def collector_consider(self, post) -> bool:
        """命中重点博主时补图并归档；返回是否已归档。

        调用点必须在 seen 标记之后、时效/关键词过滤之前。这里不判断推送开关，
        归档成功后由 monitor 根据 skip_push 决定是否继续走推送。
        """
        if not isinstance(post, dict):
            return False
        flags = self._collector_flags_ref()
        if not flags.enabled:
            return False
        uid = str(post.get("uid") or "").strip()
        if uid not in collector_accounts(getattr(self, "config", None)):
            return False
        if not collector_post_allowed(
            flags,
            is_retweet=bool(post.get("is_retweet")),
            orig_uid=str(post.get("orig_uid") or ""),
            orig_name=str(post.get("orig_name") or ""),
            pic_count=flags.min_images,
        ):
            return False
        if not self._collector_dependency_ok():
            return False
        # 配图数可能只是时间线卡片的截断值（0/9 两种形态），先强制补全再判定。
        ensure = getattr(self, "_ensure_full_pics", None)
        if callable(ensure):
            try:
                await ensure(post, force=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._collector_last_error = f"补图失败: {str(exc)[:160]}"
                logger.warning(f"collector 补图失败（mid={post.get('id')}）：{exc}")
        if not collector_post_allowed(
            flags,
            is_retweet=bool(post.get("is_retweet")),
            orig_uid=str(post.get("orig_uid") or ""),
            orig_name=str(post.get("orig_name") or ""),
            pic_count=len(post.get("pics") or []),
        ):
            return False
        archive_post = getattr(self, "archive_post", None)
        if not callable(archive_post):
            return False
        try:
            ok = bool(await archive_post(post, source="collector"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._collector_last_error = str(exc)[:200]
            logger.warning(f"collector 归档失败（mid={post.get('id')}）：{exc}")
            return False
        if ok:
            post["_collector_collected"] = True
            post["_collector_uid"] = uid
            self._collector_last_error = ""
            logger.info(
                "collector 采集入库：uid=%s mid=%s pics=%d",
                uid,
                post.get("id"),
                len(post.get("pics") or []),
            )
        return ok

    def collector_status(self) -> dict:
        """给指令与面板用的只读快照；任何一项查询失败都不抛。"""
        config = getattr(self, "config", None)
        flags = collector_flags(config)
        specs = collector_accounts(config)
        mapping = merged_category_map(config)
        dir_template, _ = naming_templates(config)
        store = None
        try:
            store_fn = getattr(self, "archive_store", None)
            store = store_fn() if callable(store_fn) else None
        except Exception:
            store = None
        accounts = []
        known_accounts = getattr(self, "accounts", None)
        known_accounts = known_accounts if isinstance(known_accounts, dict) else {}
        for uid, spec in specs.items():
            account = spec.alias or self._collector_known_author(uid) or uid
            category = resolve_category(mapping, uid=uid, account=account)
            ctx = NamingContext(
                account=account,
                category=category,
                date=date_label(),
                index=1,
                title="标题",
            )
            directory = render_directory(dir_template, ctx)
            posts = 0
            stats: dict = {}
            if store is not None:
                try:
                    posts = int(store.count_posts(uid) or 0)
                except Exception:
                    posts = 0
                try:
                    stats = dict(store.media_stats(uid) or {})
                except Exception:
                    stats = {}
            accounts.append(
                {
                    "uid": uid,
                    "account": account,
                    "category": category,
                    "directory": directory,
                    "random_command": f"/随机图 {directory}",
                    "monitored": uid in known_accounts,
                    "posts": posts,
                    "images": int(stats.get("total", 0) or 0),
                    "states": {
                        state: int(stats.get(state, 0) or 0)
                        for state in ("pending", "downloaded", "album", "bed", "thumb", "vector")
                    },
                    "failed": int(stats.get("failed", 0) or 0),
                }
            )
        deps = self._collector_dependencies()
        return {
            "enabled": bool(flags.enabled and specs),
            "configured": bool(flags.enabled),
            "include_retweets": flags.include_retweets,
            "allow_retweet_from": list(flags.allow_retweet_from),
            "min_images": flags.min_images,
            "skip_push": flags.skip_push,
            "accounts": accounts,
            "dependencies": deps,
            "last_error": str(getattr(self, "_collector_last_error", "") or ""),
        }

    def _collector_known_author(self, uid: str) -> str:
        info = (getattr(self, "accounts", None) or {}).get(uid)
        if isinstance(info, dict):
            name = str(info.get("name") or "").strip()
            if name and name != uid:
                return name
        return ""

    def _collector_dependencies(self) -> dict:
        deps = {
            "archive": False,
            "auto_archive": False,
            "download_originals": False,
            "pipeline": False,
            "pipeline_services": False,
        }
        try:
            enabled_fn = getattr(self, "_archive_enabled", None)
            bool_fn = getattr(self, "_archive_bool", None)
            deps["archive"] = bool(enabled_fn()) if callable(enabled_fn) else False
            deps["auto_archive"] = (
                bool(bool_fn("auto_archive", True)) if callable(bool_fn) else False
            )
            deps["download_originals"] = (
                bool(bool_fn("download_originals", True)) if callable(bool_fn) else False
            )
        except Exception:
            pass
        try:
            pipeline_fn = getattr(self, "pipeline_enabled", None)
            deps["pipeline"] = bool(pipeline_fn()) if callable(pipeline_fn) else False
            services_fn = getattr(self, "pipeline_services", None)
            deps["pipeline_services"] = bool(services_fn()) if callable(services_fn) else False
        except Exception:
            pass
        return deps


__all__ = [
    "CollectorFlags",
    "CollectorMixin",
    "collector_flags",
    "collector_post_allowed",
]
