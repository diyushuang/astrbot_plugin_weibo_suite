# 贡献指南

感谢你愿意改进这个插件。下面是最小必要规则；如果某条规则让你无法完成某件事，
请先开 issue 讨论，而不是默默绕过。

## 环境

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows，Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

AstrBot 本体不需要装进这个 venv：域模块是普通类，测试直接构造实例即可。

## 提交前必须通过

```bash
ruff check .                                                     # 静态检查（CI 门禁）
ruff format --check .                                            # 格式（CI 门禁）
pytest -q                                                        # 全量测试
python scripts/build_release.py --check   # 发布清单
```

四条全绿再提交。CI（`.github/workflows/ci.yml`）在 Python 3.10 与 3.12 上跑同样的门禁。

## 代码组织

- **`@filter` 装饰器只能出现在仓库根的 `main.py`。** AstrBot 按
  `handler.__module__ == 插件模块路径` 绑定处理器，写在域模块里不会被绑定；
  在域模块里定义 `Star` 子类还会被误注册成独立插件。
- 依赖方向固定为 `main -> weibo | trace | sns -> shared`；`weibo` 与 `trace`
  不得互相导入，跨界装配只能在 `main.py` 完成。
- `views_backend` 不得反向导入业务实现，只调用入口提供的同语义函数。
- 新增运行期文件必须登记到 `scripts/build_release.py` 的 `PLUGIN_FILES` 白名单，
  否则发布包里会缺模块（`tests/test_release_manifest.py` 会拦住）。
- 分层与数据流见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 注释与文档

- 每个模块有中文模块级 docstring，说清"这个模块负责什么、边界在哪"。
- 函数 docstring 写**为什么**这样写，而不是复述代码。遇到反直觉的处理
  （例如"这里故意不重试""这个地方必须用时间戳比较而不是布尔"）一定要写下来。
- 面向用户的改动要同步文档：指令改了更新 [docs/COMMANDS.md](docs/COMMANDS.md)，
  配置改了更新 [docs/CONFIGURATION.md](docs/CONFIGURATION.md)，
  路由或面板改了更新 [docs/WEBUI.md](docs/WEBUI.md)。
- Markdown 约定：一行一个段落、不留行尾空格、表格对齐、代码块标语言、
  中文与英文/数字之间加空格、不用 emoji 装饰标题。
- **不写真实数据**：示例一律用 `example.com`、`1000000001`、`示例博主`
  这类占位值。真实账号、UID、内网 IP、本机盘符路径、Cookie 一律不得进入仓库
  （含测试夹具、注释、CHANGELOG）。

## 测试

- 不 mock 被测逻辑：SQLite 用真实临时库，HTTP 用假客户端，浏览器用实现相同
  窄接口的假对象。
- 修 bug 时先写一条会失败的回归用例，并在 docstring 里写明它对应的真实故障。
- 平移/重构代码期间测试必须保持全绿，不允许用"临时跳过"掩盖回归。

## 提交信息

采用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：

```text
<类型>(<范围>): <一句话说明>
```

- 类型：`feat` / `fix` / `perf` / `refactor` / `docs` / `test` / `chore` / `build` / `ci`
- 范围：域名或模块名，例如 `weibo`、`sns`、`trace`、`archive`、`pipeline`、`docs`
- 说明用中文，不超过 50 字；需要展开时在正文写"症状 → 根因 → 修复"。

示例：

```text
fix(sns): 平台熔断值被当布尔判断，导致平台永久踢出轮询
docs: 补齐指令、配置与面板三份参考文档
```

## 分支与发布

1. 按 [docs/ROADMAP.md](docs/ROADMAP.md) 的阶段推进，不跨阶段做大规模合并。
2. 每个阶段更新 `CHANGELOG.md`（写在 `## [Unreleased]` 下，发布时再挪到版本号下）。
3. 发布前 `python scripts/build_release.py` 生成产物，
   并确认版本号在 `metadata.yaml`、`constants.py`、`weibo/constants.py`、
   `scripts/build_release.py` 四处一致。
4. 真实部署验证前不得删除旧插件数据；迁移必须先备份、再复制、最后计数核对。

## 既有约束（违反会出事故）

- 微博请求统一经过共享限速与游客身份自愈，不允许旁路 `_pace`。
- 媒体下载走 `_media_pace()`，不要套用 API 的 2–5s 间隔（会把一批图拖成几分钟）。
- 归档是原图唯一按需下载方；下游读取本地副本，失败时按设计回退远端。
- 归档是旁路，任何情况下都不能反压监控/推送主链。
- 相册去重以 AstrBot KV 台账为权威，`archive.db` 的相册状态只供观测。
- 群相册的 `sent:{gid}:{album_id}` 是 pid 级、30 天 TTL；不得改成永久 URL 去重。
- 敏感配置不得回显，代理与密钥展示必须脱敏。
- 面板只做展示和同语义操作转发，不在路由层复制业务逻辑。
