/* 用 Edge/Chrome headless + CDP 采集面板指标、生成截图矩阵。无第三方依赖
 * （Node 24 自带 fetch/WebSocket）。
 *
 * 用法：
 *   python scripts/perf_dashboard/mock_server.py 8765
 *   node scripts/perf_dashboard/measure.js metrics 8765            # 当前版本
 *   git archive HEAD views/dashboard | tar -x -C /tmp/baseline
 *   python scripts/perf_dashboard/mock_server.py 8766 /tmp/baseline/views/dashboard
 *   node scripts/perf_dashboard/measure.js metrics 8765 8766       # 前后对比
 *   node scripts/perf_dashboard/measure.js shots docs/preview
 *
 * 口径（写进 metrics.json，复现上一轮报的六组数字就靠它）：
 *   nodes        = document.querySelectorAll('*').length —— 整文档（八个页签的
 *                  DOM 都在，隐藏面板也计入）。不是只看当前容器。
 *   visibleNodes = 当前 .tab-panel.active 子树内的元素数。
 *   apiCalls     = 注入的 fetch 包装记录到的 /api/ 请求数：只算接口，
 *                  不含 index.html/style.css/app.js/logo.svg 等静态资源。
 *   首屏          = Page.navigate 后固定 3500ms；metrics 模式只停在根页签
 *                  （#overview），不逐个访问其它页签。
 *   roundCalls   = 首屏后再等 22000ms（跨过一次 20s 轮询）新增的 /api/ 请求数。
 *   roundMut*    = 同一窗口内 MutationObserver 统计的 addedNodes/removedNodes。
 *   pendingRows  = #pending-rows 里的 tr 行数（默认分页 20）。
 *   apiMs        = 上述 /api/ 请求的 (结束 - 开始) 求和，单位毫秒。
 *   longTasks    = PerformanceObserver(longtask) 收集的任务数（只报条数）。
 */

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const BROWSER_CANDIDATES = [
  process.env.PERF_BROWSER,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].filter(Boolean);
const BROWSER = BROWSER_CANDIDATES.find((p) => fs.existsSync(p));
if (!BROWSER) {
  console.error("找不到 Edge/Chrome，可用 PERF_BROWSER=<可执行文件> 指定");
  process.exit(1);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const INSTRUMENT = `
window.__net = [];
window.__lt = [];
try {
  const orig = window.fetch;
  window.fetch = function () {
    const rec = { url: String(arguments[0]), t0: performance.now(), t1: 0 };
    window.__net.push(rec);
    return orig.apply(this, arguments).then(
      (r) => { rec.t1 = performance.now(); return r; },
      (e) => { rec.t1 = performance.now(); throw e; }
    );
  };
} catch (e) {}
try {
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) window.__lt.push(Math.round(entry.duration));
  }).observe({ entryTypes: ["longtask"] });
} catch (e) {}
window.__mut = { added: 0, removed: 0 };
try {
  new MutationObserver((records) => {
    for (const rec of records) {
      window.__mut.added += rec.addedNodes.length;
      window.__mut.removed += rec.removedNodes.length;
    }
  }).observe(document, { childList: true, subtree: true });
} catch (e) {}
`;

const METRICS = `(function(){
  const api = (window.__net || []).filter(r => r.url.indexOf('/api/') >= 0);
  const apiMs = api.reduce((a,r) => a + ((r.t1||performance.now()) - r.t0), 0);
  return {
    nodes: document.querySelectorAll('*').length,
    visibleNodes: document.querySelectorAll('.tab-panel.active *').length,
    apiCalls: api.length,
    apiMs: Math.round(apiMs),
    apiNames: api.map(r => r.url.split('/api/')[1].split('?')[0]),
    longTasks: (window.__lt || []).length,
    mutAdded: (window.__mut || {}).added || 0,
    mutRemoved: (window.__mut || {}).removed || 0,
    pendingRows: document.querySelectorAll('#pending-rows tr').length,
    tabCount: document.querySelectorAll('.tab-btn').length,
    overflowX: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
    pageHeight: document.body.scrollHeight
  };
})()`;

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async eval(expression) {
    const res = await this.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
      userGesture: true,
    });
    if (res.exceptionDetails) throw new Error(res.exceptionDetails.text);
    return res.result.value;
  }
}

async function findTarget(port, timeoutMs = 15000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try {
      const resp = await fetch(`http://127.0.0.1:${port}/json/list`);
      const list = await resp.json();
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch (e) {}
    await sleep(200);
  }
  throw new Error("no CDP target");
}

let nextDebugPort = 9400;

async function capture(url, opts = {}) {
  const debugPort = opts.debugPort || nextDebugPort++;
  const width = opts.width || 1440;
  const height = opts.height || 1200;
  const proc = spawn(
    BROWSER,
    [
      "--headless=new",
      "--disable-gpu",
      "--hide-scrollbars",
      "--no-first-run",
      "--no-default-browser-check",
      `--remote-debugging-port=${debugPort}`,
      `--window-size=${width},${height}`,
      "about:blank",
    ],
    { stdio: "ignore" }
  );
  try {
    const target = await findTarget(debugPort);
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", reject, { once: true });
    });
    const cdp = new Cdp(ws);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: INSTRUMENT });
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await cdp.send("Page.navigate", { url });
    await sleep(opts.waitMs || 3500);
    if (opts.action) {
      await cdp.eval(opts.action);
      await sleep(opts.actionWaitMs || 500);
    }
    const metrics = await cdp.eval(METRICS);
    if (opts.extraWaitMs) {
      await sleep(opts.extraWaitMs);
      const after = await cdp.eval(METRICS);
      metrics.roundCalls = after.apiCalls - metrics.apiCalls;
      metrics.roundNames = after.apiNames.slice(metrics.apiCalls);
      metrics.nodesAfter = after.nodes;
      metrics.roundMutAdded = after.mutAdded - metrics.mutAdded;
      metrics.roundMutRemoved = after.mutRemoved - metrics.mutRemoved;
    }
    let shot = null;
    if (opts.file) {
      shot = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      fs.writeFileSync(opts.file, Buffer.from(shot.data, "base64"));
    }
    ws.close();
    return metrics;
  } finally {
    try { proc.kill(); } catch (e) {}
  }
}

const base = (port) => `http://127.0.0.1:${port}/`;

async function runMetrics(ports, outDir) {
  const results = {};
  for (const port of ports) {
    try {
      results[port] = await capture(base(port), { extraWaitMs: 22000 });
      const r = results[port];
      // 待推送行数只在监控页签渲染（当前版本是懒加载），单独再采一次
      const monitor = await capture(`${base(port)}#monitor`, { waitMs: 3800 });
      r.pendingRowsMonitor = monitor.pendingRows;
      console.log(`OK ${port}`, JSON.stringify({
        nodes: r.nodes,
        visibleNodes: r.visibleNodes,
        apiCalls: r.apiCalls,
        apiNames: r.apiNames,
        apiMs: r.apiMs,
        roundCalls: r.roundCalls,
        roundMutAdded: r.roundMutAdded,
        roundMutRemoved: r.roundMutRemoved,
        pendingRowsMonitor: r.pendingRowsMonitor,
        longTasks: r.longTasks,
      }));
    } catch (err) {
      results[port] = { error: String(err) };
      console.log(`FAIL ${port}`, err.message);
    }
  }
  const file = path.join(outDir, "metrics.json");
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(file, JSON.stringify(results, null, 2));
  console.log("written", file);
}

async function runShots(outDir) {
  fs.mkdirSync(outDir, { recursive: true });
  const tabs = ["overview", "monitor", "archive", "copylib", "trace", "sns", "settings", "errors"];
  const widths = [[1440, 1200], [1080, 1200], [760, 1400]];
  const themes = [["light", ""], ["dark", "?theme=dark"]];
  const port = process.argv[4] || "8765";
  const jobs = [];
  for (const [width, height] of widths) {
    for (const [theme, query] of themes) {
      for (const tab of tabs) {
        const hash = tab === "overview" ? "" : `#${tab}`;
        jobs.push({
          name: `${width}-${theme}-${tab}`,
          url: `${base(port)}${query}${hash}`,
          file: path.join(outDir, `${width}-${theme}-${tab}.png`),
          width,
          height,
          waitMs: tab === "settings" ? 5000 : 3800,
        });
      }
    }
  }
  // 边界态（1440 浅色）：弹窗、待推送展开、非法时间、接口失败
  jobs.push({
    name: "edge-modal",
    url: `${base(port)}#monitor`,
    file: path.join(outDir, "edge-modal.png"),
    width: 1440,
    height: 1200,
    action:
      `(() => { const b = [...document.querySelectorAll('#account-list button')]` +
      `.find(x => x.textContent.trim() === '移除'); if (b) b.click(); })()`,
  });
  jobs.push({
    name: "edge-expanded",
    url: `${base(port)}#monitor`,
    file: path.join(outDir, "edge-expanded.png"),
    width: 1440,
    height: 1200,
    action:
      `(() => { const b = document.getElementById('btn-pending-more');` +
      ` if (b) { b.click(); b.scrollIntoView({ block: 'center' }); } })()`,
  });
  jobs.push({
    name: "edge-badtime",
    url: `${base(port)}?badtime=1`,
    file: path.join(outDir, "edge-badtime.png"),
    width: 1440,
    height: 1200,
  });
  jobs.push({
    name: "edge-badtime-archive",
    url: `${base(port)}?badtime=1#archive`,
    file: path.join(outDir, "edge-badtime-archive.png"),
    width: 1440,
    height: 1200,
    action:
      `(() => { const node = document.getElementById('backfill-progress');` +
      ` if (node) window.scrollTo(0, node.getBoundingClientRect().top + window.scrollY - 160); })()`,
  });
  jobs.push({
    name: "edge-apifail",
    url: `${base(port)}?fail=pipeline/status#archive`,
    file: path.join(outDir, "edge-apifail.png"),
    width: 1440,
    height: 1200,
    action:
      `(() => { const badge = document.getElementById('pipeline-badge');` +
      ` const card = badge && badge.closest('.card');` +
      ` if (card) window.scrollTo(0, card.offsetTop - 140); })()`,
  });

  const filter = process.argv[5] || "";
  const selected = filter ? jobs.filter((job) => job.name.includes(filter)) : jobs;

  let failed = 0;
  for (const job of selected) {
    try {
      await capture(job.url, job);
      console.log("OK", job.name);
    } catch (err) {
      failed += 1;
      console.log("FAIL", job.name, err.message);
    }
  }
  console.log(`shots done: ${selected.length - failed}/${selected.length} -> ${outDir}`);
  if (failed) process.exitCode = 1;
}

(async () => {
  const mode = process.argv[2] || "metrics";
  if (mode === "metrics") {
    const ports = process.argv.slice(3).filter((x) => /^\d+$/.test(x));
    const outDir = path.join(__dirname, "out");
    await runMetrics(ports.length ? ports : ["8765"], outDir);
  } else if (mode === "shots") {
    await runShots(process.argv[3] || path.join(__dirname, "shots"));
  } else {
    console.error("用法：node measure.js metrics 8765 [8766] | shots <输出目录> [端口]");
    process.exit(1);
  }
})();
