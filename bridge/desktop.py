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

def _rounded(cv, x1: float, y1: float, x2: float, y2: float, r: float,
             fill: str, outline: str = "") -> int:
    """圆角矩形（tkinter 没有原生的，用多边形 + smooth 近似）。"""
    import math

    pts: list[float] = []
    for cx, cy, a0 in ((x2 - r, y1 + r, -90), (x2 - r, y2 - r, 0),
                       (x1 + r, y2 - r, 90), (x1 + r, y1 + r, 180)):
        for i in range(13):
            a = math.radians(a0 + i * 7.5)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return cv.create_polygon(pts, fill=fill, outline=outline or fill, smooth=True)



def first_run_dialog(cfg_path: Path, icon: Path | None) -> bool:
    """第一次运行时的设置窗。返回 True 表示配置已经存好。"""
    import tkinter as tk
    from tkinter import messagebox

    saved: dict = {}
    bg = "#ffffff"
    ink = "#33232d"
    muted = "#a08d97"
    line = "#f0dde6"
    pink = "#ff5f92"
    deep = "#d81b60"
    soft = "#ffffff"
    font = "Microsoft YaHei UI"

    try:  # 高 DPI 屏上别糊
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass

    root = tk.Tk()
    root.title("记忆互通 · 第一次设置")
    root.resizable(False, False)
    root.configure(bg=bg)
    if icon and icon.exists():
        try:
            root.iconbitmap(str(icon))
        except tk.TclError:
            pass

    w, h = 480, 596
    try:
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 40)}")
    except Exception:  # noqa: BLE001
        root.geometry(f"{w}x{h}")

    cv = tk.Canvas(root, width=w, height=h, bg=bg, highlightthickness=0)
    cv.pack(fill="both", expand=True)

    # ── 头部：小鱼 logo + 标题（留白，不画色块）
    cv.create_oval(48, 38, 96, 86, fill="#ffeaf1", outline="")
    cv.create_oval(60, 54, 84, 74, fill=pink, outline="")
    cv.create_polygon(82, 56, 92, 48, 92, 80, 82, 72, fill=pink, outline="")
    cv.create_oval(65, 58, 70, 63, fill="white", outline="")

    cv.create_text(110, 50, text="记忆互通", anchor="w", font=(font, 19, "bold"), fill=ink)
    cv.create_text(111, 76, text="让云端的 AstrBot 记住这台电脑", anchor="w",
                   font=(font, 9), fill=muted)
    cv.create_line(48, 110, w - 48, 110, fill=line)

    entries: dict[str, tk.Entry] = {}
    boxes: dict[str, int] = {}

    def field(y: float, label: str, key: str, hint: str, *, secret: bool = False) -> tk.Entry:
        cv.create_text(50, y, text=label, anchor="w", font=(font, 9), fill="#6f5a64")
        boxes[key] = _rounded(cv, 48, y + 14, w - 48, y + 58, 12, soft, "#f3dee8")
        ent = tk.Entry(root, font=(font, 11), bg=soft, fg=ink, relief="flat",
                       highlightthickness=0, insertbackground=pink,
                       show="●" if secret else "")
        cv.create_window(62, y + 36, window=ent, anchor="w", width=w - 168, height=34)
        ent.bind("<FocusIn>", lambda _e, k=key: cv.itemconfig(boxes[k], outline=pink))
        ent.bind("<FocusOut>", lambda _e, k=key: cv.itemconfig(boxes[k], outline="#f3dee8"))
        cv.create_text(50, y + 70, text=hint, anchor="w", font=(font, 8), fill="#bca7b0")
        entries[key] = ent
        return ent

    field(146, "面板地址", "base", "本机能打开的那个面板地址")
    field(240, "登录账号", "user", "AstrBot 面板的登录名")
    field(334, "登录密码", "password", "只写进本机 config.json，不会上传", secret=True)

    def toggle_password(_event=None) -> None:
        ent = entries["password"]
        showing = bool(ent.cget("show"))
        ent.config(show="" if showing else "●")
        eye.itemconfig(eye_text, text="隐藏" if showing else "显示")

    eye = cv.create_text(w - 70, 370, text="显示", anchor="e", font=(font, 8), fill=deep)
    eye_text = eye
    eye_tag = cv.create_rectangle(w - 96, 358, w - 66, 382, fill="", outline="")
    cv.tag_bind(eye_tag, "<Button-1>", toggle_password)
    cv.tag_bind(eye, "<Button-1>", toggle_password)
    for tag in (eye_tag, eye):
        cv.tag_bind(tag, "<Enter>", lambda _e: cv.config(cursor="hand2"))
        cv.tag_bind(tag, "<Leave>", lambda _e: cv.config(cursor=""))

    # ── 状态
    dot = cv.create_oval(48, 424, 56, 432, fill="#e8cfd9", outline="")
    status = cv.create_text(64, 428, text="填好点下面连一次。", anchor="w",
                            font=(font, 9), fill=muted)

    def set_status(text: str, color: str, dot_color: str) -> None:
        cv.itemconfig(status, text=text[:70], fill=color)
        cv.itemconfig(dot, fill=dot_color)
        root.update_idletasks()

    # ── 按钮（圆角 Label，不用默认按钮的方框）
    def pill(x: float, y: float, width: float, text: str, cmd, *, primary: bool) -> tk.Label:
        fill = pink if primary else "#fff3f8"
        fg = "white" if primary else deep
        box = _rounded(cv, x, y, x + width, y + 42, 14, fill,
                       "" if primary else "#ffd4e3")
        lab = tk.Label(root, text=text, bg=fill, fg=fg,
                       font=(font, 10, "bold" if primary else "normal"),
                       cursor="hand2")
        cv.create_window(x + width / 2, y + 21, window=lab, width=int(width - 8), height=34)
        hover = "#ff4d86" if primary else "#ffe4ee"

        def paint(color: str) -> None:
            cv.itemconfig(box, fill=color)
            lab.config(bg=color)

        lab.bind("<Enter>", lambda _e: paint(hover))
        lab.bind("<Leave>", lambda _e: paint(fill))
        lab.bind("<Button-1>", lambda _e: cmd())
        return lab

    def do_save(verify: bool = True) -> None:
        base = entries["base"].get().strip() or "http://127.0.0.1:6185"
        user = entries["user"].get().strip()
        password = entries["password"].get()
        if not user or not password:
            set_status("账号和密码都得填。", "#c0392b", "#e74c3c")
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

        set_status("正在连…隧道没开的话要等十几秒", muted, "#f3b6c8")
        save_btn.config(text="连接中…")

        def work() -> None:
            try:
                import astrbot_bridge as ab
                st = ab.AstrBot(ab.load_config()).stats()
                total = st.get("total_memories", "?")
                index = st.get("index_state", "?")
                saved["ok"] = True
                root.after(0, lambda: set_status(f"连上了：{total} 条记忆 · 索引 {index}",
                                                 "#1e7a5a", "#2ecc71"))
                root.after(700, root.destroy)
            except Exception as error:  # noqa: BLE001
                msg = f"{type(error).__name__}: {error}"

                def fail() -> None:
                    save_btn.config(text="保存并测试")
                    set_status("连不上，检查地址 / 账号 / 隧道", "#c0392b", "#e74c3c")
                    messagebox.showwarning("连不上", "地址或账号可能不对，也可能隧道没开。\n\n" + msg)

                root.after(0, fail)

        threading.Thread(target=work, daemon=True).start()

    save_btn = pill(48, 452, 262, "保存并测试", lambda: do_save(True), primary=True)
    pill(324, 452, 108, "先跳过", lambda: do_save(False), primary=False)
    cv.create_text(w // 2, 508, text="直接关掉也行，之后在托盘菜单里改",
                   font=(font, 8), fill="#c4b0b9")

    cv.create_line(48, 536, w - 48, 536, fill="#f7ebf0")
    cv.create_text(w // 2, 562, text="凭据只写在本机 config.json，不会上传；这个窗口只出现一次。",
                   font=(font, 8), fill="#c0a9b3")

    root.bind("<Return>", lambda _e: do_save(True))
    root.after(120, lambda: entries["base"].focus_force())
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
