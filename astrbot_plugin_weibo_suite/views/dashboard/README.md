# 统一 WebUI 面板

页面按 AstrBot Plugin Views 规范放在 `views/dashboard/`，通过
`window.AstrBotPluginView`（兼容旧 `AstrBotPluginPage`）调用后端：

- 总览：运行状态、四域健康度、推送统计与最近动态；
- 微博监控：账号、推送目标、相册/关键词路由、@全体、队列与预览；
- 归档与图床：归档计数、回填、对账、JSON 导入、管线补跑与命名规则；
- 文案库：清洗进度、检索、CSV/Markdown 导入；
- 溯源与签到：图库/向量状态、后台重扫、历史键修复；
- 错误日志：留存查看与清空。

hash 路由只控制显示哪个页签，不需要多 view 目录。页面只使用
`textContent` 写入服务端数据；补跑、重扫、清空和停止回填均走页内二次确认。
后端路由在 `views_backend/dashboard.py`，不得从本目录导入业务模块。
