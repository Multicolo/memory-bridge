#!/usr/bin/env python3
"""记忆实时互通守护进程。

做两件事，一直跑着：

  1. 每隔 N 秒把 AstrBot 那边的记忆 / 对话 / 人设拉下来（刷新
     memory/astrbot/*.md），所以任何会话在任意时刻读到的都是最新的；
  2. 盯着 memory/dsh-notes.jsonl，一有新笔记就推回 AstrBot 的长期记忆，
    防抖几秒，不会一条一条地打接口。

它不修改任何会话记录，只是让"两边看到的是同一份记忆"。DSH 里所有会话
（包括「爱睡懒觉的大肥鱼」那个）都走同一个工作区，所以跑起来之后它们
读到的内容会一起变新鲜。

用法：
    python realtime.py                 # 起守护进程（前台，Ctrl+C 退出）
    python realtime.py --once          # 只跑一轮，用来验证
    python realtime.py --interval 30   # 拉取间隔（秒），最快 5
    python realtime.py --status        # 看它现在活着没有

配置文件：bridge/config.json 里的 "realtime" 段，例如
    {"realtime": {"interval": 20, "debounce": 4, "chats": 120}}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import astrbot_bridge as bridge  # noqa: E402

# 状态文件跟着数据目录走（打包成 exe 后 HERE 在临时目录里，写进去就丢了）
RUN_DIR = bridge.MEM_ROOT / "bridge"
STATUS_FILE = RUN_DIR / "realtime.json"
LOCK_FILE = RUN_DIR / "realtime.lock"
LOG_FILE = RUN_DIR / "realtime.log"
NOTE_FILE = bridge.DEFAULT_NOTE_FILE

DEFAULTS = {"interval": 20, "debounce": 4, "chats": 120, "activity_minutes": 0}


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(text: str) -> None:
    line = f"[{now()}] {text}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            if fh.tell() > 512 * 1024:
                pass  # 下一轮启动时由 rotate() 截断
    except OSError:
        pass


def rotate_log() -> None:
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512 * 1024:
            tail = LOG_FILE.read_text(encoding="utf-8", errors="replace")[-200_000:]
            LOG_FILE.write_text(tail, encoding="utf-8")
    except OSError:
        pass


def settings() -> dict:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
        part = raw.get("realtime") if isinstance(raw, dict) else None
        if isinstance(part, dict):
            cfg.update({k: v for k, v in part.items() if v is not None})
    except (OSError, ValueError):
        pass
    cfg["interval"] = max(5, int(cfg["interval"]))
    cfg["debounce"] = max(1, int(cfg["debounce"]))
    return cfg


def read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_status(**patch) -> dict:
    st = read_status()
    st.update(patch)
    st["updatedAt"] = now()
    try:
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATUS_FILE)
    except OSError:
        pass
    return st


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Windows 上 os.kill(pid, 0) 会真的去发信号，用 tasklist 更稳。
        try:
            import subprocess
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=10).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Daemon:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.bot = bridge.AstrBot(bridge.load_config())
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.notes_mtime = self._mtime()
        self.pending_push_at = 0.0
        self.pulls = 0
        self.pushes = 0
        self.failures = 0
        self.last_error = ""

    def _mtime(self) -> float:
        try:
            return NOTE_FILE.stat().st_mtime
        except OSError:
            return 0.0

    # -- 拉 ------------------------------------------------------------
    def pull_once(self) -> None:
        with self.lock:
            try:
                code = bridge.cmd_pull(self.bot, int(self.cfg["chats"]))
                self.pulls += 1
                ok = code == 0
                if not ok:
                    self.failures += 1
                write_status(lastPull=now(), lastPullOk=ok, pulls=self.pulls,
                             failures=self.failures, lastError=self.last_error)
            except Exception as error:  # 网络抖动不该打死守护进程
                self.failures += 1
                self.last_error = f"{type(error).__name__}: {error}"
                write_status(lastPull=now(), lastPullOk=False, failures=self.failures,
                             lastError=self.last_error)
                log(f"拉取失败：{self.last_error}")

    # -- 推 ------------------------------------------------------------
    def push_once(self, reason: str) -> None:
        with self.lock:
            try:
                code = bridge.cmd_push(self.bot, NOTE_FILE, False, 200)
                self.pushes += 1
                ok = code == 0
                if not ok:
                    self.failures += 1
                log(f"推送完成（{reason}）：{ok}")
                write_status(lastPush=now(), lastPushOk=ok, pushes=self.pushes,
                             failures=self.failures, lastError=self.last_error)
                if ok:
                    # 推完再拉一次，让她刚记住的东西立刻出现在这边
                    try:
                        bridge.cmd_pull(self.bot, int(self.cfg["chats"]))
                        write_status(lastPull=now(), lastPullOk=True)
                    except Exception:
                        pass
            except Exception as error:
                self.failures += 1
                self.last_error = f"{type(error).__name__}: {error}"
                write_status(lastPush=now(), lastPushOk=False, failures=self.failures,
                             lastError=self.last_error)
                log(f"推送失败：{self.last_error}")

    # -- 主循环 --------------------------------------------------------
    def run(self) -> int:
        rotate_log()
        write_status(startedAt=now(), pid=os.getpid(), interval=int(self.cfg["interval"]),
                     status="running", pulls=0, pushes=0, failures=0)
        log(f"实时互通启动：每 {self.cfg['interval']}s 拉一次，笔记变了 {self.cfg['debounce']}s 内推。" )
        self.pull_once()
        next_pull = time.time() + self.cfg["interval"]

        while not self.stop.is_set():
            self.stop.wait(1.0)
            if self.stop.is_set():
                break

            mtime = self._mtime()
            if mtime != self.notes_mtime:
                self.notes_mtime = mtime
                self.pending_push_at = time.time() + self.cfg["debounce"]

            if self.pending_push_at and time.time() >= self.pending_push_at:
                self.pending_push_at = 0.0
                self.push_once("笔记有更新")

            if time.time() >= next_pull:
                next_pull = time.time() + self.cfg["interval"]
                self.pull_once()

        write_status(status="stopped", stoppedAt=now())
        log("实时互通停止。")
        return 0


def acquire_lock() -> bool:
    """单实例：已经有一个在跑就直接退出，不要互相打架。"""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if pid and pid != os.getpid() and pid_alive(pid):
            print(f"已经有一个实时同步在跑了（pid {pid}）。要重启就先关掉它。")
            return False
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError:
        pass


def show_status() -> int:
    st = read_status()
    if not st:
        print("还没跑过。启动：python realtime.py")
        return 1
    pid = int(st.get("pid") or 0)
    live = pid_alive(pid) and st.get("status") == "running"
    print(f"状态：{'运行中' if live else '未运行'}"
          f"（pid {pid or '-'}，间隔 {st.get('interval', '?')}s）")
    print(f"启动：{st.get('startedAt', '-')}   更新：{st.get('updatedAt', '-')}")
    print(f"拉取：{st.get('pulls', 0)} 次，上次 {st.get('lastPull', '-')} "
          f"{'成功' if st.get('lastPullOk') else '失败' if st.get('lastPull') else ''}")
    print(f"推送：{st.get('pushes', 0)} 次，上次 {st.get('lastPush', '-')} "
          f"{'成功' if st.get('lastPushOk') else '失败' if st.get('lastPush') else ''}")
    if st.get("lastError"):
        print(f"最近错误：{st['lastError']}")
    return 0 if live else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="realtime", description="AstrBot <-> DSH 记忆实时互通")
    ap.add_argument("--once", action="store_true", help="只跑一轮（拉一次 + 有变化就推），然后退出")
    ap.add_argument("--interval", type=int, default=0, help="拉取间隔秒数，覆盖配置")
    ap.add_argument("--status", action="store_true", help="查看运行状态")
    ap.add_argument("--stop", action="store_true", help="停掉正在跑的实例")
    args = ap.parse_args(argv)

    if args.status:
        return show_status()

    if args.stop:
        st = read_status()
        pid = int(st.get("pid") or 0)
        if pid and pid_alive(pid):
            try:
                os.kill(pid, 15)
                print(f"已请求停止（pid {pid}）。")
            except OSError as error:
                print(f"停不掉：{error}")
                return 1
        else:
            print("没有在跑的实例。")
        return 0

    cfg = settings()
    if args.interval:
        cfg["interval"] = max(5, args.interval)

    if args.once:
        d = Daemon(cfg)
        d.pull_once()
        d.push_once("手动跑一轮")
        return 0

    if not acquire_lock():
        return 0
    daemon = Daemon(cfg)
    try:
        return daemon.run()
    except KeyboardInterrupt:
        daemon.stop.set()
        write_status(status="stopped", stoppedAt=now())
        print("\n收到 Ctrl+C，停了。")
        return 0
    except Exception:
        traceback.print_exc()
        write_status(status="crashed", stoppedAt=now(), lastError=traceback.format_exc()[-400:])
        return 1
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
