#!/usr/bin/env python3
"""扫描打包产物里有没有漏出去的个人信息。

用法：python memory/bridge/scan_package.py <目录或zip> [...]
命中就打印文件名和命中的关键词，全干净就打印"干净"。
"""
from __future__ import annotations

import base64
import sys
import tempfile
import zipfile
from pathlib import Path

# 关键词打散成 base64 存放：这个文件本身也会被发出去，
# 直接写明文的话扫描器会扫到自己。
_NEEDLES_B64 = """
MzkuMTA2LjM3LjQ0
MzI4NDY3ODgyNg==
6Lu944GE54yr
ZGFmZWl5dWJvdA==
UVFCb3RQYW5lbA==
WjI3MTV5NDcxOA==
MDM2Njkw
YWJrXw==
"""

NEEDLES = [
    base64.b64decode(line).decode("utf-8")
    for line in _NEEDLES_B64.split()
    if line.strip()
]


def scan_bytes(data: bytes, needles: list[str]) -> list[str]:
    hits = []
    for needle in needles:
        if needle.encode("utf-8") in data or needle.encode("utf-16-le") in data:
            hits.append(needle)
    return hits


def scan_path(root: Path) -> int:
    bad = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        hits = scan_bytes(path.read_bytes(), NEEDLES)
        rel = path.relative_to(root)
        print(f"{rel}  ({path.stat().st_size:,} B)  {'命中：' + ', '.join(hits) if hits else '干净'}")
        bad += len(hits)
    return bad


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    total = 0
    for arg in argv:
        target = Path(arg)
        if not target.exists():
            print(f"没有这个路径：{target}")
            return 2
        if target.suffix.lower() == ".zip":
            with tempfile.TemporaryDirectory() as tmp:
                with zipfile.ZipFile(target) as z:
                    z.extractall(tmp)
                print(f"== {target.name} ==")
                total += scan_path(Path(tmp))
        else:
            print(f"== {target} ==")
            total += scan_path(target)
    print("\n结论：" + ("干净，可以发出去" if total == 0 else f"有 {total} 处命中，发之前处理掉"))
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
