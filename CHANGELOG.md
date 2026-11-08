# Changelog

## 1.1.0

**人格与挑选传送**

- 记忆列表按 `persona_id` 分组：界面里选人格 → 只看到这个人的记忆，搜索、全选、勾选
- 选择性传送：勾中的记忆（或整个人的记忆）写到指定会话 / 所有活跃会话，`--dry-run` 先演练
- `pull --persona`：只拉某人格的记忆与相关对话（本地 `memories.json` 仍保留全量）
- 人格包：`bundle-export --persona --sanitize` / `bundle-import --file --session`，格式 `memory-bridge.persona` v1
- `--sanitize` 抹掉 QQ 号、IP、邮箱、API key，包可以安全分享
- 会话挑选：界面里选工作区、勾最近会话，只回传选中的

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
