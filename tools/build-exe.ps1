<#
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
