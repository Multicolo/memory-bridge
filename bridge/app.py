#!/usr/bin/env python3
"""记忆桥本地界面：一个只有标准库的小 Web 服务。

双击「一键记忆互通.cmd」就会启动它并打开浏览器。
- GET  /            粉色主题界面
- GET  /api/state   状态、笔记、DSH 行为预览
- POST /api/run     启动任务：sync/pull/push/activity/selftest/status
- GET  /api/job     读取任务输出（游标分页）
- POST /api/note    记一条笔记（可顺带推送）
- POST /api/stop    关闭服务
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import astrbot_bridge as ab

HERE = Path(__file__).resolve().parent
UI_FILE = ab.MEM_ROOT / "ui" / "index.html"
HOST = "127.0.0.1"

_jobs: dict[str, dict] = {}
_job_seq = 0
_job_lock = threading.Lock()
_bot = None
_bot_lock = threading.Lock()
_stats_cache: dict = {"ts": 0.0, "data": None}


def get_bot() -> "ab.AstrBot":
    global _bot
    with _bot_lock:
        if _bot is None:
            _bot = ab.AstrBot(ab.load_config())
        return _bot


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ------------------------------------------------------------------ 状态

def _online() -> bool:
    url = ab.load_config().get("base", "")
    try:
        parsed = urllib.parse.urlparse(url)
        host, port = parsed.hostname or "127.0.0.1", parsed.port or 6185
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


_stats_busy = False


def _stats() -> dict:
    """缓存 60 秒；过期时先返回旧值，后台再刷新，绝不阻塞界面。"""
    global _stats_busy
    fresh = _stats_cache["data"] is not None and time.time() - _stats_cache["ts"] < 60
    if fresh:
        return _stats_cache["data"]
    if not _stats_busy:
        _stats_busy = True

        def worker():
            global _stats_busy
            try:
                data = get_bot().stats()
                _stats_cache.update(ts=time.time(), data=data)
            except Exception as e:  # noqa: BLE001
                _stats_cache["error"] = str(e)
            finally:
                _stats_busy = False

        threading.Thread(target=worker, daemon=True).start()
    return _stats_cache["data"] or {}


_acts_cache: dict = {"ts": 0.0, "rows": [], "error": None}
_acts_busy = False


def _activity_rows() -> list:
    """DSH 会话解析可能很慢（大文件），放后台算，界面先拿旧结果。"""
    global _acts_busy
    if _acts_cache["rows"] and time.time() - _acts_cache["ts"] < 30:
        return _acts_cache["rows"]
    if not _acts_busy:
        _acts_busy = True

        def worker():
            global _acts_busy
            try:
                import activity as act
                rows = act.preview(ab.WORKSPACE, days=30, limit=8)
                _acts_cache.update(ts=time.time(), rows=rows, error=None)
            except Exception as e:  # noqa: BLE001
                _acts_cache.update(ts=time.time(), error=str(e))
            finally:
                _acts_busy = False

        threading.Thread(target=worker, daemon=True).start()
    return _acts_cache["rows"]


def state() -> dict:
    st = ab.load_state()
    meta_path = ab.EXPORT_DIR / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            meta = {}
    try:
        notes = ab._load_notes(ab.DEFAULT_NOTE_FILE)
    except Exception:  # noqa: BLE001
        notes = []
    rows = _activity_rows()
    done = st.get("activity") or {}
    for r in rows:
        r["changed"] = (done.get(r["id"]) or {}).get("hash") != r["hash"]

    stats = _stats()
    return {
        "ok": True,
        "online": _online(),
        "base": ab.load_config().get("base"),
        "counts": {
            "memories": stats.get("total_memories", meta.get("memory_count", 0)),
            "messages": meta.get("message_count", 0),
            "personas": meta.get("persona_count") or len(_personas()),
            "notes": len(notes),
        },
        "times": {
            "last_pull": st.get("last_pull"),
            "last_push": st.get("last_push"),
            "last_activity": st.get("last_activity"),
        },
        "notes": [{"ts": n["ts"], "text": n["text"], "tags": n["tags"]} for n in notes[-6:]][::-1],
        "activity": rows,
        "activity_error": _acts_cache.get("error"),
        "workspace": str(ab.WORKSPACE),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _personas() -> list:
    path = ab.EXPORT_DIR / "personas.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []


# ------------------------------------------------------------------ 任务

class _Sink(io.TextIOBase):
    def __init__(self, job: dict):
        self.job = job
        self.buf = ""

    def write(self, s: str) -> int:  # noqa: D401
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.job["lines"].append(line)
        return len(s)

    def flush(self) -> None:
        if self.buf:
            self.job["lines"].append(self.buf)
            self.buf = ""


TASKS = {
    "sync": "一键同步（拉记忆 + 推笔记）",
    "pull": "拉取 AstrBot 记忆与对话",
    "push": "推送 DSH 笔记",
    "activity": "同步 DSH 会话（只推选中的）",
    "selftest": "端到端自检",
    "status": "查看状态",
}


def _run_task(name: str, job: dict) -> None:
    sink = _Sink(job)
    rc = 0
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            print(f"[{_now()}] {TASKS.get(name, name)} …")
            bot = get_bot()
            if name == "pull":
                rc = ab.cmd_pull(bot, 150)
            elif name == "push":
                rc = ab.cmd_push(bot, ab.DEFAULT_NOTE_FILE, False, 200)
            elif name == "activity":
                rc = ab.cmd_activity(bot, bool(job.get("dry")), 30, 8,
                                     sessions=job.get("sessions") or None)
            elif name == "selftest":
                rc = ab.cmd_selftest(bot, "")
            elif name == "status":
                rc = ab.cmd_status(bot)
            else:
                rc = ab.cmd_pull(bot, 150)
                rc = ab.cmd_push(bot, ab.DEFAULT_NOTE_FILE, False, 200) or rc
            print(f"[{_now()}] 完成（{'成功' if rc == 0 else '有错误，退出码 %s' % rc}）")
            _stats_cache["ts"] = 0.0
    except ab.ApiError as e:
        print(f"[{_now()}] 连接 AstrBot 失败：{e}")
        print("  检查：1) 面板隧道 8899 是否在线；2) 云端 astrbot 容器是否在跑。")
        rc = 2
    except Exception as e:  # noqa: BLE001
        print(f"[{_now()}] 出错：{e}")
        traceback.print_exc(file=sys.stdout)
        rc = 1
    finally:
        sink.flush()
        job["rc"] = rc
        job["done"] = True


# ---------------------------------------------------------------- 实时同步
# 界面上那个"实时同步"开关就是在这里起一个 realtime.Daemon 的线程：
# 它每隔几十秒拉一次云端记忆，笔记一变就推回去。

_RT: dict = {"daemon": None, "thread": None, "error": ""}
_RT_LOCK = threading.Lock()


def rt_status() -> dict:
    import realtime as rt

    st = rt.read_status() or {}
    alive = bool(_RT["thread"] and _RT["thread"].is_alive())
    return {
        "ok": True,
        "running": alive,
        "status": st.get("status") or ("running" if alive else "stopped"),
        "pulls": st.get("pulls", 0),
        "pushes": st.get("pushes", 0),
        "failures": st.get("failures", 0),
        "interval": st.get("interval"),
        "lastPull": st.get("lastPull"),
        "lastPush": st.get("lastPush"),
        "lastError": st.get("lastError") or _RT["error"],
        "updatedAt": st.get("updatedAt"),
    }


def rt_start() -> dict:
    import realtime as rt

    with _RT_LOCK:
        if _RT["thread"] and _RT["thread"].is_alive():
            return {"ok": True, "already": True}
        try:
            cfg = rt.settings()
            daemon = rt.Daemon(cfg)
            thread = threading.Thread(target=daemon.run, name="realtime", daemon=True)
            thread.start()
            _RT.update(daemon=daemon, thread=thread, error="")
            return {"ok": True, "started": True, "interval": cfg["interval"]}
        except Exception as e:  # noqa: BLE001
            _RT["error"] = f"{type(e).__name__}: {e}"
            return {"ok": False, "error": _RT["error"]}


def rt_stop() -> dict:
    with _RT_LOCK:
        daemon = _RT["daemon"]
        if daemon is None:
            return {"ok": True, "already": True}
        daemon.stop.set()
        _RT.update(daemon=None, thread=None)
        return {"ok": True, "stopped": True}


def start_job(name: str, sessions: list | None = None, dry: bool = False) -> dict:
    global _job_seq
    with _job_lock:
        _job_seq += 1
        jid = str(_job_seq)
        job = {"id": jid, "task": name, "lines": [], "done": False, "rc": None,
               "started": datetime.now().strftime("%H:%M:%S"),
               "sessions": list(sessions or []), "dry": bool(dry)}
        _jobs[jid] = job
    threading.Thread(target=_run_task, args=(name, job), daemon=True).start()
    return job


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "MemoryBridge/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # -- helpers
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            return {}

    # -- routes
    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not UI_FILE.exists():
                self._send(500, b"missing memory/ui/index.html", "text/plain; charset=utf-8")
                return
            self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            try:
                self._json(state())
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": str(e)}, 500)
            return
        if path == "/api/job":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            job = _jobs.get((q.get("id") or [""])[0])
            if not job:
                self._json({"ok": False, "error": "no such job"}, 404)
                return
            start = int((q.get("from") or ["0"])[0])
            with _job_lock:
                lines = job["lines"][start:]
                total = len(job["lines"])
            self._json({"ok": True, "lines": lines, "from": start, "total": total,
                        "done": job["done"], "rc": job["rc"]})
            return
        if path == "/api/realtime":
            self._json(rt_status())
            return
        if path == "/api/sessions":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ws = (q.get("workspace") or [""])[0] or None
            try:
                import activity as act

                workspaces = act.list_workspaces()
                key = ws or (workspaces[0]["key"] if workspaces else None)
                sessions = act.list_sessions(key, days=365, limit=300)
                self._json({"ok": True, "workspaces": workspaces,
                            "workspace": key, "sessions": sessions})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": str(e)}, 500)
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        if path == "/api/run":
            name = str(body.get("task") or "sync")
            if name not in TASKS:
                self._json({"ok": False, "error": f"unknown task {name}"}, 400)
                return
            job = start_job(name)
            self._json({"ok": True, "id": job["id"], "task": name})
            return
        if path == "/api/note":
            text = ab._clean_text(body.get("text") or "")
            if not text:
                self._json({"ok": False, "error": "内容为空"}, 400)
                return
            tags = [t.strip() for t in str(body.get("tags") or "").replace("，", ",").split(",") if t.strip()]
            importance = float(body.get("importance") or 0.7)
            rc = ab.cmd_note(text, importance, tags)
            pushed = None
            if rc == 0 and body.get("push", True):
                with contextlib.redirect_stdout(io.StringIO()):
                    pushed = ab.cmd_push(get_bot(), ab.DEFAULT_NOTE_FILE, False, 200)
            self._json({"ok": rc == 0, "pushed": pushed})
            return
        if path == "/api/activity":
            sessions = [str(s) for s in (body.get("sessions") or []) if s]
            if not sessions:
                self._json({"ok": False, "error": "没有选中任何会话"}, 400)
                return
            job = start_job("activity", sessions, dry=bool(body.get("dry")))
            self._json({"ok": True, "id": job["id"], "task": "activity",
                        "count": len(sessions), "dry": bool(body.get("dry"))})
            return
        if path == "/api/stop":
            self._json({"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return
        if path == "/api/realtime":
            action = str(body.get("action") or "status")
            if action == "start":
                self._json(rt_start())
            elif action == "stop":
                self._json(rt_stop())
            else:
                self._json(rt_status())
            return
        if path == "/api/open":
            target = str(body.get("target") or "workspace")
            p = ab.MEM_ROOT if target == "memory" else ab.WORKSPACE
            try:
                os.startfile(str(p))  # type: ignore[attr-defined]
                self._json({"ok": True})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": str(e)}, 500)
            return
        self._json({"ok": False, "error": "not found"}, 404)


def _pick_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket() as s:
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    return start


def build_server(start_port: int = 8765):
    """起一个服务但不进入循环，交给调用方（托盘版要自己控制生命周期）。"""
    port = _pick_port(start_port)
    srv = ThreadingHTTPServer((HOST, port), Handler)
    return srv, port


def main(argv: list[str]) -> int:
    srv, port = build_server(int(os.environ.get("MEM_BRIDGE_PORT", "8765")))
    url = f"http://{HOST}:{port}/"
    print("=" * 52)
    print("  记忆桥  ·  AstrBot ↔ DSH 工作区")
    print(f"  界面：{url}")
    print("  关掉这个窗口或点界面里的「关闭」即可停止。")
    print("=" * 52)
    if "--no-browser" not in argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    if "--no-pull" not in argv:
        # 打开界面就顺手拉一次，保证 memory/astrbot/ 里的 bot 记忆是最新的
        threading.Timer(1.2, lambda: start_job("pull")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0



if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
