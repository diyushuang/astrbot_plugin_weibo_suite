# 参与开发

## 拆分纪律

- 依赖方向固定为 `main -> weibo/trace -> shared`；`weibo` 与 `trace` 不得互相导入。
- 跨域协作只能通过 `main.py` 或 `weibo/pipeline.py` 的参数注入完成。
- `views_backend` 不得反向导入业务实现；只调用入口提供的同一语义业务函数。
- 新增任何运行期文件都必须登记到 `astrbot_plugin_weibo_suite/scripts/build_release.py`
  的 `PLUGIN_FILES` 白名单，并由 `tests/test_release_manifest.py` 校验。
- 平移代码期间测试必须保持全绿，不允许用“临时跳过”掩盖回归。

## 开发流程

1. 按 `开发计划.md` 的阶段推进，不跨阶段做大规模合并。
2. 提交前运行 `ruff check .` 与 `pytest -q`。
3. 每个阶段更新 `CHANGELOG.md`，并通过 `python -m astrbot_plugin_weibo_suite.scripts.build_release`
   验证发布白名单与版本号。
4. 真实部署验证前不得删除旧插件数据；迁移必须先备份、再复制、最后计数核对。

## 既有约束

- 微博请求统一经过共享限速与游客身份自愈，不允许旁路 `_pace`。
- 归档是原图唯一按需下载方；下游读取本地副本，失败时按设计回退远端。
- 相册去重以 AstrBot KV 台账为权威，archive.db 的相册状态只供观测。
- 群相册的 `sent:{gid}:{album_id}` 是 pid 级、30 天 TTL；不得改成永久 URL 去重。
- 敏感配置不得回显，代理与密钥展示必须脱敏。
- 面板只做展示和同语义操作转发，不在路由层复制业务逻辑。
