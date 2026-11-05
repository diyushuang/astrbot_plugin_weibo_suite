"""面板前端的两个竞态守卫（app.js 轮询串行化、settings.js 保存互斥）。

仓库没有 jsdom / node 测试基建，这里用源码级断言锁住关键分支；运行时行为由
`load()` 的 in-flight 标记 + `loadQueued` 补跑、以及 `saveSettings()` 入口的
`state.saving` 守卫保证（快捷键路径同样经过该入口）。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "views" / "dashboard" / "app.js").read_text(encoding="utf-8")
SETTINGS = (ROOT / "views" / "dashboard" / "settings.js").read_text(encoding="utf-8")


def test_app_load_has_single_flight_guard():
    assert "if (loadInFlight) {" in APP
    assert "loadQueued = true;" in APP
    assert "setTimeout(load, 0);" in APP


def test_settings_save_rejects_concurrent_shortcut():
    assert "if (state.saving) return;" in SETTINGS
    assert "state.saving = true;" in SETTINGS
    assert "state.saving = false;" in SETTINGS
