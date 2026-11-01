"""多平台订阅域：抖音 / 小红书 / Instagram 博主更新的登录态抓取与推送。

本包只做"取数 + 判风控 + 排队推送"，不注册任何 `@filter` 处理器，也不直接
调用 AstrBot 的发送接口：投递统一走 `main.py` 装配时注入的
`plugin.deliver_sns_payload()`（微博域里那套 @全体闸门 / 拒收摘段 / 发送超时
的实现），避免把两条投递链各写一遍。

模块划分：

- `constants`：平台/间隔/冷却/信号表等全部可调数值
- `types`：`SnsAccount` / `SnsPost` / `FetchResult` 数据形状
- `guard`：把一次抓取结果折算成冷却、健康度与平台熔断（纯函数 + 薄状态）
- `scheduler`：谁该在这一轮被检查、间隔怎么算、错峰怎么铺
- `state`：`data/sns_state.json` 的读写（原子替换）
- `identity`：登录态与凭据的磁盘口径（0600、脱敏展示）
- `browser`：Playwright 持久化上下文的生命周期（M1 模式共用一份浏览器）
- `fetch_m1` / `fetch_m2`：浏览器取数与带 Cookie 的 HTTP 取数
- `adapters/*`：把平台原始响应解析成 `SnsPost`（纯函数，离线可测）
- `service`：编排（跳过冷却 → 抓取 → 基线/新条目 → 投递 → 落盘）
- `commands`：`订阅` 指令的业务实现（注册仍在 main.py）
"""
