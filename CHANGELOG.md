# Changelog

## 1.1.2

**实时同步更安静、更耐活**

- `realtime.py --quiet`：只在记忆/对话条数变化时写一行，不再每 20 秒刷一段日志（连续跑一晚从 190 KB 降到几十 KB）
- 不加 `--quiet` 时行为与旧版完全一致，老用法不受影响
- 桌面版的「实时同步」开关、界面里的守护进程都默认走静默模式
- 后台常驻推荐用独立进程启动，不挂在 DSH 的后台任务上（关掉对话窗口也继续跑）：
  `Start-Process -FilePath python -ArgumentList "realtime.py","--interval","30","--quiet" -WindowStyle Hidden`
- `--stop` 照旧能停（pid 记在 `realtime.json`）

**修的问题**

- 1.1.1 源码包里的 `bridge/realtime.py` 是 1.1.0 之后的旧文件（比 exe 里少几行），这次三处（仓库 / exe / 源码包）用同一份
- 打包顺序改成「先出源码包 → 再用源码包里的 `bridge/*.py` 回灌仓库与 exe」，避免再出现源码包落后于 exe

**升级**

- 单文件版替换 `记忆互通.exe` 即可，`config.json` 与 `memory\` 原地保留；文件夹版整目录替换（`_internal\` 一起换）；源码/便携版解压覆盖，`bridge\config.json` 不会被覆盖


## 1.1.1

**首次设置窗重做**

- 白底简洁版式：手绘小鱼头像 + 粉色主按钮，不铺色块、不放示例 IP；窗口 480×596 居中打开
- 面板地址 / 账号 / 密码 三栏，每栏带灰字提示；密码可点「显示」临时看一眼
- 「保存并测试」当场连一次 AstrBot，成功显示「连上了：N 条记忆 · 索引 ready」，失败给出可读原因
- 连接中按钮变灰、文字变「连接中…」，避免连点；回车等同点「保存并测试」
- 「先跳过」只存配置不测试；「退出」干净地关窗
- 高 DPI 屏不再糊，窗口居中

**修的问题**

- 1.1.0 的 exe 里 `bridge/` 是旧的一份（`transfer` / `bundle-export` 命令缺失，界面点了会报错）；这次 exe 与源码包都从同一份 1.1.1 代码重新打包
- 「退出」按钮与两个主按钮不在一条基线上

## 1.1.0

**人格与挑选传送**

- 记忆列表按 `persona_id` 分组：界面里选人格 → 只看到这个人的记忆，搜索、全选、勾选
- 选择性传送：勾中的记忆（或整个人的记忆）写到指定会话 / 所有活跃会话，`--dry-run` 先演练
- `pull --persona`：只拉某人格的记忆与相关对话（本地 `memories.json` 仍保留全量）
- 人格包：`bundle-export --persona --sanitize` / `bundle-import --file --session`，格式 `memory-bridge.persona` v1
- `--sanitize` 抹掉 QQ 号、IP、邮箱、API key，包可以安全分享
- 会话挑选：界面里选工作区、勾最近会话，只回传选中的

**桌面版 exe（本次新增）**

- `memory-bridge-1.1.0-windows-onefile.zip`：单文件 `记忆互通.exe`（纯 Win64，免安装，解压双击即用）
- `memory-bridge-1.1.0-windows-folder.zip`：文件夹版，启动更快，整个文件夹一起拷走
- exe 带托盘菜单（打开界面 / 一键同步 / 拉取 / 推送 / 实时 / 开机自启 / 看日志）、首启配置窗、粉色界面
- 首次运行在 exe 旁边生成 `config.json` 和 `memory/`，不写注册表（除可选的 Run 自启项）
- 用 PyInstaller 6.22 + Python 3.12 构建，构建路径特意放在无用户名的目录，二进制里不含本机用户名

**修复**

- 导出里没有 `id` 只有 `original_id`，导致 `transfer` 报“没有符合条件的记忆可传”
- 旧版 realtime 进程会用无 id 的导出覆盖 `memories.json`（现已改为全量 + id 映射）
- `state.json` 不再保存 chat API key（移到 `%LOCALAPPDATA%\MemoryBridge`）
- 打包脚本把本机面板名与示例里的 QQ 号替换掉，`scan_package.py` 两个包均“干净”

## 1.0.0

第一个公开版本。

**同步**

- 拉：把 LivingMemory 的记忆 / 最近对话 / 人设导出成 `memory/astrbot/*.md`
- 推：`memory/dsh-notes.jsonl` 里的笔记写进 bot 的长期记忆
- 实时：常驻守护，每 20 秒拉一次，笔记一改就推（防抖 4 秒）
- 回传：挑工作区、勾会话，只把选中的 DSH 会话摘要成记忆

**界面与桌面**

- 粉色本地界面：一键同步 / 拉取 / 推送 / 自检 / 记笔记 / 选会话 / 实时开关
- 托盘程序：右键菜单（打开界面、同步、实时、开机自启、看日志、退出），心情、状态徽标
- 首次运行弹出设置窗，填面板地址、账号、密码并当场测连通

**打包**

- 单文件 exe、免安装文件夹版、便携 Python 版、源码包
- `tools/build-exe.ps1` 一条命令打出两种 exe
- `bridge/scan_package.py` 分享前扫个人信息（IP、QQ 号、账号、密码、本机路径）

**踩过的坑（都修了）**

- Python 3.12 没有 `compression.zstd`，会话同步曾静默空跑 → 改用 `zstandard`
- `zstd.open().read(n)` 一次只返回一帧 → 读文件头改成循环
- PyInstaller 的 `--add-data` 相对路径按 specpath 解析 → 改用绝对路径
- 打包目录为了图标加了只读属性，重打包 `PermissionError` → 先 `attrib -r` 再删
- PowerShell 5.1 把 `.ps1` 按 ANSI 解码 → 脚本一律存 UTF-8 with BOM
