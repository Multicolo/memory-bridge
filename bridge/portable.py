#!/usr/bin/env python3
"""把记忆互通打成一个能直接分享的便携包。

产物：
    dist/记忆互通-<版本>/
        memory/bridge/*.py        记忆桥本体（内嵌配置向导、实时同步）
        memory/ui/index.html      粉色界面
        memory/dsh-notes.jsonl    空笔记文件（DSH 侧写这里）
        启动记忆互通.cmd           打开粉色界面
        实时互通.cmd              常驻实时同步
        配置向导.cmd              第一次用先跑这个
        README.md                 给别人的说明
        icon.ico                  图标（16-256 多尺寸）

打包只挑文件，不复制 config.json / state.json / _remote.py / __pycache__，
所以凭据、会话 id、本机路径都不会跟着走。
"""
from __future__ import annotations

from base64 import b64decode
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # memory/bridge
MEM_ROOT = HERE.parent                          # memory
WORKSPACE = MEM_ROOT.parent                     # 工作区
VERSION = "1.1.0"
NAME = f"记忆互通-{VERSION}"

PY_FILES = ["astrbot_bridge.py", "realtime.py", "activity.py", "app.py", "setup.py"]

LAUNCH_UI = """@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0memory\\bridge"
where py >nul 2>nul && (py -3 app.py & goto :end)
where python >nul 2>nul && (python app.py & goto :end)
echo 没找到 Python。装一个 3.9+ 的版本再来，安装时记得勾选 Add to PATH。
pause
:end
"""

LAUNCH_REALTIME = """@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0memory\\bridge"
echo 实时同步会一直占着这个窗口，关掉窗口就停。
where py >nul 2>nul && (py -3 realtime.py %* & goto :end)
where python >nul 2>nul && (python realtime.py %* & goto :end)
echo 没找到 Python。
pause
:end
"""

LAUNCH_SETUP = """@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0memory\\bridge"
where py >nul 2>nul && (py -3 setup.py %* & goto :end)
where python >nul 2>nul && (python setup.py %* & goto :end)
echo 没找到 Python。
pause
:end
"""

# 打包最后要扫一遍"有没有漏出去的私人信息"。关键词 base64 存着，
# 免得这个文件自己成了泄漏源。
_PERSONAL_B64 = """
MzI4NDY3ODgyNg==
MzkuMTA2LjM3LjQ0
6Lu944GE54yr
QWRtaW5pc3RyYXRvcg==
ZGFmZWl5dWJvdA==
QzpcXFVzZXJzXFxBZG1pbmlzdHJhdG9y
"""

PERSONAL = [
    b64decode(line).decode("utf-8")
    for line in _PERSONAL_B64.split()
    if line.strip()
]


# 界面里的示例文案也要换掉（原文里有私人信息，base64 存着免得这文件自己泄漏）
_UI_OLD_B64 = (
    "cGxhY2Vob2xkZXI9IuS+i+Wmgu+8muWuneeahCBRUSDmmK8gMzI4NDY3ODgyNu+8jOensOWR"
    "vOWlueS4uuOAjOi7veOBhOeMq+OAjTsi"
)


def scrub_ui(text: str) -> str:
    text = text.replace(
        b64decode(_UI_OLD_B64).decode("utf-8"),
        'placeholder="例如：明天六点半要上课，叫她早起;"',
    )
    return text


def make_icon(target: Path, source: Path) -> bool:
    try:
        from PIL import Image
    except ImportError:
        print("跳过图标：当前 Python 没有 Pillow（pip install pillow 后重跑）")
        return False
    im = Image.open(source).convert("RGB")
    w, h = im.size
    side = int(min(w, h) * 0.72)
    cx, cy = int(w * 0.47), int(h * 0.41)
    left = max(0, min(w - side, cx - side // 2))
    top = max(0, min(h - side, cy - side // 2))
    im = im.crop((left, top, left + side, top + side))
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    im.save(target, format="ICO", sizes=sizes)
    print(f"图标：{target.name}（{side}px 源，{len(sizes)} 档）")
    return True


def build(out_root: Path, icon_source: Path | None) -> Path:
    dist = out_root / NAME
    if dist.exists():
        # 上一轮给目录加了只读（为了让资源管理器显示图标），先摘掉再删
        try:
            import subprocess
            subprocess.run(["attrib", "-r", "-h", "-s",
                            str(dist / "*"), "/s", "/d"], check=False,
                           capture_output=True)
            os.chmod(dist, stat.S_IWRITE)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(dist, ignore_errors=True)
        if dist.exists():
            raise SystemExit(f"删不掉旧的 {dist}，先关掉资源管理器里打开着的窗口再试。")
    (dist / "memory" / "bridge").mkdir(parents=True)
    (dist / "memory" / "ui").mkdir(parents=True)
    (dist / "memory" / "astrbot").mkdir(parents=True)

    for name in PY_FILES:
        shutil.copy2(HERE / name, dist / "memory" / "bridge" / name)

    ui_src = MEM_ROOT / "ui" / "index.html"
    ui_text = scrub_ui(ui_src.read_text(encoding="utf-8"))
    (dist / "memory" / "ui" / "index.html").write_text(ui_text, encoding="utf-8")

    (dist / "memory" / "dsh-notes.jsonl").write_text("", encoding="utf-8")

    readme = (HERE / "portable-readme.md").read_text(encoding="utf-8")
    readme = readme.replace("{{VERSION}}", VERSION)
    (dist / "README.md").write_text(readme, encoding="utf-8")

    (dist / "启动记忆互通.cmd").write_text(LAUNCH_UI, encoding="utf-8")
    (dist / "实时互通.cmd").write_text(LAUNCH_REALTIME, encoding="utf-8")
    (dist / "配置向导.cmd").write_text(LAUNCH_SETUP, encoding="utf-8")

    cfg_example = {
        "_note": "复制成 memory/bridge/config.json 再填。也可以直接跑 配置向导.cmd。",
        "base": "http://127.0.0.1:6185",
        "panel": "http://127.0.0.1:8899",
        "user": "",
        "password": "",
        "pin": "",
        "realtime": {"interval": 20, "debounce": 4, "chats": 120},
    }
    (dist / "memory" / "bridge" / "config.example.json").write_text(
        json.dumps(cfg_example, ensure_ascii=False, indent=2), encoding="utf-8")

    if icon_source and icon_source.exists():
        make_icon(dist / "icon.ico", icon_source)
        # 让资源管理器里的文件夹本身就用这张图标
        (dist / "desktop.ini").write_text(
            "[.ShellClassInfo]\nIconResource=icon.ico,0\n", encoding="utf-8")
        try:
            import subprocess
            subprocess.run(["attrib", "+h", "+s", str(dist / "desktop.ini")], check=False)
            subprocess.run(["attrib", "+r", str(dist)], check=False)
        except Exception:
            pass

    # 打包前把「本机面板」的名字换成通用名：别人没有 AstrBotPanel，也不该看到它
    for path in (dist / "memory" / "bridge").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "AstrBotPanel" in text:
            path.write_text(text.replace("AstrBotPanel", "AstrBotPanel"), encoding="utf-8")

    # 最后扫一遍：不该出现的个人信息
    leaked = []
    for path in dist.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".py", ".md", ".html", ".json", ".cmd"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for needle in PERSONAL:
                if needle in text:
                    leaked.append(f"{path.relative_to(dist)} ← {needle}")
    if leaked:
        print("警告：这些文件还带着个人信息：")
        for row in leaked:
            print("  " + row)
    else:
        print("个人信息扫描：干净")

    zip_path = out_root / NAME
    archive = shutil.make_archive(str(zip_path), "zip", root_dir=str(out_root), base_dir=NAME)
    size = Path(archive).stat().st_size / 1024
    print(f"打包完成：{archive}（{size:.0f} KB）")
    return dist


def main(argv: list[str]) -> int:
    out_root = Path(argv[1]) if len(argv) > 1 else WORKSPACE / "dist"
    icon = None
    if len(argv) > 2:
        icon = Path(argv[2])
    else:
        guess = Path.home() / ".dsh"
        if guess.exists():
            for candidate in sorted(guess.glob("attachments/v1/objects/8f/8f78da88*")):
                icon = candidate
                break
    out_root.mkdir(parents=True, exist_ok=True)
    build(out_root, icon)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
