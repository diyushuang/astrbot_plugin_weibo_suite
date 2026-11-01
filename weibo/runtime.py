"""微博域拆分后各 mixin 共用的导入汇总。

本模块是拆分过程中**唯一**允许的宽命名空间桥：业务模块保持单向依赖，
即 mixin -> runtime -> parser/shared，避免各 mixin 互相 import 成环。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html as html_lib
import ipaddress
import json
import logging
import os
import random
import re
import shutil
import socket
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse, urlunparse

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import AtAll, Image, Video
from astrbot.api.star import Context, Star, StarTools

try:
    from astrbot.core import astrbot_config
except ImportError:
    astrbot_config = None

try:
    from astrbot.core.log import LogManager
except ImportError:
    LogManager = None

from ..shared.common import *  # noqa: F403
from ..shared.common import (
    album_rule_token,
    cooldown_remaining_minutes,
    header_int,
    jsonp_data,
    keyword_rules_upsert,
    merge_response_cookies,
    parse_cookie_string,
    parse_umo,
    redact_proxy,
)
from ..shared.errorlog import (  # noqa: F401
    ERRLOG_BUFFER,
    ERRLOG_MAX_BYTES,
    _ErrorLogStore,
    install_error_log,
    remove_error_log,
)
from ..shared.http import (  # noqa: F401
    ResponseTooLargeError,
    UrlBlockedError,
    guarded_request,
    read_limited_bytes,
    read_limited_text,
    resolve_public_addrs,
    stream_to_file,
)
from ..shared.napcat import *  # noqa: F403
from ..shared.napcat import b64_payload
from .constants import *  # noqa: F403
from .napcat_album import (
    ALBUM_LIST_ITEM_ID_KEYS,
    ALBUM_LIST_ITEM_NAME_KEYS,
    MEDIA_NAME_KEYS,
    NapCatAlbum,
    NapCatError,
    pick,
)
from .parser import *  # noqa: F403

__all__ = [name for name in globals() if not name.startswith("__")]
