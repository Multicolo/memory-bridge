#!/usr/bin/env python3
"""AstrBot <-> DSH 工作区 记忆桥。

双向通道：
  pull  : AstrBot(LivingMemory 长期记忆 + 对话记录 + 人设) -> memory/astrbot/
  push  : memory/dsh-notes.jsonl -> AstrBot LivingMemory（官方 import API）

只依赖 Python 标准库。凭据默认从本机面板配置读取。

用法：
  python astrbot_bridge.py status
  python astrbot_bridge.py pull  [--chats 120]
  python astrbot_bridge.py push  [--dry-run] [--file dsh-notes.jsonl] [--session UMO] [--persona ID]
  python astrbot_bridge.py note "一句话事实" [--importance 0.7] [--tags a,b]
  python astrbot_bridge.py recall "查询" [--top-k 5]
  python astrbot_bridge.py chat "在吗" [--session panel-debug]
  python astrbot_bridge.py selftest
  python astrbot_bridge.py sync
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 可搬运的根目录：打包成 exe 后代码在临时目录里，数据得落在 exe 旁边，
# 所以允许用 MEMORY_BRIDGE_HOME 指定（默认还是 memory/）。
_home = os.environ.get("MEMORY_BRIDGE_HOME")
MEM_ROOT = Path(_home).expanduser().resolve() if _home else HERE.parent   # memory/
WORKSPACE = MEM_ROOT.parent                 # 工作区根目录
DEFAULT_NOTE_FILE = MEM_ROOT / "dsh-notes.jsonl"
STATE_FILE = Path(os.environ.get("MEMORY_BRIDGE_STATE") or (MEM_ROOT / "bridge" / "state.json"))
EXPORT_DIR = MEM_ROOT / "astrbot"
BUNDLE_DIR = MEM_ROOT / "bundles"
CFG_FILE = Path(os.environ.get("MEMORY_BRIDGE_CONFIG") or (MEM_ROOT / "bridge" / "config.json"))
# 面板配置：优先用环境变量指定的文件，其次看本目录的 config.json（便携版就这么用）。
# 空字符串表示"没有外部面板配置"，这时只认 config.json / 环境变量。
_PANEL_DEFAULT = Path(os.environ.get("LOCALAPPDATA", "")) / "AstrBotPanel" / "config.json"
_PANEL_ALT = Path.home() / "AstrBotPanel" / "config.json"
PANEL_CFG = Path(os.environ.get("QQBOT_PANEL_CONFIG") or
                 (_PANEL_ALT if _PANEL_ALT.exists() else _PANEL_DEFAULT))

LM = "astrbot_plugin_livingmemory"
API_PREFIX = f"/api/plug/{LM}/page"

# chat 用的 API Key 存在工作区之外，避免跟着工作区被提交/分享
KEY_FILE = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MemoryBridge" / "chat_key.txt"


def _load_chat_key() -> str:
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _store_chat_key(key: str) -> None:
    try:
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(key, encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    cfg = {
        "base": os.environ.get("ASTRBOT_BASE", "http://127.0.0.1:6185"),
        "panel": os.environ.get("QQBOT_PANEL_BASE", "http://127.0.0.1:8899"),
        "user": os.environ.get("ASTRBOT_USER", ""),
        "password": os.environ.get("ASTRBOT_PASS", ""),
        "pin": os.environ.get("QQBOT_PANEL_PIN", ""),
        "timeout": 45,
    }
    extra = CFG_FILE if CFG_FILE.exists() else (HERE / "config.json")
    if extra.exists():
        try:
            cfg.update(json.loads(extra.read_text(encoding="utf-8")))
        except ValueError:
            pass
    if str(PANEL_CFG) and PANEL_CFG.exists():
        try:
            p = json.loads(PANEL_CFG.read_text(encoding="utf-8-sig"))
            a = p.get("astrbot") or {}
            cfg["user"] = cfg["user"] or a.get("user", "")
            cfg["password"] = cfg["password"] or a.get("pass", "")
            cfg["pin"] = cfg.get("pin") or str(p.get("accessPin") or "")
            port = (p.get("ports") or {}).get("astrbot")
            if port and not os.environ.get("ASTRBOT_BASE"):
                cfg["base"] = f"http://127.0.0.1:{port}"
            pp = p.get("panelPort")
            if pp and not os.environ.get("QQBOT_PANEL_BASE"):
                cfg["panel"] = f"http://127.0.0.1:{pp}"
        except Exception:
            pass
    return cfg


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"pushed_lines": 0, "pushed_hashes": [], "last_pull": None, "last_push": None}


def save_state(st: dict) -> None:
    st["pushed_hashes"] = st.get("pushed_hashes", [])[-5000:]
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- HTTP

class ApiError(RuntimeError):
    pass


def _request(url: str, method: str = "GET", body: dict | None = None,
             headers: dict | None = None, timeout: int = 45, raw: bool = False):
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "dsh-astrbot-bridge/1"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise ApiError(f"HTTP {e.code} {url}: {detail}") from None
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise ApiError(f"连接失败 {url}: {e}") from None
    if raw:
        return payload
    return json.loads(payload.decode("utf-8"))


def _is_tunnel_down(err: Exception) -> bool:
    """本地 6185 端口像是隧道断了（而不是 AstrBot 本身报错）。"""
    msg = str(err).lower()
    return any(k in msg for k in ("积极拒绝", "refused", "10054", "强迫关闭", "timed out", "超时",
                                  "closed connection", "without response", "connection reset"))


class AstrBot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = cfg["base"].rstrip("/")
        self.token = None
        self.token_at = 0.0
        self.panel_cookie = ""
        self._chat_key = ""

    # -- auth
    def _login_once(self) -> dict:
        return _request(self.base + "/api/auth/login", "POST",
                        {"username": self.cfg["user"], "password": self.cfg["password"]},
                        timeout=self.cfg["timeout"])

    def login(self, force: bool = False) -> str:
        if self.token and not force and time.time() - self.token_at < 1800:
            return self.token
        if not self.cfg["user"] or not self.cfg["password"]:
            raise ApiError("缺少 AstrBot 账号密码（config.json 的 astrbot.user/pass）")
        try:
            r = self._login_once()
        except ApiError as e:
            if not _is_tunnel_down(e):
                raise
            if not self.heal_tunnel():
                raise
            r = self._login_once()
        tok = ((r or {}).get("data") or {}).get("token")
        if not tok:
            raise ApiError(f"登录失败: {str(r)[:300]}")
        self.token, self.token_at = tok, time.time()
        return tok

    def heal_tunnel(self, wait: int = 45) -> bool:
        """6185 隧道掉了就让本机面板重启它，然后等端口回来。"""
        print("  · AstrBot 端口不通，正在请面板重开 SSH 隧道 ...")
        try:
            self.panel("/api/tunnel/restart", "POST", {})
        except ApiError as e:
            print(f"  · 面板也没能重启隧道：{e}")
            return False
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(2)
            try:
                _request(self.base + "/", timeout=8, raw=True)
                print("  · 隧道已恢复。")
                return True
            except ApiError:
                continue
        print("  · 隧道没能在预期时间内恢复。")
        return False

    def api(self, path: str, method: str = "GET", body: dict | None = None, timeout: int | None = None):
        tok = self.login()
        try:
            return _request(self.base + path, method, body,
                            headers={"Authorization": "Bearer " + tok},
                            timeout=timeout or self.cfg["timeout"])
        except ApiError as e:
            if "HTTP 401" in str(e):
                tok = self.login(force=True)
                return _request(self.base + path, method, body,
                                headers={"Authorization": "Bearer " + tok},
                                timeout=timeout or self.cfg["timeout"])
            raise

    # -- LivingMemory
    def stats(self) -> dict:
        r = self.api(API_PREFIX + "/stats")
        return (r or {}).get("data") or {}

    def export_memories(self) -> dict:
        r = self.api(API_PREFIX + "/memories/export", "POST", {"format": "json"}, timeout=120)
        data = (r or {}).get("data") or {}
        content = data.get("content")
        if isinstance(content, str):
            try:
                return json.loads(content)
            except Exception:
                return {"raw": content}
        return content or {}

    def recall(self, query: str, top_k: int = 5) -> dict:
        r = self.api(API_PREFIX + "/recall/test", "POST", {"query": query, "top_k": top_k}, timeout=120)
        return (r or {}).get("data") or {}

    def list_memories(self, limit: int = 500) -> list[dict]:
        r = self.api(f"{API_PREFIX}/memories?limit={int(limit)}")
        d = (r or {}).get("data") or {}
        if isinstance(d, list):
            return d
        return d.get("memories") or d.get("items") or []

    def delete_memories(self, ids: list) -> dict:
        r = self.api(API_PREFIX + "/memories/batch-delete", "POST", {"memory_ids": ids})
        return (r or {}).get("data") or {}

    def import_memories(self, memories: list[dict], dry_run: bool = False) -> dict:
        payload = {"format": "json", "content": json.dumps({"memories": memories}, ensure_ascii=False),
                   "dry_run": dry_run, "duplicate_strategy": "skip"}
        r = self.api(API_PREFIX + "/memories/import", "POST", payload, timeout=180)
        return (r or {}).get("data") or {}

    # -- 面板
    def personas(self) -> list:
        last_err = None
        for path in ("/api/persona/list", "/api/personas"):
            try:
                r = self.api(path)
            except ApiError as e:
                last_err = e
                continue
            d = (r or {}).get("data")
            if isinstance(d, dict):
                d = d.get("personas") or d.get("items") or []
            if d:
                return d
            return []
        if last_err:
            raise last_err
        return []

    def panel(self, path: str, method: str = "GET", body: dict | None = None, timeout: int = 90):
        url = self.cfg["panel"].rstrip("/") + path
        last = None
        for attempt in range(4):
            try:
                return self._panel_once(url, method, body, timeout)
            except ApiError as e:
                last = e
                msg = str(e)
                if "需要口令" in msg or "口令" in msg:
                    self.panel_login(force=True)
                    continue
                if "积极拒绝" in msg or "Connection refused" in msg:
                    time.sleep(3)
                    continue
                raise
        raise last

    def _panel_once(self, url: str, method: str, body: dict | None, timeout: int):
        headers = {}
        if self.panel_cookie:
            headers["Cookie"] = self.panel_cookie
        return _request(url, method, body, headers=headers, timeout=timeout)

    def panel_login(self, force: bool = False) -> None:
        if self.panel_cookie and not force:
            return
        pin = self.cfg.get("pin", "")
        # 面板被看门狗重启后 accessPin 会变（有时变成 astrbot 密码，有时清空），
        # 所以每次都重读面板配置，并且把「空口令」也当作候选。
        fresh = ""
        try:
            if PANEL_CFG.exists():
                fresh = str(json.loads(PANEL_CFG.read_text(encoding="utf-8-sig")).get("accessPin") or "")
        except Exception:
            fresh = ""
        candidates = []
        for c in (pin, fresh, ""):
            if c not in candidates:
                candidates.append(c)
        tried = []
        last_err = None
        for candidate in candidates:
            tried.append(candidate)
            req = urllib.request.Request(
                self.cfg["panel"].rstrip("/") + "/api/login",
                data=json.dumps({"pin": candidate}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    cookies = r.headers.get_all("Set-Cookie") or []
            except urllib.error.HTTPError as e:
                last_err = e
                continue
            for c in cookies:
                if c.lower().startswith("qbp="):
                    self.panel_cookie = c.split(";", 1)[0]
                    self.cfg["pin"] = candidate
                    return
            if not cookies:
                self.panel_cookie = ""  # 面板未设口令
                self.cfg["pin"] = candidate
                return
            raise ApiError("面板登录失败：没有拿到会话 Cookie")
        if last_err is not None:
            raise ApiError(f"面板口令被拒绝（试过 {tried}）: {last_err.read()[:200]!r}") from None
        raise ApiError("面板登录失败")

    def messages(self, limit: int = 120, session: str = "") -> list:
        body = {"limit": max(1, min(int(limit), 300))}
        if session:
            body["session"] = session
        r = self.panel("/api/messages", "POST", body)
        d = (r or {}).get("data") or {}
        msgs = d.get("messages") or d.get("items") or (d if isinstance(d, list) else [])
        return msgs or []

    def sessions(self) -> list[dict]:
        try:
            r = self.panel("/api/messages/sessions")
        except ApiError:
            return []
        d = (r or {}).get("data") or {}
        return d.get("sessions") or []

    # -- 服务器命令（面板 /api/dev/exec，仅本机可用）
    def exec(self, cmd: str, timeout: int = 120) -> str:
        r = self.panel("/api/dev/exec", "POST", {"cmd": cmd}, timeout=timeout)
        out = str((r or {}).get("out") or "")
        if not (r or {}).get("ok"):
            err = str((r or {}).get("err") or out)
            raise ApiError(f"远端命令失败: {err[:300]}")
        return out

    def default_persona(self) -> str:
        """AstrBot 的全局默认人格 id（召回和写入都带这个 persona）。"""
        st = load_state()
        if st.get("default_persona"):
            return str(st["default_persona"])
        code = (
            "python3 -c \"import json;c=json.load(open('/opt/qqbot/astrbot-data/cmd_config.json',"
            "encoding='utf-8-sig'));print(c['agent_runner']['config']['persona']['persona_id'])\""
        )
        try:
            out = self.exec(code).strip()
        except ApiError:
            out = ""
        pid = out.splitlines()[-1].strip() if out else ""
        if pid:
            st["default_persona"] = pid
            save_state(st)
        return pid

    def targets(self) -> list[dict]:
        """要把 DSH 笔记写进哪些会话（每个会话一份，召回按 session 过滤）。"""
        persona = self.default_persona()
        st = load_state()
        try:
            umos = [str(s.get("session") or "") for s in self.sessions()]
        except ApiError:
            umos = []
        umos = [u for u in umos if u]
        if not umos:
            # 会话列表一时取不到（隧道抖动）时，沿用上次的目标，别退化成 dsh-workspace
            cached = [t for t in (st.get("targets") or []) if t.get("session")]
            if cached:
                return cached
        qq = [u for u in umos if u.startswith("qq-onebot:")]
        other = [u for u in umos if not u.startswith("qq-onebot:")]
        chosen = (qq + other)[:4] or ["dsh-workspace"]
        out = [{"session": u, "persona": persona} for u in chosen]
        st["targets"] = out
        save_state(st)
        return out

    # -- AstrBot /api/v1/chat（webchat 会话，用来做端到端召回自检）
    def chat_key(self, force: bool = False) -> str:
        if not force and self._chat_key:
            return self._chat_key
        if not force:
            saved = _load_chat_key()
            if saved:
                self._chat_key = saved
                return saved
        r = self.api("/api/v1/api-keys", "POST",
                     {"name": "dsh-bridge", "scopes": ["chat"], "expires_in_days": 365})
        key = str(((r or {}).get("data") or {}).get("api_key") or "")
        if not key:
            raise ApiError("没能创建带 chat 权限的 API Key")
        self._chat_key = key
        _store_chat_key(key)
        return key

    def chat(self, message: str, session: str = "panel-debug", username: str = "panel",
             timeout: int = 240) -> str:
        resp = None
        for attempt in range(2):
            key = self.chat_key(force=attempt > 0)
            body = json.dumps({"message": message, "session_id": session,
                               "username": username, "enable_streaming": True},
                              ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                self.base + "/api/v1/chat", data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + key})
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
                break
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    continue
                raise ApiError(f"对话请求失败 HTTP {e.code}: {e.read()[:200]!r}") from None
            except (urllib.error.URLError, socket.timeout) as e:
                raise ApiError(f"对话连接失败: {e}") from None
        if resp is None:
            raise ApiError("对话请求失败")
        parts, err = [], None
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except Exception:
                continue
            t = ev.get("type")
            if t == "plain" and ev.get("chain_type") != "reasoning":
                parts.append(str(ev.get("data") or ""))
            elif t in ("error", "run_failed"):
                err = ev.get("data") or ev.get("message") or "运行失败"
                break
            elif t == "complete":
                break
        if err:
            raise ApiError(f"对话失败: {err}")
        return "".join(parts).strip()


def session_label(umo: str) -> str:
    """把 qq-onebot:GroupMessage:123 之类的会话标识换成好读的名字。"""
    if not umo:
        return "（未知会话）"
    parts = umo.split(":")
    plat = parts[0]
    if len(parts) >= 3 and parts[1] == "FriendMessage":
        return f"{plat} 私聊 · {parts[2]}"
    if len(parts) >= 3 and parts[1] == "GroupMessage":
        return f"{plat} 群聊 · {parts[2]}"
    return umo


# ---------------------------------------------------------------- 格式化

def _ts(value) -> str:
    if value in (None, "", 0):
        return ""
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            v = float(value)
            if v > 1e11:
                v /= 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        s = str(value).replace("T", " ")
        return s[:16]
    except Exception:
        return str(value)[:16]


def _norm_messages(raw: list) -> list[dict]:
    out = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        t = m.get("content") or m.get("text") or m.get("message") or ""
        if isinstance(t, list):
            parts = []
            for p in t:
                if isinstance(p, dict):
                    parts.append(str(p.get("text") or p.get("content") or ""))
                else:
                    parts.append(str(p))
            t = " ".join(x for x in parts if x)
        text = str(t).replace("\r", " ").strip()
        if not text:
            continue
        # 主动对话之类的系统任务会被塞进 user 消息里，导出时跳过
        if text.startswith(("[系统任务", "【系统任务", "<system_reminder")):
            continue
        out.append({
            "time": _ts(m.get("timestamp") or m.get("time") or m.get("created_at") or m.get("ts")),
            "session": str(m.get("session_id") or m.get("session") or m.get("chat_id") or ""),
            "sender": str(m.get("sender_name") or m.get("sender") or m.get("nickname") or m.get("user_id") or ""),
            "text": text,
        })
    return out


def _norm_memories(data) -> list[dict]:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("memories") or data.get("items") or data.get("long_term_memories") or []
    else:
        items = []
    out = []
    for m in items:
        if not isinstance(m, dict):
            continue
        content = m.get("content") or m.get("text") or m.get("memory") or ""
        if not str(content).strip():
            continue
        md = m.get("metadata") if isinstance(m.get("metadata"), dict) else {}
        out.append({
            "id": str(m.get("id") or m.get("memory_id") or m.get("original_id") or ""),
            "content": str(content).strip(),
            "importance": m.get("importance"),
            "session_id": m.get("session_id") or md.get("session_id") or "",
            "persona_id": m.get("persona_id") or md.get("persona_id") or "",
            "created_at": m.get("created_at") or m.get("timestamp") or md.get("create_time") or md.get("ts") or "",
            "updated_at": m.get("updated_at") or "",
            "status": m.get("status") or md.get("status") or "",
            "metadata": md,
        })
    return out


def _fmt_importance(v) -> str:
    try:
        return f"{float(v):.2f}"
    except Exception:
        return "?"


def build_memory_md(memories: list[dict], stats: dict, meta: dict) -> str:
    lines = ["# AstrBot 长期记忆（自动导出）", ""]
    lines.append(f"- 导出时间：{meta['exported_at']}")
    lines.append(f"- 记忆总数：{len(memories)}（LivingMemory stats: {stats.get('total_memories', '?')}）")
    lines.append(f"- 来源：{meta['base']}  ·  LivingMemory export API")
    lines.append("")
    if not memories:
        lines.append("_（机器人侧目前没有长期记忆。对话中产生的重要事实会在反思后写入。）_")
        lines.append("")
        return "\n".join(lines)

    def sort_key(m):
        imp = m.get("importance")
        try:
            imp = float(imp)
        except Exception:
            imp = 0.0
        return (-imp, str(m.get("created_at") or ""))

    groups: dict[str, list[dict]] = {}
    for m in sorted(memories, key=sort_key):
        key = m.get("session_id") or "（全局）"
        groups.setdefault(key, []).append(m)

    for gname, items in groups.items():
        title = gname
        if gname.startswith("dsh-"):
            title = gname + "（来自 DSH）"
        lines.append(f"## {title} · {len(items)} 条")
        lines.append("")
        for m in items:
            imps = _fmt_importance(m.get("importance"))
            when = _ts(m.get("created_at"))
            suffix = f" _(重要性 {imps}{'，' + when if when else ''})_"
            text = m["content"].replace("\n", " ").strip()
            lines.append(f"- {text}{suffix}")
        lines.append("")
    return "\n".join(lines)


def build_digest_md(memories: list[dict], stats: dict, personas: list[dict], meta: dict,
                    msgs: list[dict] | None = None, limit: int = 40) -> str:
    """去重后的一页速览：DSH 会话开始时优先读这个，不够再翻 MEMORY.md。"""
    def imp(m):
        try:
            return float(m.get("importance"))
        except Exception:
            return 0.0

    merged: dict[str, dict] = {}
    for m in sorted(memories, key=lambda x: (-imp(x), str(x.get("created_at") or ""))):
        text = m["content"].replace("\n", " ").strip()
        key = re.sub(r"\W+", "", text.lower())[:200]
        if not key:
            continue
        if key in merged:
            merged[key]["sessions"].add(m.get("session_id") or "（全局）")
        else:
            if len(text) > 220:
                text = text[:220].rstrip() + "…"
            merged[key] = {"text": text, "importance": imp(m),
                           "when": _ts(m.get("created_at")), "sessions": {m.get("session_id") or "（全局）"}}

    items = sorted(merged.values(), key=lambda x: (-x["importance"], x["text"]))
    lines = ["# bot 记忆速览（去重）", ""]
    lines.append(f"- 导出时间：{meta['exported_at']}")
    lines.append(f"- 原始 {len(memories)} 条 → 去重后 {len(items)} 条"
                 f"（同一条记忆会按会话各存一份，这里合并）")
    persona = ""
    for p in personas or []:
        pid = str(p.get("persona_id") or "")
        if pid and pid != "default":
            persona = pid
            break
    if not persona and personas:
        persona = str(personas[0].get("persona_id") or "")
    if persona:
        lines.append(f"- bot 人设：{persona}")
    lines.append("")

    if not items:
        lines.append("_（机器人侧目前没有长期记忆）_")
        return "\n".join(lines) + "\n"

    lines.append("## 要点")
    lines.append("")
    for i, it in enumerate(items[:limit], 1):
        extra = []
        if it["when"]:
            extra.append(it["when"])
        if len(it["sessions"]) > 1:
            extra.append(f"{len(it['sessions'])} 个会话")
        suffix = f" _({'，'.join(extra)})_" if extra else ""
        lines.append(f"{i}. {it['text']}{suffix}")
    lines.append("")

    recent = [m for m in (msgs or []) if m.get("text")]
    if recent:
        lines.append("## 最近在聊（节选）")
        lines.append("")
        for m in recent[-6:]:
            who = m.get("sender") or "?"
            text = m["text"].replace("\n", " ")
            if len(text) > 120:
                text = text[:120] + "…"
            lines.append(f"- {who}：{text}")
        lines.append("")
    return "\n".join(lines)


def build_recent_md(msgs: list[dict], limit: int) -> str:
    lines = ["# AstrBot 最近对话（自动导出）", ""]
    lines.append(f"- 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}  ·  共 {len(msgs)} 条")
    lines.append("")
    if not msgs:
        lines.append("_（暂无对话记录）_")
        return "\n".join(lines)
    cur = None
    for m in msgs:
        label = m.get("label") or session_label(m.get("session", ""))
        if label != cur:
            cur = label
            if lines and lines[-1] != "":
                lines.append("")
            stamp = f"（最后活跃 {m['updated_at']}）" if m.get("updated_at") else ""
            lines.append(f"## {label}{stamp}")
            lines.append("")
        text = m["text"].replace("\n", " ")
        if len(text) > 300:
            text = text[:300] + "…"
        who = m["sender"] or "?"
        when = f" `{m['time']}`" if m["time"] else ""
        lines.append(f"- **{who}**{when}：{text}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 命令

def cmd_status(bot: AstrBot) -> int:
    st = load_state()
    print(f"AstrBot: {bot.base}")
    s = bot.stats()
    print(f"长期记忆: {s.get('total_memories', '?')} 条；会话分布: {json.dumps(s.get('sessions', {}), ensure_ascii=False)}")
    im = s.get("index_maintenance") or {}
    print(f"索引: {im.get('state')} / {im.get('message', '')}")
    try:
        msgs = bot.messages(5)
        print(f"对话接口: OK（样本 {len(msgs)} 条）")
    except Exception as e:
        print(f"对话接口: 不可用 -> {e}")
    print(f"上次 pull: {st.get('last_pull') or '从未'}；上次 push: {st.get('last_push') or '从未'}")
    notes = DEFAULT_NOTE_FILE
    if notes.exists():
        n = sum(1 for line in notes.read_text(encoding="utf-8").splitlines() if line.strip())
        print(f"本地 dsh-notes.jsonl: {n} 行（已推送 {st.get('pushed_lines', 0)} 行）")
    return 0


def _filter_memories(memories: list[dict], persona: str = "", session: str = "",
                     ids: list | None = None) -> list[dict]:
    """按人格 / 会话 / 记忆 id 挑记忆。空条件表示不过滤。"""
    want_ids = {str(i) for i in (ids or []) if str(i)}
    out = []
    for m in memories:
        if want_ids and str(m.get("id") or m.get("original_id") or "") not in want_ids:
            continue
        if persona and str(m.get("persona_id") or "") != persona:
            continue
        if session and str(m.get("session_id") or "") != session:
            continue
        out.append(m)
    return out


def cmd_pull(bot: AstrBot, chats: int, persona: str = "") -> int:
    """拉取 bot 的记忆到本地。

    persona 给了就只拉这个人格的记忆（"只传一个人格"而不是全部），
    对话也只保留跟这些记忆有关的会话。
    """
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stats = bot.stats()
    all_memories = _norm_memories(bot.export_memories())
    memories = _filter_memories(all_memories, persona=persona)
    personas = bot.personas()
    if persona:
        personas = [p for p in personas if str(p.get("persona_id") or "") == persona] or \
                   [{"persona_id": persona}]

    msgs: list[dict] = []
    chat_err = None
    try:
        sess_list = bot.sessions()
        targets: list[tuple[str, str]] = []
        for s in sess_list[:4]:
            umo = str(s.get("session") or "")
            if umo:
                targets.append((umo, str(s.get("last") or "")))
        if persona:
            keep = {str(m.get("session_id") or "") for m in memories}
            keep.discard("")
            if keep:
                targets = [t for t in targets if t[0] in keep]
        if not targets:
            targets = [("", "")]
        for umo, last in targets:
            for m in _norm_messages(bot.messages(chats, umo)):
                if not m["text"]:
                    continue
                m["session"] = umo or m["session"]
                m["label"] = session_label(umo) if umo else "（未知会话）"
                m["updated_at"] = last
                msgs.append(m)
    except Exception as e:
        chat_err = str(e)

    (EXPORT_DIR / "memories.json").write_text(
        json.dumps({"exported_at": now, "stats": stats, "memories": all_memories, "personas": personas},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {"exported_at": now, "base": bot.base, "persona": persona,
            "memory_count": len(memories), "total_memory_count": len(all_memories),
            "message_count": len(msgs), "chat_error": chat_err}
    (EXPORT_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (EXPORT_DIR / "MEMORY.md").write_text(build_memory_md(memories, stats, meta), encoding="utf-8")
    (EXPORT_DIR / "DIGEST.md").write_text(
        build_digest_md(memories, stats, personas, meta, msgs), encoding="utf-8")
    (EXPORT_DIR / "recent.md").write_text(build_recent_md(msgs, chats), encoding="utf-8")
    (EXPORT_DIR / "personas.json").write_text(json.dumps(personas, ensure_ascii=False, indent=2), encoding="utf-8")

    st = load_state()
    st["last_pull"] = now
    save_state(st)

    print(f"已拉取：记忆 {len(memories)} 条 · 对话 {len(msgs)} 条 · 人设 {len(personas)} 个 -> {EXPORT_DIR}")
    if persona:
        print(f"  按人格过滤：{persona}（原始 {len(all_memories)} 条，命中 {len(memories)} 条；"
              f"本地 memories.json 仍保留全量，方便之后再挑别的）")
    print(f"  本地全量记忆：{len(all_memories)} 条")
    if chat_err:
        print(f"  ! 对话拉取失败：{chat_err}")
    return 0


def _clean_text(value) -> str:
    """去掉首尾空白和 BOM / 零宽字符（Windows 管道常自带 BOM）。"""
    return str(value or "").strip().strip("\ufeff\u200b\u200e\u200f").strip()


def _load_notes(path: Path) -> list[dict]:
    notes = []
    if not path.exists():
        return notes
    for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = _clean_text(line)
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            print(f"  ! 第 {i} 行不是合法 JSON，已跳过")
            continue
        text = _clean_text(obj.get("text") or obj.get("content"))
        if not text:
            continue
        notes.append({
            "line": i,
            "text": text,
            "importance": obj.get("importance", 0.6),
            "tags": obj.get("tags") or [],
            "ts": obj.get("ts") or obj.get("time") or "",
        })
    return notes


def cmd_push(bot: AstrBot, path: Path, dry_run: bool, limit: int,
             session: str = "", persona: str = "") -> int:
    """把 dsh-notes.jsonl 写进 AstrBot 长期记忆。

    LivingMemory 的召回按 session_id + persona_id 过滤，所以每条笔记会给
    每个目标会话各写一份，这样 QQ 私聊、群聊、网页调试会话都能想起来。
    """
    st = load_state()
    pushed: dict = dict(st.get("pushed") or {})
    notes = _load_notes(path)
    fresh, seen = [], set()
    for n in notes:
        h = hashlib.sha1(n["text"].encode("utf-8")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        n["hash"] = h
        fresh.append(n)
    fresh = fresh[:limit]
    if not fresh:
        print("没有新的 DSH 笔记需要推送。")
        return 0

    if session:
        targets = [{"session": session, "persona": persona or bot.default_persona()}]
    else:
        targets = bot.targets()
    if persona:
        targets = [{"session": t["session"], "persona": persona} for t in targets]
    if not persona and not any(t["persona"] for t in targets):
        print("  ! 没读到 AstrBot 的默认人格，召回时可能被 persona 过滤掉")

    jobs: list[tuple[dict, list[tuple[dict, str]]]] = []
    for t in targets:
        rows = [(n, f"{n['hash']}|{t['session']}") for n in fresh
                if f"{n['hash']}|{t['session']}" not in pushed]
        if rows:
            jobs.append((t, rows))
    if not jobs:
        print("没有新的 DSH 笔记需要推送（目标会话里都已存在）。")
        return 0

    total = sum(len(rows) for _, rows in jobs)
    print(f"待推送 {total} 条 × {len(jobs)} 个会话 -> LivingMemory"
          + ("（dry-run）" if dry_run else ""))
    ts = datetime.now().isoformat(timespec="seconds")
    rc = 0
    for t, rows in jobs:
        print(f"  [{session_label(t['session'])}] persona={t['persona'] or '（默认）'}")
        memories = []
        for n, _key in rows:
            meta = {"source": "dsh", "ts": n["ts"] or ts}
            if n["tags"]:
                meta["tags"] = n["tags"]
            memories.append({
                "content": n["text"],
                "importance": float(n["importance"]),
                "session_id": t["session"],
                "persona_id": t["persona"] or "",
                "metadata": meta,
            })
        try:
            pre = bot.import_memories(memories, dry_run=True)
            print(f"    dry-run: valid={pre.get('valid_count')} duplicate={pre.get('duplicate_count')} "
                  f"planned={pre.get('planned_import_count')}")
            if pre.get("errors"):
                print(f"    ! 校验错误: {json.dumps(pre['errors'], ensure_ascii=False)[:300]}")
            if dry_run:
                continue
            res = bot.import_memories(memories, dry_run=False)
        except ApiError as e:
            print(f"    ! 导入失败：{e}")
            rc = 1
            continue

        imported = int(res.get("imported_count") or 0)
        skipped = int(res.get("skipped_duplicate_count") or 0)
        failed = int(res.get("failed_count") or 0)
        print(f"    结果: imported={imported} skipped={skipped} failed={failed}")
        if res.get("errors"):
            print(f"    ! 错误: {json.dumps(res['errors'], ensure_ascii=False)[:400]}")
        if imported + skipped > 0 and failed == 0:
            for _n, key in rows:
                pushed[key] = ts
        else:
            rc = 1

    if not dry_run:
        st["pushed"] = pushed
        st["pushed_lines"] = max(st.get("pushed_lines", 0), max(n["line"] for n in fresh))
        st["last_push"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_state(st)
    return rc


def _activity_ids(bot: AstrBot, session_key: str, target: str) -> list:
    """找出某条 DSH 会话在某个目标会话里已有的行为记忆。"""
    ids = []
    for m in bot.list_memories(500):
        md = m.get("metadata") or {}
        if md.get("source") != "dsh-activity" or md.get("session_key") != session_key:
            continue
        if md.get("session_id") and md["session_id"] != target:
            continue
        if m.get("id"):
            ids.append(m["id"])
    return ids


def cmd_activity(bot: AstrBot, dry_run: bool = False, days: int = 30, limit: int = 8,
                 sessions: list | None = None) -> int:
    """把 DSH 工作区里做过的事摘要成记忆，推回 AstrBot。

    sessions 给了就只处理这几条会话文件（界面上勾选的），不给就退回
    "这个工作区最近的 limit 条"。
    """
    import activity

    if sessions:
        rows = activity.build_rows(list(sessions))
    else:
        rows = activity.collect(WORKSPACE, days=days, limit=limit)
    if not rows:
        print("没有找到这个工作区的 DSH 会话记录。")
        return 0
    st = load_state()
    done: dict = dict(st.get("activity") or {})
    targets = bot.targets()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"DSH 行为 {len(rows)} 条会话 × {len(targets)} 个会话目标"
          + ("（dry-run）" if dry_run else ""))

    rc = 0
    for row in rows:
        sid, h = row["id"], row["hash"]
        changed = (done.get(sid) or {}).get("hash") != h
        print(f"  《{row['title']}》 {row['end']} · {row['events']} 事件"
              + ("" if changed else " · 无变化，跳过"))
        if not changed:
            continue
        print(f"    {row['text']}")
        for t in targets:
            perm = f"[{session_label(t['session'])}] "
            mem = {
                "content": row["text"],
                "importance": 0.55,
                "session_id": t["session"],
                "persona_id": t["persona"] or "",
                "metadata": {
                    "source": "dsh-activity",
                    "session_key": sid,
                    "hash": h,
                    "ts": ts,
                },
            }
            try:
                old = _activity_ids(bot, sid, t["session"])
                if dry_run:
                    pre = bot.import_memories([mem], dry_run=True)
                    print(f"    {perm}dry-run planned={pre.get('planned_import_count')}"
                          + (f"，会先删旧记忆 {len(old)} 条" if old else ""))
                    continue
                if old:
                    bot.delete_memories(old)
                res = bot.import_memories([mem], dry_run=False)
            except ApiError as e:
                print(f"    ! {perm}{e}")
                rc = 1
                continue
            failed = int(res.get("failed_count") or 0)
            print(f"    {perm}imported={res.get('imported_count')} failed={failed}"
                  + (f"（替换 {len(old)} 条）" if old else ""))
            if failed:
                print(f"    ! 错误: {json.dumps(res.get('errors'), ensure_ascii=False)[:300]}")
                rc = 1
        if not dry_run:
            done[sid] = {"hash": h, "title": row["title"], "pushed": ts}

    if not dry_run:
        st["activity"] = done
        st["last_activity"] = ts
        save_state(st)
    return rc


def persona_stats(memories: list[dict]) -> list[dict]:
    """按人格聚合记忆条数与涉及的会话，给界面下拉框用。"""
    agg: dict[str, dict] = {}
    for m in memories:
        pid = str(m.get("persona_id") or "")
        row = agg.setdefault(pid, {"persona_id": pid, "count": 0, "sessions": set()})
        row["count"] += 1
        sid = str(m.get("session_id") or "")
        if sid:
            row["sessions"].add(sid)
    out = []
    for row in agg.values():
        row["sessions"] = sorted(row["sessions"])
        out.append(row)
    return sorted(out, key=lambda x: (-x["count"], x["persona_id"]))


def cmd_personas(bot: AstrBot) -> int:
    memories = _norm_memories(bot.export_memories())
    rows = persona_stats(memories)
    print(f"共 {len(memories)} 条记忆，{len(rows)} 个人格维度：")
    for r in rows:
        name = r["persona_id"] or "（无人格 / 全局）"
        print(f"  - {name}: {r['count']} 条 · 会话 {len(r['sessions'])} 个")
        for s in r["sessions"]:
            print(f"      {session_label(s)}")
    return 0


def _load_export(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"没有导出文件：{path}（先跑 pull）")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return _norm_memories(data)


def cmd_transfer(bot: AstrBot, path: Path, ids: list | None = None, persona: str = "",
                 session: str = "", to_persona: str = "", dry_run: bool = False) -> int:
    """把本地导出里的指定记忆（按 id / 人格）写到目标会话。

    - 只给 --persona：这个人格的记忆全传（"选择一个人格"）
    - 给 --ids：只传勾中的那几条
    - 不给 --session：写到所有活跃会话（每个会话各一份）
    """
    memories = _load_export(path)
    picked = _filter_memories(memories, persona=persona, ids=ids)
    if not picked:
        print("没有符合条件的记忆可传。")
        return 1

    targets = ([{"session": session, "persona": to_persona or bot.default_persona()}]
               if session else bot.targets())
    if to_persona:
        targets = [dict(t, persona=to_persona) for t in targets]
    if not targets:
        print("没有目标会话可写。")
        return 1

    print(f"选中记忆 {len(picked)} 条 × {len(targets)} 个目标会话"
          + ("（dry-run）" if dry_run else ""))
    rc = 0
    for t in targets:
        memories_payload = []
        for m in picked:
            meta = {"source": "transferred", "origin_id": str(m.get("id") or ""),
                    "origin_session": str(m.get("session_id") or ""), "ts": datetime.now().isoformat(timespec="seconds")}
            memories_payload.append({
                "content": m["content"],
                "importance": float(m.get("importance") or 0.6),
                "session_id": t["session"],
                "persona_id": t["persona"] or "",
                "metadata": meta,
            })
        try:
            pre = bot.import_memories(memories_payload, dry_run=True)
            print(f"  [{session_label(t['session'])}] persona={t['persona'] or '（默认）'} "
                  f"valid={pre.get('valid_count')} duplicate={pre.get('duplicate_count')} "
                  f"planned={pre.get('planned_import_count')}")
            if dry_run:
                continue
            res = bot.import_memories(memories_payload, dry_run=False)
        except ApiError as e:
            print(f"  ! 写入失败：{e}")
            rc = 1
            continue
        failed = int(res.get("failed_count") or 0)
        print(f"    imported={res.get('imported_count')} skipped={res.get('skipped_duplicate_count')} failed={failed}")
        if res.get("errors"):
            print(f"    ! {json.dumps(res['errors'], ensure_ascii=False)[:300]}")
        if failed:
            rc = 1
    return rc


BUNDLE_FORMAT = "memory-bridge.persona"
BUNDLE_VERSION = 1


def _scrub_text(text: str) -> str:
    """分享前把容易泄露身份的东西抹掉：QQ 号、IP、邮箱、API key。

    额外要抹的词放环境变量 `MEMORY_BRIDGE_REDACT`（逗号分隔），
    比如昵称、真名——写死在代码里的话，这行代码自己就成了泄露源。
    """
    s = str(text or "")
    s = re.sub(r"\b[0-9a-zA-Z._%+-]+@[0-9a-zA-Z.-]+\.[A-Za-z]{2,}\b", "<邮箱>", s)
    s = re.sub(r"\b(?:sk|abk)[-_][A-Za-z0-9._-]{8,}\b", "<API_KEY>", s)
    s = re.sub(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", "<IP>", s)
    s = re.sub(r"(?<![\d.])\d{5,12}(?![\d.])", "<号码>", s)
    for word in (os.environ.get("MEMORY_BRIDGE_REDACT") or "").split(","):
        word = word.strip()
        if word:
            s = s.replace(word, "<昵称>")
    return s


def _scrub_persona(pdef: dict) -> dict:
    out = dict(pdef or {})
    if out.get("system_prompt"):
        out["system_prompt"] = _scrub_text(out["system_prompt"])
    if isinstance(out.get("begin_dialogs"), list):
        out["begin_dialogs"] = [_scrub_text(x) if isinstance(x, str) else x for x in out["begin_dialogs"]]
    return out


def cmd_bundle_export(bot: AstrBot, persona: str, out: str = "", sanitize: bool = False) -> int:
    """把一个人格的定义 + 它的记忆打成一个可搬运的 JSON 包。"""
    if not persona:
        print("需要 --persona 指定要导出哪个人格（先用 personas 子命令看有哪些）。")
        return 1
    memories = _filter_memories(_norm_memories(bot.export_memories()), persona=persona)
    if not memories:
        print(f"人格 {persona} 没有记忆可导出。")
        return 1
    # 同一条记忆在每个会话里各存一份，打包时按内容去重（留重要度高的那条）
    uniq: dict[str, dict] = {}
    for m in memories:
        key = re.sub(r"\s+", "", m["content"]).lower()
        cur = uniq.get(key)
        if cur is None or (m.get("importance") or 0) > (cur.get("importance") or 0):
            uniq[key] = m
    raw_count, memories = len(memories), list(uniq.values())
    if raw_count != len(memories):
        print(f"  按内容去重：{raw_count} 条 -> {len(memories)} 条")
    pdef = next((p for p in bot.personas() if str(p.get("persona_id") or "") == persona), None)
    if pdef is None:
        print(f"  ! 面板里没找到人格定义，只导出记忆（{persona}）")
        pdef = {"persona_id": persona}

    rows = []
    for m in memories:
        row = {
            "id": str(m.get("id") or ""),
            "content": _scrub_text(m["content"]) if sanitize else m["content"],
            "importance": m.get("importance"),
            "created_at": m.get("created_at") or "",
        }
        if not sanitize:
            row["session_id"] = str(m.get("session_id") or "")
            row["persona_id"] = str(m.get("persona_id") or "")
            md = dict(m.get("metadata") or {})
            md.pop("session_key", None)
            row["metadata"] = md
        rows.append(row)

    bundle = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sanitized": bool(sanitize),
        "persona": _scrub_persona(pdef) if sanitize else pdef,
        "sessions": [] if sanitize else sorted({str(m.get("session_id") or "") for m in memories} - {""}),
        "memories": rows,
        "counts": {"memories": len(rows)},
    }
    if out:
        target = Path(out)
    else:
        safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_.-]+", "_", persona)[:40] or "persona"
        target = BUNDLE_DIR / f"{safe}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"人格包：{target}（{len(rows)} 条记忆，脱敏={'是' if sanitize else '否'}）")
    return 0


def _ensure_persona(bot: AstrBot, pdef: dict) -> bool:
    """目标 bot 没有这个人格时，尝试用面板接口建一个；失败只提示不中断。"""
    pid = str(pdef.get("persona_id") or "")
    if not pid:
        return False
    try:
        if any(str(p.get("persona_id") or "") == pid for p in bot.personas()):
            return True
    except ApiError:
        return False
    body = {k: v for k, v in pdef.items() if k in
            ("persona_id", "system_prompt", "begin_dialogs", "tools", "name")}
    body.setdefault("persona_id", pid)
    for path in ("/api/persona/create", "/api/persona/add"):
        try:
            bot.api(path, "POST", body)
            print(f"  已在目标 bot 里创建人格：{pid}")
            return True
        except ApiError:
            continue
    print(f"  ! 目标 bot 没有人格 {pid}，也没能自动创建；记忆仍会导入，但该人格下召回不到。")
    return False


def cmd_bundle_import(bot: AstrBot, path: Path, session: str = "", persona: str = "",
                      dry_run: bool = False) -> int:
    """导入人格包：人格定义（尽力创建）+ 包里的记忆。"""
    if not path.exists():
        print(f"没有这个文件：{path}")
        return 1
    bundle = json.loads(path.read_text(encoding="utf-8-sig"))
    if str(bundle.get("format") or "") != BUNDLE_FORMAT:
        print(f"不是人格包（format={bundle.get('format')!r}）")
        return 1
    pdef = dict(bundle.get("persona") or {})
    target_persona = persona or str(pdef.get("persona_id") or "")
    rows = bundle.get("memories") or []
    if not rows:
        print("包里没有记忆。")
        return 1
    print(f"人格包：{target_persona or '（无人格）'} · {len(rows)} 条记忆"
          + ("（dry-run）" if dry_run else ""))
    if not dry_run:
        _ensure_persona(bot, pdef)

    targets = ([{"session": session, "persona": target_persona}] if session else bot.targets())
    if not targets:
        targets = [{"session": "dsh-workspace", "persona": target_persona}]
    if any(not t.get("persona") for t in targets):
        targets = [dict(t, persona=target_persona) for t in targets]

    rc = 0
    for t in targets:
        payload = [{
            "content": r.get("content") or "",
            "importance": float(r.get("importance") or 0.6),
            "session_id": t["session"],
            "persona_id": t["persona"] or "",
            "metadata": {"source": "persona-bundle", "bundle": Path(path).name},
        } for r in rows if str(r.get("content") or "").strip()]
        try:
            pre = bot.import_memories(payload, dry_run=True)
            print(f"  [{session_label(t['session'])}] planned={pre.get('planned_import_count')} "
                  f"duplicate={pre.get('duplicate_count')}")
            if dry_run:
                continue
            res = bot.import_memories(payload, dry_run=False)
        except ApiError as e:
            print(f"  ! {e}")
            rc = 1
            continue
        print(f"    imported={res.get('imported_count')} skipped={res.get('skipped_duplicate_count')} "
              f"failed={res.get('failed_count')}")
        if res.get("errors"):
            print(f"    ! {json.dumps(res['errors'], ensure_ascii=False)[:300]}")
    return rc


def cmd_note(text: str, importance: float, tags: list[str]) -> int:
    text = _clean_text(text)
    if not text:
        text = _clean_text(sys.stdin.readline())
    if not text:
        print("笔记内容为空。")
        return 1
    DEFAULT_NOTE_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "text": text,
           "importance": importance, "tags": tags}
    with DEFAULT_NOTE_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"已追加到 {DEFAULT_NOTE_FILE}: {text[:80]}")
    return 0


def cmd_recall(bot: AstrBot, query: str, top_k: int) -> int:
    data = bot.recall(query, top_k)
    items = data.get("results") or data.get("memories") or []
    print(f"查询: {query}  -> {len(items)} 条")
    for it in items:
        if isinstance(it, dict):
            print(f"  - [{_fmt_importance(it.get('importance') or it.get('score'))}] {str(it.get('content') or it.get('text'))[:160]}")
        else:
            print(f"  - {str(it)[:160]}")
    return 0


def _webchat_args(umo: str) -> tuple[str, str]:
    """webchat:FriendMessage:webchat!<user>!<session> -> (user, session)。"""
    tail = umo.split("webchat!", 1)[-1] if "webchat!" in umo else umo
    if "!" in tail:
        user, sess = tail.split("!", 1)
        return user, sess
    return "panel", tail


def cmd_chat(bot: AstrBot, message: str, session: str, username: str) -> int:
    reply = bot.chat(message, session=session, username=username)
    print(reply or "（空回复）")
    return 0


def cmd_selftest(bot: AstrBot, token: str = "") -> int:
    """端到端自检：DSH 写一条带随机令牌的记忆，再让 bot 在网页会话里复述它。"""
    token = token.strip().upper() or ("DSH-" + hashlib.sha1(str(time.time()).encode()).hexdigest()[:6].upper())
    fact = f"自检：DSH 工作区记忆桥的令牌是 {token}。"
    targets = bot.targets()
    written = 0
    for t in targets:
        mem = [{
            "content": fact,
            "importance": 0.3,
            "session_id": t["session"],
            "persona_id": t["persona"] or "",
            "metadata": {"source": "dsh-selftest", "ts": datetime.now().isoformat(timespec="seconds")},
        }]
        try:
            res = bot.import_memories(mem, dry_run=False)
        except ApiError as e:
            print(f"  写入 {session_label(t['session'])} 失败：{e}")
            continue
        ok = int(res.get("imported_count") or 0) + int(res.get("skipped_duplicate_count") or 0)
        written += ok
        print(f"  写入 {session_label(t['session'])}: imported={res.get('imported_count')} "
              f"skipped={res.get('skipped_duplicate_count')} failed={res.get('failed_count')}")
    if not written:
        print("自检失败：一条记忆都没写进去。")
        return 1

    web = next((t for t in targets if t["session"].startswith("webchat:")), None)
    if not web:
        print("没有网页调试会话，跳过对话验证（写入已完成）。")
        _cleanup_selftest(bot, token)
        return 0
    user, sess = _webchat_args(web["session"])
    print(f"在 {session_label(web['session'])} 里问 bot ...")
    try:
        reply = bot.chat("刚才的记忆里，DSH 自检令牌是多少？只回令牌本身。", session=sess, username=user)
    except ApiError as e:
        print(f"  对话失败：{e}")
        _cleanup_selftest(bot, token)
        return 1
    print(f"  bot: {reply[:200]}")
    ok = token.replace(" ", "") in reply.upper().replace(" ", "")
    _cleanup_selftest(bot, token)
    if ok:
        print(f"自检通过：bot 从长期记忆里想起了 {token}")
        return 0
    print(f"自检未通过：回复里没有 {token}")
    return 1


def _cleanup_selftest(bot: AstrBot, token: str) -> None:
    """把自检写进去的临时记忆删掉，免得污染长期记忆。"""
    try:
        rows = bot.list_memories(500)
    except ApiError:
        return
    ids = [m.get("id") or m.get("memory_id") for m in rows
           if token in str(m.get("text") or m.get("content") or "")]
    ids = [i for i in ids if i]
    if not ids:
        return
    try:
        bot.delete_memories(ids)
        print(f"  已清理 {len(ids)} 条自检记忆。")
    except ApiError as e:
        print(f"  清理自检记忆失败（不影响使用）：{e}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="astrbot_bridge", description="AstrBot <-> DSH 记忆桥")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="查看双向状态")

    pp = sub.add_parser("pull", help="从 AstrBot 拉取记忆/对话/人设（可只拉一个人格）")
    pp.add_argument("--chats", type=int, default=120)
    pp.add_argument("--persona", default="", help="只拉这个人格的记忆，默认全部")

    sub.add_parser("personas", help="列出人格维度的记忆条数与会话")

    pt = sub.add_parser("transfer", help="把导出文件里选中的记忆传送到目标会话")
    pt.add_argument("--file", default=str(EXPORT_DIR / "memories.json"))
    pt.add_argument("--ids", default="", help="逗号分隔的记忆 id，只传这几条")
    pt.add_argument("--persona", default="", help="只传这个人格的记忆")
    pt.add_argument("--session", default="", help="只写到这个会话（默认所有活跃会话）")
    pt.add_argument("--to-persona", default="", help="写入时用的人格，默认 bot 默认人格")
    pt.add_argument("--dry-run", action="store_true")

    pb = sub.add_parser("bundle-export", help="把一个人格 + 它的记忆打成可搬运的 JSON 包")
    pb.add_argument("--persona", required=True)
    pb.add_argument("--out", default="")
    pb.add_argument("--sanitize", action="store_true", help="去掉会话 id 与元数据，方便分享")

    bi = sub.add_parser("bundle-import", help="导入人格包（人格定义 + 记忆）")
    bi.add_argument("--file", required=True)
    bi.add_argument("--session", default="", help="只写到这个会话（默认所有活跃会话）")
    bi.add_argument("--persona", default="", help="覆盖包里的人格 id")
    bi.add_argument("--dry-run", action="store_true")

    pu = sub.add_parser("push", help="把 dsh-notes.jsonl 推送到 AstrBot 长期记忆")
    pu.add_argument("--file", default=str(DEFAULT_NOTE_FILE))
    pu.add_argument("--dry-run", action="store_true")
    pu.add_argument("--limit", type=int, default=200)
    pu.add_argument("--session", default="", help="只推到指定会话（unified_msg_origin）")
    pu.add_argument("--persona", default="", help="指定人格 id，默认用 AstrBot 的全局默认人格")

    pn = sub.add_parser("note", help="追加一条 DSH 笔记（不带内容时从标准输入读一行）")
    pn.add_argument("text", nargs="?", default="")
    pn.add_argument("--importance", type=float, default=0.6)
    pn.add_argument("--tags", default="")

    pr = sub.add_parser("recall", help="在 AstrBot 侧测试召回")
    pr.add_argument("query")
    pr.add_argument("--top-k", type=int, default=5)

    pc = sub.add_parser("chat", help="以网页调试会话跟 bot 说一句话")
    pc.add_argument("message")
    pc.add_argument("--session", default="panel-debug")
    pc.add_argument("--username", default="panel")

    ps = sub.add_parser("selftest", help="端到端自检：写入记忆并让 bot 复述")
    ps.add_argument("--token", default="")

    pa = sub.add_parser("activity", help="把 DSH 工作区里做过的事摘要后回传 AstrBot")
    pa.add_argument("--dry-run", action="store_true")
    pa.add_argument("--days", type=int, default=30)
    pa.add_argument("--limit", type=int, default=8)

    sub.add_parser("sync", help="pull + push")

    a = p.parse_args(argv)
    bot = AstrBot(load_config())
    try:
        if a.cmd == "status":
            return cmd_status(bot)
        if a.cmd == "pull":
            return cmd_pull(bot, a.chats, a.persona)
        if a.cmd == "personas":
            return cmd_personas(bot)
        if a.cmd == "transfer":
            ids = [i.strip() for i in a.ids.split(",") if i.strip()]
            return cmd_transfer(bot, Path(a.file), ids, a.persona, a.session, a.to_persona, a.dry_run)
        if a.cmd == "bundle-export":
            return cmd_bundle_export(bot, a.persona, a.out, a.sanitize)
        if a.cmd == "bundle-import":
            return cmd_bundle_import(bot, Path(a.file), a.session, a.persona, a.dry_run)
        if a.cmd == "push":
            return cmd_push(bot, Path(a.file), a.dry_run, a.limit, a.session, a.persona)
        if a.cmd == "activity":
            return cmd_activity(bot, a.dry_run, a.days, a.limit)
        if a.cmd == "note":
            tags = [t.strip() for t in a.tags.split(",") if t.strip()]
            return cmd_note(a.text, a.importance, tags)
        if a.cmd == "recall":
            return cmd_recall(bot, a.query, a.top_k)
        if a.cmd == "chat":
            return cmd_chat(bot, a.message, a.session, a.username)
        if a.cmd == "selftest":
            return cmd_selftest(bot, a.token)
        if a.cmd == "sync":
            rc = cmd_pull(bot, 120)
            rc2 = cmd_push(bot, DEFAULT_NOTE_FILE, False, 200)
            rc3 = cmd_activity(bot, False, 30, 8)
            return rc or rc2 or rc3
    except ApiError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
