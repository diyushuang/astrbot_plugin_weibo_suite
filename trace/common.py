"""共享 common 辅助工具的兼容性导入。"""

from ..shared import common as _common

for _name in dir(_common):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_common, _name)

del _name
