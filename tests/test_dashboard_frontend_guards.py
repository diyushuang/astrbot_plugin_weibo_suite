"""面板前端的两个竞态守卫（app.js 轮询串行化、settings.js 保存互斥）。

静态断言按函数体结构检查分支顺序（守卫在入口、标志位在守卫之后置位、
清理在 finally 且先复位再补跑），而不是散落的字符串 grep。

若本机装了 Node（CI 一般不保证），再用最小 stub 真跑一遍控制流：
并发调用只触发一次请求、排队的那次在结束后补跑一次、结束后标志位复位。
Node 缺失时这两个用例跳过，不新增依赖，也不改 CI 配置。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "views" / "dashboard" / "app.js").read_text(encoding="utf-8")
SETTINGS = (ROOT / "views" / "dashboard" / "settings.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(source: str, header: str) -> str:
    """从 header 处截出完整函数文本：按大括号配对，跳过字符串、模板与注释。"""
    start = source.index(header)
    index = source.index("{", start)
    depth = 0
    quote = ""
    while index < len(source):
        char = source[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in {'"', "'", "`"}:
            quote = char
        elif source.startswith("//", index):
            index = source.index("\n", index)
            continue
        elif source.startswith("/*", index):
            index = source.index("*/", index) + 2
            continue
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
        index += 1
    raise AssertionError(f"未能截出函数体：{header}")


def _run_node(tmp_path: Path, script: str, name: str) -> str:
    path = tmp_path / f"{name}.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


# ---------------------------------------------------------------- app.js load()


def test_app_load_has_single_flight_guard():
    assert "if (loadInFlight) {" in APP
    assert "loadQueued = true;" in APP
    assert "setTimeout(load, 0);" in APP


def test_settings_save_rejects_concurrent_shortcut():
    assert "if (state.saving) return;" in SETTINGS
    assert "state.saving = true;" in SETTINGS
    assert "state.saving = false;" in SETTINGS


def test_app_load_guard_precedes_work_and_requeues_in_finally():
    body = _extract_function(APP, "async function load() {")
    # 守卫是入口第一步：并发到达时只打补跑标记并退出，不会走到 loadAll
    assert body.index("if (loadInFlight) {") < body.index("loadInFlight = true;")
    assert body.index("loadInFlight = true;") < body.index("await loadAll();")
    # 复位与补跑都在 finally 里，且先复位再补跑（否则补跑会被自己的守卫挡住）
    assert body.index("try {") < body.index("finally {")
    finally_block = body[body.index("finally {") :]
    assert "loadQueued = false;" in finally_block
    assert finally_block.index("loadInFlight = false;") < finally_block.index(
        "setTimeout(load, 0);"
    )


APP_HARNESS = r"""
const assert = require("assert");
let loadInFlight = false;
let loadQueued = false;
let calls = 0;
let release;
const gate = new Promise((resolve) => { release = resolve; });
async function loadAll() {
  calls += 1;
  if (calls === 1) await gate;
}
__FUNC__

(async () => {
  const first = load();
  assert.strictEqual(calls, 1, "首次调用应直接进入 loadAll");
  const second = load();
  await second;
  assert.strictEqual(calls, 1, "并发调用不应重复进入 loadAll");
  assert.strictEqual(loadQueued, true, "并发调用应记录补跑标记");
  release();
  await first;
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.strictEqual(calls, 2, "补跑应在当前轮结束后执行一次");
  assert.strictEqual(loadInFlight, false, "结束时 in-flight 标记应复位");
  console.log("app-load-ok");
})().catch((error) => { console.error(error); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="本机无 Node，跳过 JS 控制流运行时验证")
def test_app_load_single_flight_runtime(tmp_path):
    script = APP_HARNESS.replace("__FUNC__", _extract_function(APP, "async function load() {"))
    assert "app-load-ok" in _run_node(tmp_path, script, "app_load")


# --------------------------------------------------------- settings.js saveSettings


def test_settings_save_guard_and_flag_reset_ordering():
    body = _extract_function(SETTINGS, "async function saveSettings() {")
    # 互斥守卫在置位之前；置位在真正发起请求之前
    assert body.index("if (state.saving) return;") < body.index("state.saving = true;")
    assert body.index("state.saving = true;") < body.index("apiPost(")
    finally_block = body[body.index("finally {") :]
    assert finally_block.index("state.saving = false;") < finally_block.index(
        "nodes.save.disabled = false;"
    )


SETTINGS_HARNESS = r"""
const assert = require("assert");
const statuses = [];
let posts = 0;
let release;
const gate = new Promise((resolve) => { release = resolve; });
const state = {
  saving: false,
  dirty: new Map([["push.at_all", true]]),
  pages: [],
  counts: {},
  values: {},
  secrets: {},
  generatedTs: 0,
  hooks: {
    status: (message, tone) => statuses.push([message, tone]),
    confirm: async () => true,
  },
};
const nodes = { save: { disabled: false }, reload: { disabled: false } };
function collectChanges() { return { payload: { "push.at_all": true }, errors: [] }; }
function fieldByKey() { return null; }
function api() {
  return { apiPost: async () => { posts += 1; await gate; return { pages: [] }; } };
}
function deepEqual(a, b) { return a === b; }
function renderAll() {}
__FUNC__

(async () => {
  const first = saveSettings();
  assert.strictEqual(state.saving, true, "进入保存后应置 saving");
  const second = saveSettings();
  await second;
  assert.strictEqual(posts, 1, "保存进行中再次触发不应重复提交");
  release();
  await first;
  assert.strictEqual(state.saving, false, "结束后 saving 应复位");
  assert.strictEqual(nodes.save.disabled, false, "结束后保存按钮应恢复可用");
  assert.strictEqual(nodes.reload.disabled, false, "结束后重载按钮应恢复可用");
  assert.ok(statuses.some(([, tone]) => tone === "ok"), "成功后应上报 ok 状态");
  console.log("settings-save-ok");
})().catch((error) => { console.error(error); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="本机无 Node，跳过 JS 控制流运行时验证")
def test_settings_save_mutex_runtime(tmp_path):
    script = SETTINGS_HARNESS.replace(
        "__FUNC__", _extract_function(SETTINGS, "async function saveSettings() {")
    )
    assert "settings-save-ok" in _run_node(tmp_path, script, "settings_save")
