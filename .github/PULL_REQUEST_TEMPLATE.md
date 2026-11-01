# 变更说明

## 改了什么

<!-- 一句话说明这个 PR 做了什么；如果是修 bug，写清"症状 → 根因" -->

## 为什么

<!-- 关联 issue 编号，或说明为什么现有实现不够 -->

## 自检

- [ ] `ruff check .` 通过
- [ ] `ruff format --check .` 通过
- [ ] `pytest -q` 全绿
- [ ] `python scripts/build_release.py --check` 通过

## 影响面

- [ ] 改了指令或参数 → 已同步 `docs/COMMANDS.md`
- [ ] 改了配置项 → 已同步 `docs/CONFIGURATION.md`
- [ ] 改了面板路由或界面 → 已同步 `docs/WEBUI.md`
- [ ] 新增运行期文件 → 已登记到 `scripts/build_release.py` 的 `PLUGIN_FILES`
- [ ] 改了 `@filter` 处理器 → 确认装饰器只出现在仓库根的 `main.py`
- [ ] 涉及跨域调用 → 确认 `weibo` 与 `trace` 仍未互相导入，装配点在 `main.py`
- [ ] 已在 `CHANGELOG.md` 的 `## [Unreleased]` 下记一条

## 测试

<!-- 新增或修改了哪些用例？修 bug 的话，回归用例是哪一条？ -->

## 数据安全

- [ ] 本次改动没有引入真实账号、UID、内网 IP、本机路径、Cookie 或密钥
- [ ] 新增的日志/错误输出经过 `redact_url()` 或 `sanitize_error_text()` 脱敏
- [ ] 新增的外部请求走 `shared/http.py` 的 SSRF 防护，没有旁路
