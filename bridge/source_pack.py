#!/usr/bin/env python3
"""把「记忆互通」打成一个源码包，方便别人读、改、自己编译 exe。

产物：
    dist/记忆互通-<版本>-源码/
        README.md                 结构 / 从源码跑 / 编译 exe / 接口说明
        requirements.txt          运行与打包依赖
        bridge/*.py               本体（记忆桥、界面服务、实时同步、会话摘要、桌面托盘）
        ui/index.html             粉色界面
        config.example.json       配置模板（复制成 bridge/config.json）
        启动记忆互通.cmd           直接跑源码打开界面
        实时互通.cmd              常驻实时同步
        配置向导.cmd              配置 + 连通性检查
        tools/build-exe.ps1       一键打两个版本（单文件 + 免安装）
        tools/scan_package.py     分享前扫个人信息
        icon.ico                  图标

和便携包的区别：便携包是"给不想装 Python 的人"，这里是"给想看代码的人"，
所以带 PyInstaller 构建脚本、依赖清单和接口文档，不带 exe。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # memory/bridge
MEM_ROOT = HERE.parent                          # memory
WORKSPACE = MEM_ROOT.parent                     # 工作区
VERSION = "1.0.0"
NAME = f"记忆互通-{VERSION}-源码"

PY_FILES = [
    "astrbot_bridge.py",   # 面板登录、记忆 import/export、笔记推送、会话摘要入口
    "activity.py",         # DSH 会话（zstd jsonl）→ 人话摘要，含工作区/会话列举
    "realtime.py",         # 常驻实时同步（拉 + 监听笔记变化推）
    "app.py",             # 本地 Web 服务 + 粉色界面 API
    "setup.py",            # 命令行配置向导
    "desktop.py",          # 桌面版入口：托盘、首启设置窗、打包成 exe 的 main
    "portable.py",         # 打便携包（给不装 Python 的人）
    "source_pack.py",      # 打源码包（就是本文件）
    "scan_package.py",     # 分享前扫个人信息
]

LAUNCH_UI = """@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0bridge"
where py >nul 2>nul && (py -3 app.py %* & goto :end)
where python >nul 2>nul && (python app.py %* & goto :end)
echo 没找到 Python。装一个 3.10+ 的版本再来，安装时记得勾选 Add to PATH。
pause
:end
"""

LAUNCH_DESKTOP = """@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0bridge"
where py >nul 2>nul && (py -3 desktop.py %* & goto :end)
where python >nul 2>nul && (python desktop.py %* & goto :end)
echo 没找到 Python。
pause
:end
"""

LAUNCH_REALTIME = """@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0bridge"
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
cd /d "%~dp0bridge"
where py >nul 2>nul && (py -3 setup.py %* & goto :end)
where python >nul 2>nul && (python setup.py %* & goto :end)
echo 没找到 Python。
pause
:end
"""

REQUIREMENTS = """\
# 1) 必需：Python 3.14 以下没有 compression.zstd，DSH 会话是 zstd 压缩的
zstandard>=0.22

# 2) 桌面版（托盘 + 图标）才需要；只跑 app.py / 实时同步可以不装
pystray>=0.19
pillow>=10

# 3) 只有自己编译 exe 才需要
# pyinstaller>=6
"""

BUILD_PS1 = r"""<#
    打两个版本：
        单文件      dist\app\记忆互通.exe
        免安装文件夹 dist\app-dir\记忆互通\记忆互通.exe
    用法：
        powershell -ExecutionPolicy Bypass -File tools\build-exe.ps1
        powershell -ExecutionPolicy Bypass -File tools\build-exe.ps1 -OneFileOnly
#>
param(
    [string]$Python = "",
    [string]$OutDir = "",
    [switch]$OneFileOnly,
    [switch]$DirOnly
)
# 注意：这里不能用 Stop —— PyInstaller 会把进度日志写到 stderr，
# PowerShell 5.1 会把它当成错误记录直接抛异常。所有失败点都自己查 $LASTEXITCODE。
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $OutDir) { $OutDir = Join-Path $root "dist" }

if (-not $Python) {
    # PATH 上的 python 可能是还没装 PyInstaller 的版本（比如 3.15），
    # 所以按"谁能 import PyInstaller"来挑，而不是抓第一个。
    $cands = @()
    if ($env:PYTHON) { $cands += $env:PYTHON }
    foreach ($name in @("py", "python")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $cands += $cmd.Source }
    }
    $cands += (Get-ChildItem (Join-Path $env:LOCALAPPDATA "Programs\Python\Python3*\python.exe") `
        -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    # DeepSeek Harness 自带的 Python（在这个工作区里编译时通常已经装好了 PyInstaller）
    if ($env:DSH_HOME) {
        $cands += (Join-Path $env:DSH_HOME "dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe")
    }
    foreach ($c in ($cands | Select-Object -Unique)) {
        & $c -c "import PyInstaller" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $Python = $c; break }
    }
    if (-not $Python) {
        $first = ($cands | Select-Object -Unique | Select-Object -First 1)
        $hint = "pip install pyinstaller zstandard pystray"
        if ($first) { $hint = "& `"$first`" -m pip install pyinstaller zstandard pystray" }
        throw "没找到装了 PyInstaller 的 Python。先运行：$hint；装好后重跑，或用 -Python 指定解释器路径。"
    }
    Write-Host "用这个解释器：$Python"
}

$ui = Join-Path $root "ui\index.html"
$ico = Join-Path $root "icon.ico"
$entry = Join-Path $root "bridge\desktop.py"
$common = @(
    "--noconfirm", "--windowed", "--name", "记忆互通",
    "--add-data", "$ui;memory/ui",
    "--hidden-import", "pystray._win32",
    "--hidden-import", "zstandard",
    "--collect-submodules", "zstandard"
)
if (Test-Path $ico) { $common += @("--icon", $ico) }

Push-Location $root
try {
    if (-not $DirOnly) {
        & $Python -m PyInstaller @common --onefile `
            --distpath (Join-Path $OutDir "app") `
            --workpath (Join-Path $OutDir "..\build\pyi") `
            --specpath (Join-Path $OutDir "..\build") $entry
        if ($LASTEXITCODE -ne 0) { throw "单文件构建失败" }
        Write-Host "单文件：$(Join-Path $OutDir 'app\记忆互通.exe')"
    }
    if (-not $OneFileOnly) {
        & $Python -m PyInstaller @common --onedir `
            --distpath (Join-Path $OutDir "app-dir") `
            --workpath (Join-Path $OutDir "..\build\pyi-dir") `
            --specpath (Join-Path $OutDir "..\build\dir") $entry
        if ($LASTEXITCODE -ne 0) { throw "免安装版构建失败" }
        Write-Host "免安装：$(Join-Path $OutDir 'app-dir\记忆互通\记忆互通.exe')"
    }
}
finally {
    Pop-Location
}
Write-Host "构建完成。第一次运行会在 exe 旁边生成 config.json 和 memory\ 目录。"
"""

README = r"""
# 记忆互通 · 源码

AstrBot 的长期记忆 ⇄ 这台电脑上 DSH 的工作区，双向同步。
源码全在这里，改完直接跑；也可以用附带的脚本自己编译成 exe。

## 两分钟跑起来

```bat
配置向导.cmd                 :: 填面板地址 / 账号 / 密码，顺手测连通
启动记忆互通.cmd              :: 起服务并打开粉色界面
```

只想用命令行：

```bat
cd bridge
python setup.py --check      :: 看当前配置能不能连上
python astrbot_bridge.py pull
python astrbot_bridge.py note "一句话事实" --tags 项目,偏好
python astrbot_bridge.py push
python astrbot_bridge.py status
```

## 目录

| 路径 | 干什么 |
| --- | --- |
| `bridge/astrbot_bridge.py` | 面板登录、记忆 import/export、笔记推送、配置加载；命令行入口 |
| `bridge/activity.py` | DSH 会话（zstd 压缩的 jsonl）→ 人话摘要；列工作区 / 列会话 |
| `bridge/app.py` | 本地 HTTP 服务 + 界面 API（默认从 8765 起找空端口） |
| `bridge/realtime.py` | 常驻同步：定时拉，笔记一变就推 |
| `bridge/desktop.py` | 桌面版入口：托盘图标、首启设置窗、`main()` |
| `bridge/setup.py` | 命令行配置向导 + 连通性检查 |
| `bridge/portable.py` | 打便携包（给不想装 Python 的人） |
| `bridge/source_pack.py` | 打源码包（本文件所在目录的上一级） |
| `bridge/scan_package.py` | 分享前扫一遍包里有没有个人信息 |
| `ui/index.html` | 粉色界面（单文件，无外部依赖） |
| `tools/build-exe.ps1` | 打两个版本：单文件 / 免安装文件夹 |

## 依赖

```
pip install -r requirements.txt
```

- **zstandard** 是必需的：Python 3.14 以下没有 `compression.zstd`，而 DSH 的会话文件是 zstd 压缩的。
  缺了它，`activity.py` 会安静地读出空结果。
- **pystray / pillow** 只有桌面版（`desktop.py`）要，托盘图标用。
- **pyinstaller** 只有自己编译 exe 要。

## 编译成 exe

```powershell
pip install pyinstaller
powershell -ExecutionPolicy Bypass -File tools\build-exe.ps1
```

产物：

- `dist\app\记忆互通.exe` —— 单文件，拷哪都能双击
- `dist\app-dir\记忆互通\记忆互通.exe` —— 免安装文件夹版，启动快一些

只想要其中一个：`-OneFileOnly` 或 `-DirOnly`；换解释器：`-Python <路径>`。

> 打出来的 exe 第一次运行会在 **exe 旁边**生成 `config.json` 和 `memory/` 目录；
> 分享时把 `config.json` 删掉，对方打开会重新问。

## 配置和数据放哪

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `MEMORY_BRIDGE_HOME` | 源码模式下是 `memory/`，打包后是 exe 旁边的 `memory/` | 数据根目录 |
| `MEMORY_BRIDGE_CONFIG` | `<根>\bridge\config.json` | 配置文件 |
| `MEMORY_BRIDGE_STATE` | `<根>\bridge\state.json` | 已推送内容的指纹 |
| `QQBOT_PANEL_CONFIG` | 空 | 想直接复用 QQBot 面板的 config.json 时指过去 |

配置文件长这样（`config.example.json` 是空模板）：

```json
{
  "base": "http://127.0.0.1:6185",
  "panel": "http://127.0.0.1:8899",
  "user": "面板账号",
  "password": "面板密码",
  "pin": "面板 PIN",
  "realtime": { "interval": 20, "debounce": 4, "chats": 120 }
}
```

## 界面接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/state` | 概览：连接状态、记忆条数、笔记条数、实时同步状态 |
| GET | `/api/sessions?workspace=` | 工作区列表 + 该工作区的会话（只读文件头拿标题） |
| POST | `/api/activity` | `{"sessions":[...], "dry":true}` 只推勾中的会话 |
| GET/POST | `/api/realtime` | 实时同步 `status` / `start` / `stop` |
| POST | `/api/run` | `{"task":"pull"\\|"push"\\|"sync"\\|"activity"\\|"selftest"\\|"status"}` |
| GET | `/api/job?id=&from=` | 轮询任务输出 |
| POST | `/api/note` | 记一条笔记，`push:true` 顺手推云端 |
| POST | `/api/stop` | 关掉后台服务 |

## 分享前

```bat
python bridge\\scan_package.py <zip 或目录>
```

扫服务器地址、QQ 号、账号、本机路径；输出「干净，可以发出去」再发。

## 许可

自己用和改都随意。依赖的 AstrBot / LivingMemory 归各自作者。
"""


def build(out_root: Path) -> Path:
    dist = out_root / NAME
    if dist.exists():
        shutil.rmtree(dist, ignore_errors=True)
    (dist / "bridge").mkdir(parents=True)
    (dist / "ui").mkdir(parents=True)
    (dist / "tools").mkdir(parents=True)

    for name in PY_FILES:
        src = HERE / name
        if not src.exists():
            print(f"跳过（不存在）：{name}")
            continue
        shutil.copy2(src, dist / "bridge" / name)

    shutil.copy2(MEM_ROOT / "ui" / "index.html", dist / "ui" / "index.html")
    shutil.copy2(HERE / "scan_package.py", dist / "tools" / "scan_package.py")

    cfg_example = {
        "_note": "复制成 bridge/config.json 再填；也可以直接跑 配置向导.cmd。",
        "base": "http://127.0.0.1:6185",
        "panel": "http://127.0.0.1:8899",
        "user": "",
        "password": "",
        "pin": "",
        "realtime": {"interval": 20, "debounce": 4, "chats": 120},
    }
    (dist / "config.example.json").write_text(
        json.dumps(cfg_example, ensure_ascii=False, indent=2), encoding="utf-8")

    (dist / "README.md").write_text(README, encoding="utf-8")
    (dist / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    # .ps1 必须带 BOM，否则 Windows PowerShell 5.1 按 ANSI 解码，中文注释会把脚本解析炸掉
    (dist / "tools" / "build-exe.ps1").write_text(BUILD_PS1, encoding="utf-8-sig")

    (dist / "启动记忆互通.cmd").write_text(LAUNCH_UI, encoding="utf-8")
    (dist / "桌面版.cmd").write_text(LAUNCH_DESKTOP, encoding="utf-8")
    (dist / "实时互通.cmd").write_text(LAUNCH_REALTIME, encoding="utf-8")
    (dist / "配置向导.cmd").write_text(LAUNCH_SETUP, encoding="utf-8")

    icon = WORKSPACE / "dist" / "icon.ico"
    if icon.exists():
        shutil.copy2(icon, dist / "icon.ico")
    else:
        print("跳过图标：dist\\icon.ico 不存在（先跑 portable.py 生成，或自己放一个）")

    zip_path = shutil.make_archive(str(out_root / NAME), "zip",
                                   root_dir=str(out_root), base_dir=NAME)
    size = Path(zip_path).stat().st_size / 1024
    print(f"源码包：{zip_path}（{size:.0f} KB）")
    return dist


def main(argv: list[str]) -> int:
    out_root = Path(argv[1]) if len(argv) > 1 else WORKSPACE / "dist"
    out_root.mkdir(parents=True, exist_ok=True)
    build(out_root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
