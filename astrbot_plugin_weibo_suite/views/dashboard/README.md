# 面板静态资源

`views/dashboard/` 是管理面板的前端，纯静态资源，随插件目录一起发布：

- `index.html`：页签导航与各卡片的表单骨架。
- `app.js`：全部页面逻辑（原生 JavaScript，ES module），通过插件桥接对象的 `apiGet()` / `apiPost()` 调后端，并负责页内确认框与 20 秒自动刷新。
- `style.css`：面板样式，跟随 AstrBot 主题在明暗之间切换。

数据来自 `astrbot_plugin_weibo_suite/views_backend/` 注册的 35 条路由（`dashboard.py` 28 条、`sns_api.py` 7 条），由宿主转发到 `/api/v1/plugins/extensions/<插件名>/<endpoint>`。本目录只有静态文件，不导入任何业务模块。

没有构建步骤：不需要 npm、打包器或编译，原生 JavaScript 直接运行。但静态资源是随插件加载进视图的，改完前端后需要重载插件，页面刷新才能看到新版本。

约定：

- 服务端数据只通过 `textContent` 写入，避免注入。
- 破坏性操作统一走 `confirmDialog()`；插件页 iframe 没有 `allow-modals`，原生 `confirm()` 会被静默拦截并当成取消。
- 新增页签或卡片时，需要同时更新 `app.js` 里的 `TAB_DEFS` 与卡片归属判断。

页签功能、接口清单与安全边界见 [docs/WEBUI.md](../../../docs/WEBUI.md)；分层与提交约定见 [CONTRIBUTING.md](../../../CONTRIBUTING.md)。
