import {
  initSettings,
  settingsConfirmLeave,
  settingsHasUnsaved,
  settingsOnShown,
} from "./settings.js";

/* 微博实时转发 · 控制面板逻辑
 * 通过 AstrBot 插件页面桥接对象与插件后端通信：
 *   window.AstrBotPluginView（新版）/ window.AstrBotPluginPage（旧版）
 * 所有动态内容一律用 textContent 写入，不拼 HTML，防注入。 */

const POLL_MS = 20000;

/* 取消后的可撤销窗口（秒）。由后端 overview.status.cancel_undo_seconds 下发——
 * 窗口常量只在 main.py 定义一份，页面照自己的常量估算早晚会与后端对不上。 */
let cancelUndoSeconds = 1800;

/* @全体成员总开关的当前状态，由每次 overview 覆盖；卡片上的按钮按它决定这一次
 * 是"开启"还是"关闭"，所以点击时取反发出 set_switch 就够了。 */
let atAllOn = false;

function undoMinutes() {
  return Math.max(1, Math.round(cancelUndoSeconds / 60));
}

function fmtRemain(seconds) {
  if (!seconds || seconds <= 0) return "即将清理";
  return `约 ${Math.ceil(seconds / 60)} 分钟`;
}

const KIND_LABEL = {
  push: "推送成功",
  push_fail: "推送放弃",
  push_cancel: "推送取消",
  push_restore: "推送恢复",
  album: "相册上传",
  album_fail: "相册失败",
  video_fail: "视频未转发",
  risk: "风控冷却",
  error: "检查异常",
  at_all: "@全体成员",
  info: "事件",
};

const LEVEL_LABEL = {
  ERROR: "错误",
  CRITICAL: "严重",
  WARNING: "警告",
};

const els = {};
// 命名规则表单有未保存改动时，20 秒自动刷新不得把输入框覆盖回旧值
let namingDirty = false;
for (const id of document.querySelectorAll("[id]")) els[id.id] = id;

/* ---------- 工具 ---------- */

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== "") node.textContent = text;
  return node;
}

/* 时间兜底：后端缺字段、字段为 null、或脏数据（"abc"、负数、NaN）时，
 * 绝不能把 NaN 直接拼进界面（"NaN-NaN-NaN NaN:NaN" / "NaN 天前"）。 */
function timestampOf(ts) {
  if (ts === null || ts === undefined || ts === "") return 0;
  const n = Number(ts);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

function relTime(ts) {
  const seconds = timestampOf(ts);
  if (!seconds) return "时间未知";
  const diff = Date.now() / 1000 - seconds;
  if (diff < 0) {
    const future = -diff;
    if (future < 60) return "1 分钟内";
    if (future < 3600) return `${Math.round(future / 60)} 分钟后`;
    if (future < 86400) return `${Math.round(future / 3600)} 小时后`;
    return `${Math.round(future / 86400)} 天后`;
  }
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
  return `${Math.round(diff / 86400)} 天前`;
}

function fmtTime(ts) {
  if (ts === null || ts === undefined || ts === "") return "—";
  const seconds = timestampOf(ts);
  const d = seconds ? new Date(seconds * 1000) : new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/* 后端没给耗时（0 / null / 非数字）时不再显示「用时 0s」——那等于谎报。 */
function fmtElapsed(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n) || n <= 0) return "";
  if (n < 60) return ` · 用时 ${n.toFixed(1)} 秒`;
  const minutes = Math.floor(n / 60);
  return ` · 用时 ${minutes} 分 ${Math.round(n % 60)} 秒`;
}

function dayKey(offsetDays) {
  const d = new Date(Date.now() - offsetDays * 86400000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

let statusTimer = null;
function setStatus(message, tone = "") {
  const node = els.status;
  if (!message) {
    node.hidden = true;
    node.textContent = "";
    node.className = "status-line";
    return;
  }
  if (statusTimer) clearTimeout(statusTimer);
  node.hidden = false;
  node.textContent = message;
  node.className = `status-line ${tone}`.trim();
  if (tone === "ok") statusTimer = setTimeout(() => setStatus(""), 6000);
}

function applyTheme(ctx) {
  const dark = ctx && (ctx.isDark === true || ctx.isDark === "true");
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

function busy(btn, on) {
  if (!btn) return;
  btn.disabled = on;
}

/* ---------- 页内确认框 ----------
 * AstrBot 插件页 iframe 的 sandbox 只有 allow-scripts/forms/downloads，没有
 * allow-modals：window.confirm() 会被浏览器静默拦截并返回 false，导致「移除」
 * 永远无法确认。所有原生弹窗都不能用，确认交互一律走本对话框。 */

function confirmDialog(message) {
  return new Promise((resolve) => {
    const previouslyFocused = document.activeElement;
    const mask = el("div", "modal-mask");
    const box = el("div", "modal");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.tabIndex = -1;
    const suffix = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    const title = el("div", "modal-title", "请确认操作");
    title.id = `modal-title-${suffix}`;
    const body = el("div", "modal-body", message);
    body.id = `modal-body-${suffix}`;
    box.setAttribute("aria-labelledby", title.id);
    box.setAttribute("aria-describedby", body.id);
    box.append(title, body);
    const actions = el("div", "modal-actions");
    const cancel = el("button", "btn", "取消");
    const ok = el("button", "btn danger", "确认");
    cancel.type = "button";
    ok.type = "button";
    actions.append(cancel, ok);
    box.append(actions);
    mask.append(box);
    document.body.append(mask);

    // 背景锁滚动：弹窗打开期间页面不能继续滚（否则弹窗跟着视口跑、
    // 底层按钮也被滚进可点击区域）。
    const previousOverflow = document.documentElement.style.overflow;
    document.documentElement.style.overflow = "hidden";

    const close = (result) => {
      document.removeEventListener("keydown", onKey);
      mask.remove();
      document.documentElement.style.overflow = previousOverflow;
      // 关闭后焦点回到触发按钮：否则键盘用户会掉到文档开头
      if (previouslyFocused && previouslyFocused.isConnected &&
          typeof previouslyFocused.focus === "function") {
        try { previouslyFocused.focus(); } catch (e) { /* 元素已被移除时忽略 */ }
      }
      resolve(result);
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") {
        ev.preventDefault();
        close(false);
        return;
      }
      if (ev.key !== "Tab") return;
      // 焦点陷阱：Tab 只在「取消 / 确认」之间循环，不跑到背景按钮
      const first = cancel;
      const last = ok;
      if (ev.shiftKey && document.activeElement === first) {
        ev.preventDefault();
        last.focus();
      } else if (!ev.shiftKey && document.activeElement === last) {
        ev.preventDefault();
        first.focus();
      } else if (!box.contains(document.activeElement)) {
        ev.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    // 点击遮罩视为取消；点在对话框内不关闭
    mask.addEventListener("click", (ev) => {
      if (ev.target === mask) close(false);
    });
    cancel.addEventListener("click", () => close(false));
    ok.addEventListener("click", () => close(true));
    // 初始焦点给「取消」：危险操作不应该是回车默认项
    cancel.focus();
  });
}

/* ---------- 渲染 ---------- */

function tile(label, value, sub, tone) {
  const box = el("div", `tile ${tone || ""}`.trim());
  box.append(el("div", "t-label", label), el("div", "t-value", value));
  if (sub) box.append(el("div", "t-sub", sub));
  return box;
}

/* 增量列表渲染：按业务键复用节点，只有签名变化的行才重建。
 * 后端的列表上限是待推送 100 / 动态 50 / 错误 50，过去每 20 秒把整张表
 * 拆掉重插，既丢滚动位置、又打飞按钮焦点、还会触发整页重排。 */
function safeJSON(value) {
  try {
    return JSON.stringify(value);
  } catch (e) {
    return String(value);
  }
}

function syncList(container, items, keyOf, build, options = {}) {
  if (!container) return;
  const sigOf = options.sig || ((item) => safeJSON(item));
  const prevMap = container.__syncMap || new Map();
  const prevText = container.__syncText || "";
  const seen = new Map();
  const entries = [];
  for (const item of items) {
    let key = String(keyOf(item));
    const n = (seen.get(key) || 0) + 1;
    seen.set(key, n);
    if (n > 1) key = `${key}#${n}`;
    entries.push({ item, key, sig: String(sigOf(item)) });
  }
  const text = entries.map((e) => `${e.key}\u0001${e.sig}`).join("\u0002");
  if (text === prevText && container.childElementCount === entries.length) return;

  const nextMap = new Map();
  for (const entry of entries) {
    const old = prevMap.get(entry.key);
    let node = old;
    if (!old || old.getAttribute("data-sig") !== entry.sig) {
      if (old) old.remove();
      node = build(entry.item);
      node.setAttribute("data-sig", entry.sig);
      node.setAttribute("data-k", entry.key);
    }
    nextMap.set(entry.key, node);
  }
  for (const [key, node] of prevMap) {
    if (!nextMap.has(key) && node.parentNode === container) node.remove();
  }

  const place = (index) => {
    const node = nextMap.get(entries[index].key);
    const ref = container.children[index] || null;
    if (ref !== node) container.insertBefore(node, ref);
  };
  // 首屏一次性要插几十行时按帧分片，避免一帧内插入上百个单元格；
  // 后续轮询只移动/更新变化行，不再整表重建。
  //
  // paint token：分片是异步的，而 __syncMap/__syncText 本轮就已写入。若分片
  // 还没画完又来一次更新（旧节点已被 remove），旧帧仍持有旧的 entries/nextMap，
  // 会把已删除的行重新 insertBefore 回来并永久残留；切页签后还会继续往隐藏
  // 面板里插。每帧校验 token，换代即停。
  const token = (container.__syncToken || 0) + 1;
  container.__syncToken = token;
  const firstPaint = container.childElementCount <= 1;
  if (firstPaint && entries.length > 30) {
    let index = 0;
    const step = () => {
      if (container.__syncToken !== token) return;
      // 页签被切走后容器在 display:none 的祖先里，measurable rect 为空：
      // 停止分片，等下次该页签可见时由新的 syncList 续画。
      if (typeof container.getClientRects === "function" && container.getClientRects().length === 0) {
        return;
      }
      const end = Math.min(entries.length, index + 20);
      while (index < end) {
        place(index);
        index += 1;
      }
      if (index < entries.length) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  } else {
    for (let index = 0; index < entries.length; index += 1) place(index);
  }
  container.__syncMap = nextMap;
  container.__syncText = text;
}

function renderTiles(container, specs) {
  syncList(
    container,
    specs,
    (spec) => spec.label,
    (spec) => tile(spec.label, spec.value, spec.sub, spec.tone),
    { sig: (spec) => `${spec.value}\u0001${spec.sub || ""}\u0001${spec.tone || ""}` }
  );
}

/* 动态/错误里的相对时间不参与行签名，免得每分钟一次全表重建；
 * 统一用 data-ts 扫描，只改文本节点。 */
function refreshRelativeTimes() {
  for (const node of document.querySelectorAll("[data-ts]")) {
    const ts = Number(node.dataset.ts);
    if (!ts) continue;
    const text = relTime(ts);
    if (node.textContent !== text) node.textContent = text;
  }
}

function renderStatus(st) {
  const grid = els["status-grid"];

  // 「停止本轮推送」只在真有轮次在跑时可用：平时点它只会得到一句"没有正在
  // 进行的轮次"，不如直接把按钮置灰并说明原因
  const stopBtn = els["btn-stop-round"];
  if (stopBtn) {
    stopBtn.disabled = !st.push_round_active;
    stopBtn.title = st.push_round_active
      ? "结束当前这一轮推送，剩余条目原样留队、下一轮继续"
      : "当前没有正在进行的推送轮次";
  }

  renderHealth(st);
  renderTiles(grid, [
    {
      label: "轮询任务",
      value: st.poll_running ? (st.checking ? "运行中 · 正在检查" : "运行中") : "已停止",
      sub: `间隔 ${st.poll_interval} 秒`,
      tone: st.poll_running ? "ok" : "err",
    },
    {
      label: "上次检查",
      value: st.last_check_ts ? relTime(st.last_check_ts) : "尚未检查",
      sub: st.last_check_ts ? fmtTime(st.last_check_ts) : "启动后首轮检查完成后更新",
    },
    {
      label: "下次检查",
      value: st.poll_running && st.next_check_ts ? `约 ${relTime(st.next_check_ts)}` : "—",
      sub: "按轮询间隔估算，实际有随机抖动",
    },
    {
      label: "待推送队列",
      value: `${st.pending_count} 条`,
      sub:
        st.pending_retrying > 0
          ? `其中 ${st.pending_retrying} 条在重试` +
            (st.cancelled_count > 0 ? ` · 另有 ${st.cancelled_count} 条已取消待恢复` : "")
          : st.cancelled_count > 0
            ? `队列状态健康 · 另有 ${st.cancelled_count} 条已取消待恢复`
            : "队列状态健康",
      tone: st.pending_retrying > 0 ? "warn" : "",
    },
    {
      label: "风控状态",
      value: st.risk_blocked
        ? `冷却中 · 剩约 ${Math.max(1, Math.round((st.risk_until_ts - Date.now() / 1000) / 60))} 分钟`
        : "正常",
      sub: st.risk_blocked ? "触发 418/432 后的全局冷却" : "无 IP 级风控限制",
      tone: st.risk_blocked ? "warn" : "ok",
    },
    {
      label: "身份模式",
      value: st.identity_mode === "custom" ? "自定义 Cookie" : "免 Cookie 游客",
      sub:
        st.identity_mode === "custom"
          ? `Cookie 键：${(st.identity_cookie_keys || []).join("、") || "—"}`
          : st.visitor_ts
            ? `游客身份 ${relTime(st.visitor_ts)}更新`
            : "游客身份未获取",
    },
    { label: "出网方式", value: st.proxy ? "代理" : "直连", sub: st.proxy || "未配置代理" },
    {
      label: "群相册",
      value: st.album_enabled ? "开启" : "关闭",
      sub: st.album_enabled
        ? st.ffmpeg
          ? "ffmpeg 可用，实况图可转 GIF"
          : "ffmpeg 缺失，实况图仅传封面静图"
        : "在插件配置面板开启",
      tone: st.album_enabled ? "ok" : "",
    },
    memTileSpec(st.mem),
    diskTileSpec(st.disk),
    tasksTileSpec(st.bg_tasks),
  ]);
}

/* 顶部健康摘要：先给结论，再展开 11 个明细 tile。 */
function renderHealth(st) {
  const strip = els["health-strip"];
  if (!strip) return;
  const mem = st.mem || {};
  const disk = st.disk || {};
  const specs = [
    {
      label: "轮询",
      value: st.poll_running ? "运行中" : "已停止",
      tone: st.poll_running ? "ok" : "err",
    },
    {
      label: "风控",
      value: st.risk_blocked ? "冷却中" : "正常",
      tone: st.risk_blocked ? "warn" : "ok",
    },
    {
      label: "队列",
      value: `${st.pending_count || 0} 条`,
      tone: st.pending_retrying > 0 ? "warn" : "ok",
    },
    {
      label: "内存",
      value: mem.total_mb ? `${fmtMB(mem.available_mb)} 可用` : "不可读",
      tone: mem.total_mb ? (mem.available_mb < 512 ? "err" : mem.available_mb < 1024 ? "warn" : "ok") : "",
    },
    {
      label: "磁盘",
      value: disk.total_mb ? `${fmtMB(disk.free_mb)} 可用` : "不可读",
      tone: disk.total_mb ? (disk.free_mb < 1024 ? "err" : disk.free_mb < 2048 ? "warn" : "ok") : "",
    },
  ];
  syncList(
    strip,
    specs,
    (spec) => spec.label,
    (spec) => {
      const box = tile(spec.label, spec.value, "", spec.tone);
      box.classList.add("health-pill");
      return box;
    },
    { sig: (spec) => `${spec.value}\u0001${spec.tone || ""}` }
  );
}

function memTileSpec(mem) {
  if (!mem || !mem.total_mb) {
    return { label: "服务器内存", value: "—", sub: "当前环境读不到内存水位", tone: "" };
  }
  const free = mem.available_mb;
  const total = mem.total_mb;
  // 两次"服务器无响应"都是内存顶穿：<512MB 视为危险，<1GB 提示留意
  const tone = free < 512 ? "err" : free < 1024 ? "warn" : "ok";
  return {
    label: "服务器内存",
    value: `${fmtMB(free)} 可用`,
    sub: `共 ${fmtMB(total)} · 已用 ${Math.round(((total - free) / total) * 100)}%`,
    tone,
  };
}

function diskTileSpec(disk) {
  if (!disk || !disk.total_mb) {
    return { label: "磁盘余量", value: "—", sub: "数据目录所在盘", tone: "" };
  }
  // 插件媒体下载有 1GB 保护线，余量逼近它就该清盘了
  const tone = disk.free_mb < 1024 ? "err" : disk.free_mb < 2048 ? "warn" : "";
  return {
    label: "磁盘余量",
    value: fmtMB(disk.free_mb),
    sub: `共 ${fmtMB(disk.total_mb)} · 媒体下载/视频缓存落盘位置`,
    tone,
  };
}

function tasksTileSpec(bg) {
  const video = (bg && bg.video_download) || 0;
  const album = (bg && bg.album_upload) || 0;
  const n = video + album;
  return {
    label: "后台任务",
    value: n ? `${n} 个在途` : "空闲",
    sub: video || album ? `视频下载 ${video} · 相册上传 ${album}` : "无在途的视频下载与相册上传",
    tone: n ? "warn" : "",
  };
}

function fmtMB(mb) {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;
}

function renderListEmpty(listEl, emptyEl, count) {
  emptyEl.hidden = count > 0;
}

function accountRow(acc) {
  const li = el("li");
  const main = el("div", "li-main");
  const name = el("span", "clamp-2", acc.name);
  name.title = acc.name || "";
  main.append(name);
  main.append(el("span", "li-sub", `uid ${acc.uid}`));
  li.append(main);
  if (acc.fail_count > 0) {
    li.append(
      el(
        "span",
        "badge err",
        acc.next_retry_ts > Date.now() / 1000
          ? `连续失败 ${acc.fail_count} 次`
          : `失败 ${acc.fail_count} 次`
      )
    );
  } else if (!acc.baseline_done) {
    li.append(el("span", "badge warn", "待建基线"));
  }
  li.append(el("span", "badge", `已见 ${acc.seen_count}`));
  if (acc.push_ok > 0) li.append(el("span", "badge", `已推 ${acc.push_ok}`));
  const atBtn = el(
    "button",
    "btn-mini btn-at" + (acc.at_all ? " on" : ""),
    acc.at_all ? "@全体 开" : "@全体"
  );
  atBtn.type = "button";
  atBtn.title = acc.at_all
    ? "点击取消：该博主的新微博推送不再 @全体成员"
    : "点击开启：该博主的新微博推送时 @全体成员";
  atBtn.addEventListener("click", () => {
    const turningOn = !acc.at_all;
    act(atBtn, "at-all-uids", { action: turningOn ? "add" : "remove", uid: acc.uid }, () =>
      setStatus(turningOn ? `已设为 @全体：${acc.name}` : `已取消 @全体：${acc.name}`, "ok")
    );
  });
  li.append(atBtn);
  const rm = el("button", "btn-mini", "移除");
  rm.type = "button";
  rm.addEventListener("click", async () => {
    const confirmed = await confirmDialog(`确定取消监控「${acc.name}」（uid ${acc.uid}）吗？`);
    if (!confirmed) return;
    act(rm, "accounts", { action: "remove", uid: acc.uid }, () =>
      setStatus(`已取消监控：${acc.name}`, "ok")
    );
  });
  li.append(rm);
  return li;
}

function renderAccounts(accounts) {
  els["accounts-count"].textContent = String(accounts.length);
  const list = els["account-list"];
  syncList(list, accounts, (acc) => acc.uid, accountRow);
  renderListEmpty(list, els["accounts-empty"], accounts.length);
}

/* @全体「上一次到底生效没有」的措辞：QQ 对无权限/超次数的 at 段不会报错，
   只会把它降级成一条纯文本，所以面板必须说清是"已确认落地"还是"被降级" */
const AT_ALL_STATE_LABEL = {
  ok: "已确认落地",
  degraded: "被降级为纯文本",
  skipped: "未发送",
  failed: "发送失败",
  sent: "已发出",
  unknown: "未确认",
};

function renderAtAllLast(s) {
  const node = els["at-all-last"];
  if (!node) return;
  const limit = Number(s.quota_limit || 0);
  const used = Number(s.quota_used || 0);
  const quota = limit ? `24 小时内已用 ${used}/${limit} 次（所有群共享）` : `24 小时内已用 ${used} 次（未设上限）`;
  const last = s.last || {};
  const parts = [quota];
  if (last.detail) {
    const label = AT_ALL_STATE_LABEL[last.state] || last.state || "事件";
    parts.push(`上次 ${last.state === "ok" ? "✅" : "⚠️"} ${label}（${fmtTime(last.ts)}）：${last.detail}`);
  }
  node.textContent = parts.join(" · ");
  node.hidden = false;
}

/* 「推送 @全体成员」卡片：总开关 + 博主名单 + 关键词名单，三道闸门都在这一张卡里改 */
function renderAtAll(settings) {
  const s = settings || {};
  atAllOn = !!s.at_all;
  els["btn-at-all-switch"].textContent = atAllOn ? "关闭 @全体" : "开启 @全体";
  const state = els["at-all-switch-state"];
  state.textContent = atAllOn ? "总开关：开启" : "总开关：关闭";
  state.className = "badge" + (atAllOn ? " ok" : "");
  renderAtAllLast(s);

  const bloggers = s.bloggers || [];
  els["at-all-count"].hidden = bloggers.length === 0;
  els["at-all-count"].textContent = String(bloggers.length);
  const uidList = els["at-uid-list"];
  syncList(uidList, bloggers, (b) => b.uid, (b) => {
    const li = el("li");
    const main = el("div", "li-main");
    const name = el("span", "clamp-2", b.name);
    name.title = b.name || "";
    main.append(name);
    main.append(el("span", "li-sub", `uid ${b.uid}`));
    li.append(main);
    if (!b.monitored) li.append(el("span", "badge warn", "已取消监控"));
    const rm = el("button", "btn-mini", "移除");
    rm.type = "button";
    rm.addEventListener("click", () =>
      act(rm, "at-all-uids", { action: "remove", uid: b.uid }, () =>
        setStatus(`已从 @全体名单移除：${b.name}`, "ok")
      )
    );
    li.append(rm);
    return li;
  });
  renderListEmpty(uidList, els["at-uid-empty"], bloggers.length);

  const keywords = s.keywords || [];
  const kwList = els["at-kw-list"];
  syncList(kwList, keywords, (kw) => kw, (kw) => {
    const li = el("li");
    li.append(el("div", "li-main", kw));
    const rm = el("button", "btn-mini", "移除");
    rm.type = "button";
    rm.addEventListener("click", () =>
      act(rm, "at-all-settings", { action: "remove_keyword", keyword: kw }, () =>
        setStatus(`已移除 @全体关键词：${kw}`, "ok")
      )
    );
    li.append(rm);
    return li;
  });
  renderListEmpty(kwList, els["at-kw-empty"], keywords.length);
}

function sessionRow(umo) {
  const li = el("li");
  const parts = umo.split(":");
  const main = el("div", "li-main");
  if (parts.length === 3) {
    main.append(el("span", "", parts[2]));
    main.append(el("span", "li-sub", `${parts[0]} · ${parts[1]}`));
  } else {
    main.append(el("span", "", umo));
  }
  li.append(main);
  const rm = el("button", "btn-mini", "移除");
  rm.type = "button";
  rm.addEventListener("click", async () => {
    const confirmed = await confirmDialog(`确定解除该推送目标吗？\n${umo}`);
    if (!confirmed) return;
    act(rm, "sessions", { action: "remove", umo }, () => setStatus("已移除推送目标", "ok"));
  });
  li.append(rm);
  return li;
}

function renderSessions(sessions) {
  els["sessions-count"].textContent = String(sessions.length);
  const list = els["session-list"];
  syncList(list, sessions, (umo) => umo, sessionRow, { sig: () => "" });
  renderListEmpty(list, els["sessions-empty"], sessions.length);
}

function ruleRow(rule) {
  const tr = el("tr");
  const nameCell = el("td");
  nameCell.append(el("span", "", rule.name || rule.uid));
  nameCell.append(el("span", "dim", `（${rule.uid}）`));
  tr.append(nameCell, el("td", "", rule.gid), el("td", "", rule.album));
  const op = el("td", "col-op");
  const rm = el("button", "btn-mini", "移除");
  rm.type = "button";
  rm.addEventListener("click", async () => {
    const confirmed = await confirmDialog(
      `确定移除该相册绑定吗？\n${rule.name || rule.uid} → 群 ${rule.gid} 相册「${rule.album}」`
    );
    if (!confirmed) return;
    act(
      rm,
      "album-rules",
      { action: "remove", uid: rule.uid, gid: rule.gid },
      () => setStatus("已移除相册绑定规则", "ok")
    );
  });
  op.append(rm);
  tr.append(op);
  return tr;
}

function renderRules(rules) {
  els["rules-count"].textContent = String(rules.length);
  const tbody = els["rule-rows"];
  syncList(tbody, rules, (rule) => `${rule.uid}|${rule.gid}`, ruleRow);
  renderListEmpty(tbody, els["rules-empty"], rules.length);
}

function keywordRuleRow(rule) {
  const tr = el("tr");
  const scopeCell = el("td");
  if (rule.uid) {
    scopeCell.append(el("span", "", rule.name || rule.uid));
    scopeCell.append(el("span", "dim", `（${rule.uid}）`));
  } else {
    scopeCell.append(el("span", "badge", "全局"));
  }
  tr.append(scopeCell, el("td", "", rule.keyword), el("td", "", rule.gid), el("td", "", rule.album));
  const op = el("td", "col-op");
  const rm = el("button", "btn-mini", "移除");
  rm.type = "button";
  rm.addEventListener("click", async () => {
    const confirmed = await confirmDialog(
      `确定移除该关键词路由吗？\n${rule.name || "全局"} 命中「${rule.keyword}」→ 群 ${rule.gid} 相册「${rule.album}」`
    );
    if (!confirmed) return;
    act(
      rm,
      "keyword-rules",
      { action: "remove", uid: rule.uid || "", keyword: rule.keyword, gid: rule.gid },
      () => setStatus("已移除关键词路由规则", "ok")
    );
  });
  op.append(rm);
  tr.append(op);
  return tr;
}

function renderKeywordRules(rules) {
  els["kw-rules-count"].textContent = String(rules.length);
  const tbody = els["kw-rule-rows"];
  syncList(tbody, rules, (rule) => `${rule.uid || "*"}|${rule.keyword}|${rule.gid}`, keywordRuleRow);
  renderListEmpty(tbody, els["kw-rules-empty"], rules.length);
}

/* 待推送是最长的一张表（后端最多回 100 条 × 5 列）。默认只铺前 20 条，
 * 展开后可再收起；后端把上限放开时也不会把整表一次铺满页面。 */
const PENDING_PAGE = 20;
let pendingLimit = PENDING_PAGE;
let pendingCache = [];
let pendingTotal = 0;

function pendingRow(item) {
  const tr = el("tr");
  tr.append(el("td", "", item.name || item.uid || "—"));
  const textCell = el("td");
  if (item.video) textCell.append(el("span", "badge", "含视频"));
  else if (item.video_fail) textCell.append(el("span", "badge warn", "视频未转发"));
  if (item.text) {
    const text = el("span", "dim clamp-3", item.text);
    text.title = item.text;
    textCell.append(text);
  } else if (!item.video) textCell.append(el("span", "dim", "（无内容）"));
  if (item.video_fail && !item.video)
    textCell.append(el("div", "dim", "失败原因：" + item.video_fail));
  if (item.reject) textCell.append(el("div", "dim", "拒收详情：" + item.reject));
  tr.append(textCell);
  tr.append(el("td", "dim", item.created_ts ? fmtTime(item.created_ts) : "—"));
  const retry = el("td");
  if (item.sending) retry.append(el("span", "badge warn", "发送中"));
  if (item.retries > 0) retry.append(el("span", "danger", `${item.retries} 次`));
  else if (!item.sending) retry.append(el("span", "dim", "—"));
  tr.append(retry);
  const op = el("td", "col-op");
  const cancelBtn = el("button", "btn-mini", "取消");
  cancelBtn.type = "button";
  cancelBtn.addEventListener("click", async () => {
    // 发送中的条目取消不彻底：已经交给协议端的那一次收不回来，确认框必须
    // 说清这一点，否则用户以为"点了就什么都没发出去"
    const confirmed = await confirmDialog(
      item.sending
        ? `确定取消这条推送吗？\n${item.name || item.uid}\n\n该条正在发送：` +
          "已经交给协议端的那一次收不回来，群里可能仍会收到；本条不再重发。"
        : `确定取消这条推送吗？\n${item.name || item.uid}\n\n` +
          `本条不再重发，也不会触发群相册上传；${undoMinutes()} 分钟内可在` +
          "「已取消（可恢复）」里恢复。"
    );
    if (!confirmed) return;
    act(cancelBtn, "cancel-pending", { post_id: item.post_id }, () =>
      setStatus("已取消该条推送，可在「已取消（可恢复）」里恢复", "ok")
    );
  });
  op.append(cancelBtn);
  tr.append(op);
  return tr;
}

function renderPending(pending, totalCount) {
  els["pending-count"].textContent = String(totalCount);
  pendingCache = pending;
  pendingTotal = totalCount;
  const expanded = pendingLimit >= pending.length && pending.length > PENDING_PAGE;
  const shown = pendingLimit >= pending.length ? pending : pending.slice(0, pendingLimit);
  const tbody = els["pending-rows"];
  syncList(tbody, shown, (item) => item.post_id, pendingRow);
  renderListEmpty(tbody, els["pending-empty"], totalCount);
  const more = els["btn-pending-more"];
  if (more) {
    more.hidden = pending.length <= PENDING_PAGE;
    more.setAttribute("aria-expanded", expanded ? "true" : "false");
    if (expanded) {
      more.textContent = `收起（仅显示前 ${PENDING_PAGE} 条）`;
    } else if (totalCount > pending.length) {
      // 后端返回的条数被上限截断时不能写「显示全部 N 条」——N 不是全部
      more.textContent = `展开更多（已加载前 ${pending.length} 条，共 ${totalCount} 条）`;
    } else {
      more.textContent = `展开更多（还有 ${pending.length - shown.length} 条）`;
    }
  }
}

function cancelledRow(item) {
  const tr = el("tr");
  tr.append(el("td", "", item.name || item.uid || "—"));
  const textCell = el("td");
  if (item.video) textCell.append(el("span", "badge", "含视频"));
  if (item.text) {
    const text = el("span", "dim clamp-3", item.text);
    text.title = item.text;
    textCell.append(text);
  } else if (!item.video) textCell.append(el("span", "dim", "（无内容）"));
  if (item.was_sending)
    textCell.append(el("div", "dim", "取消时正在发送，已发出的部分收不回来"));
  if (item.retries > 0)
    textCell.append(el("div", "dim", `取消时已重试 ${item.retries} 次（恢复后保持不变）`));
  tr.append(textCell);
  tr.append(el("td", "dim", item.cancelled_ts ? fmtTime(item.cancelled_ts) : "—"));
  tr.append(el("td", "dim", fmtRemain(item.remain_seconds)));
  const op = el("td", "col-op");
  const restoreBtn = el("button", "btn-mini", "恢复");
  restoreBtn.type = "button";
  restoreBtn.addEventListener("click", () => {
    act(restoreBtn, "restore-pending", { post_id: item.post_id }, () =>
      setStatus("已恢复该条推送：回到队列尾部，重试进度保持不变", "ok")
    );
  });
  op.append(restoreBtn);
  tr.append(op);
  return tr;
}

function renderCancelled(items) {
  els["cancelled-count"].textContent = String(items.length);
  const tbody = els["cancelled-rows"];
  syncList(tbody, items, (item) => item.post_id, cancelledRow);
  renderListEmpty(tbody, els["cancelled-empty"], items.length);
}

let statsSig = "";

function renderStats(stats) {
  // 统计只在计数变化时重绘：KPI + 7 根柱子 + Top5 一共二十多个节点，
  // 没必要每 20 秒跟着 overview 一起推倒重来。跨天时 sig 会变，今日值自然刷新。
  const sig = `${dayKey(0)}|${safeJSON(stats)}`;
  if (sig === statsSig) return;
  statsSig = sig;
  const tiles = els["stat-tiles"];
  tiles.replaceChildren();
  const daily = stats.daily || {};
  const today = daily[dayKey(0)] || {};
  const items = [
    { label: "累计推送成功", value: stats.push_ok || 0, cls: "ok" },
    { label: "累计推送放弃", value: stats.push_fail || 0, cls: stats.push_fail ? "err" : "" },
    { label: "今日推送", value: today.push_ok || 0, cls: "" },
    { label: "相册已传图片", value: stats.album_uploaded || 0, cls: "" },
    { label: "相册去重跳过", value: stats.album_skipped || 0, cls: "" },
    { label: "相册失败图片", value: stats.album_failed || 0, cls: stats.album_failed ? "err" : "" },
  ];
  for (const it of items) {
    const box = el("div", `stat-tile ${it.cls}`.trim());
    box.append(el("div", "s-value", String(it.value)), el("div", "s-label", it.label));
    tiles.append(box);
  }

  // 近 7 天柱状图（纯 CSS，同参考项目做法）
  const bars = els["daily-bars"];
  bars.replaceChildren();
  const days = [];
  for (let i = 6; i >= 0; i--) {
    const key = dayKey(i);
    days.push({ key, value: (daily[key] || {}).push_ok || 0 });
  }
  const max = Math.max(...days.map((d) => d.value), 1);
  for (const d of days) {
    const col = el("div", "bar-col");
    col.append(el("div", "bar-num", d.value > 0 ? String(d.value) : ""));
    const track = el("div", "bar-track");
    const bar = el("div", `bar ${d.value ? "" : "zero"}`.trim());
    bar.style.height = `${Math.max((d.value / max) * 100, 2)}%`;
    bar.title = `${d.key}：推送成功 ${d.value} 条`;
    track.append(bar);
    col.append(track);
    col.append(el("div", "bar-label", d.key.slice(5).replace("-", "/")));
    bars.append(col);
  }

  // 博主排行 Top5
  const ranking = els.ranking;
  ranking.replaceChildren();
  const entries = Object.entries(stats.by_uid || {})
    .map(([uid, info]) => ({ uid, name: info.name || uid, count: info.push_ok || 0 }))
    .filter((e) => e.count > 0)
    .sort((a, b) => b.count - a.count)
    .slice(0, 5);
  for (const e of entries) {
    const li = el("li");
    const name = el("span", "clamp-2", e.name);
    name.title = e.name || "";
    li.append(name);
    li.append(el("span", "li-sub", `uid ${e.uid} · ${e.count} 条`));
    ranking.append(li);
  }
  els["ranking-empty"].hidden = entries.length > 0;
}

function activityRow(act) {
  const li = el("li");
  li.append(el("div", `act-dot ${act.kind || "info"}`));
  const body = el("div", "act-body");
  const line = el("div", "act-line");
  line.append(el("span", `act-kind ${act.kind || "info"}`, KIND_LABEL[act.kind] || "事件"));
  if (act.name) line.append(el("span", "act-name", act.name));
  if (act.detail) line.append(el("span", "act-detail", act.detail));
  body.append(line);
  if (act.text) {
    const text = el("div", "act-text clamp-3", act.text);
    text.title = act.text;
    body.append(text);
  }
  li.append(body);
  const time = el("span", "act-time", act.ts ? relTime(act.ts) : "");
  if (act.ts) time.dataset.ts = act.ts;
  li.append(time);
  return li;
}

function renderActivity(activity) {
  const list = els["activity-list"];
  const items = [...activity].reverse(); // 最新在前
  syncList(
    list,
    items,
    (act) => `${act.ts || 0}|${act.kind || ""}|${act.name || ""}|${act.detail || ""}|${act.text || ""}`,
    activityRow
  );
  els["activity-empty"].hidden = items.length > 0;
}

/* overview 一份数据同时服务三个页签，但只在可见页签渲染它的那部分：
 * 总览看状态/统计/动态，监控看账号/队列，归档看图床规则。 */
function applyOverviewMeta(data) {
  if (data.version) {
    els["version-chip"].hidden = false;
    els["version-chip"].textContent = data.version;
  }
  const status = data.status || {};
  if (status.cancel_undo_seconds > 0) cancelUndoSeconds = status.cancel_undo_seconds;
}

function renderOverview(data) {
  const status = data.status || {};
  renderStatus(status);
  renderStats(data.stats || {});
  renderActivity(data.activity || []);
}

function renderMonitorSections(data) {
  const status = data.status || {};
  renderAccounts(data.accounts || []);
  renderAtAll(data.at_all_settings);
  renderSessions(data.sessions || []);
  renderPending(data.pending || [], status.pending_count || 0);
  renderCancelled(data.cancelled || []);
  // 博主名单存了却不 @ 的唯一原因是总开关关着，这时才提示，平时不打扰
  const atAll = data.at_all_settings || {};
  els["at-all-tip"].hidden = atAll.at_all || !(atAll.bloggers || []).length;
}

function renderArchiveRules(data) {
  renderRules(data.album_rules || []);
  renderKeywordRules(data.keyword_rules || []);
  els["album-disabled-tip"].hidden = !!(data.status || {}).album_enabled;
}

function renderVisibleTabs() {
  if (!cache.overview) return;
  if (currentTab === "overview") renderOverview(cache.overview);
  else if (currentTab === "monitor") renderMonitorSections(cache.overview);
  else if (currentTab === "archive") renderArchiveRules(cache.overview);
}

function errorRow(item) {
  const li = el("li");
  const level = item.level === "WARNING" ? "warn" : "err";
  li.append(el("div", `act-dot ${level}`));
  const body = el("div", "act-body");
  const line = el("div", "act-line");
  line.append(el("span", `act-kind ${level}`, LEVEL_LABEL[item.level] || item.level || "告警"));
  if (item.src) line.append(el("span", "act-name", item.src));
  if (item.restored) line.append(el("span", "badge", "上次运行"));
  body.append(line);
  const msg = el("div", "act-text clamp-3", item.msg || "");
  msg.title = item.msg || "";
  body.append(msg);
  li.append(body);
  const time = el("span", "act-time", item.ts ? relTime(item.ts) : "");
  if (item.ts) time.dataset.ts = item.ts;
  li.append(time);
  if (item.ts) li.title = fmtTime(item.ts);
  return li;
}

function renderErrors(items) {
  els["errors-count"].hidden = items.length === 0;
  els["errors-count"].textContent = String(items.length);
  const list = els["errors-list"];
  syncList(list, items, (item) => `${item.ts || 0}|${item.level || ""}|${item.src || ""}|${item.msg || ""}`, errorRow);
  els["errors-empty"].hidden = items.length > 0;
}

/* ---------- 数据加载与操作 ---------- */

const TAB_DEFS = [
  ["overview", "总览"],
  ["monitor", "微博监控"],
  ["archive", "归档与图床"],
  ["copylib", "文案库"],
  ["trace", "溯源与签到"],
  ["sns", "多平台订阅"],
  ["settings", "设置"],
  ["errors", "错误日志"],
];

function nodeTab(node) {
  const ids = new Set([node.id, ...Array.from(node.querySelectorAll("[id]"), (n) => n.id)]);
  if (node.classList.contains("sns-panel")) return "sns";
  if (ids.has("settings-card")) return "settings";
  if (
    ids.has("archive-card") ||
    ids.has("pipeline-card") ||
    ids.has("rules-card") ||
    ids.has("kw-rules-card")
  )
    return "archive";
  if (ids.has("copylib-card")) return "copylib";
  if (ids.has("trace-card")) return "trace";
  if (ids.has("errors-list")) return "errors";
  if (ids.has("status-grid") || ids.has("stat-tiles") || ids.has("activity-list")) return "overview";
  if (node.querySelector(".about")) return "overview";
  return "monitor";
}

let currentTab = "";
let tabsReady = false;

async function activateTab(name) {
  const chosen = TAB_DEFS.some(([key]) => key === name) ? name : "overview";
  // 设置页有未保存改动时，切页前必须二次确认；取消则把 hash 恢复回来。
  if (currentTab === "settings" && chosen !== "settings" && settingsHasUnsaved()) {
    const leave = await settingsConfirmLeave();
    if (!leave) {
      try { history.replaceState(null, "", `#${currentTab}`); } catch (e) { /* 忽略 */ }
      return;
    }
  }
  currentTab = chosen;
  let activeBtn = null;
  for (const btn of document.querySelectorAll(".tab-btn")) {
    const on = btn.dataset.tab === chosen;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
    if (on) activeBtn = btn;
  }
  for (const panel of document.querySelectorAll(".tab-panel")) {
    panel.classList.toggle("active", panel.dataset.tab === chosen);
  }
  // 窄屏下页签栏是横向滚动容器：切到靠右的页签或深链进来时，
  // 当前页签可能整个在视口外（如 #errors 落在 x=754..858）。
  if (activeBtn) {
    try {
      activeBtn.scrollIntoView({ block: "nearest", inline: "nearest" });
    } catch (e) { /* 老宿主不支持对象参数时忽略 */ }
  }
  try { history.replaceState(null, "", `#${chosen}`); } catch (e) { /* iframe hash 受限时忽略 */ }
  if (chosen === "settings") settingsOnShown();
  // 先用缓存同步出这个页签的内容，再按 TTL 补拉；不再渲染看不见的页签。
  renderVisibleTabs();
  if (tabsReady) load();
}

function arrangeTabs() {
  const shell = document.querySelector(".page-shell");
  const nav = els["tab-nav"];
  if (!shell || !nav || shell.querySelector(".tab-panel")) return;
  nav.setAttribute("role", "tablist");
  const panels = new Map();
  for (const [key, title] of TAB_DEFS) {
    const panel = el("section", "tab-panel");
    panel.dataset.tab = key;
    panel.id = `panel-${key}`;
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", `tab-${key}`);
    panel.setAttribute("aria-label", title);
    panels.set(key, panel);
    nav.after(panel);
  }
  for (const btn of document.querySelectorAll(".tab-btn")) {
    const key = btn.dataset.tab;
    btn.id = `tab-${key}`;
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-controls", `panel-${key}`);
    btn.setAttribute("aria-selected", "false");
  }
  const movable = Array.from(shell.children).filter((node) =>
    node !== nav && (node.matches("section.card") || node.matches(".grid-2"))
  );
  for (const node of movable) panels.get(nodeTab(node)).append(node);
  const wanted = (location.hash || "#overview").slice(1);
  activateTab(wanted);
  addEventListener("hashchange", () => activateTab((location.hash || "#overview").slice(1)));
  for (const btn of document.querySelectorAll(".tab-btn")) {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  }
}

function panelFailure(id, error) {
  const node = els[id];
  if (!node) return;
  node.textContent = `读取失败：${error && error.message ? error.message : error}`;
}

function compactDate(ts) {
  return ts ? fmtTime(ts) : "时间未知";
}

function renderArchive(data) {
  const enabled = Boolean(data.enabled);
  els["archive-badge"].textContent = enabled ? "归档已启用" : "归档未启用";
  els["archive-badge"].className = `badge ${enabled ? "ok" : "warn"}`;
  const media = data.media || {};
  const copy = data.copylib || {};
  renderTiles(els["archive-status"], [
    { label: "归档帖子", value: data.posts || 0, tone: enabled ? "ok" : "err" },
    { label: "媒体", value: media.total || 0, sub: `已下载 ${media.downloaded || 0}` },
    { label: "图床完成", value: media.bed || 0, sub: `缩略图 ${media.thumb || 0}` },
    {
      label: "向量完成",
      value: media.vector || 0,
      sub: `失败 ${media.failed || 0} · 放弃 ${media.abandoned || 0}`,
      tone: media.failed || media.abandoned ? "err" : "ok",
    },
    { label: "文案已清洗", value: copy.cleaned || 0, sub: `共 ${copy.total || 0}` },
  ]);
  const rows = data.recent || [];
  const tbody = els["archive-rows"];
  syncList(tbody, rows, (row) => String(row.mid ?? `${row.uid}-${row.created_at}`), (row) => {
    const tr = document.createElement("tr");
    tr.append(el("td", "", compactDate(row.created_at)));
    tr.append(el("td", "", row.author || row.uid || "未知"));
    const text = el("td", "clamp-3", (row.text || "（无正文）").slice(0, 110));
    text.title = row.text || "";
    tr.append(text);
    tr.append(el("td", "", String(row.pic_total ?? 0)));
    return tr;
  });
  els["archive-empty"].hidden = rows.length > 0;
  const bf = data.backfill || {};
  const bs = bf.stats || {};
  els["backfill-progress"].textContent = bf.running
    ? `进行中 · 第 ${bs.pages || 0} 页 · 已抓 ${bs.fetched || 0} 条 · 新增 ${bs.inserted || 0}`
    : bs.status
      ? `最近任务：${bs.status} · ${bs.inserted || 0} 新增 / ${bs.existing || 0} 已有${fmtElapsed(bs.elapsed)}`
      : "尚无回填任务";
}

function renderPipeline(data) {
  const status = data.status || {};
  els["pipeline-badge"].textContent = status.enabled ? "管线已启用" : "管线未启用";
  els["pipeline-badge"].className = `badge ${status.enabled ? "ok" : "warn"}`;
  const media = status.media || {};
  const lines = [
    `模式：${status.mode || "—"} · 队列 ${status.queue || 0} · 待出队 ${status.pending || 0}`,
    `缺口帖子：${status.posts || 0}`,
    `媒体：${Object.entries(media).map(([k, v]) => `${k} ${v}`).join(" · ") || "暂无"}`,
    `最近错误：${status.last_error || "无"}`,
  ];
  els["pipeline-status"].textContent = lines.join("\n");
}

function renderNaming(data) {
  if (namingDirty) return;
  els["naming-directory"].value = data.naming_directory || "";
  els["naming-file"].value = data.naming_file || "";
  els["naming-categories"].value = JSON.stringify(data.categories || {}, null, 2);
}

function renderCopylib(data) {
  const cleaned = data.cleaned || 0;
  const total = data.total || 0;
  els["copylib-badge"].textContent = `${cleaned}/${total} 已清洗`;
  els["copylib-badge"].className = `badge ${data.enabled ? "ok" : "warn"}`;
  renderTiles(els["copylib-status"], [
    { label: "帖子", value: total },
    {
      label: "已清洗",
      value: cleaned,
      sub: `${Math.max(0, total - cleaned)} 待清洗`,
      tone: cleaned === total ? "ok" : "warn",
    },
    { label: "长文", value: data.long || 0 },
    { label: "转发", value: data.retweet || 0 },
    { label: "带图", value: data.with_pics || 0 },
  ]);
}

/* 文案库后端当前上限 50 条，但上限一旦放开，一张 1000 行的表会把页面拉到
 * 几万像素高。前端先铺 20 条，展开/收起成对，不引入虚拟滚动。 */
const COPYLIB_PAGE = 20;
let copylibLimit = COPYLIB_PAGE;
let copylibCache = [];

function renderCopylibRows(items) {
  const tbody = els["copylib-rows"];
  copylibCache = items || [];
  const total = copylibCache.length;
  const expanded = copylibLimit >= total && total > COPYLIB_PAGE;
  const rows = expanded || copylibLimit >= total
    ? copylibCache
    : copylibCache.slice(0, copylibLimit);
  syncList(tbody, rows, (row) => String(row.mid ?? `${row.uid}-${row.created_at}`), (row) => {
    const tr = document.createElement("tr");
    tr.append(el("td", "", row.published_at || compactDate(row.created_at)));
    tr.append(el("td", "", row.author || row.uid || "未知"));
    const text = el("td", "clamp-3", (row.text || "（无正文）").slice(0, 160));
    text.title = row.text || "";
    tr.append(text);
    tr.append(el("td", "", row.cleaned ? "已清洗" : "待清洗"));
    return tr;
  });
  els["copylib-empty"].hidden = total > 0;
  const more = els["btn-copylib-more"];
  if (more) {
    more.hidden = total <= COPYLIB_PAGE;
    more.setAttribute("aria-expanded", expanded ? "true" : "false");
    more.textContent = expanded
      ? `收起（仅显示前 ${COPYLIB_PAGE} 条）`
      : `展开更多（还有 ${total - rows.length} 条）`;
  }
}

function renderTrace(data) {
  const lib = data.library || {};
  const vector = data.vector || {};
  els["trace-badge"].textContent = data.checkin_mode === "weibo" ? "微博签到模式" : "随机语句模式";
  els["trace-badge"].className = "badge ok";
  renderTiles(els["trace-status"], [
    { label: "图库条目", value: lib.total || 0, sub: `索引 ${lib.indexed || 0}` },
    {
      label: "向量库",
      value: vector.count ?? "—",
      sub: vector.enabled ? vector.model || "已启用" : "未启用",
    },
    { label: "签到模式", value: data.checkin_mode === "weibo" ? "微博" : "随机语句" },
    {
      label: "重扫",
      value: data.rescan && data.rescan.running ? "进行中" : "空闲",
      tone: "ok",
    },
  ]);
  const detail = [];
  if (vector.error) detail.push(`向量库不可用：${vector.error}`);
  if (data.rescan && data.rescan.error) detail.push(`最近重扫失败：${data.rescan.error}`);
  for (const msg of (data.rescan && data.rescan.messages) || []) detail.push(msg);
  els["trace-detail"].textContent = detail.join("\n") || "图库与签到数据正常。";
}

/* 每个页签只拉它用到的接口，并给每类数据一个 10 秒 TTL：
 * 20 秒轮询下本来就不需要每次都重拉 11 个接口；切页 10 秒内先吃缓存再后台刷新。
 * force=true 用于「刷新 / 立即检查 / 增删改后」的强制拉取。 */
const DOMAIN_TTL = 10000;
const lastFetchAt = Object.create(null);
const cache = {
  overview: null,
  preview: null,
  errors: null,
  archive: null,
  pipeline: null,
  naming: null,
  copylib: null,
  copylibList: null,
  trace: null,
  sns: null,
  snsCredentials: null,
};
let loadForce = false;

function markFetched(key) {
  lastFetchAt[key] = Date.now();
}

function needFetch(key, force) {
  if (force || loadForce) return true;
  return Date.now() - (lastFetchAt[key] || 0) >= DOMAIN_TTL;
}

/* 失败时除了正文写「读取失败」，同步把卡片徽标从「读取中」改成「读取失败」：
 * 否则接口 404 后徽标会永久停在加载态，比正文更误导。 */
async function safeGet(endpoint, render, badge) {
  try {
    return await window.__bridge.apiGet(endpoint);
  } catch (e) {
    if (render) panelFailure(render, e);
    if (badge && els[badge]) {
      els[badge].textContent = "读取失败";
      els[badge].className = "badge err";
    }
    return null;
  }
}

async function loadArchive(force = false) {
  if (!needFetch("archive", force)) {
    if (cache.archive) renderArchive(cache.archive);
    return;
  }
  const data = await safeGet("archive/overview", "backfill-progress", "archive-badge");
  if (!data) return;
  cache.archive = data;
  markFetched("archive");
  renderArchive(data);
}

async function loadPipeline(force = false) {
  const jobs = [];
  if (needFetch("pipeline", force)) {
    jobs.push(
      safeGet("pipeline/status", "pipeline-status", "pipeline-badge").then((data) => {
        if (!data) return;
        cache.pipeline = data;
        markFetched("pipeline");
        renderPipeline(data);
      })
    );
  } else if (cache.pipeline) {
    renderPipeline(cache.pipeline);
  }
  if (needFetch("naming", force)) {
    jobs.push(
      safeGet("pipeline/naming").then((data) => {
        if (!data) return;
        cache.naming = data;
        markFetched("naming");
        renderNaming(data);
      })
    );
  } else if (cache.naming) {
    renderNaming(cache.naming);
  }
  await Promise.all(jobs);
}

async function loadCopylib(force = false) {
  const jobs = [];
  if (needFetch("copylib", force)) {
    jobs.push(
      safeGet("copylib/overview", "copylib-status", "copylib-badge").then((data) => {
        if (!data) return;
        cache.copylib = data;
        markFetched("copylib");
        renderCopylib(data);
      })
    );
  } else if (cache.copylib) {
    renderCopylib(cache.copylib);
  }
  // 用户正在按账号/关键词检索时，自动刷新不能把结果列表换回默认列表
  const uid = (els["copylib-uid"]?.value || "").trim();
  const keyword = (els["copylib-keyword"]?.value || "").trim();
  if (uid || keyword) {
    await Promise.all(jobs);
    return;
  }
  if (needFetch("copylibList", force)) {
    jobs.push(
      safeGet("copylib/list").then((data) => {
        if (!data) return;
        cache.copylibList = data.items || [];
        markFetched("copylibList");
        renderCopylibRows(cache.copylibList);
      })
    );
  } else if (cache.copylibList) {
    renderCopylibRows(cache.copylibList);
  }
  await Promise.all(jobs);
}

async function loadTrace(force = false) {
  if (!needFetch("trace", force)) {
    if (cache.trace) renderTrace(cache.trace);
    return;
  }
  const data = await safeGet("trace/status", "trace-detail", "trace-badge");
  if (!data) return;
  cache.trace = data;
  markFetched("trace");
  renderTrace(data);
}

function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error("文件读取失败"));
    reader.onload = () => resolve(String(reader.result || "").split(",", 2)[1] || "");
    reader.readAsDataURL(file);
  });
}

async function uploadFile(button, endpoint, inputId, extra = {}) {
  const input = els[inputId];
  const file = input && input.files && input.files[0];
  if (!file) {
    setStatus("请先选择文件", "warning");
    return;
  }
  busy(button, true);
  try {
    const content_base64 = await readFileBase64(file);
    await window.__bridge.apiPost(endpoint, { filename: file.name, content_base64, ...extra });
    setStatus(`已导入 ${file.name}`, "ok");
    input.value = "";
    loadForce = true;
    await load();
  } catch (e) {
    setStatus(`导入失败：${e && e.message ? e.message : e}`, "error");
  } finally {
    busy(button, false);
  }
}

function wireUnified() {
  // 手动刷新也走统一调度器：直接调 loadArchive(true) 会绕过单飞，
  // 与在飞轮次的旧快照互相覆盖。
  els["btn-archive-refresh"]?.addEventListener("click", () => {
    loadForce = true;
    load();
  });
  els["btn-backfill"]?.addEventListener("click", async (ev) => {
    const uid = els["backfill-uid"].value.trim();
    if (!uid) return setStatus("请输入回填 uid", "warning");
    const ok = await confirmDialog(`确认开始回填 ${uid}？\n\n任务会在后台翻页，可随时停止，游标会保留。`);
    if (!ok) return;
    act(ev.currentTarget, "archive/backfill", {
      uid,
      since: els["backfill-since"].value,
      full: els["backfill-full"].checked,
    }, () => setStatus("回填任务已启动", "ok"));
  });
  els["btn-backfill-stop"]?.addEventListener("click", async (ev) => {
    const ok = await confirmDialog("确认停止当前回填？当前页处理完后退出，游标保留可续跑。");
    if (!ok) return;
    act(ev.currentTarget, "archive/backfill", { action: "stop" }, () => setStatus("已请求停止回填", "ok"));
  });
  els["btn-reconcile"]?.addEventListener("click", async (ev) => {
    const ok = await confirmDialog("确认运行归档对账？大库扫描可能需要一些时间。");
    if (!ok) return;
    busy(ev.currentTarget, true);
    try {
      const report = await window.__bridge.apiPost("archive/reconcile", {
        uid: els["reconcile-uid"].value.trim(),
      });
      els["reconcile-report"].textContent = JSON.stringify(report, null, 2);
      setStatus("归档对账完成", "ok");
    } catch (e) {
      setStatus(`对账失败：${e && e.message ? e.message : e}`, "error");
    } finally {
      busy(ev.currentTarget, false);
    }
  });
  els["btn-archive-import"]?.addEventListener("click", (ev) =>
    uploadFile(ev.currentTarget, "archive/import", "archive-file")
  );
  els["btn-pipeline-retry"]?.addEventListener("click", async (ev) => {
    const ok = await confirmDialog("确认把未完成帖重新加入图床/向量队列？重复补跑是幂等的。");
    if (!ok) return;
    act(ev.currentTarget, "pipeline/retry", { limit: Number(els["pipeline-limit"].value || 200) }, () =>
      setStatus("已触发管线补跑", "ok")
    );
  });
  for (const id of ["naming-directory", "naming-file", "naming-categories"]) {
    els[id]?.addEventListener("input", () => {
      namingDirty = true;
    });
  }
  els["naming-form"]?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    let categories;
    try { categories = JSON.parse(els["naming-categories"].value || "{}"); }
    catch (e) { return setStatus("博主分类不是有效 JSON", "error"); }
    act(ev.submitter, "pipeline/naming", {
      naming_directory: els["naming-directory"].value.trim(),
      naming_file: els["naming-file"].value.trim(),
      categories,
    }, () => {
      namingDirty = false;
      setStatus("命名规则已保存", "ok");
    });
  });
  els["btn-copylib-search"]?.addEventListener("click", async (ev) => {
    busy(ev.currentTarget, true);
    try {
      const data = await window.__bridge.apiPost("copylib/list", {
        uid: els["copylib-uid"].value.trim(),
        keyword: els["copylib-keyword"].value.trim(),
        limit: 20,
      });
      renderCopylibRows(data.items || []);
    } catch (e) {
      setStatus(`文案查询失败：${e && e.message ? e.message : e}`, "error");
    } finally {
      busy(ev.currentTarget, false);
    }
  });
  els["btn-copylib-more"]?.addEventListener("click", () => {
    copylibLimit = copylibLimit >= copylibCache.length ? COPYLIB_PAGE : copylibCache.length;
    renderCopylibRows(copylibCache);
  });
  els["btn-copylib-import"]?.addEventListener("click", (ev) =>
    uploadFile(ev.currentTarget, "copylib/import", "copylib-file", {
      uid: els["copylib-uid"].value.trim(),
    })
  );
  els["btn-trace-rescan"]?.addEventListener("click", async (ev) => {
    const ok = await confirmDialog("确认启动图库重扫？强制模式会先删除扫描来源的旧索引。");
    if (!ok) return;
    act(ev.currentTarget, "trace/rescan", { force: els["rescan-force"].checked }, () =>
      setStatus("图库重扫已在后台启动", "ok")
    );
  });
  els["btn-trace-repair"]?.addEventListener("click", async (ev) => {
    const ok = await confirmDialog("确认清理会话历史中带装饰前缀的原图键？只改本地键，不动图床。");
    if (!ok) return;
    act(ev.currentTarget, "trace/repair-history", {}, () => setStatus("历史键修复完成", "ok"));
  });
}

// 轮询与 visibilitychange 共用同一个调度器：一轮还没结束时再来的刷新请求
// 只记一个「补跑」标记，等当前轮结束再跑。否则慢请求返回的旧快照会覆盖
// 刚渲染的新状态（例如刚改完命名规则又被 20 秒前的响应刷回旧值）。
let loadInFlight = false;
let loadQueued = false;
/* 被丢进队里的那一轮必须记住「强制刷新」意图：在飞那轮结束时 loadAll 的
 * finally 会把 loadForce 清掉，若不在这里接管，排队轮次会按 TTL 吃缓存，
 * 增删改的结果最多要等 20 秒才出现在页面上。 */
let loadQueuedForce = false;

/* 慢接口提示：正常轮询通常 100ms 内返回，只有超过 400ms 才显示胶囊 +
 * aria-busy，避免每次刷新都闪一下。typeof 守卫让 tests 里的 Node harness
 * 能单独提取 load() 运行（那里没有 DOM 与 els）。 */
let refreshHintTimer = null;
function refreshHint(on) {
  if (refreshHintTimer) {
    clearTimeout(refreshHintTimer);
    refreshHintTimer = null;
  }
  if (on) {
    refreshHintTimer = setTimeout(() => {
      refreshHintTimer = null;
      const node = els["refresh-hint"];
      if (node) node.hidden = false;
      const shell = document.querySelector(".page-shell");
      if (shell) shell.setAttribute("aria-busy", "true");
    }, 400);
    return;
  }
  const node = els["refresh-hint"];
  if (node) node.hidden = true;
  const shell = document.querySelector(".page-shell");
  if (shell) shell.removeAttribute("aria-busy");
}

async function load() {
  if (loadInFlight) {
    loadQueued = true;
    if (loadForce) {
      loadQueuedForce = true;
      loadForce = false;
    }
    return;
  }
  loadInFlight = true;
  if (typeof refreshHint === "function") refreshHint(true);
  try {
    await loadAll();
  } finally {
    if (typeof refreshHint === "function") refreshHint(false);
    loadInFlight = false;
    if (loadQueued) {
      loadQueued = false;
      if (loadQueuedForce) {
        loadQueuedForce = false;
        loadForce = true;
      }
      setTimeout(load, 0);
    }
  }
}

const NEEDS_OVERVIEW = new Set(["overview", "monitor", "archive"]);
let bootstrapped = false;

async function loadAll() {
  const jobs = [];
  // 首屏无论落在哪个页签都取一次 overview（版本号 + 全局状态），之后按需；
  if (!bootstrapped || NEEDS_OVERVIEW.has(currentTab)) {
    bootstrapped = true;
    jobs.push(loadOverview());
  }
  if (currentTab === "monitor") jobs.push(loadPreview());
  else if (currentTab === "archive") jobs.push(loadArchive(), loadPipeline());
  else if (currentTab === "copylib") jobs.push(loadCopylib());
  else if (currentTab === "trace") jobs.push(loadTrace());
  else if (currentTab === "sns") jobs.push(loadSns());
  else if (currentTab === "errors") jobs.push(loadErrors());
  try {
    await Promise.all(jobs);
  } finally {
    loadForce = false;
  }
  refreshRelativeTimes();
}

async function loadOverview() {
  if (!needFetch("overview")) {
    renderVisibleTabs();
    return;
  }
  try {
    const data = await window.__bridge.apiGet("overview");
    cache.overview = data;
    markFetched("overview");
    applyOverviewMeta(data);
    renderVisibleTabs();
  } catch (e) {
    setStatus(`加载失败：${e && e.message ? e.message : e}`, "error");
  }
}

async function loadErrors(force = false) {
  if (!needFetch("errors", force)) {
    if (cache.errors) renderErrors(cache.errors);
    return;
  }
  try {
    const data = await window.__bridge.apiGet("errors");
    cache.errors = data.errors || [];
    markFetched("errors");
    renderErrors(cache.errors);
  } catch (e) {
    // 错误留存读取失败不打断面板其它区块
  }
}

async function loadPreview(force = false) {
  if (!needFetch("preview", force)) {
    if (cache.preview) renderPreview(cache.preview);
    return;
  }
  try {
    const data = await window.__bridge.apiGet("preview-push");
    cache.preview = data;
    markFetched("preview");
    renderPreview(data);
  } catch (e) {
    // 预览读取失败不打断面板其它区块，就地写明原因
    const box = els["preview-text"];
    if (box) box.textContent = `（预览读取失败：${e && e.message ? e.message : e}）`;
  }
}

function renderPreview(data) {
  const badges = els["preview-badges"];
  badges.replaceChildren();
  if (!data.at_all_on) {
    badges.append(el("span", "badge", "@全体成员 关"));
  } else if (data.at_all) {
    badges.append(el("span", "badge ok", "@全体成员 会带上"));
  } else {
    badges.append(el("span", "badge warn", "@全体成员 受关键词限定 · 本样例未命中"));
  }
  badges.append(el("span", "badge", data.with_images ? "推送带图 开" : "推送带图 关"));
  badges.append(el("span", "badge", data.with_videos ? "推送带视频 开" : "推送带视频 关"));
  const kws = data.at_all_keywords || [];
  const uids = data.at_all_uids || [];
  if (data.at_all_on && uids.length) {
    badges.append(el("span", "preview-kw", `@全体博主：${uids.join("、")}（名单外的博主不 @）`));
  }
  if (data.at_all_on && kws.length) {
    badges.append(el("span", "preview-kw", `@全体关键词：${kws.join("、")}`));
  }
  els["preview-text"].textContent = data.text || "（空）";
}

async function act(btn, endpoint, body, onOk) {
  busy(btn, true);
  try {
    await window.__bridge.apiPost(endpoint, body);
    if (onOk) onOk();
    loadForce = true; // 增删改后必须看到最新结果，跳过 TTL
    await load();
  } catch (e) {
    setStatus(`操作失败：${e && e.message ? e.message : e}`, "error");
  } finally {
    busy(btn, false);
  }
}

function wireForms() {
  els["account-form"].addEventListener("submit", (ev) => {
    ev.preventDefault();
    const input = els["account-input"];
    const uid = input.value.trim();
    if (!uid) return;
    act(ev.submitter, "accounts", { action: "add", uid }, () => {
      setStatus(`已添加监控：${uid}。下一轮检查将先建立基线，不推历史微博。`, "ok");
      input.value = "";
    });
  });

  els["session-form"].addEventListener("submit", (ev) => {
    ev.preventDefault();
    const input = els["session-input"];
    const umo = input.value.trim();
    if (!umo) return;
    act(ev.submitter, "sessions", { action: "add", umo }, () => {
      setStatus("已添加推送目标", "ok");
      input.value = "";
    });
  });

  els["rule-form"].addEventListener("submit", (ev) => {
    ev.preventDefault();
    const body = {
      action: "add",
      uid: els["rule-uid"].value.trim(),
      gid: els["rule-gid"].value.trim(),
      album: els["rule-album"].value.trim(),
    };
    if (!body.uid || !body.gid || !body.album) {
      setStatus("添加相册规则需要填写 uid、群号、相册名三项", "warning");
      return;
    }
    act(ev.submitter, "album-rules", body, () => {
      setStatus("已添加相册绑定规则", "ok");
      els["rule-uid"].value = "";
      els["rule-gid"].value = "";
      els["rule-album"].value = "";
    });
  });

  els["kw-rule-form"].addEventListener("submit", (ev) => {
    ev.preventDefault();
    const body = {
      action: "add",
      uid: els["kw-rule-uid"].value.trim(),
      keyword: els["kw-rule-keyword"].value.trim(),
      gid: els["kw-rule-gid"].value.trim(),
      album: els["kw-rule-album"].value.trim(),
    };
    if (!body.keyword || !body.gid || !body.album) {
      setStatus("添加关键词路由需要填写关键词、群号、相册名（博主 uid 选填）", "warning");
      return;
    }
    act(ev.submitter, "keyword-rules", body, () => {
      setStatus("已添加关键词路由规则", "ok");
      els["kw-rule-uid"].value = "";
      els["kw-rule-keyword"].value = "";
      els["kw-rule-gid"].value = "";
      els["kw-rule-album"].value = "";
    });
  });

  els["btn-at-all-switch"].addEventListener("click", (ev) => {
    const btn = ev.currentTarget;
    const turnOn = !atAllOn;
    act(btn, "at-all-settings", { action: "set_switch", value: turnOn }, () => {
      // 名单与关键词都空着就开开关 = 每条推送都打扰全员，值得在提示里点一句
      const noScope = els["at-all-count"].hidden && !els["at-kw-list"].children.length;
      if (turnOn && noScope) {
        setStatus("已开启 @全体：所有博主的每条新微博都会 @全体成员，可用博主名单或关键词收窄", "ok");
      } else if (turnOn) {
        setStatus("已开启 @全体总开关", "ok");
      } else {
        setStatus("已关闭 @全体总开关（博主名单与关键词保留）", "ok");
      }
    });
  });

  els["at-uid-form"].addEventListener("submit", (ev) => {
    ev.preventDefault();
    const input = els["at-uid-input"];
    const uid = input.value.trim();
    if (!uid) return;
    act(ev.submitter, "at-all-uids", { action: "add", uid }, () => {
      setStatus("已加入 @全体名单", "ok");
      input.value = "";
    });
  });

  els["at-kw-form"].addEventListener("submit", (ev) => {
    ev.preventDefault();
    const input = els["at-kw-input"];
    const keyword = input.value.trim();
    if (!keyword) return;
    act(ev.submitter, "at-all-settings", { action: "add_keyword", keyword }, () => {
      setStatus(`已加入 @全体关键词：${keyword}`, "ok");
      input.value = "";
    });
  });

  els["btn-check"].addEventListener("click", (ev) => {
    const btn = ev.currentTarget;
    busy(btn, true);
    window.__bridge
      .apiPost("check-now", {})
      .then(() => {
        setStatus("已触发检查，完成后会出现在「最近动态」里", "ok");
        setTimeout(() => {
          loadForce = true;
          load();
        }, 3000);
      })
      .catch((e) => setStatus(`触发失败：${e && e.message ? e.message : e}`, "error"))
      .finally(() => busy(btn, false));
  });

  els["btn-refresh"].addEventListener("click", () => {
    loadForce = true;
    load();
  });

  els["btn-pending-more"]?.addEventListener("click", () => {
    // 展开/收起成对：展开后按钮不消失，再点回到默认 20 条
    pendingLimit = pendingLimit >= pendingCache.length ? PENDING_PAGE : pendingCache.length;
    renderPending(pendingCache, pendingTotal);
  });

  // 推送预览随 load() 一起刷新；改完配置后可单独点「刷新预览」
  els["btn-preview-refresh"].addEventListener("click", () => {
    loadForce = true;
    load();
  });

  // 「停止本轮推送」不走 act()：act 末尾的 busy(btn, false) 会与 renderStatus
  // 里按 push_round_active 计算的置灰状态打架，这里按「立即检查」的做法手写
  els["btn-stop-round"].addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    const confirmed = await confirmDialog(
      "确定停止本轮推送吗？\n\n本轮剩余条目不会发送，会原样留在待推送队列里" +
        "（不消耗重试次数），下一轮检查时继续尝试。\n" +
        "若想让某一条彻底不再重发，请改用该行的「取消」。"
    );
    if (!confirmed) return;
    busy(btn, true);
    window.__bridge
      .apiPost("stop-push-round", {})
      .then(() => {
        setStatus("已停止本轮推送：剩余条目留待下一轮检查", "ok");
        setTimeout(() => {
          loadForce = true;
          load();
        }, 1000);
      })
      .catch((e) => setStatus(`停止失败：${e && e.message ? e.message : e}`, "error"))
      .finally(() => {
        busy(btn, false);
        loadForce = true;
        load();
      });
  });

  els["btn-clear-errors"].addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    const confirmed = await confirmDialog(
      "确定清空错误日志留存吗？\n内存缓冲与 errors.log 将一并清掉，仅建议在问题处理完后操作。"
    );
    if (!confirmed) return;
    act(btn, "clear-errors", {}, () => setStatus("已清空错误日志留存", "ok"));
  });
}

/* ---------- 多平台订阅（抖音 / 小红书 / Instagram） ---------- */

const SNS_MODE_LABEL = {
  browser: "浏览器（本机 Edge）",
  http: "HTTP（自备 Cookie）",
  rss: "RSS 订阅源",
};

const SNS_PLATFORM_LABEL = { douyin: "抖音", xhs: "小红书", instagram: "Instagram" };

const SNS_KIND_LABEL = {
  risk: "风控",
  error: "错误",
  baseline: "基线",
  info: "提示",
  push: "推送",
  push_fail: "推送失败",
};

function snsModeLabel(mode) {
  return SNS_MODE_LABEL[mode] || mode || "未知";
}

function snsRowMain(title, meta, sub) {
  const main = el("div", "li-main");
  const name = el("span", "clamp-2", title);
  name.title = title;
  main.append(name);
  if (meta) main.append(el("span", "li-sub", meta));
  if (sub) {
    const line = el("div", "li-sub clamp-3");
    line.textContent = sub;
    line.title = sub;
    main.append(line);
  }
  return main;
}

function snsAccountRow(row) {
  const li = el("li");
  const title = row.name || row.account_id;
  const meta = `${SNS_PLATFORM_LABEL[row.platform] || row.platform} · 健康度 ${Number(
    row.health || 0
  ).toFixed(2)}`;
  li.append(snsRowMain(title, meta, row.last_error || ""));

  if (!row.enabled) li.append(el("span", "badge", "已停用"));
  if (row.paused) li.append(el("span", "badge err", row.paused_reason || "已暂停"));
  else if (row.cooling) li.append(el("span", "badge warn", `冷却 ${relTime(row.cooldown_until)}解冻`));
  else if (!row.baseline_done) li.append(el("span", "badge warn", "待建基线"));
  if (row.push_ok > 0) li.append(el("span", "badge", `已推 ${row.push_ok}`));

  const toggle = el("button", "btn-mini", row.enabled ? "停用" : "启用");
  toggle.type = "button";
  toggle.title = row.enabled ? "暂停轮询该订阅（已读记录保留）" : "恢复轮询该订阅";
  toggle.addEventListener("click", () => {
    act(
      toggle,
      "sns/accounts",
      { action: "toggle", platform: row.platform, account_id: row.account_id },
      () => setStatus(row.enabled ? `已停用：${title}` : `已启用：${title}`, "ok")
    );
  });
  li.append(toggle);

  const rm = el("button", "btn-mini", "移除");
  rm.type = "button";
  rm.addEventListener("click", async () => {
    const ok = await confirmDialog(`确定取消订阅「${title}」吗？已读记录会一并清掉。`);
    if (!ok) return;
    act(
      rm,
      "sns/accounts",
      { action: "remove", platform: row.platform, account_id: row.account_id },
      () => setStatus(`已取消订阅：${title}`, "ok")
    );
  });
  li.append(rm);
  return li;
}

function renderSnsAccounts(rows) {
  const list = els["sns-account-list"];
  const items = rows || [];
  syncList(list, items, (row) => `${row.platform}|${row.account_id}`, snsAccountRow);
  els["sns-accounts-empty"].hidden = items.length > 0;
  els["sns-accounts-count"].textContent = String(items.length);
}

function snsPlatformRow(row) {
  const tr = el("tr");
    tr.append(el("td", "", row.label || SNS_PLATFORM_LABEL[row.platform] || row.platform));
    tr.append(el("td", "", String(row.accounts || 0)));
    tr.append(el("td", "", `${row.base_interval || 0} 秒`));
    const login = row.login || {};
    const cell = el("td");
    if (login.logged_in) cell.append(el("span", "badge ok", "已登录"));
    else if (login.cookie_configured) cell.append(el("span", "badge warn", "仅有 Cookie"));
    else cell.append(el("span", "badge", "未登录"));
    if (row.paused_until && row.paused_until * 1000 > Date.now()) {
      cell.append(el("span", "badge err", "平台熔断中"));
    }
    tr.append(cell);

    const ops = el("td");
    const openBtn = el("button", "btn-mini", "打开登录");
    openBtn.type = "button";
    openBtn.title = "在本机弹出持久化浏览器窗口，手动登录一次即可";
    openBtn.addEventListener("click", () => {
      act(openBtn, "sns/session", { action: "open", platform: row.platform }, () =>
        setStatus("已申请打开登录窗口，请在本机浏览器里完成登录", "ok")
      );
    });
    const markBtn = el("button", "btn-mini", "标记已登录");
    markBtn.type = "button";
    markBtn.title = "在浏览器里登录成功后点一下，面板只记录时间，不保存 Cookie";
    markBtn.addEventListener("click", () => {
      act(markBtn, "sns/session", { action: "mark", platform: row.platform }, () =>
        setStatus("已记录登录态", "ok")
      );
    });
  ops.append(openBtn, markBtn);
  tr.append(ops);
  return tr;
}

function renderSnsPlatforms(rows) {
  const body = els["sns-platform-rows"];
  syncList(body, rows || [], (row) => row.platform, snsPlatformRow);
}

function renderSnsGuard(data) {
  const guard = data.guard || {};
  const stats = data.stats || {};
  const tripHits = guard.trip_hits || 3;
  const tripWindow = Math.round((guard.trip_window || 600) / 60);
  renderTiles(els["sns-guard-status"], [
    {
      label: "判定",
      value: "指数退避",
      sub: `${tripHits} 次风控 / ${tripWindow} 分钟 → 平台熔断`,
    },
    { label: "冷却", value: `${guard.base_cooldown || 0} 秒起`, sub: `上限 ${guard.cooldown_cap || 0} 秒` },
    {
      label: "健康阈值",
      value: `${Number(guard.health_pause || 0).toFixed(2)} 暂停`,
      sub: `${Number(guard.health_degrade || 0).toFixed(2)} 降速`,
    },
    {
      label: "累计风控",
      value: String(stats.risk || 0),
      sub: `检查 ${stats.check || 0} 次`,
      tone: (stats.risk || 0) > 0 ? "warn" : "",
    },
  ]);

  const setIfIdle = (id, value) => {
    const node = els[id];
    if (node && document.activeElement !== node) node.value = value;
  };
  setIfIdle("sns-guard-base", guard.base_cooldown ?? "");
  setIfIdle("sns-guard-cap", guard.cooldown_cap ?? "");
  setIfIdle("sns-guard-pause", guard.health_pause ?? "");
  setIfIdle("sns-guard-degrade", guard.health_degrade ?? "");
  setIfIdle("sns-guard-warmup", guard.protect_mode_hours ?? "");
  els["sns-guard-night"].checked = Boolean(guard.night_slowdown);
  els["sns-guard-aggressive"].checked = Boolean(data.aggressive);
  els["sns-guard-badge"].textContent = data.aggressive ? "激进档位" : "保号档位";
  els["sns-guard-badge"].className = `badge ${data.aggressive ? "warn" : "ok"}`;
}

function renderSnsLatency(latency) {
  const body = els["sns-latency-rows"];
  let any = false;
  const items = [];
  for (const platform of ["douyin", "xhs", "instagram"]) {
    const row = (latency || {})[platform] || {};
    if (row.accounts) any = true;
    items.push({ platform, label: SNS_PLATFORM_LABEL[platform] || platform, ...row });
  }
  syncList(body, items, (row) => row.platform, (row) => {
    const tr = el("tr");
    tr.append(el("td", "", row.accounts ? row.label : `${row.label}（未订阅）`));
    tr.append(el("td", "", String(row.accounts || 0)));
    tr.append(el("td", "", row.interval ? `${row.interval} 秒` : "—"));
    tr.append(el("td", "", row.p50 ? `${row.p50} 秒` : "—"));
    tr.append(el("td", "", row.p95 ? `${row.p95} 秒` : "—"));
    return tr;
  });
  els["sns-latency-note"].textContent = any
    ? "延迟是从博主发布到群里看到消息的估算：到点时间均匀铺开，中位数约半个间隔，最坏等于一个间隔。"
    : "还没有订阅账号，无法估算延迟。";
}

function snsCredentialRow(row) {
  const li = el("li");
  const bits = [];
  bits.push(row.logged_in ? "浏览器已登录" : "浏览器未登录");
  bits.push(row.cookie_configured ? `Cookie ${row.cookie_preview || "已配置"}` : "Cookie 未配置");
  if (row.missing_keys && row.missing_keys.length) bits.push(`缺少 ${row.missing_keys.join(" / ")}`);
  li.append(snsRowMain(row.label || SNS_PLATFORM_LABEL[row.platform] || row.platform, bits.join(" · "), ""));
  if (row.profile_ready) li.append(el("span", "badge", "浏览器目录已建"));
  return li;
}

function renderSnsCredentials(rows) {
  const list = els["sns-credential-list"];
  syncList(list, rows || [], (row) => row.platform, snsCredentialRow);
}

function snsEventRow(item) {
  const li = el("li");
  li.append(el("div", `act-dot ${item.kind || "info"}`));
  const body = el("div", "act-body");
  const line = el("div", "act-line");
  line.append(el("span", `act-kind ${item.kind || "info"}`, SNS_KIND_LABEL[item.kind] || "事件"));
  if (item.name) line.append(el("span", "act-name", item.name));
  if (item.detail) line.append(el("span", "act-detail", item.detail));
  body.append(line);
  if (item.text) {
    const text = el("div", "act-text clamp-3", item.text);
    text.title = item.text;
    body.append(text);
  }
  li.append(body);
  const time = el("span", "act-time", item.ts ? relTime(item.ts) : "");
  if (item.ts) time.dataset.ts = item.ts;
  li.append(time);
  if (item.ts) li.title = fmtTime(item.ts);
  return li;
}

function renderSnsEvents(items) {
  const list = els["sns-events"];
  const rows = items || [];
  syncList(list, rows, (item) => `${item.ts || 0}|${item.kind || ""}|${item.name || ""}|${item.detail || ""}|${item.text || ""}`, snsEventRow);
  els["sns-events-empty"].hidden = rows.length > 0;
}

function renderSns(data) {
  const enabled = Boolean(data.enabled);
  els["sns-badge"].textContent = enabled ? `订阅 · ${snsModeLabel(data.fetch_mode)}` : "订阅未启用";
  els["sns-badge"].className = `badge ${enabled ? "ok" : "warn"}`;
  els["sns-fetch-mode"].value = data.fetch_mode || "browser";

  const stats = data.stats || {};
  const browser = data.browser || {};
  const last = data.last_round || {};
  renderTiles(els["sns-status"], [
    {
      label: "取数方式",
      value: snsModeLabel(data.fetch_mode),
      sub: data.aggressive ? "激进档位" : "保号档位",
      tone: data.aggressive ? "warn" : "",
    },
    { label: "订阅账号", value: String((data.accounts || []).length), sub: "抖音 / 小红书 / IG" },
    {
      label: "浏览器",
      value: browser.running ? "运行中" : browser.available ? "待启动" : "不可用",
      sub: browser.channel ? `channel=${browser.channel}` : browser.reason || "",
      tone: browser.available ? "" : "warn",
    },
    { label: "推送目标", value: String((data.sessions || []).length), sub: "会话" },
    {
      label: "累计检查",
      value: String(stats.check || 0),
      sub: `推送成功 ${stats.push_ok || 0} · 失败 ${stats.push_fail || 0}`,
    },
    {
      label: "最近一轮",
      value: last.checked === undefined ? "未跑过" : `${last.checked} 个账号`,
      sub: last.checked === undefined ? "" : `推送 ${last.pushed || 0} 条`,
    },
  ]);

  const tip = els["sns-browser-tip"];
  const note = data.browser_error || (!browser.available ? browser.reason : "");
  tip.hidden = !note;
  tip.textContent = note || "";

  renderSnsPlatforms(data.platforms || []);
  renderSnsAccounts(data.accounts || []);
  renderSnsGuard(data);
  renderSnsLatency(data.latency || {});
  renderSnsEvents(data.events || []);
}

async function loadSns(force = false) {
  const jobs = [];
  if (needFetch("sns", force)) {
    jobs.push(
      window.__bridge
        .apiGet("sns/overview")
        .then((data) => {
          cache.sns = data;
          markFetched("sns");
          renderSns(data);
        })
        .catch((e) => {
          setStatus(`订阅面板加载失败：${e && e.message ? e.message : e}`, "error");
        })
    );
  } else if (cache.sns) {
    renderSns(cache.sns);
  }
  jobs.push(loadSnsCredentials(force));
  await Promise.all(jobs);
}

async function loadSnsCredentials(force = false) {
  if (!needFetch("snsCredentials", force)) {
    if (cache.snsCredentials) renderSnsCredentials(cache.snsCredentials);
    return;
  }
  try {
    const data = await window.__bridge.apiGet("sns/credentials");
    // 后端字段名是 rows（与其它列表接口一致）；credentials 仅作旧字段兼容
    cache.snsCredentials = data.rows || data.credentials || [];
    markFetched("snsCredentials");
    renderSnsCredentials(cache.snsCredentials);
  } catch (e) {
    // 凭据读取失败不打断面板其它区块
  }
}

function wireSns() {
  els["sns-account-form"]?.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const platform = els["sns-platform-select"].value;
    const target = els["sns-account-input"].value.trim();
    if (!target) return setStatus("请输入主页链接或账号 ID", "warning");
    act(ev.currentTarget.querySelector("button[type=submit]"), "sns/accounts", {
      action: "add",
      platform,
      target,
    }, () => {
      els["sns-account-input"].value = "";
      setStatus("已添加订阅，首轮成功取数只建立基线", "ok");
    });
  });

  els["btn-sns-save-mode"]?.addEventListener("click", () => {
    act(els["btn-sns-save-mode"], "sns/guard", { fetch_mode: els["sns-fetch-mode"].value }, () =>
      setStatus("取数方式已保存", "ok")
    );
  });

  els["btn-sns-check-now"]?.addEventListener("click", () => {
    act(els["btn-sns-check-now"], "sns/check-now", {}, () =>
      setStatus("已请求立即检查一轮", "ok")
    );
  });

  els["btn-sns-save-guard"]?.addEventListener("click", () => {
    act(els["btn-sns-save-guard"], "sns/guard", {
      cooldown_base_seconds: Number(els["sns-guard-base"].value || 0),
      cooldown_cap_seconds: Number(els["sns-guard-cap"].value || 0),
      health_pause_threshold: Number(els["sns-guard-pause"].value || 0),
      health_degrade_threshold: Number(els["sns-guard-degrade"].value || 0),
      protect_mode_hours: Number(els["sns-guard-warmup"].value || 0),
      night_slowdown: els["sns-guard-night"].checked,
      aggressive: els["sns-guard-aggressive"].checked,
    }, () => setStatus("守卫参数已保存", "ok"));
  });

  els["btn-sns-drill"]?.addEventListener("click", async () => {
    const out = els["sns-drill-output"];
    const btn = els["btn-sns-drill"];
    busy(btn, true);
    try {
      const data = await window.__bridge.apiPost("sns/preview", {
        platform: els["sns-platform-select"].value,
      });
      out.hidden = false;
      out.textContent = data && data.text ? data.text : JSON.stringify(data, null, 2);
      setStatus("已按当前模板渲染示例文案（不会发出去）", "ok");
    } catch (e) {
      out.hidden = false;
      out.textContent = `预览失败：${e && e.message ? e.message : e}`;
    } finally {
      busy(btn, false);
    }
  });
}

/* ---------- 初始化 ---------- */

/* logo.svg 在宿主静态资源重写失败时会 404，浏览器直接显示破图图标。
 * 载入失败就换成同尺寸的文字标记，不让头部留一个裂开的图片。 */
function wireLogoFallback() {
  const logo = document.querySelector(".brand-logo");
  if (!logo) return;
  const fallback = () => {
    if (logo.dataset.fallback === "1") return;
    logo.dataset.fallback = "1";
    const mark = el("span", "brand-logo brand-logo-fallback", "微");
    mark.setAttribute("aria-hidden", "true");
    if (logo.parentNode) logo.parentNode.replaceChild(mark, logo);
    else logo.replaceWith(mark);
  };
  logo.addEventListener("error", fallback, { once: true });
  // 脚本执行时图片可能已经失败（error 事件早已触发）
  if (logo.complete && logo.naturalWidth === 0) fallback();
}

async function init() {
  wireLogoFallback();
  const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
  if (!bridge) {
    setStatus(
      "未检测到 AstrBot 桥接环境：请在 AstrBot WebUI 的「插件 - 微博实时转发 - 详情页」中打开本页面",
      "error"
    );
    return;
  }
  window.__bridge = bridge;

  try {
    const ctx = await bridge.ready();
    applyTheme(ctx);
    if (typeof bridge.onContext === "function") {
      bridge.onContext(applyTheme);
    }
  } catch (e) {
    // 主题读取失败不影响面板功能
  }

  arrangeTabs();
  wireForms();
  wireUnified();
  wireSns();
  initSettings({
    confirm: confirmDialog,
    status: (message, tone) => setStatus(message, tone || ""),
  });
  // 设置页启动时不拉配置（179 个键的请求只在真正看得见时发）；
  // 但如果带着 #settings 深链进来，切页动作发生在 initSettings 之前，这里补一次。
  if (currentTab === "settings") settingsOnShown();
  tabsReady = true;
  await load();
  // 页面切到后台时暂停轮询，切回前台时立即刷新一次
  setInterval(() => {
    if (!document.hidden) load();
  }, POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) load();
  });
}

init();
