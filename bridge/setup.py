#!/usr/bin/env python3
"""记忆互通 · 配置向导。

第一次用的时候跑一次，把连接信息存进同目录的 config.json。之后所有命令
（status / pull / push / realtime / 粉色界面）都会自己读它。

用法：
    python setup.py                     # 交互式填
    python setup.py --from-panel <文件> # 从本机面板的 config.json 导入
    python setup.py --check             # 只检查现有配置能不能连上

除 config.json 之外不会写任何地方；不想留凭据就只填地址，把账号密码交给
环境变量 ASTRBOT_USER / ASTRBOT_PASS。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = HERE / "config.json"


def ask(prompt: str, default: str = "") -> str:
    tip = f"{prompt} [{default}]: " if default else f"{prompt}: "
    try:
        value = input(tip).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return value or default


def load() -> dict:
    if CFG.exists():
        try:
            return json.loads(CFG.read_text(encoding="utf-8"))
        except ValueError:
            print("config.json 不是合法 JSON，会重建一个。")
    return {}


def save(cfg: dict) -> None:
    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {CFG}")


def from_panel(path: Path, cfg: dict) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    astr = raw.get("astrbot") or {}
    ports = raw.get("ports") or {}
    cfg.setdefault("base", f"http://127.0.0.1:{ports.get('astrbot', 6185)}")
    cfg.setdefault("panel", f"http://127.0.0.1:{raw.get('panelPort', 8899)}")
    cfg["user"] = astr.get("user", "") or cfg.get("user", "")
    cfg["password"] = astr.get("pass", "") or cfg.get("password", "")
    cfg["pin"] = str(raw.get("accessPin") or cfg.get("pin", ""))
    print(f"从 {path} 读到了账号 {cfg['user'] or '（空）'}")
    return cfg


def interactive(cfg: dict) -> dict:
    print("记忆互通 · 配置向导")
    print("填错也没关系，重新跑一次就会覆盖。\n")
    cfg["base"] = ask("AstrBot 面板地址", cfg.get("base") or "http://127.0.0.1:6185")
    cfg["user"] = ask("AstrBot 登录账号", cfg.get("user", ""))
    cfg["password"] = ask("AstrBot 登录密码", cfg.get("password", ""))
    print("\n下面两项只有「让面板代为重启隧道」才用得上，不知道就直接回车跳过。")
    cfg["panel"] = ask("本机面板地址", cfg.get("panel") or "http://127.0.0.1:8899")
    cfg["pin"] = ask("Panel 访问口令（PIN）", cfg.get("pin", ""))
    print("\n实时同步：默认每 20 秒拉一次云端记忆，笔记一有变化就推送。")
    every = ask("拉取间隔（秒，5-600）", str((cfg.get("realtime") or {}).get("interval", 20)))
    try:
        interval = max(5, min(600, int(every)))
    except ValueError:
        interval = 20
    cfg["realtime"] = {"interval": interval, "debounce": 4, "chats": 120}
    return cfg


def check() -> int:
    if not CFG.exists():
        print("还没有 config.json。先跑一次 python setup.py。")
        return 1
    sys.path.insert(0, str(HERE))
    import astrbot_bridge as bridge

    cfg = bridge.load_config()
    print(f"地址：{cfg['base']}")
    print(f"账号：{cfg['user'] or '（空）'}")
    if not cfg["user"] or not cfg["password"]:
        print("缺账号或密码：要么跑向导填上，要么设 ASTRBOT_USER / ASTRBOT_PASS。")
        return 1
    try:
        bot = bridge.AstrBot(cfg)
        stats = bot.stats()
        print("连接成功。")
        print(f"  记忆 {stats.get('total_memories', '?')} 条 · "
              f"索引 {stats.get('index_state', '?')}")
        return 0
    except Exception as error:
        print(f"连不上：{type(error).__name__}: {error}")
        print("检查：1) 面板地址和端口对不对；2) 账号密码；3) 如果 AstrBot 在服务器上，"
              "本机到 6185 的隧道通不通。")
        return 2


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="setup", description="记忆互通配置向导")
    ap.add_argument("--from-panel", default="", help="从本机面板的 config.json 导入连接信息")
    ap.add_argument("--check", action="store_true", help="只检查现有配置")
    args = ap.parse_args(argv)

    if args.check:
        return check()

    cfg = load()
    if args.from_panel:
        cfg = from_panel(Path(args.from_panel), cfg)
        cfg.setdefault("realtime", {"interval": 20, "debounce": 4, "chats": 120})
        save(cfg)
        return check()

    cfg = interactive(cfg)
    save(cfg)
    print()
    return check()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
