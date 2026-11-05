/* 设置页：按后端下发的 schema 目录动态渲染 179 个配置项。
 *
 * 设计约束：
 * - 数据只来自 `GET settings`，写回只走 `POST settings/save`（批量）。
 * - secret 字段永远不接收服务端原值；输入框留空 = 保持不变，显式「清除」才写空串。
 * - 未保存改动保存在内存，搜索/切页/20 秒轮询都不会覆盖；离开设置页会二次确认。
 * - 所有动态文本用 textContent / DOM API 写入，不拼 HTML。
 */

const SECRET_KEEP = "__KEEP__";
const ICON_BY_PAGE = {
  "monitor-push": "./icon-monitor.svg",
  "archive-pipeline": "./icon-archive.svg",
  storage: "./icon-storage.svg",
  "trace-checkin": "./icon-trace.svg",
  sns: "./icon-sns.svg",
  about: "./icon-about.svg",
};

const state = {
  initialized: false,
  loaded: false,
  loading: false,
  version: "",
  pages: [],
  counts: {},
  values: {},
  secrets: {},
  dirty: new Map(),
  // Ctrl+S 不经过按钮禁用态：没有这个标记，连按两次会并发两次保存
  saving: false,
  activePage: "",
  filter: "",
  generatedTs: 0,
  hooks: { confirm: async () => true, status: () => {} },
};

const nodes = {};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== "") node.textContent = text;
  return node;
}

function api() {
  return window.__bridge;
}

function deepEqual(a, b) {
  if (a === b) return true;
  if (a === null || b === null || a === undefined || b === undefined) return false;
  if (typeof a !== typeof b) return false;
  if (typeof a === "object") {
    try {
      return JSON.stringify(a) === JSON.stringify(b);
    } catch (e) {
      return false;
    }
  }
  return false;
}

function allFields() {
  const out = [];
  for (const page of state.pages) {
    for (const group of page.groups || []) {
      for (const field of group.fields || []) {
        out.push({ page, group, field });
      }
    }
  }
  return out;
}

function fieldByKey(key) {
  for (const item of allFields()) {
    if (item.field.key === key) return item;
  }
  return null;
}

function short(text, limit = 56) {
  const value = String(text ?? "");
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

function defaultText(field) {
  if (field.secret) {
    const def = field.default;
    return def === null || def === undefined || def === ""
      ? "默认：空"
      : `默认：${short(def, 24)}（不显示当前值）`;
  }
  const value = field.default;
  if (field.type === "bool") return `默认：${value ? "开" : "关"}`;
  if (field.type === "list") {
    return `默认：${Array.isArray(value) && value.length ? `${value.length} 项` : "空列表"}`;
  }
  if (field.type === "object" || field.type === "dict") {
    const keys = value && typeof value === "object" ? Object.keys(value).length : 0;
    return `默认：${keys ? `${keys} 个键` : "空对象"}`;
  }
  if (field.type === "text" || (typeof value === "string" && value.length > 24)) {
    return `默认：${short(value, 40) || "空"}`;
  }
  return `默认：${value === "" || value === undefined || value === null ? "空" : String(value)}${field.unit || ""}`;
}

function rangeText(field) {
  if (field.min !== null && field.min !== undefined && field.max !== null && field.max !== undefined) {
    const unit = field.unit ? ` ${field.unit}` : "";
    return `范围 ${field.min}–${field.max}${unit}`;
  }
  if (field.options && field.options.length) return `${field.options.length} 个可选值`;
  if (field.type === "list") return "每行一项";
  if (field.type === "object" || field.type === "dict") return "JSON 对象";
  return "";
}

function conditionMatches(field) {
  if (!field.condition || !field.condition.length) return true;
  for (const cond of field.condition) {
    const target = fieldByKey(cond.field);
    if (!target) return true;
    const value = currentValue(target.field);
    if (value !== cond.value) return false;
  }
  return true;
}

function conditionText(field) {
  if (!field.condition || !field.condition.length) return "";
  return field.condition.map((item) => `仅当 ${item.label} 生效`).join("；");
}

function currentValue(field) {
  if (field.secret) return "";
  if (state.dirty.has(field.key)) return state.dirty.get(field.key);
  if (Object.prototype.hasOwnProperty.call(state.values, field.key)) {
    return state.values[field.key];
  }
  return field.default;
}

function originalValue(field) {
  if (Object.prototype.hasOwnProperty.call(state.values, field.key)) {
    return state.values[field.key];
  }
  return field.default;
}

function isModified(field) {
  return state.dirty.has(field.key);
}

/* ---------------- 布局：摘要 / 覆盖 / 导航 ---------------- */

function renderSummary() {
  const c = state.counts || {};
  nodes.summary.textContent =
    `${c.groups || 0} 个分组 · ${c.keys || 0} 个配置键全部可编辑 · ` +
    `${c.secrets || 0} 个凭据字段脱敏 · 会话沿用 AstrBot 登录态`;
}

function renderCoverage() {
  nodes.coverage.replaceChildren();
  const c = state.counts || {};
  const items = [
    ["主题页", `${c.pages || 0}`, "按业务线归并"],
    ["分组", `${c.groups || 0}`, "schema 全覆盖"],
    ["配置键", `${c.keys || 0}`, "无只读项"],
    ["可写", `${c.writable || 0}`, "批量写回"],
    ["覆盖率", "100%", "键 → 控件"],
  ];
  for (const [label, value, sub] of items) {
    const box = el("div", "coverage-tile");
    box.append(el("div", "coverage-value", value));
    box.append(el("div", "coverage-label", label));
    box.append(el("div", "coverage-sub", sub));
    nodes.coverage.append(box);
  }
}

function renderRail() {
  nodes.rail.replaceChildren();
  const filtering = state.filter.trim() !== "";
  for (const page of state.pages) {
    const fields = [];
    for (const group of page.groups || []) fields.push(...group.fields);
    const count = filtering
      ? fields.filter((field) => matches(page, groupOf(field), field)).length
      : fields.length;
    const btn = el("button", "rail-btn");
    btn.type = "button";
    btn.dataset.page = page.id;
    if (!filtering && page.id === state.activePage) btn.classList.add("active");
    const icon = el("img", "rail-icon");
    icon.src = ICON_BY_PAGE[page.id] || "./icon-about.svg";
    icon.alt = "";
    const copy = el("span", "rail-copy");
    copy.append(el("span", "rail-title", page.title));
    copy.append(el("span", "rail-sub", page.about ? "版本与安全边界" : `${count} 项配置`));
    const badge = el("span", "rail-count", page.about ? "i" : String(count));
    btn.append(icon, copy, badge);
    btn.addEventListener("click", async () => {
      state.activePage = page.id;
      state.filter = "";
      nodes.search.value = "";
      renderAll();
    });
    nodes.rail.append(btn);
  }
  const tip = el("div", "rail-tip");
  tip.append(el("strong", "", "批量保存"));
  tip.append(
    el("span", "", "改动先留在页面里，点「保存全部」一次性写回 AstrBot 配置；失败会留在原地。")
  );
  nodes.rail.append(tip);
}

function groupOf(field) {
  for (const group of allFields()) {
    if (group.field.key === field.key) return group.group;
  }
  return { title: "" };
}

function matches(page, group, field) {
  const needle = state.filter.trim().toLowerCase();
  if (!needle) return true;
  const haystack = [
    field.label,
    field.key,
    field.description,
    group.title,
    page.title,
    field.requirement,
    ...(field.options || []).map((item) => item.label),
  ]
    .join(" ")
    .toLowerCase();
  return needle.split(/\s+/).every((part) => haystack.includes(part));
}

/* ---------------- 卡片与配置行 ---------------- */

function chip(label, tone) {
  return el("span", `s-chip ${tone || ""}`.trim(), label);
}

function defaultChipText(field) {
  return defaultText(field);
}

function buildRow(field) {
  const row = el("div", "setting-row");
  row.dataset.key = field.key;
  if (field.danger) row.classList.add("is-danger");
  if (!conditionMatches(field)) row.classList.add("cond-inactive");

  const text = el("div", "setting-text");
  const labelLine = el("div", "setting-label-line");
  labelLine.append(el("span", "setting-label", field.label));
  if (field.requirement && /必填|二选一/.test(field.requirement)) {
    labelLine.append(chip(field.requirement, "req"));
  }
  if (field.danger) labelLine.append(chip("危险", "danger"));
  if (field.managed) labelLine.append(chip("指令维护", "managed"));
  if (field.plaintext) labelLine.append(chip("明文 JSON", "plaintext"));
  if (field.condition && field.condition.length) {
    labelLine.append(chip(conditionText(field), "cond"));
  }
  const modified = chip("已修改", "modified");
  modified.hidden = !isModified(field);
  modified.dataset.role = "modified";
  labelLine.append(modified);
  text.append(labelLine);
  if (field.description) text.append(el("div", "setting-desc", field.description));

  const meta = el("div", "setting-meta");
  meta.append(el("span", "meta-item", defaultChipText(field)));
  const range = rangeText(field);
  if (range) meta.append(el("span", "meta-item", range));
  meta.append(el("code", "setting-key", field.key));
  text.append(meta);

  row.append(text, buildControl(field));
  return row;
}

function buildControl(field) {
  const wrap = el("div", `setting-control control-${field.widget}`);
  if (field.widget === "switch") return buildSwitch(field, wrap);
  if (field.widget === "select") return buildSelect(field, wrap);
  if (field.widget === "number") return buildNumber(field, wrap);
  if (field.widget === "password") return buildPassword(field, wrap);
  if (field.widget === "textarea") return buildTextarea(field, wrap);
  if (field.widget === "list") return buildList(field, wrap);
  if (field.widget === "json") return buildJson(field, wrap);
  return buildText(field, wrap);
}

function markDirty(field, value) {
  const original = originalValue(field);
  if (!field.secret && deepEqual(value, original)) {
    state.dirty.delete(field.key);
  } else {
    state.dirty.set(field.key, value);
  }
  refreshDirtyUI();
}

function updateRowModified(field) {
  const row = nodes.body.querySelector(`.setting-row[data-key="${cssEscape(field.key)}"]`);
  if (!row) return;
  const badge = row.querySelector('[data-role="modified"]');
  if (badge) badge.hidden = !isModified(field);
  row.classList.toggle("dirty", isModified(field));
}

function cssEscape(text) {
  if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(text);
  return String(text).replace(/["\\]/g, "\\$&");
}

function refreshDirtyUI() {
  const n = state.dirty.size;
  nodes.savebar.hidden = n === 0;
  nodes.dirtyCount.textContent = String(n);
  for (const { field } of allFields()) updateRowModified(field);
}

function buildSwitch(field, wrap) {
  const label = el("label", "switch");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = Boolean(currentValue(field));
  input.setAttribute("role", "switch");
  input.addEventListener("change", () => {
    markDirty(field, Boolean(input.checked));
    updateConditionStyles();
  });
  const track = el("span", "switch-track");
  track.append(el("span", "switch-thumb"));
  label.append(input, track);
  wrap.append(label);
  const stateText = el("span", "control-state", input.checked ? "开启" : "关闭");
  input.addEventListener("change", () => {
    stateText.textContent = input.checked ? "开启" : "关闭";
  });
  wrap.append(stateText);
  return wrap;
}

function buildSelect(field, wrap) {
  const select = document.createElement("select");
  const current = currentValue(field);
  for (const option of field.options || []) {
    const node = document.createElement("option");
    node.value = String(option.value);
    node.textContent = option.label;
    if (String(current) === String(option.value)) node.selected = true;
    select.append(node);
  }
  select.addEventListener("change", () => {
    let value = select.value;
    if (field.type === "int" || field.type === "float") value = Number(value);
    markDirty(field, value);
    updateConditionStyles();
  });
  wrap.append(select);
  return wrap;
}

function buildNumber(field, wrap) {
  const box = el("div", "number-box");
  const input = document.createElement("input");
  input.type = "number";
  if (field.min !== null && field.min !== undefined) input.min = String(field.min);
  if (field.max !== null && field.max !== undefined) input.max = String(field.max);
  input.step = String(field.step ?? (field.type === "float" ? "0.01" : "1"));
  const value = currentValue(field);
  input.value = value === null || value === undefined ? "" : String(value);
  const unit = el("span", "number-unit", field.unit || "");
  box.append(input);
  if (field.unit) box.append(unit);
  wrap.append(box);
  const hint = el("span", "control-hint", "");
  wrap.append(hint);
  const sync = () => {
    if (input.value.trim() === "") {
      // 空输入不再按 0 写入：min > 0 的字段会被后端 400 拒绝，而 0 对
      // "关闭类"字段又可能是一个用户没明确选择过的值。留空 = 不提交这一项。
      hint.textContent = "需要填写数字";
      hint.className = "control-hint bad";
      state.dirty.delete(field.key);
      refreshDirtyUI();
      return;
    }
    const number = Number(input.value);
    if (!Number.isFinite(number)) {
      hint.textContent = "需要数字";
      hint.className = "control-hint bad";
      state.dirty.delete(field.key);
      refreshDirtyUI();
      return;
    }
    if (field.min !== null && field.min !== undefined && number < field.min) {
      hint.textContent = `不能小于 ${field.min}`;
      hint.className = "control-hint bad";
    } else if (field.max !== null && field.max !== undefined && number > field.max) {
      hint.textContent = `不能大于 ${field.max}`;
      hint.className = "control-hint bad";
    } else {
      hint.textContent = "";
      hint.className = "control-hint";
    }
    markDirty(field, number);
  };
  input.addEventListener("input", sync);
  return wrap;
}

function buildPassword(field, wrap) {
  const box = el("div", "secret-box");
  const input = document.createElement("input");
  input.type = "password";
  input.autocomplete = "new-password";
  input.spellcheck = false;
  const configured = Boolean(state.secrets && state.secrets[field.key] && state.secrets[field.key].configured);
  const hasDefault =
    field.default !== null && field.default !== undefined && String(field.default) !== "";
  const dirty = state.dirty.has(field.key);
  const dirtyValue = dirty ? state.dirty.get(field.key) : "";
  input.value = typeof dirtyValue === "string" && dirtyValue !== "" ? dirtyValue : "";
  const clearing = dirty && dirtyValue === "";
  input.placeholder = clearing
    ? "保存后清除"
    : configured
      ? "已配置 · 留空保持不变"
      : hasDefault
        ? "使用默认值 · 留空保持不变"
        : "未配置 · 留空保持不变";
  input.addEventListener("input", () => {
    if (input.value === "") {
      state.dirty.delete(field.key);
    } else {
      state.dirty.set(field.key, input.value);
    }
    refreshDirtyUI();
    syncSecret();
  });
  const clear = el("button", "btn-mini", "清除");
  clear.type = "button";
  clear.disabled = clearing;
  clear.addEventListener("click", () => {
    state.dirty.set(field.key, "");
    input.value = "";
    input.placeholder = "保存后清除";
    clear.disabled = true;
    refreshDirtyUI();
    syncSecret();
  });
  const status = el("span", "control-state");
  const syncSecret = () => {
    const isDirty = state.dirty.has(field.key);
    if (!isDirty) {
      status.textContent = configured
        ? "已配置（不回显）"
        : hasDefault
          ? "使用默认值"
          : "未配置";
      status.className = "control-state";
    } else if (state.dirty.get(field.key) === "") {
      status.textContent = "待清除";
      status.className = "control-state warn";
    } else {
      status.textContent = "待更新";
      status.className = "control-state ok";
    }
    clear.disabled = state.dirty.has(field.key) && state.dirty.get(field.key) === "";
  };
  syncSecret();
  box.append(input, clear);
  wrap.append(box, status);
  return wrap;
}

function buildText(field, wrap) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = currentValue(field) ?? "";
  input.placeholder = field.placeholder || "";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.addEventListener("input", () => markDirty(field, input.value));
  wrap.append(input);
  return wrap;
}

function buildTextarea(field, wrap) {
  const area = document.createElement("textarea");
  area.rows = field.key === "push.message_format" || field.key === "sns.message_format" ? 4 : 3;
  area.value = currentValue(field) ?? "";
  area.placeholder = field.placeholder || "";
  area.spellcheck = false;
  area.addEventListener("input", () => markDirty(field, area.value));
  wrap.append(area);
  if (field.key === "push.message_format") {
    wrap.append(
      el("span", "control-hint", "占位符：{name} {time} {weibo} {link}；字面量 \\n 表示换行")
    );
  } else if (field.key === "sns.message_format") {
    wrap.append(
      el("span", "control-hint", "占位符：{platform} {name} {time} {text} {link}")
    );
  }
  return wrap;
}

function buildList(field, wrap) {
  const area = document.createElement("textarea");
  area.spellcheck = false;
  const value = currentValue(field);
  const items = Array.isArray(value) ? value : typeof value === "string" ? value.split("\n") : [];
  area.rows = Math.min(5, Math.max(2, items.length + 1));
  if (Array.isArray(value)) area.value = value.join("\n");
  else if (typeof value === "string") area.value = value;
  else area.value = "";
  area.placeholder = "每行一项；空行会被忽略";
  area.addEventListener("input", () => {
    const items = area.value
      .split("\n")
      .map((item) => item.trim())
      .filter(Boolean);
    markDirty(field, items);
  });
  wrap.append(area);
  const note = el("span", "control-hint", "每行一项，保存为字符串数组");
  wrap.append(note);
  return wrap;
}

function buildJson(field, wrap) {
  const area = document.createElement("textarea");
  area.rows = 5;
  area.spellcheck = false;
  area.className = "json-editor";
  const value = currentValue(field);
  area.value =
    value && typeof value === "object" ? JSON.stringify(value, null, 2) : value == null ? "" : String(value);
  area.placeholder = "{}";
  const hint = el("span", "control-hint", "");
  const sync = () => {
    const text = area.value.trim();
    if (!text) {
      hint.textContent = "空内容会保存成 {}";
      hint.className = "control-hint";
      markDirty(field, {});
      return;
    }
    try {
      const parsed = JSON.parse(text);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        hint.textContent = "需要一个 JSON 对象，不能是数组";
        hint.className = "control-hint bad";
        // 非法输入必须撤销上一次合法解析留下的 dirty，否则点保存时会把
        // 旧对象写回去，而页面显示的是这段非法文本。
        state.dirty.delete(field.key);
        refreshDirtyUI();
        return;
      }
      hint.textContent = `JSON 有效 · ${Object.keys(parsed).length} 个键`;
      hint.className = "control-hint ok";
      markDirty(field, parsed);
    } catch (e) {
      hint.textContent = `JSON 语法错误：${e.message}`;
      hint.className = "control-hint bad";
      state.dirty.delete(field.key);
      refreshDirtyUI();
    }
  };
  area.addEventListener("input", sync);
  wrap.append(area, hint);
  return wrap;
}

function updateConditionStyles() {
  for (const { field } of allFields()) {
    const row = nodes.body.querySelector(`.setting-row[data-key="${cssEscape(field.key)}"]`);
    if (!row) continue;
    row.classList.toggle("cond-inactive", !conditionMatches(field));
  }
}

function buildGroupCard(page, group, fields, options = {}) {
  const card = el("section", `settings-card accent-${group.accent || "neutral"}`);
  card.dataset.group = group.id;
  const head = el("header", "settings-card-head");
  const titleBox = el("div", "settings-card-title");
  const titleLine = el("div", "settings-card-title-line");
  titleLine.append(el("span", "accent-bar"), el("h3", "", group.title));
  titleLine.append(el("span", "group-count", `${fields.length} 项`));
  titleBox.append(titleLine, el("p", "settings-card-sum", group.summary));
  head.append(titleBox);
  const actions = el("div", "settings-card-actions");
  const reset = el("button", "btn-mini", "恢复本组默认");
  reset.type = "button";
  reset.addEventListener("click", async () => {
    const changed = fields.filter((field) => {
      if (field.secret) {
        return Boolean(state.secrets[field.key] && state.secrets[field.key].configured);
      }
      // 已保存的非默认值也算"可恢复"：否则打开设置页后首次点恢复会被误判为空。
      return state.dirty.has(field.key) || !deepEqual(originalValue(field), field.default);
    });
    if (!changed.length) {
      state.hooks.status("本组没有可恢复的改动", "warning");
      return;
    }
    const ok = await state.hooks.confirm(
      `把「${group.title}」的 ${changed.length} 项恢复为默认值？凭据字段会恢复默认或清除。`
    );
    if (!ok) return;
    for (const field of fields) {
      state.dirty.delete(field.key);
      if (field.secret) {
        const hasDefault =
          field.default !== null && field.default !== undefined && String(field.default) !== "";
        if (state.secrets[field.key] && state.secrets[field.key].configured) {
          // 少数凭据字段有非空默认值（如鉴权头名称）；恢复默认不是清空。
          state.dirty.set(field.key, hasDefault ? String(field.default) : "");
        }
        continue;
      }
      if (!deepEqual(originalValue(field), field.default)) {
        state.dirty.set(field.key, field.default);
      }
    }
    renderAll();
    state.hooks.status(`已恢复「${group.title}」的默认值（尚未保存）`, "ok");
  });
  actions.append(reset);
  head.append(actions);
  card.append(head);

  const normal = fields.filter((field) => !field.advanced);
  const advanced = fields.filter((field) => field.advanced);
  for (const field of normal) card.append(buildRow(field));
  if (advanced.length) {
    const details = el("details", "settings-advanced");
    details.open = Boolean(state.filter);
    const summary = el("summary");
    summary.append(el("span", "", "高级选项"), el("span", "group-count", `${advanced.length} 项`));
    details.append(summary);
    for (const field of advanced) details.append(buildRow(field));
    card.append(details);
  }
  if (options.searchNote) {
    card.classList.add("search-hit");
  }
  return card;
}

function renderAboutPage(page) {
  const wrap = el("div", "settings-about");
  const hero = el("section", "about-hero");
  const logo = el("img", "about-logo");
  logo.src = "./logo.svg";
  logo.alt = "微博全链路助手";
  const copy = el("div");
  copy.append(el("div", "eyebrow", "ABOUT"));
  copy.append(el("h2", "", "微博全链路助手"));
  copy.append(
    el(
      "p",
      "about-lead",
      "一个插件装下四条业务线：微博监控转发、整帖归档与图床/向量管线、图片溯源与签到、多平台订阅。设置页直接读写同一份 AstrBot 插件配置。"
    )
  );
  const version = el("div", "about-version");
  version.append(chip(state.version || "运行中", "req"));
  hero.append(logo, copy);

  const counts = el("section", "about-grid");
  const c = state.counts || {};
  const stats = [
    ["主题页", String(c.pages || 0)],
    ["schema 分组", String(c.groups || 0)],
    ["配置键", String(c.keys || 0)],
    ["可写", String(c.writable || 0)],
    ["凭据脱敏", String(c.secrets || 0)],
  ];
  for (const [label, value] of stats) {
    const tile = el("div", "about-tile");
    tile.append(el("div", "about-value", value));
    tile.append(el("div", "about-label", label));
    counts.append(tile);
  }

  const lines = el("section", "about-lines");
  const entries = [
    ["监控转发", "轮询、过滤、推送队列、@全体、群相册与图片投递。", "./icon-monitor.svg"],
    ["归档与管线", "archive.db 账本、原图下载、图床/向量管线与文案清洗。", "./icon-archive.svg"],
    ["图床与存储", "CloudFlare-ImgBed、通用 HTTP 图床、R2 与 OCI。", "./icon-storage.svg"],
    ["溯源与签到", "pHash / Qdrant 双引擎、随机图与每日签到卡片。", "./icon-trace.svg"],
    ["多平台订阅", "抖音 / 小红书 / Instagram 的浏览器、HTTP 与 RSS 通道。", "./icon-sns.svg"],
  ];
  for (const [title, desc, icon] of entries) {
    const row = el("div", "about-line");
    const img = el("img", "about-line-icon");
    img.src = icon;
    img.alt = "";
    const body = el("div");
    body.append(el("strong", "", title), el("span", "", desc));
    row.append(img, body);
    lines.append(row);
  }

  const security = el("section", "about-security");
  security.append(el("h3", "", "安全边界"));
  const list = el("ul");
  for (const text of [
    "secret 字段只显示「是否已配置」，任何响应都不包含原值；输入框留空表示保持不变，点「清除」才会写空串。",
    "配置文件本身仍是明文。备份、截图、发 issue 前请剔除 Cookie、Token、Access Key 与代理账号。",
    "面板写入等同于聊天指令的管理操作；面板访问权限完全由 AstrBot WebUI 登录会话决定，插件侧没有额外 token。",
    "这些设置写入 schema 已声明的 179 个分组键；旧布局的顶层镜像键不在此页展示，保存后由插件同步为运行期平铺键。",
    "少数项需要重载插件才生效（例如面板路由、ffmpeg 探测、已缓存的并发与浏览器目录）。",
  ]) {
    list.append(el("li", "", text));
  }
  security.append(list);

  const footer = el("p", "about-foot");
  footer.textContent = "由插件 WebUI 面板提供 · 设置数据每次打开页面时读取 · 保存后立即写盘";
  wrap.append(hero, counts, lines, security, footer);
  return wrap;
}

/* ---------------- 页面主体 ---------------- */

function renderBody() {
  nodes.body.replaceChildren();
  const filtering = state.filter.trim() !== "";
  if (filtering) {
    const head = el("div", "search-head");
    const results = allFields().filter(({ page, group, field }) => matches(page, group, field));
    head.append(el("h2", "", `搜索结果`), el("span", "search-count", `${results.length} 项匹配`));
    head.append(el("code", "", state.filter.trim()));
    nodes.body.append(head);
    if (!results.length) {
      nodes.body.append(
        el("p", "empty", "没有匹配的配置项。可以试试点名、键名或分组名，例如 cookie、超时、图床。")
      );
      return;
    }
    const byPage = new Map();
    for (const item of results) {
      if (!byPage.has(item.page.id)) byPage.set(item.page.id, []);
      byPage.get(item.page.id).push(item);
    }
    for (const [pageId, items] of byPage) {
      const page = state.pages.find((item) => item.id === pageId);
      if (page.about) continue;
      const pageHead = el("div", "search-page-head");
      pageHead.append(el("span", "search-page-dot"), el("h3", "", page.title));
      nodes.body.append(pageHead);
      const byGroup = new Map();
      for (const item of items) {
        if (!byGroup.has(item.group.id)) byGroup.set(item.group.id, []);
        byGroup.get(item.group.id).push(item);
      }
      for (const [groupId, groupItems] of byGroup) {
        const group = page.groups.find((item) => item.id === groupId);
        nodes.body.append(
          buildGroupCard(page, group, groupItems.map((item) => item.field), { searchNote: true })
        );
      }
    }
    return;
  }

  const page = state.pages.find((item) => item.id === state.activePage) || state.pages[0];
  if (!page) return;
  const head = el("div", "page-intro");
  const left = el("div");
  left.append(el("div", "eyebrow", page.about ? "SECURITY" : "SETTINGS"));
  left.append(el("h2", "", page.title));
  left.append(el("p", "page-intro-sum", page.summary));
  head.append(left);
  const right = el("div", "page-intro-meta");
  if (page.about) {
    right.append(el("span", "intro-badge", "不含配置键"));
  } else {
    const total = page.groups.reduce((sum, group) => sum + group.fields.length, 0);
    right.append(el("span", "intro-badge", `${page.groups.length} 组`));
    right.append(el("span", "intro-badge", `${total} 项`));
  }
  head.append(right);
  nodes.body.append(head);

  if (page.about) {
    nodes.body.append(renderAboutPage(page));
    return;
  }
  for (const group of page.groups) {
    nodes.body.append(buildGroupCard(page, group, group.fields));
  }
}

function renderAll() {
  renderSummary();
  renderCoverage();
  renderRail();
  renderBody();
  refreshDirtyUI();
}

function renderLoadError(error) {
  nodes.summary.textContent = "设置目录读取失败";
  nodes.coverage.replaceChildren();
  nodes.body.replaceChildren();
  const box = el("div", "settings-error");
  box.append(el("strong", "", "无法读取设置"));
  box.append(el("p", "", String(error && error.message ? error.message : error)));
  box.append(
    el("p", "hint", "请确认插件已加载、AstrBot 版本支持 register_web_api，并且 /settings 路由注册成功。")
  );
  nodes.body.append(box);
}

/* ---------------- 交互：读取 / 保存 / 放弃 ---------------- */

async function loadSettings(options = {}) {
  if (!state.initialized || state.loading) return;
  state.loading = true;
  nodes.reload.disabled = true;
  try {
    const data = await api().apiGet("settings");
    if (!data || !Array.isArray(data.pages)) throw new Error("设置接口返回格式不正确");
    state.pages = data.pages;
    state.counts = data.counts || {};
    state.values = data.values || {};
    state.secrets = data.secrets || {};
    state.version = String(data.version || "");
    state.dirty.clear();
    state.loaded = true;
    state.generatedTs = Number(data.generated_ts || 0);
    if (!state.activePage || !state.pages.some((page) => page.id === state.activePage)) {
      state.activePage = state.pages[0] ? state.pages[0].id : "";
    }
    const versionChip = document.getElementById("version-chip");
    if (versionChip && data.version) {
      versionChip.textContent = data.version;
      versionChip.hidden = false;
    }
    renderAll();
    if (!options.quiet) state.hooks.status("设置目录已读取", "ok");
  } catch (error) {
    state.loaded = false;
    renderLoadError(error);
    state.hooks.status(`设置读取失败：${error && error.message ? error.message : error}`, "error");
  } finally {
    state.loading = false;
    nodes.reload.disabled = false;
  }
}

function collectChanges() {
  const errors = [];
  const payload = {};
  for (const [key, value] of state.dirty) {
    const item = fieldByKey(key);
    if (!item) continue;
    const field = item.field;
    if (field.secret) {
      if (value === SECRET_KEEP) continue;
      payload[key] = String(value);
      continue;
    }
    if (field.widget === "number") {
      if (value === "" || value === null || value === undefined) {
        errors.push(`${field.label} 需要填写数字`);
        continue;
      }
      if (!Number.isFinite(Number(value))) {
        errors.push(`${field.label} 需要数字`);
        continue;
      }
      payload[key] = Number(value);
      continue;
    }
    if (field.widget === "json") {
      if (typeof value === "string") {
        try {
          payload[key] = JSON.parse(value);
        } catch (e) {
          errors.push(`${field.label} 的 JSON 语法错误`);
        }
      } else {
        payload[key] = value;
      }
      continue;
    }
    payload[key] = value;
  }
  return { payload, errors };
}

async function saveSettings() {
  if (state.saving) return;
  if (!state.dirty.size) {
    state.hooks.status("没有未保存的改动", "warning");
    return;
  }
  const { payload, errors } = collectChanges();
  if (errors.length) {
    state.hooks.status(errors[0], "error");
    return;
  }
  // 保存请求期间输入框仍可编辑：成功返回后只清掉"提交时是什么、现在还是什么"
  // 的项，保存过程中新输入的值继续留在 dirty 里，避免被下一次 renderAll 覆盖。
  const submitted = new Map(state.dirty);
  const dangerous = [];
  for (const key of Object.keys(payload)) {
    const item = fieldByKey(key);
    if (item && item.field.danger) dangerous.push(item.field.label);
  }
  if (dangerous.length) {
    const ok = await state.hooks.confirm(
      `以下危险项将被修改：\n\n${dangerous.join("、")}\n\n确认写入插件配置？`
    );
    if (!ok) return;
  }
  nodes.save.disabled = true;
  nodes.reload.disabled = true;
  state.saving = true;
  state.hooks.status("正在保存设置…", "");
  try {
    const data = await api().apiPost("settings/save", { values: payload });
    if (!data || !Array.isArray(data.pages)) throw new Error("保存接口返回格式不正确");
    state.pages = data.pages;
    state.counts = data.counts || {};
    state.values = data.values || {};
    state.secrets = data.secrets || {};
    for (const [key, value] of submitted) {
      if (state.dirty.has(key) && deepEqual(state.dirty.get(key), value)) {
        state.dirty.delete(key);
      }
    }
    state.generatedTs = Number(data.generated_ts || 0);
    renderAll();
    const savedText = `已保存 ${data.saved || Object.keys(payload).length} 项配置`;
    if (data.reinit_ok === false) {
      state.hooks.status(
        `${savedText}，但重建运行期配置失败：${data.reinit_error || "未知错误"}（可重启插件生效）`,
        "warning"
      );
    } else {
      state.hooks.status(savedText, "ok");
    }
  } catch (error) {
    state.hooks.status(
      `保存失败：${error && error.message ? error.message : error}（改动仍留在页面里）`,
      "error"
    );
  } finally {
    state.saving = false;
    nodes.save.disabled = false;
    nodes.reload.disabled = false;
  }
}

async function discardSettings() {
  if (!state.dirty.size) return;
  const ok = await state.hooks.confirm(`放弃 ${state.dirty.size} 项未保存改动？`);
  if (!ok) return;
  state.dirty.clear();
  renderAll();
  state.hooks.status("已放弃未保存改动", "ok");
}

async function reloadSettings() {
  if (state.dirty.size) {
    const ok = await state.hooks.confirm(
      `重新读取会丢弃 ${state.dirty.size} 项未保存改动，确定继续？`
    );
    if (!ok) return;
  }
  await loadSettings();
}

/* ---------------- 对外接口 ---------------- */

export function settingsHasUnsaved() {
  return state.dirty.size > 0;
}

export async function settingsConfirmLeave() {
  if (!state.dirty.size) return true;
  return await state.hooks.confirm(
    `设置页还有 ${state.dirty.size} 项未保存改动。离开后改动会留在本页面，` +
      "直到你回来保存或放弃。确定离开？"
  );
}

export function settingsOnShown() {
  if (!state.initialized) return;
  if (!state.loaded) {
    loadSettings({ quiet: true });
    return;
  }
  // 已加载且没有未保存改动时，超过 90 秒再静默刷新一次；有改动绝不覆盖。
  if (!state.dirty.size && Date.now() / 1000 - state.generatedTs > 90) {
    loadSettings({ quiet: true });
  }
}

export function initSettings(hooks) {
  state.hooks = { ...state.hooks, ...(hooks || {}) };
  nodes.summary = document.getElementById("settings-summary");
  nodes.coverage = document.getElementById("settings-coverage");
  nodes.rail = document.getElementById("settings-rail");
  nodes.body = document.getElementById("settings-body");
  nodes.search = document.getElementById("settings-search");
  nodes.reload = document.getElementById("btn-settings-reload");
  nodes.save = document.getElementById("btn-settings-save");
  nodes.discard = document.getElementById("btn-settings-discard");
  nodes.savebar = document.getElementById("settings-savebar");
  nodes.dirtyCount = document.getElementById("settings-dirty-count");
  if (!nodes.body || !nodes.rail) return;
  state.initialized = true;

  let timer = null;
  nodes.search.addEventListener("input", () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      state.filter = nodes.search.value || "";
      renderRail();
      renderBody();
    }, 160);
  });
  nodes.reload.addEventListener("click", reloadSettings);
  nodes.save.addEventListener("click", saveSettings);
  nodes.discard.addEventListener("click", discardSettings);
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      const card = document.getElementById("settings-card");
      if (card && card.offsetParent !== null) {
        event.preventDefault();
        saveSettings();
      }
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (!state.dirty.size) return;
    event.preventDefault();
    event.returnValue = "";
  });
  loadSettings({ quiet: true });
}
