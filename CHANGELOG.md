# Changelog

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
