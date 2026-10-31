# -*- coding: utf-8 -*-
"""Dump the key structure of both source config schemas (temporary analysis tool)."""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PATHS = {
    "trace": r"reference/astrbot-image-trace\astrbot_plugin_image_trace\_conf_schema.json",
    "forward": r"reference/weibo-forward\_conf_schema.json",
}

for name, path in PATHS.items():
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    print(f"=== {name}: {len(data)} top keys")
    for k, v in data.items():
        t = v.get("type", "?")
        default = json.dumps(v.get("default"), ensure_ascii=False)[:70]
        print(f"  {k}: {t}  default={default}")
    print()
