"""微博数据解析、媒体提取与文案纯函数。"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from datetime import datetime
from typing import Any

from .constants import *


class WeiboFetchError(Exception):
    """微博接口请求/解析失败。"""


class WeiboAuthError(WeiboFetchError):
    """游客身份失效，需要重新换取后再试。"""


class WeiboRiskError(WeiboFetchError):
    """触发微博 IP 级风控（HTTP 418/432），需要全局冷却。"""


class MediaTooLarge(WeiboFetchError):
    """媒体响应头声明的体积就超过上限。

    单列一类是为了和"瞬时故障"区分：超限的档重取直链、换轮次都不会变成能下，
    只该换下一档或直接降级，不必白烧一遍带宽。
    """

    def __init__(self, message: str, size_bytes: int = 0):
        super().__init__(message)
        self.size_bytes = size_bytes


_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_ALT_ATTR_RE = re.compile(
    r"""\balt\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""",
    re.IGNORECASE,
)


def _img_alt_text(match: re.Match[str]) -> str:
    """整段 <img ...> 换成它的 alt 文本（三种引号写法都认，没有 alt 就换成空）。"""
    alt = _IMG_ALT_ATTR_RE.search(match.group(0))
    if not alt:
        return ""
    return alt.group(1) or alt.group(2) or alt.group(3) or ""


def _clean_text(raw: str, limit: int) -> str:
    """清洗 m.weibo.cn 返回的 HTML 富文本，截断到 limit。"""
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    # 微博表情等内嵌图片，保留 alt 文本（如 [笑cry]）。alt 的三种写法都要认：
    # 双引号、单引号、无引号——改版后裸值写法出现过，旧正则只认双引号，
    # 结果表情被整段剥掉，正文里出现无意义的空洞。正则用 [^>]*? 且字符类
    # 互斥，不存在灾难性回溯路径。
    text = _IMG_TAG_RE.sub(_img_alt_text, text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if limit > 0 and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _parse_created_epoch(raw: str) -> float:
    """把微博标准时间（Sat Mar 08 16:51:30 +0800 2025）解析为 Unix 时间戳。

    相对时间等无法解析的格式返回 0，由调用方采用宽松策略。
    """
    m = _STD_TIME_RE.match((raw or "").strip())
    if not m or m.group(2) not in _MONTH_MAP:
        return 0.0
    try:
        dt = datetime(
            int(m.group(7)),
            _MONTH_MAP[m.group(2)],
            int(m.group(3)),
            int(m.group(4)),
            int(m.group(5)),
            int(m.group(6)),
            tzinfo=_TZ_CN,
        )
        return dt.timestamp()
    except ValueError:
        return 0.0


def _format_time(raw: str) -> str:
    """把接口时间转成可读字符串；相对时间（如“5分钟前”）原样返回。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = _STD_TIME_RE.match(raw)
    if m and m.group(2) in _MONTH_MAP:
        year, mon, day = m.group(7), _MONTH_MAP[m.group(2)], int(m.group(3))
        ymd = f"{year}-{mon:02d}-{day:02d}"
        if year != str(datetime.now(tz=_TZ_CN).year):
            return f"{ymd} {m.group(4)}:{m.group(5)}"
        return f"{mon:02d}-{day:02d} {m.group(4)}:{m.group(5)}"
    return raw


def _extract_uid(text: str) -> str | None:
    text = (text or "").strip()
    for pat in _UID_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    if text.isdigit() and len(text) >= 4:
        return text
    return None


def to_original(url: str) -> str:
    """把任意 sinaimg 缩略图地址改写成 /large/ 原图地址。"""
    url = (url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    m = SINAIMG_RE.search(url)
    if not m:
        return url
    scheme = url[: m.start()] if url[: m.start()].endswith("://") else "https://"
    token, pid, ext = m.group(3), m.group(4), m.group(5)
    if token in ("large", "original"):
        return url
    return f"{scheme}{m.group(2)}.sinaimg.cn/large/{pid}.{ext}"


def to_light(url: str) -> str:
    """把任意 sinaimg 地址改写成 /mw2000/ 轻量档（长边 2000px），仅供推送消息带图。

    消息里的图是 NapCat 自己去 sina CDN 抓的：/large/ 原图单张 5~30MB，九张串在
    一条消息里很容易顶穿 message_send_timeout，超时又按"发送失败"进重试，结果同一条
    微博在群里重复出现。mw2000 是微博 CDN 原生档位，改一个路径段就有，不需要本地
    下载也不需要压缩。群相册那条仍然用 /large/ 原图，本函数不影响它。
    """
    url = (url or "").strip()
    m = SINAIMG_RE.search(url)
    if not m:
        return url
    return f"https://{m.group(2)}.sinaimg.cn/mw2000/{m.group(4)}.{m.group(5)}"


# ---------------- 微博正文润色（表情翻译 / 话题标签） ----------------

# 微博表情码 -> emoji 映射（内置高频子集）：键是方括号表情码去掉方括号的名字。
# [doge] 这类没有严格对应 emoji 的按社群惯用近似映射；映射表里没有的码原样
# 保留，正文里普通方括号文字（不属于任何已知表情码）不受影响。
WEIBO_EMOJI_MAP: dict[str, str] = {
    "嘻嘻": "😄",
    "哈哈": "😆",
    "笑cry": "🤣",
    "doge": "🐶",
    "二哈": "🐕",
    "太开心": "🥳",
    "微笑": "🙂",
    "可爱": "😊",
    "乖": "😌",
    "偷笑": "🤭",
    "偷乐": "😏",
    "色": "😍",
    "花痴": "😍",
    "害羞": "😳",
    "羞嗒嗒": "😳",
    "得意": "😎",
    "酷": "😎",
    "憧憬": "🤩",
    "惊喜": "🤩",
    "围观": "👀",
    "偷看": "👀",
    "亲亲": "😘",
    "亲吻": "😘",
    "抱抱": "🤗",
    "可怜": "🥺",
    "委屈": "🥹",
    "悲伤": "😢",
    "泪": "😢",
    "大哭": "😭",
    "允悲": "😭",
    "心": "❤️",
    "心碎": "💔",
    "赞": "👍",
    "good": "👍",
    "weak": "👎",
    "给力": "💪",
    "加油": "💪",
    "威武": "💪",
    "拳头": "✊",
    "OK": "👌",
    "ok": "👌",
    "胜利": "✌️",
    "耶": "✌️",
    "鼓掌": "👏",
    "抱拳": "🤝",
    "怒": "😠",
    "怒骂": "🤬",
    "生气": "😤",
    "哼": "😤",
    "汗": "😅",
    "黑线": "😑",
    "无语": "🙄",
    "白眼": "🙄",
    "翻白眼": "🙄",
    "晕": "😵",
    "抓狂": "🤯",
    "崩溃": "😫",
    "裂开": "😖",
    "捂脸": "🤦",
    "打脸": "🤦",
    "睡觉": "😴",
    "困": "🥱",
    "打哈欠": "🥱",
    "生病": "🤒",
    "感冒": "🤧",
    "咳嗽": "😷",
    "口罩": "😷",
    "惊讶": "😮",
    "吃惊": "😲",
    "呆": "😐",
    "疑问": "🤔",
    "思考": "🤔",
    "若有所思": "🤔",
    "并不简单": "🧐",
    "嘘": "🤫",
    "闭嘴": "🤐",
    "馋嘴": "😋",
    "吃瓜": "🍉",
    "柠檬": "🍋",
    "酸": "🍋",
    "咖啡": "☕",
    "蛋糕": "🍰",
    "礼物": "🎁",
    "钟": "⏰",
    "月亮": "🌙",
    "太阳": "☀️",
    "下雨": "🌧️",
    "彩虹": "🌈",
    "烟花": "🎆",
    "钱": "💰",
    "蜡烛": "🕯️",
    "草泥马": "🦙",
    "兔子": "🐰",
    "熊猫": "🐼",
    "猪": "🐷",
    "猫猫": "🐱",
    "喵喵": "🐱",
    "话筒": "🎤",
    "音乐": "🎵",
    "照相机": "📷",
    "手机": "📱",
    "电脑": "💻",
    "灯泡": "💡",
    "浮云": "☁️",
    "鲜花": "💐",
    "微风": "🍃",
    "叶子": "🍃",
    "爱心传递": "💗",
    "好运": "🍀",
    "足球": "⚽",
    "篮球": "🏀",
    "乒乓": "🏓",
    "游泳": "🏊",
    "跑步": "🏃",
    "庆祝": "🎉",
    "hooray": "🎉",
    "no": "🙅",
}

_WEIBO_EMOJI_RE = re.compile(r"\[([^\[\]\n]{1,10})\]")

# 微博话题：两端 # 包住话题名（内不含 # 与换行，可含空格），非贪婪取最短。
_WEIBO_HASHTAG_RE = re.compile(r"#([^#\n]{1,50}?)#")


def translate_weibo_emojis(text: str) -> str:
    """把正文里的微博表情码（[笑cry][doge]）替换成 emoji，认识的才换。"""
    if not text:
        return text

    def _sub(m: re.Match) -> str:
        return WEIBO_EMOJI_MAP.get(m.group(1), m.group(0))

    return _WEIBO_EMOJI_RE.sub(_sub, text)


def extract_hashtags(raw: str, limit: int = 10) -> list[str]:
    """从微博正文提取 #话题#，按出现顺序去重（正文被截断时可能取不全）。"""
    tags: list[str] = []
    for m in _WEIBO_HASHTAG_RE.finditer(raw or ""):
        tag = m.group(1).strip()
        if tag and tag not in tags:
            tags.append(tag)
            if len(tags) >= limit:
                break
    return tags


def _pic_nodes(mb: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容两种结构：m.weibo.cn 的 mblog.pics 与桌面端 ajax 的 pic_infos。"""
    pics = mb.get("pics")
    if isinstance(pics, list) and pics:
        return [p for p in pics if isinstance(p, dict)]
    infos = mb.get("pic_infos")
    if isinstance(infos, dict) and infos:
        out: list[dict[str, Any]] = []
        for pid in mb.get("pic_ids") or list(infos):
            entry = infos.get(pid)
            if isinstance(entry, dict):
                merged = dict(entry)
                merged.setdefault("pid", pid)
                out.append(merged)
        return out
    return []


def _abs_https(url: str) -> str:
    """把接口里的协议相对地址（//host/...）与 http 地址统一成 https。"""
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def _only_live_video(nodes: Any) -> bool:
    """时间线卡片里的视频是否只有"正在直播"这一种。

    直播中的流没有成品直链，详情接口也不会有；但只要卡片里还挂着一个非直播
    的 video 结构（可能是转发原文里的那条），就仍值得补一次详情请求。
    """
    live = False
    for node in nodes or []:
        pi = node.get("page_info") if isinstance(node, dict) else None
        if isinstance(pi, dict) and str(pi.get("type") or "") == "video":
            if str(pi.get("live_status") or "") == "1":
                live = True
            else:
                return False
    return live


def _video_candidates(mi: dict[str, Any], prefer_hd: bool, max_bytes: int | float = 0) -> list[str]:
    """从 media_info 提取有序的 mp4 直链候选。

    优先取新版多码率数组 playback_list 里的 mp4 档（m3u8 是 HLS 切片，
    QQ 视频消息吃不了），hd 按清晰度降序、sd 升序；其后是旧版字段的
    降级链。返回的每个 URL 都可直接下载，签名会过期，拿到就要马上用。

    max_bytes 非零时按 playback_list 自带的体积元数据把候选分桶排序：
    体积适配 cap 的排最前——内嵌发送有体积帽，宁可给用户 720p 的视频也别
    先把 1080p 下到一半才发现超帽白白烧一遍带宽（体积未知的候选仍按原
    顺序跟在后面，已知超帽的垫底；真实体积以下载时的实际拦截为准）。
    """
    cands: list[str] = []
    seen: set[str] = set()
    playlist: list[tuple[int, int, str]] = []
    pl = mi.get("playback_list")
    if isinstance(pl, list):
        for e in pl:
            if not isinstance(e, dict):
                continue
            info = e.get("play_info") or {}
            url = _abs_https(str(info.get("url") or ""))
            if not url or url in seen:
                continue
            mime = str(info.get("mime") or "")
            if url.lower().split("?")[0].endswith(".m3u8") or "mpegurl" in mime:
                continue
            try:
                width = int(info.get("width") or 0)
            except (TypeError, ValueError):
                width = 0
            try:
                size = int(info.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            seen.add(url)
            playlist.append((width, size, url))
    playlist.sort(key=lambda t: (t[0], t[1]), reverse=prefer_hd)
    if 0 < max_bytes < float("inf"):
        fits = [t for t in playlist if t[1] and t[1] <= max_bytes]
        unknown = [t for t in playlist if not t[1]]
        overs = [t for t in playlist if t[1] and t[1] > max_bytes]
        cands.extend(u for _, _, u in fits)
        cands.extend(u for _, _, u in unknown)
    else:
        cands.extend(u for _, _, u in playlist)
    for key in (
        "replay_hd",
        "stream_url_hd",
        "stream_url",
        "mp4_hd_url",
        "mp4_720p_mp4",
        "mp4_sd_url",
        "h5_url",
        "video_clear",
    ):
        url = _abs_https(str(mi.get(key) or ""))
        if url and url not in seen and not url.lower().split("?")[0].endswith(".m3u8"):
            seen.add(url)
            cands.append(url)
    # 旧版结构的档位表挂在 media_info["urls"] 里（mp4_hd_mp4 / mp4_720p_mp4 /
    # mp4_ld_mp4），顶层并没有同名键——只按上面的顶层键降级时这一档一条也取不到
    nested = mi.get("urls")
    if isinstance(nested, dict):
        ladder = ["mp4_hd_mp4", "mp4_720p_mp4", "mp4_sd_url", "mp4_ld_mp4"]
        if not prefer_hd:
            ladder.reverse()
        for key in ladder:
            url = _abs_https(str(nested.get(key) or ""))
            if not url or url in seen:
                continue
            if url.lower().split("?")[0].endswith(".m3u8") or "format=m3u8" in url.lower():
                continue
            seen.add(url)
            cands.append(url)
    if 0 < max_bytes < float("inf"):
        cands.extend(u for _, _, u in overs)
    return cands


def _mix_media_nodes(
    mb: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """解析图文视频混排结构 mix_media_info。

    返回 (图片节点列表——与 _pic_nodes 的条目同构，可直接交给 _extract_pics 的
    逐项转换逻辑, 视频的 media_info 列表)。各接口变体里媒体对象可能挂在
    item.data / item.data.pic / item.page_info 下，逐层探测。
    """
    pic_nodes: list[dict[str, Any]] = []
    medias: list[dict[str, Any]] = []
    mix = mb.get("mix_media_info")
    items = mix.get("items") if isinstance(mix, dict) else None
    if not isinstance(items, list):
        return pic_nodes, medias
    for it in items:
        if not isinstance(it, dict):
            continue
        data = it.get("data")
        node = data if isinstance(data, dict) else it
        mtype = str(it.get("type") or "")
        if mtype == "video":
            mi = node.get("media_info")
            if not isinstance(mi, dict):
                pi = node.get("page_info")
                if isinstance(pi, dict):
                    mi = pi.get("media_info")
            if isinstance(mi, dict):
                medias.append(mi)
        elif mtype == "pic":
            pic = node.get("pic")
            if isinstance(pic, dict):
                pic_nodes.append(pic)
            elif node.get("url") or node.get("pid"):
                pic_nodes.append(node)
    return pic_nodes, medias


def _extract_pics(mb: dict[str, Any], rt_mb: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """从 mblog 提取图片列表（转发微博时原微博的图也算），视频条目一律跳过。

    只有 kind=pic（普通图）与 kind=live（实况图，video_url 为视频段）会进入
    相册上传链路；纯视频条目与"带视频地址但非实况图"的条目（视频封面等）
    都不提取——群相册只收图片，检测到视频就不传。

    每项：{"pid","url"(改写后的 /large/ 原图),"alt_url"(接口原地址，原图 404 时兜底),
    "ext","kind","video_url"}。
    """
    imgs: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in (mb, rt_mb):
        if not node:
            continue
        for p in _pic_nodes(node):
            largest = p.get("largest") or {}
            large = p.get("large") or {}
            base = (
                largest.get("url")
                or large.get("url")
                or (p.get("original") or {}).get("url")
                or p.get("url")
                or ""
            )
            if not base:
                continue
            seg = str(base).split("?")[0].rstrip("/").split("/")
            pid = str(p.get("pid") or "")
            if not pid:
                # 接口缺 pid 时从 URL 提取：sinaimg 路径是 /<尺寸token>/<pid>.<ext>，
                # 文件名主干才是 pid（取 seg[-2] 会拿到 "large" 这类尺寸段，
                # 整批图共享同一个 pid，去重会把多张压成一张、本地文件互相覆盖）
                pid = seg[-1].rsplit(".", 1)[0] if seg and "." in seg[-1] else ""
            if not pid or pid in seen:
                continue
            seen.add(pid)
            ptype = str(p.get("type") or "")
            video = str(p.get("videoSrc") or p.get("video_src") or "")
            if ptype == "video" or (video and ptype != "livephoto"):
                # 群相册仅支持图片：纯视频条目跳过；带视频地址但不是实况图的
                # 条目（接口变体里的视频封面等）也按视频处理，一律不上传
                continue
            ext = (seg[-1].rsplit(".", 1)[-1] if "." in seg[-1] else "jpg").lower()
            kind = "live" if video else "pic"
            orig = to_original(str(base))
            imgs.append(
                {
                    "pid": pid,
                    "url": orig,
                    "alt_url": "" if orig == str(base) else str(base),
                    "ext": ext,
                    "kind": kind,
                    "video_url": video if kind == "live" else "",
                }
            )
    return imgs


def _fill_pics_from_ids(
    detail: dict[str, Any],
    rt: dict[str, Any] | None,
    merged: list[dict[str, str]],
    known: set[str],
) -> int:
    """pic_ids 权威列表兜底：id 有、pics 详情缺的 pid 按图床地址构造条目。

    受限/审核中的帖（yellow_tip「审核中，暂时无法传播」）pics 数组会被
    接口裁剪：真机实测一条帖 pic_ids 给了 11 个 id、pics 数组只带 9 条
    详情（其中还混着 1 条视频封面）。pid 本身就是图床文件名、地址可预测，
    图片在 CDN 上照常可访问（用插件自己的下载头实测，缺失图全部拉回），
    所以缺详情的 pid 直接构造 /large/ 条目（alt 兜底 /mw2000/ 转码档）。

    正常帖 pic_ids 与 pics 详情一一对应，本函数构造 0 条、零影响；hosts
    沿用同帖已识别图片的节点（wx1~wx4 实测任一可用），取不到默认 wx1。
    构造条目 ext 固定 jpg（pid 推不出真实扩展名，微博 CDN 会按请求档位
    转码；GIF 原图此场景拿不到属已知取舍）。返回构造条数。
    """
    host = ""
    for p in merged:
        m = re.search(r"https://(wx\d)\.sinaimg\.cn/", str(p.get("url") or ""))
        if m:
            host = m.group(1)
            break
    built = 0
    for node in (detail, rt if isinstance(rt, dict) else None):
        ids = (node or {}).get("pic_ids")
        if not isinstance(ids, list):
            continue
        for pid in ids:
            pid = str(pid or "").strip()
            if not pid or pid in known:
                continue
            merged.append(
                {
                    "pid": pid,
                    "url": f"https://{host or 'wx1'}.sinaimg.cn/large/{pid}.jpg",
                    "alt_url": f"https://{host or 'wx1'}.sinaimg.cn/mw2000/{pid}.jpg",
                    "ext": "jpg",
                    "kind": "pic",
                    "video_url": "",
                }
            )
            known.add(pid)
            built += 1
    return built


def _pic_stem(pic: dict[str, str]) -> str:
    """本地文件名主干：必须用整串 pid（同一博主多张图的 pid 前缀可能完全相同）。"""
    pid = str(pic.get("pid") or "")
    if pid:
        return BAD_NAME_RE.sub("", pid)[:64] or "pic"
    return hashlib.sha1(str(pic.get("url") or "").encode("utf-8")).hexdigest()[:16]


def _mark(pic: dict[str, str]) -> str:
    """相册里的稳定标识：整串 pid（或 URL 摘要），用来判断"这张已经传过"。"""
    return _pic_stem(pic).lower()


def _parse_album_rules(raw_rules) -> dict[tuple[str, str], str]:
    """album_rules 配置列表 → {(uid, 群号): 相册名或ID}，条目格式 uid:群号:相册。"""
    rules: dict[tuple[str, str], str] = {}
    for item in raw_rules or []:
        parts = str(item).strip().split(":", 2)
        if len(parts) != 3:
            continue
        uid, gid, album = (p.strip() for p in parts)
        if uid.isdigit() and gid.isdigit() and album:
            rules[(uid, gid)] = album
    return rules


def _parse_keyword_rules(raw_rules) -> list[tuple[str | None, str, str, str]]:
    """album_keyword_rules 配置列表 → [(uid或None, 关键词, 群号, 相册名或ID)]，保持配置顺序。

    条目格式：全局「关键词:群号:相册」；博主「uid:关键词:群号:相册」（uid=None 表示
    全局）。相册名可含冒号与空格（尾段原样拼接），关键词不可含冒号。区分依据：
    4 段且第 1、3 段均为纯数字 → 博主规则；否则第 2 段为纯数字 → 全局规则。格式
    非法（含关键词为空）的条目静默跳过，与 _parse_album_rules 的容错口径一致。
    """
    rules: list[tuple[str | None, str, str, str]] = []
    for item in raw_rules or []:
        parts = str(item).strip().split(":")
        uid: str | None = None
        if len(parts) >= 4 and parts[0].isdigit() and parts[2].isdigit():
            uid, kw, gid = parts[0], parts[1].strip(), parts[2]
            album = ":".join(parts[3:]).strip()
        elif len(parts) >= 3 and parts[1].isdigit():
            kw, gid = parts[0].strip(), parts[1]
            album = ":".join(parts[2:]).strip()
        else:
            continue
        if kw and gid.isdigit() and album:
            rules.append((uid, kw, gid, album))
    return rules


def _keyword_rules_to_cfg(rules: list[tuple[str | None, str, str, str]]) -> list[str]:
    """[(uid或None, 关键词, 群号, 相册)] → 配置列表，博主规则带 uid 前缀。"""
    return [
        f"{uid}:{kw}:{gid}:{album}" if uid else f"{kw}:{gid}:{album}"
        for uid, kw, gid, album in rules
    ]


__all__ = [name for name in globals() if not name.startswith("__")]
