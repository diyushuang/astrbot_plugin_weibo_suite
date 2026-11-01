"""parser._clean_text 对微博富文本的健壮性回归（P3-11）。"""

from __future__ import annotations

from astrbot_plugin_weibo_suite.weibo import parser


def test_img_alt_double_quoted():
    raw = '<p>a<img src="x.gif" alt="[笑cry]">b</p>'
    assert parser._clean_text(raw, 100) == "a[笑cry]b"


def test_img_alt_single_quoted():
    raw = "<img alt='[允悲]' src='x.gif'>"
    assert parser._clean_text(raw, 100) == "[允悲]"


def test_img_alt_bare_value():
    # 改版后出现过不写引号的 alt：旧正则只认双引号，整段表情被剥掉
    raw = "前<img alt=[doge] src=x.gif>后"
    assert parser._clean_text(raw, 100) == "前[doge]后"


def test_img_without_alt_is_dropped():
    raw = 'a<img src="x.gif">b'
    assert parser._clean_text(raw, 100) == "ab"


def test_img_alt_ignores_attr_order():
    raw = '<IMG SRC="x.gif" ALT="[ok]">'
    assert parser._clean_text(raw, 100) == "[ok]"


def test_img_without_alt_keeps_neighbouring_text_linear():
    # 无 alt 的 img 一多，旧写法 '' 的贪婪匹配在长文本上仍然成立；
    # 这里只锁"不吞正文"的行为，不测耗时（CI 机器抖动太大）
    raw = ("图" + '<img src="a.gif">') * 200
    assert parser._clean_text(raw, 0) == "图" * 200
