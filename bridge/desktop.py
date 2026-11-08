#!/usr/bin/env python3
"""记忆互通 · 桌面版入口（打包成 exe 用）。

双击之后：
  1. 在 exe 旁边建好 memory/ 目录（界面、笔记、导出、配置都在这）；
  2. 第一次运行弹一个设置窗，填面板地址和账号，顺手测一下通不通；
  3. 后台起本地服务，右下角托盘里出现一条大肥鱼：左键点开界面，右键有菜单
     （一键同步 / 拉取 / 推送 / 实时同步 / 开机自启 / 打开文件夹 / 退出）。

没有控制台，所有输出都写进 exe 旁边的 记忆互通.log。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "记忆互通"


# ------------------------------------------------------------------ 路径

def app_dir() -> Path:
    """exe 所在目录；源码运行时是工作区根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundle_dir() -> Path:
    """打包后的资源目录（onefile 会解到临时目录）。"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def prepare(mem: Path) -> None:
    for sub in ("bridge", "astrbot", "ui"):
        (mem / sub).mkdir(parents=True, exist_ok=True)
    notes = mem / "dsh-notes.jsonl"
    if not notes.exists():
        notes.write_text("", encoding="utf-8")

    ui_dst = mem / "ui" / "index.html"
    if not ui_dst.exists():
        src = bundle_dir() / "memory" / "ui" / "index.html"
        if src.exists():
            ui_dst.write_bytes(src.read_bytes())
        else:
            ui_dst.write_text("<h1>界面文件缺失</h1>", encoding="utf-8")


# ------------------------------------------------------------------ 开机自启

def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
            return True
    except Exception:  # noqa: BLE001
        return False


def set_autostart(on: bool) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  f'"{sys.executable}" --no-browser')
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return False


# ------------------------------------------------------------------ 托盘图标

def tray_image(icon_path: Path):
    from PIL import Image, ImageDraw

    try:
        im = Image.open(icon_path).convert("RGBA")
    except Exception:  # noqa: BLE001
        im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(im)
        draw.ellipse((2, 2, 61, 61), fill=(38, 62, 122, 255))
        draw.ellipse((14, 26, 44, 46), fill=(206, 228, 255, 255))
        draw.polygon([(44, 32), (58, 22), (58, 50)], fill=(206, 228, 255, 255))
        return im

    # 圆的，跟托盘里那些图标站一起不违和
    side = min(im.size)
    im = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                  (im.width + side) // 2, (im.height + side) // 2)).resize((64, 64), Image.LANCZOS)
    mask = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 63, 63), fill=255)
    out = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


# ------------------------------------------------------------------ 主流程

def first_run_dialog(cfg_path: Path, icon: Path | None) -> bool:
    """第一次运行时的设置窗。返回 True 表示配置已经存好。"""
    import tkinter as tk
    from tkinter import messagebox, ttk

    saved: dict = {}
    root = tk.Tk()
    root.title("记忆互通 · 第一次设置")
    root.resizable(False, False)
    root.configure(bg="#fdf1f4")
    if icon and icon.exists():
        try:
            root.iconbitmap(str(icon))
        except tk.TclError:
            pass

    pad = {"padx": 14, "pady": 6}
    tk.Label(root, text="跟云上的 AstrBot 连一下", bg="#fdf1f4",
             font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, columnspan=2, pady=(16, 4))
    tk.Label(root, text="填你本机能打开的那个面板地址。\n在服务器上的话，先把端口映射到本机再来填。",
             bg="#fdf1f4", fg="#8a6a74", font=("Microsoft YaHei UI", 9)).grid(
        row=1, column=0, columnspan=2, pady=(0, 10))

    fields = [("面板地址", "base", ""),
              ("登录账号", "user", ""),
              ("登录密码", "password", "")]
    entries = {}
    for i, (label, key, default) in enumerate(fields, start=2):
        tk.Label(root, text=label, bg="#fdf1f4", font=("Microsoft YaHei UI", 10)).grid(
            row=i, column=0, sticky="e", **pad)
        ent = ttk.Entry(root, width=32, show="*" if key == "password" else "")
        ent.insert(0, default)
        ent.grid(row=i, column=1, sticky="w", **pad)
        entries[key] = ent

    status = tk.Label(root, text="", bg="#fdf1f4", fg="#8a6a74", font=("Microsoft YaHei UI", 9))
    status.grid(row=6, column=0, columnspan=2, pady=(2, 0))

    def do_save(verify: bool = True) -> None:
        base = entries["base"].get().strip() or "http://127.0.0.1:6185"
        user = entries["user"].get().strip()
        password = entries["password"].get()
        if not user or not password:
            status.config(text="账号和密码都得填。", fg="#c0392b")
            return
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg = {
            "_note": "本机凭据，别分享出去。",
            "base": base.rstrip("/"),
            "panel": "",
            "user": user,
            "password": password,
            "pin": "",
            "realtime": {"interval": 20, "debounce": 4, "chats": 120},
        }
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        if not verify:
            saved["ok"] = True
            root.destroy()
            return

        status.config(text="正在测试连接…", fg="#8a6a74")
        root.update_idletasks()

        def work() -> None:
            try:
                import astrbot_bridge as ab
                st = ab.AstrBot(ab.load_config()).stats()
                text = f"连上了：{st.get('total_memories', '?')} 条记忆，索引 {st.get('index_state', '?')}"
                root.after(0, lambda: status.config(text=text, fg="#1e7a5a"))
                saved["ok"] = True
                root.after(500, root.destroy)
            except Exception as error:  # noqa: BLE001
                msg = f"{type(error).__name__}: {error}"

                def fail() -> None:
                    status.config(text="连不上：" + msg[:60], fg="#c0392b")
                    messagebox.showwarning("连不上", "地址或账号可能不对，也可能隧道没开。\n\n" + msg)

                root.after(0, fail)

        threading.Thread(target=work, daemon=True).start()

    btns = tk.Frame(root, bg="#fdf1f4")
    btns.grid(row=5, column=0, columnspan=2, pady=(12, 6))
    ttk.Button(btns, text="保存并测试", command=lambda: do_save(True)).pack(side="left", padx=6)
    ttk.Button(btns, text="跳过测试", command=lambda: do_save(False)).pack(side="left", padx=6)
    ttk.Button(btns, text="退出", command=root.destroy).pack(side="left", padx=6)

    root.mainloop()
    return bool(saved.get("ok"))


def run_tray(port: int, mem: Path, log_path: Path, icon_path: Path) -> int:
    """右下角托盘。服务已经在后台线程里跑着了。"""
    import pystray
    from pystray import MenuItem as Item
    from pystray import Menu

    base = f"http://127.0.0.1:{port}"
    ui_url = f"{base}/"
    state = {"busy": False, "summary": "", "memories": "?"}

    def api(path: str, body: dict | None = None, timeout: float = 10.0) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            base + path, data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data else "GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def notify(text: str, title: str = "记忆互通") -> None:
        try:
            tray.notify(text, title)
        except Exception:  # noqa: BLE001
            pass

    def open_ui(*_a) -> None:
        import webbrowser
        webbrowser.open(ui_url)

    def run_task(task: str, label: str) -> None:
        if state["busy"]:
            notify("还有一个任务在跑，等它完。")
            return
        state["busy"] = True
        try:
            job = api("/api/run", {"task": task})
            jid = job.get("id")
            cursor, info = 0, {}
            deadline = time.time() + 240
            while time.time() < deadline:
                info = api(f"/api/job?id={jid}&from={cursor}")
                cursor = info.get("total", cursor)
                if info.get("done"):
                    break
                time.sleep(0.7)
            ok = info.get("rc") == 0
            lines = [ln for ln in (info.get("lines") or []) if str(ln).strip()]
            tail = lines[-1] if lines else ""
            state["summary"] = f"上次{label}：" + ("成功" if ok else "有问题")
            notify((f"{label}完成" if ok else f"{label}出错") + (f"\n{tail}" if tail else ""))
            refresh(False)
        except Exception as error:  # noqa: BLE001
            traceback.print_exc()
            notify(f"{label}失败：{type(error).__name__}: {error}")
        finally:
            state["busy"] = False
            try:
                tray.update_menu()
            except Exception:  # noqa: BLE001
                pass

    def toggle_realtime(*_a) -> None:
        try:
            cur = api("/api/realtime")
            if cur.get("running"):
                api("/api/realtime", {"action": "stop"})
                notify("实时同步已停止")
            else:
                api("/api/realtime", {"action": "start"})
                notify("实时同步已开启：以后记忆会自己动")
            refresh(False)
        except Exception as error:  # noqa: BLE001
            notify(f"操作失败：{error}")

    def realtime_on() -> bool:
        try:
            return bool(api("/api/realtime").get("running"))
        except Exception:  # noqa: BLE001
            return False

    def toggle_autostart(*_a) -> None:
        want = not autostart_enabled()
        if set_autostart(want):
            notify("开机自启已开启（开机后安静地待在托盘里）" if want else "开机自启已关闭")
        else:
            notify("改注册表失败，可能被安全软件拦了。")
        refresh(False)

    def open_mem(*_a) -> None:
        try:
            os.startfile(str(mem))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    def open_log(*_a) -> None:
        try:
            os.startfile(str(log_path))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    def quit_app(*_a) -> None:
        try:
            api("/api/stop", {}, timeout=3)
        except Exception:  # noqa: BLE001
            pass
        try:
            tray.stop()
        finally:
            os._exit(0)

    def status_line(_item=None) -> str:
        bits = [f"已连 {state['memories']} 条记忆"]
        if state["summary"]:
            bits.append(state["summary"])
        return "·".join(bits)

    menu = Menu(
        Item("打开界面", open_ui, default=True),
        Menu.SEPARATOR,
        Item(status_line, None, enabled=False),
        Menu.SEPARATOR,
        Item("一键同步", lambda *_: run_task("sync", "同步")),
        Item("拉取记忆", lambda *_: run_task("pull", "拉取")),
        Item("推送笔记", lambda *_: run_task("push", "推送")),
        Menu.SEPARATOR,
        Item("实时同步", toggle_realtime, checked=lambda _i: realtime_on()),
        Item("开机自启", toggle_autostart, checked=lambda _i: autostart_enabled()),
        Menu.SEPARATOR,
        Item("打开数据文件夹", open_mem),
        Item("看日志", open_log),
        Menu.SEPARATOR,
        Item("退出", quit_app),
    )

    tray = pystray.Icon("dsh-memory-bridge", tray_image(icon_path),
                        "记忆互通 · 正在连接…", menu)

    def refresh(update_title: bool = True) -> None:
        try:
            st = api("/api/state")
            n = (st.get("counts") or {}).get("memories")
            if n is not None:
                state["memories"] = n
            if update_title:
                tray.title = f"记忆互通 · 已连接 · {state['memories']} 条记忆"
            tray.update_menu()
        except Exception:  # noqa: BLE001
            pass

    def heartbeat() -> None:
        while True:
            time.sleep(60)
            refresh()

    threading.Thread(target=heartbeat, daemon=True).start()
    def first_ping() -> None:
        refresh()
        notify(f"在托盘里待着了 · 已连上 {state['memories']} 条记忆\n左键点开界面，右键看菜单")

    threading.Timer(1.5, first_ping).start()

    print(f"托盘已就绪：{ui_url}")
    tray.run()          # 阻塞在 Windows 的消息循环里
    return 0


def main() -> int:
    root_dir = app_dir()
    mem = root_dir / "memory"
    prepare(mem)

    os.environ.setdefault("MEMORY_BRIDGE_HOME", str(mem))
    # 打包后：配置直接放在 exe 旁边，整个文件夹拷走就能带走账号和设置
    # 源码跑：还是用 memory/bridge/config.json，跟命令行那套保持一致
    if getattr(sys, "frozen", False):
        cfg_path = root_dir / "config.json"
        legacy_cfg = mem / "bridge" / "config.json"
        if not cfg_path.exists() and legacy_cfg.exists():
            try:
                cfg_path.write_bytes(legacy_cfg.read_bytes())
                legacy_cfg.unlink()
            except OSError:
                traceback.print_exc()
    else:
        cfg_path = mem / "bridge" / "config.json"
    os.environ.setdefault("MEMORY_BRIDGE_CONFIG", str(cfg_path))
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    code_dir = bundle_dir() / "memory" / "bridge"
    sys.path.insert(0, str(code_dir if (code_dir / "astrbot_bridge.py").exists() else Path(__file__).resolve().parent))

    # 没有控制台，就把输出丢进日志，出问题还能查
    log_path = root_dir / "记忆互通.log"
    try:
        log = log_path.open("a", encoding="utf-8", buffering=1)
        sys.stdout = log
        sys.stderr = log
    except OSError:
        pass

    import datetime
    print(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} 启动 ({sys.executable}) =====")

    try:
        import astrbot_bridge as ab
        import app as ui_app
    except Exception:
        traceback.print_exc()
        return 1

    cfg_path = Path(os.environ["MEMORY_BRIDGE_CONFIG"])
    cfg = ab.load_config()
    if not cfg.get("user") or not cfg.get("password"):
        icon = bundle_dir() / "icon.ico"
        if not first_run_dialog(cfg_path, icon):
            return 0
        cfg = ab.load_config()

    # 服务放后台线程，主线程留给托盘消息循环
    srv, port = ui_app.build_server(int(os.environ.get("MEM_BRIDGE_PORT", "8765")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"服务已起：{url}")

    args = [a for a in sys.argv[1:] if a in ("--no-browser", "--no-pull")]
    if "--no-pull" not in args:
        threading.Timer(1.5, lambda: ui_app.start_job("pull")).start()
    if "--no-browser" not in args:
        threading.Timer(0.8, lambda: __import__("webbrowser").open(url)).start()

    icon_path = bundle_dir() / "icon.ico"
    try:
        return run_tray(port, mem, log_path, icon_path)
    except Exception:
        # 托盘起不来（极少见）：退化成老样子，开浏览器 + 前台跑
        traceback.print_exc()
        print("托盘起不来，退回前台模式。")
        __import__("webbrowser").open(url)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
