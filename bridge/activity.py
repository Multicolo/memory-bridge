#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 DSH 那边的"行为"（会话记录）压缩成一两条可推送的记忆。

DSH 的会话存在 ~/.dsh/sessions/<workspace>/session-*/session.v4.jsonl.zstd，
每条会话是一串 JSON 事件。这里只取对长期记忆有用的事实：
用户在做什么、做成了什么、碰了哪些文件，然后写成一段人话。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

try:  # Python 3.14+
    from compression import zstd  # type: ignore
except Exception:  # pragma: no cover
    zstd = None

if zstd is None:  # 3.12/3.13 没有标准库 zstd，用 zstandard 包（打包进 exe）
    try:
        import zstandard as _zstandard

        class _ZstdShim:
            """把 zstandard 包包装成和 compression.zstd 一样的 open()。"""

            @staticmethod
            def open(path, mode="rb"):  # noqa: A003
                return _zstandard.open(path, mode)

        zstd = _ZstdShim()  # type: ignore[assignment]
    except Exception:  # pragma: no cover
        zstd = None

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_FILE = Path(__file__).resolve().parent / ".activity-cache.json"
_PERSIST: dict[str, dict] = {}


def _load_persist() -> None:
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _PERSIST.update(data)
    except Exception:
        pass


def _save_persist() -> None:
    try:
        if len(_PERSIST) > 60:
            keep = sorted(_PERSIST.items(), key=lambda kv: kv[1].get("mtime") or 0, reverse=True)[:60]
            _PERSIST.clear()
            _PERSIST.update(dict(keep))
        tmp = _CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_PERSIST, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_FILE)
    except Exception:
        pass


_load_persist()


def sessions_root() -> Path:
    return Path.home() / ".dsh" / "sessions"


def _unpack(path: Path) -> list[dict]:
    raw: bytes
    if zstd is not None:
        # 会话文件是「很多个小 zstd 帧拼起来」的，一次性 zstd.decompress()
        # 会逐帧慢慢解（9MB 要 30 秒）；ZstdFile 流式读只要 0.4 秒。
        try:
            with zstd.open(path, "rb") as fh:
                raw = fh.read()
        except Exception:
            raw = path.read_bytes()
    else:
        raw = path.read_bytes()
    text = raw.decode("utf-8", "replace")
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _text_parts(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for p in content:
        if isinstance(p, dict) and p.get("type") in ("text", None) and p.get("text"):
            parts.append(str(p["text"]))
    return "\n".join(parts).strip()


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _hms(ms) -> str:
    try:
        return datetime.fromtimestamp(float(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def read_session(path: Path) -> dict | None:
    """解析一条会话文件；解析不出来就返回 None。"""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    hit = _CACHE.get(key)
    if hit and hit[0] == st.st_mtime and hit[1].get("_size") == st.st_size:
        return hit[1]

    try:
        events = _unpack(path)
    except Exception:
        return None
    if not events:
        return None

    meta = next((e for e in events if e.get("type") == "session"), {})
    title = ""
    for e in events:
        if e.get("type") == "session/title":
            title = (e.get("data") or {}).get("title") or title

    asks: list[str] = []
    last_assistant = ""
    files: list[str] = []
    tools: dict[str, int] = {}
    first_ms = meta.get("createdAt") or (events[0].get("time") if events else None)
    last_ms = events[-1].get("time") if events else None

    for e in events:
        t = e.get("type")
        data = e.get("data") or {}
        if t == "user/message":
            src = (data.get("source") or {}).get("kind")
            if src == "user":
                text = _text_parts(data.get("content"))
                if text:
                    asks.append(text)
        elif t == "assistant/message":
            msg = data.get("message") or {}
            text = _text_parts(msg.get("content"))
            if text:
                last_assistant = text
        elif t == "tool/call":
            name = str(data.get("name") or "?")
            tools[name] = tools.get(name, 0) + 1
            if name in ("write", "edit", "read"):
                try:
                    args = json.loads(data.get("arguments") or "{}")
                except Exception:
                    args = {}
                fp = args.get("file_path") or args.get("path")
                if fp:
                    files.append(str(fp))

    info = {
        "_size": st.st_size,
        "id": str(meta.get("id") or path.parent.name),
        "cwd": str(meta.get("cwd") or ""),
        "title": title or (asks[0][:32] if asks else "（未命名会话）"),
        "start": _hms(first_ms),
        "end": _hms(last_ms),
        "asks": asks,
        "last_assistant": last_assistant,
        "files": files,
        "tools": tools,
        "events": len(events),
        "mtime": st.st_mtime,
    }
    _CACHE[key] = (st.st_mtime, info)
    return info


def _short_files(files: list[str], limit: int = 5) -> list[str]:
    seen: list[str] = []
    for f in files:
        name = Path(f).name
        if name and name not in seen:
            seen.append(name)
        if len(seen) >= limit:
            break
    return seen


def _plain(text: str) -> str:
    """去掉 markdown 记号，留下能读的人话。"""
    out = []
    for ch in str(text or ""):
        if ch in "#*`>|[]()":
            out.append(" ")
        else:
            out.append(ch)
    return " ".join("".join(out).split())


def _snip(text: str, limit: int) -> str:
    """截到最近的句子边界，避免半句话结尾。"""
    text = re.sub(r"^先说结论[:：]\s*", "", str(text or "").strip())
    text = " ".join(text.split())
    if len(text) <= limit:
        return text.strip("。.；; ")
    cut = text[:limit]
    idx = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind(". "))
    if idx >= 30:
        out = cut[:idx].strip("。.；; ")
    else:
        out = cut.rstrip("，,、;；。. ") + "…"
    # 去掉结尾孤零零的序号（“…没问题。 2”这种）
    out = re.sub(r"[\s。.]*\d+[。.]?$", "", out)
    return out.strip(" 。.；;")


def digest_text(info: dict, workspace: Path | None = None) -> str:
    """把一条会话写成人话，控制在 300 字以内。"""
    asks = info.get("asks") or []
    title = _snip(_plain(info.get("title") or ""), 28) or "（未命名会话）"
    when = info.get("end") or info.get("start") or ""

    parts = [f"【DSH 活动】{when}《{title}》。"]
    if asks:
        head = _snip(_plain(asks[0]), 130)
        seg = f"用户让 DSH：{head}。"
        if len(asks) > 1:
            seg += f"之后又交代了 {len(asks) - 1} 件事。"
        parts.append(seg)
    done = _snip(_plain(info.get("last_assistant") or ""), 110)
    if done:
        parts.append(f"结果：{done}。")
    fs = _short_files(info.get("files") or [], 4)
    if fs:
        parts.append("涉及：" + "、".join(fs) + "。")
    text = "".join(parts)
    return text if len(text) <= 420 else text[:417] + "…"


def collect(workspace: Path, days: int = 30, limit: int = 5) -> list[dict]:
    """收集这个工作区最近的 DSH 会话，按时间新到旧。

    解析过的大会话会持久化到 .activity-cache.json，重开程序也不用重算。
    """
    root = sessions_root()
    if not root.exists():
        return []
    cutoff = time.time() - days * 86400
    found: list[tuple[float, Path]] = []
    for p in root.rglob("session.v4.jsonl.zstd"):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff:
            found.append((st.st_mtime, p))
    found.sort(reverse=True)

    out: list[dict] = []
    ws = str(workspace).lower()
    changed = False
    for m, p in found:
        key = str(p)
        try:
            st = p.stat()
        except OSError:
            continue
        hit = _CACHE.get(key)
        hint_persist = _PERSIST.get(key)
        if hit and hit[0] == st.st_mtime and hit[1].get("_size") == st.st_size:
            info = hit[1]
        elif hint_persist and hint_persist.get("mtime") == st.st_mtime and hint_persist.get("size") == st.st_size:
            out.append(dict(hint_persist["row"]))
            if len(out) >= limit:
                break
            continue
        else:
            info = read_session(p)
            changed = True
        if not info:
            continue
        if info.get("cwd") and info["cwd"].lower() != ws:
            continue
        text = digest_text(info, workspace)
        row = {
            "id": info["id"],
            "title": info["title"],
            "start": info["start"],
            "end": info["end"],
            "asks": len(info["asks"]),
            "events": info["events"],
            "tools": sum((info.get("tools") or {}).values()),
            "files": _short_files(info.get("files") or []),
            "text": text,
            "hash": hashlib.sha1(text.encode("utf-8")).hexdigest()[:16],
        }
        _PERSIST[key] = {"mtime": st.st_mtime, "size": st.st_size, "row": row}
        out.append(row)
        if len(out) >= limit:
            break

    if changed:
        _save_persist()
    return out


def preview(workspace: Path, days: int = 30, limit: int = 5) -> list[dict]:
    """给 UI 用的精简版（collect 已经只保留必要字段）。"""
    return collect(workspace, days, limit)


# ---------------------------------------------------------------- 挑着同步
# 一次性把整个工作区所有会话都推上去，会往云端灌一堆不相干的记忆。
# 下面这几个函数是给界面用的：先列出「哪些工作区 / 哪些会话」，用户勾了再解析。


def _decode_dir(name: str) -> str:
    """`--C-Users-me-Documents-ws--` → `C:\\Users\\me\\Documents\\ws`。

    目录名里 `-` 同时充当路径分隔符，所以这只是个近似值；真值优先用
    read_meta() 从会话文件头里读出来的 cwd。
    """
    s = name.strip("-").replace("-", "\\")
    if len(s) >= 2 and s[1] == "\\" and s[0].isalpha():
        s = s[0] + ":" + s[1:]
    return s


_META: dict[str, tuple[float, dict]] = {}


def read_meta(path: Path) -> dict:
    """只读会话文件开头那几十 KB，拿到 id / cwd / 标题，用来做列表。

    整份解析动辄几 MB，列表里只为了显示一行标题不值得；文件头的 zstd 帧
    里就有 session 事件。
    """
    try:
        st = path.stat()
    except OSError:
        return {}
    hit = _META.get(str(path))
    if hit and hit[0] == st.st_mtime:
        return hit[1]
    meta: dict = {}
    try:
        if zstd is not None:
            # zstd 文件是「很多小帧拼起来」的，read() 会一次只返回一帧，
            # 所以要循环读到够长（或读到文件尾）为止。
            buf = bytearray()
            with zstd.open(path, "rb") as fh:
                while len(buf) < 256 * 1024:
                    chunk = fh.read(64 * 1024)
                    if not chunk:
                        break
                    buf.extend(chunk)
            head = bytes(buf)
        else:
            head = path.read_bytes()[: 256 * 1024]
        for line in head.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") == "session":
                meta["id"] = str(e.get("id") or "")
                d = e.get("data") or {}
                meta["cwd"] = str(d.get("cwd") or e.get("cwd") or "")
            elif e.get("type") == "session/title":
                meta["title"] = str((e.get("data") or {}).get("title") or "")
            elif e.get("type") == "user/message" and not meta.get("first_ask"):
                src = (e.get("data") or {}).get("source") or {}
                if src.get("kind") == "user":
                    txt = _text_parts((e.get("data") or {}).get("content"))
                    if txt:
                        meta["first_ask"] = _clip(txt, 40)
            if meta.get("cwd") and meta.get("title"):
                break
    except Exception:
        pass
    _META[str(path)] = (st.st_mtime, meta)
    return meta


def list_workspaces() -> list[dict]:
    """列出 ~/.dsh/sessions 下的工作区（新到旧）。"""
    root = sessions_root()
    if not root.exists():
        return []
    out = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        files = list(d.glob("session-*/session.v4.jsonl.zstd"))
        if not files:
            continue
        newest = max(files, key=lambda p: p.stat().st_mtime)
        st = newest.stat()
        meta = read_meta(newest)
        out.append({
            "key": d.name,
            "cwd": meta.get("cwd") or _decode_dir(d.name),
            "count": len(files),
            "last": _hms(st.st_mtime * 1000),
            "mtime": st.st_mtime,
        })
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def list_sessions(workspace_key: str | None = None, days: int = 365,
                  limit: int = 300) -> list[dict]:
    """列出会话（不解析大文件；标题只用之前的缓存，没有就显示成「(未解析)」）。"""
    root = sessions_root()
    if not root.exists():
        return []
    cutoff = time.time() - days * 86400
    if workspace_key:
        pattern = f"{workspace_key}/session-*/session.v4.jsonl.zstd"
    else:
        pattern = "*/session-*/session.v4.jsonl.zstd"
    rows = []
    for p in root.glob(pattern):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        cached = (_CACHE.get(str(p)) or (0, {}))[1] or (_PERSIST.get(str(p)) or {}).get("row") or {}
        meta = read_meta(p) if not cached.get("title") else {}
        rows.append({
            "key": str(p),
            "workspace": workspace_key or p.parent.parent.name,
            "id": p.parent.name.replace("session-", ""),
            "title": cached.get("title") or meta.get("title") or meta.get("first_ask") or "",
            "end": cached.get("end") or "",
            "when": _hms(st.st_mtime * 1000),
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


def build_rows(paths: list[str]) -> list[dict]:
    """把选中的会话文件解析成要推送的行（和 collect 的行同构）。"""
    out = []
    for key in paths or []:
        p = Path(key)
        if not p.exists():
            continue
        info = read_session(p)
        if not info:
            continue
        text = digest_text(info)
        out.append({
            "id": info["id"],
            "title": info["title"],
            "start": info["start"],
            "end": info["end"],
            "asks": len(info["asks"]),
            "events": info["events"],
            "tools": sum((info.get("tools") or {}).values()),
            "files": _short_files(info.get("files") or []),
            "text": text,
            "hash": hashlib.sha1(text.encode("utf-8")).hexdigest()[:16],
        })
    _save_persist()
    return out
