"""面板重构的两条回归：增量列表 syncList、强制刷新撞上在飞请求的排队轮次。

与 test_dashboard_frontend_guards.py 一样，从 app.js 按大括号配对截出函数体，
再用最小 stub 在 Node 里真跑控制流；本机没有 Node 时跳过，不新增依赖。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "views" / "dashboard" / "app.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

_OPENING = re.compile(r"\)\s*\{")


def _extract_function(source: str, header: str) -> str:
    """从 header 处截出完整函数文本：按大括号配对，跳过字符串、模板与注释。

    比按第一个 "{" 定位更稳：形参里带 default 对象（options = {}）时，
    第一个 "{" 是形参默认值，不是函数体。
    """
    start = source.index(header)
    match = _OPENING.search(source, start)
    if match is None:
        raise AssertionError(f"未能找到函数体起点：{header}")
    index = match.end() - 1
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


# ------------------------------------------------------------- syncList 增量渲染

SYNC_STUBS = r"""
let frames = [];
global.requestAnimationFrame = (cb) => { frames.push(cb); };
function runFrames() {
  const pending = frames;
  frames = [];
  for (const cb of pending) cb();
}
function makeContainer() {
  return {
    kids: [],
    get children() { return this.kids; },
    get childElementCount() { return this.kids.length; },
    insertBefore(node, ref) {
      if (node.parentNode) node.remove();
      const at = ref ? this.kids.indexOf(ref) : -1;
      if (at < 0) this.kids.push(node);
      else this.kids.splice(at, 0, node);
      node.parentNode = this;
    },
  };
}
function makeNode(item) {
  return {
    item,
    attrs: {},
    parentNode: null,
    setAttribute(key, value) { this.attrs[key] = String(value); },
    getAttribute(key) { return this.attrs[key]; },
    remove() {
      if (!this.parentNode) return;
      const at = this.parentNode.kids.indexOf(this);
      if (at >= 0) this.parentNode.kids.splice(at, 1);
      this.parentNode = null;
    },
  };
}
"""

SYNC_HARNESS = r"""
const assert = require("assert");
__STUBS__
__FUNCS__

const container = makeContainer();
const keyOf = (item) => item.id;
const build = (item) => makeNode(item);
const ids = () => container.kids.map((node) => node.item.id);
const dataKeys = () => container.kids.map((node) => node.getAttribute("data-k"));

// 键冲突：同一 post_id 出现两次时第二条自动加 #2，两行都要保留
syncList(container, [{ id: "a", t: 1 }, { id: "a", t: 2 }], keyOf, build);
assert.deepStrictEqual(ids(), ["a", "a"]);
assert.deepStrictEqual(dataKeys(), ["a", "a#2"]);

// 删行：第一条消失后旧节点必须被移除，剩下的那条按新签名复用
const firstNode = container.kids[0];
syncList(container, [{ id: "a", t: 2 }], keyOf, build);
assert.deepStrictEqual(ids(), ["a"]);
assert.strictEqual(container.kids[0].item.t, 2);
assert.strictEqual(container.kids[0].getAttribute("data-k"), "a");
assert.strictEqual(firstNode.parentNode, null, "被删掉的节点不能留在容器里");

// 重排：只移动节点，不重建（同一节点对象应保持）
syncList(container, [{ id: "b" }, { id: "c" }, { id: "d" }], keyOf, build);
const before = new Map(container.kids.map((node) => [node.item.id, node]));
syncList(container, [{ id: "d" }, { id: "b" }, { id: "c" }], keyOf, build);
assert.deepStrictEqual(ids(), ["d", "b", "c"]);
assert.strictEqual(container.kids[0], before.get("d"), "重排应复用原节点");
assert.strictEqual(container.kids[1], before.get("b"), "重排应复用原节点");
assert.strictEqual(container.kids[2], before.get("c"), "重排应复用原节点");

// 分片 paint token：40 行首帧还没画完就来新数据，旧帧不得复活已删除的行
const bigContainer = makeContainer();
const big = Array.from({ length: 40 }, (_, i) => ({ id: "i" + i }));
syncList(bigContainer, big, keyOf, build);
assert.strictEqual(frames.length, 1, "40 行首屏应走 rAF 分片");
syncList(bigContainer, [{ id: "only" }], keyOf, build);
runFrames();
assert.deepStrictEqual(
  bigContainer.kids.map((node) => node.item.id),
  ["only"],
  "旧分片帧必须因 token 换代而停止"
);
assert.strictEqual(bigContainer.childElementCount, 1);
console.log("sync-list-ok");
"""


@pytest.mark.skipif(NODE is None, reason="本机无 Node，跳过 JS 控制流运行时验证")
def test_sync_list_key_conflict_delete_and_reorder(tmp_path):
    funcs = "\n".join(
        [
            _extract_function(APP, "function safeJSON(value) {"),
            _extract_function(
                APP, "function syncList(container, items, keyOf, build, options = {}) {"
            ),
        ]
    )
    script = SYNC_HARNESS.replace("__STUBS__", SYNC_STUBS).replace("__FUNCS__", funcs)
    assert "sync-list-ok" in _run_node(tmp_path, script, "sync_list")


# ------------------------------------------- loadForce 撞上在飞请求的排队轮次

LOAD_FORCE_HARNESS = r"""
const assert = require("assert");
let loadInFlight = false;
let loadQueued = false;
let loadQueuedForce = false;
let loadForce = false;
let calls = 0;
const forceAtCall = [];
let release;
const gate = new Promise((resolve) => { release = resolve; });
async function loadAll() {
  calls += 1;
  forceAtCall.push(loadForce);
  try {
    if (calls === 1) await gate;
  } finally {
    loadForce = false;  // 与真实 loadAll 的 finally 一致：在飞轮结束会清掉 force
  }
}
__FUNC__

(async () => {
  const first = load();
  assert.strictEqual(calls, 1, "首次调用应直接进入 loadAll");
  // act()：先置 force，再调 load()，此时撞上在飞请求
  loadForce = true;
  await load();
  assert.strictEqual(loadQueued, true, "并发调用应进队列");
  release();
  await first;
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.strictEqual(calls, 2, "排队轮次应在当前轮结束后补跑一次");
  assert.deepStrictEqual(
    forceAtCall,
    [false, true],
    "排队轮次必须保住强制刷新意图，不能按 TTL 吃缓存"
  );
  assert.strictEqual(loadForce, false, "补跑结束后不得残留 force 标志");
  assert.strictEqual(loadInFlight, false, "结束时 in-flight 标记应复位");
  console.log("load-force-ok");
})().catch((error) => { console.error(error); process.exit(1); });
"""


def test_app_load_force_survives_queued_round():
    body = _extract_function(APP, "async function load() {")
    # 队列轮次要为 force 单独记账：不能只置 loadQueued
    assert "loadQueuedForce = true;" in body
    assert "loadForce = true;" in body.split("finally {")[-1]


@pytest.mark.skipif(NODE is None, reason="本机无 Node，跳过 JS 控制流运行时验证")
def test_app_load_force_survives_queued_round_runtime(tmp_path):
    script = LOAD_FORCE_HARNESS.replace(
        "__FUNC__", _extract_function(APP, "async function load() {")
    )
    assert "load-force-ok" in _run_node(tmp_path, script, "load_force")
