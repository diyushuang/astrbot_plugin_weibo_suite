"""图像感知特征提取：pHash / dHash / aHash。

仅依赖 Pillow 与 numpy：DCT 通过预计算的正交矩阵做矩阵乘法实现，无需 scipy。
特征以十六进制字符串表示，方便入库与比对。

- pHash 的位数随 hash_size 变化，其十六进制长度必须统一从
  phash_hex_len() 取，main / library 不得各自推导；
- dHash / aHash 固定 64bit，目前入库留存、检索仅用 pHash，
  预留作为后续二级确认字段。

注意：compute_features 为 CPU 密集操作，调用方应通过 asyncio.to_thread 执行。
"""

from __future__ import annotations

import contextlib
import io
import os
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from PIL import features as pil_features

try:  # iPhone 默认相册格式 HEIC/HEIF 的解码支持；可选依赖，缺失时按不支持处理
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HEIF_AVAILABLE = True
except Exception:  # ImportError 及其它注册失败一律降级
    _HEIF_AVAILABLE = False

# 防解压炸弹：打开后、解码前先按尺寸拒绝；不修改 Pillow 的进程级全局配置。
_MAX_LOAD_PIXELS = 50_000_000

try:  # Pillow >= 9.1
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # 旧版本兜底
    _RESAMPLE = Image.LANCZOS

# 受支持的图片格式 → MIME。image_mime(path) 与 image_bytes_ok(data) 共用同一份
# 真值表：两条校验路径判据必须一致，否则「落盘校验通过、内存校验不通过」这类
# 差异会让回退逻辑在不同入口表现不一。
_MIME_BY_FORMAT = {
    "AVIF": "image/avif",
    "BMP": "image/bmp",
    "GIF": "image/gif",
    "HEIF": "image/heic",
    "JPEG": "image/jpeg",
    "MPO": "image/jpeg",
    "PNG": "image/png",
    "TIFF": "image/tiff",
    "WEBP": "image/webp",
}


def phash_hex_len(hash_size: int) -> int:
    """pHash 十六进制串长度：hash_size² bit 经 packbits 补齐到字节边界后的字符数。

    期望长度在 main（建库）与 library（过滤）两侧使用，必须统一从这里取；
    此前两处各自用 hash_size²//4 推导，与实际长度仅在 hash_size 为 4 的
    倍数时一致，其它取值会导致全库数据被过滤、索引静默清空。
    """
    bits = hash_size * hash_size
    return 2 * ((bits + 7) // 8)


@dataclass
class ImageFeatures:
    phash: str  # pHash 十六进制（hash_size=16 时为 256bit / 64 字符）
    dhash: str  # dHash 十六进制（固定 64bit / 16 字符，预留二级确认）
    ahash: str  # aHash 十六进制（固定 64bit / 16 字符，预留二级确认）
    width: int
    height: int


@cache
def _dct_matrix(n: int) -> np.ndarray:
    """归一化正交 DCT-II 变换矩阵。"""
    k = np.arange(n, dtype=np.float64)
    mat = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n))
    mat[0, :] *= np.sqrt(1.0 / n)
    mat[1:, :] *= np.sqrt(2.0 / n)
    return mat


def _load_rgb_info(path: str, *, require_supported: bool) -> tuple[Image.Image, str | None]:
    """打开图片并完整解码，同时返回其 MIME（可选要求属于受支持格式）。

    把「解码一次」的产物同时用于格式校验与特征计算，调用方不必再为了拿
    MIME 或校验而二次 `Image.open()`。require_supported=True 时，格式不在
    `_MIME_BY_FORMAT` 白名单内直接拒绝（与 image_file_ok 判据一致）。
    """
    with Image.open(path) as src:  # with 退出时释放原文件句柄
        if src.width * src.height > _MAX_LOAD_PIXELS:
            raise ValueError(f"图片像素数 {src.width}x{src.height} 超过上限 {_MAX_LOAD_PIXELS}")
        mime = _supported_mime(src)
        if require_supported and mime is None:
            raise ValueError(f"不受支持的图片格式: {src.format or '未知'}")
        im = src
        if getattr(im, "is_animated", False):  # 动图先取首帧，再处理方向
            im.seek(0)
        with contextlib.suppress(Exception):
            im = ImageOps.exif_transpose(im)  # 总是返回新对象（副本/转正副本）
        if im.mode not in ("RGB", "L"):  # 灰度图不必经 RGB 往返
            im = im.convert("RGB")
        elif im is src:
            im = src.copy()  # 不向外返回依赖文件句柄的原对象
        im.load()  # 像素读入内存，返回后不再依赖任何文件句柄
        return im, mime


def _load_rgb(path: str) -> Image.Image:
    return _load_rgb_info(path, require_supported=False)[0]


def _bits_to_hex(bits: np.ndarray) -> str:
    return np.packbits(bits.astype(np.uint8)).tobytes().hex()


def is_degenerate_phash(phash: str | None) -> bool:
    """判断 pHash 是否为"退化码"（全零）。全项目唯一的退化判定口径。

    纯色、纯渐变、大面积平坦图算不出有意义的 pHash，_phash 会为它们回退
    到全零约定码（见下方 _DEGENERATE_MEDIAN_RATIO）。全零码的问题不在于
    "算得对不对"，而在于**它与另一张纯色图的汉明距离恒为 0**：只要库里有
    一条全零行，任意纯色图都会被判成那张图的"重复"或"100% 相似"。

    因此口径统一为：**退化码不入库、不入索引、也不作为查询**。判定必须走
    这里——main（登记去重、向量 point id）与 library（入库、重建缓存、检索）
    各自比较字符串早晚会漏掉一处。

    - 空值（None / ""）一并视为退化：空哈希没有任何检索意义；
    - 只承认"全部为 0"：近白页、低对比度但仍有纹理的图会得到少量非零位，
      属于正常哈希，必须照常入库与检索（不要按"接近全零"放宽）。
    """
    if not phash:
        return True
    return not str(phash).strip().lower().strip("0")


# 退化判定阈值：中位数绝对值 / 低频块绝对值均值 低于此值即认为落在浮点噪声里。
#
# 纯色、纯渐变、大面积平坦图会让中位数落到浮点噪声量级（1e-15~1e-29），而
# hash_size² 个低频系数里有一半恰好是精确的 0.0。此时 `0.0 > 噪声` 的真假完全
# 由求和顺序决定，同一份算法在不同 BLAS/求和序下都会分叉——数学上不可消除。
# 更关键的是：img-indexer（Node 侧，见其 src/lib/phash.mjs）会为同一张图算 pHash
# 入库，两侧必须给出**同一个**码，否则哈希检索会静默全量落空。
# 处理方式：检出后退化为全零约定码（is_degenerate_phash 判据）。
# 全零码必须**不入库也不参与检索**：它与另一张纯色图的距离恒为 0，一旦入库，
# 任意纯色图都会被判成它的"重复"或"100% 相似"（filter 见 library.py）。
_DEGENERATE_MEDIAN_RATIO = 1e-12


def _phash(gray: Image.Image, hash_size: int) -> str:
    size = hash_size * 4
    pixels = np.asarray(gray.resize((size, size), _RESAMPLE), dtype=np.float64)
    mat = _dct_matrix(size)
    dct = mat @ pixels @ mat.T
    low = dct[:hash_size, :hash_size]
    flat = low.flatten()
    med = float(np.median(low))
    scale = float(np.abs(flat).mean())
    if not scale > 0 or abs(med) <= scale * _DEGENERATE_MEDIAN_RATIO:
        # 退化输入：中位数落在浮点噪声里，二值化无意义 → 全零约定码
        # 该码由 is_degenerate_phash 统一识别，下游据此拒绝入库/检索
        return "0" * phash_hex_len(hash_size)
    return _bits_to_hex((low > med).flatten())


def _dhash(gray: Image.Image) -> str:
    pixels = np.asarray(gray.resize((9, 8), _RESAMPLE), dtype=np.int16)
    return _bits_to_hex((pixels[:, 1:] > pixels[:, :-1]).flatten())


def _ahash(gray: Image.Image) -> str:
    pixels = np.asarray(gray.resize((8, 8), _RESAMPLE), dtype=np.float64)
    return _bits_to_hex((pixels > pixels.mean()).flatten())


def _supported_mime(image) -> str | None:
    """按 Pillow 识别出的格式映射 MIME；不受支持/识别不出返回 None。"""
    return _MIME_BY_FORMAT.get(image.format or "")


def _mime_if_decodable(image) -> str | None:
    """对已打开的图片执行统一判据：像素上限 → 格式白名单 → 完整解码。

    image_mime（路径）与 image_bytes_ok（内存字节）都必须走这一份判据，
    否则「落盘校验通过、内存校验不通过」这类差异会让回退逻辑在不同入口
    表现不一。解压炸弹闸门与完整解码要求也在此一并收口。
    """
    if image.width * image.height > _MAX_LOAD_PIXELS:
        return None
    mime = _supported_mime(image)
    if mime is None:
        return None
    image.load()
    return mime


@lru_cache(maxsize=64)
def _image_mime_cached(path: str, _mtime_ns: int, _size: int) -> str | None:
    try:
        with Image.open(path) as image:
            return _mime_if_decodable(image)
    except Exception:
        return None


def image_mime(path: str) -> str | None:
    """按真实内容识别受支持的图片格式，并完整解码以拒绝截断文件。

    结果按 (路径, mtime_ns, size) 缓存：同一次请求里「解析校验 → 取 MIME →
    登记同步」会对同一张图连续问两三次，每次都完整解码纯属浪费。文件被替换
    时 mtime/size 变化，缓存自然失效；条目上限 64，不会长期占用内存。
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return _image_mime_cached(path, stat.st_mtime_ns, stat.st_size)


def image_bytes_ok(data: bytes) -> bool:
    """按字节校验：可完整解码且格式受支持（与 image_file_ok 同一判据）。

    回退路径的图片本就在内存里，无需先落盘再 `image_file_ok`——落盘只为校验
    等于白白多一次磁盘往返。判据与 image_file_ok 完全一致（同一张格式表、
    同一个解压炸弹闸门、同样要求完整解码），因此两者可互换。
    """
    if not data:
        return False
    try:
        with Image.open(io.BytesIO(data)) as image:
            return _mime_if_decodable(image) is not None
    except Exception:
        return False


def heif_available() -> bool:
    """服务端是否具备 HEIC/HEIF 解码能力（未装 pillow-heif 时为 False）。"""
    return _HEIF_AVAILABLE


def avif_available() -> bool:
    """服务端 Pillow 是否带 AVIF 解码器（11.3+ 才有）。

    扫描目录据此决定要不要把 `.avif` 纳入图片扩展名：旧版 Pillow 能识别
    文件头却解不开，收进来只会让每张 AVIF 都记一条「扫描失败」。
    """
    try:
        return bool(pil_features.check("avif"))
    except Exception:
        return False


def image_file_ok(path: str) -> bool:
    """校验文件是可完整解码且受支持的图片。

    用于把「QQ 图床返回错误体 / 链接过期 / 格式不受支持」这类坏下载拦在
    引擎之前，避免把无效字节送进哈希/向量引擎后报出晦涩的下游错误。
    """
    return image_mime(path) is not None


def _validate_hash_size(hash_size: int) -> int:
    size = int(hash_size)
    if not 4 <= size <= 32:
        raise ValueError("hash_size 仅支持 4~32")
    return size


def _features_from_image(im: Image.Image, hash_size: int) -> ImageFeatures:
    gray = im if im.mode == "L" else im.convert("L")
    return ImageFeatures(
        phash=_phash(gray, hash_size),
        dhash=_dhash(gray),
        ahash=_ahash(gray),
        width=im.width,
        height=im.height,
    )


def compute_features(path: str, hash_size: int = 16) -> ImageFeatures:
    """计算图片的感知特征。

    对压缩、缩放、轻微水印有较强鲁棒性；EXIF 方向会自动转正，动图取首帧。
    """
    size = _validate_hash_size(hash_size)
    return _features_from_image(_load_rgb(path), size)


def compute_features_with_mime(path: str, hash_size: int = 16) -> tuple[ImageFeatures, str]:
    """一次解码同时完成「格式校验 + 感知特征」。

    查询图/登记图的解析与特征计算共用这一次 `Image.open()`：调用方拿到
    MIME 后可直接传给向量引擎，不必再为同一张图重复解码一次。
    """
    size = _validate_hash_size(hash_size)
    im, mime = _load_rgb_info(path, require_supported=True)
    if mime is None:
        raise ValueError("不受支持的图片格式")
    return _features_from_image(im, size), mime


def _thumbnail_jpeg_bytes(im: Image.Image, *, max_side: int, quality: int) -> bytes:
    """把已解码的图像压成 JPEG 缩略图字节。

    - 等比缩放到长边不超过 max_side；原图更小则按原尺寸输出（不放大，
      放大只会让体积变大而信息量不变）；
    - 一律转 RGB：JPEG 无 alpha 通道，PNG 的透明区在缩略图里只剩颜色值；
    - 不携带 EXIF/ICC：缩略图是纯展示用途，元数据既无用又可能带隐私；
    - quality 收敛到 1~95，与图床侧 720px/q72 的现网口径对齐。
    """
    limit = int(max_side)
    if limit > 0 and max(im.size) > limit:
        ratio = limit / float(max(im.size))
        target = (max(1, round(im.width * ratio)), max(1, round(im.height * ratio)))
        thumb = im.resize(target, _RESAMPLE)
    else:
        thumb = im.copy()
    if thumb.mode != "RGB":
        thumb = thumb.convert("RGB")
    for key in ("exif", "icc_profile"):
        thumb.info.pop(key, None)
    buffer = io.BytesIO()
    thumb.save(buffer, format="JPEG", quality=max(1, min(95, int(quality))))
    return buffer.getvalue()


def compute_features_with_thumbnail(
    path: str | Path,
    hash_size: int = 16,
    *,
    max_side: int = 720,
    quality: int = 72,
) -> tuple[ImageFeatures, str, bytes]:
    """一次解码同时完成「格式校验 + 感知特征 + 缩略图」。

    与 compute_features_with_mime 逐字段一致，只多返回一份 720px JPEG 字节：
    归档链路本来要「算特征 + 传缩略图」，各读一次文件意味着同一张图解码两次、
    峰值内存翻倍；这里共用同一次 Image.open()。
    """
    size = _validate_hash_size(hash_size)
    im, mime = _load_rgb_info(path, require_supported=True)
    if mime is None:
        raise ValueError("不受支持的图片格式")
    feats = _features_from_image(im, size)
    thumb = _thumbnail_jpeg_bytes(im, max_side=max_side, quality=quality)
    return feats, mime, thumb
