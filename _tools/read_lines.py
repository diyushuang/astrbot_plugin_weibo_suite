# -*- coding: utf-8 -*-
"""Read a line range of a file. Usage: python -I _tools/read_lines.py <path> <a> <b>"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

path = sys.argv[1]
a = int(sys.argv[2]) if len(sys.argv) > 2 else 1
b = int(sys.argv[3]) if len(sys.argv) > 3 else 0
lines = open(path, encoding="utf-8").read().splitlines()
if b <= 0:
    b = len(lines)
print(f"lines {a}-{b} of {len(lines)}")
for i in range(a - 1, min(b, len(lines))):
    print(f"{i + 1}\t{lines[i]}")
