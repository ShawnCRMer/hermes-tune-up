"""Which chat is this about? Links cleanup items to the conversation that made or used them.

People don't know chats by ID. For each item we look for the chat that created or mentioned it
and return its title, date and the first thing the user asked, read-only:
  * web-chat records:     webui/sessions/<id>.json (title) or the session with that id;
  * saved tool output:    cache/spillover/call_<id>.txt  -> the message with that tool-call id;
  * anything else:        the earliest chat whose text or tool calls mention the item's name,
                          via the chat-history search index (messages_fts), confirmed by an exact
                          substring match so a common word never links the wrong chat.
Results are cached per item (path + modified time). Lookups stop after a time budget; items
not reached simply show no chat. Nothing is sent anywhere.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import state
from .scope import Scope

CACHE = "chat-links.json"
BUDGET_SECONDS = 8.0
_GENERIC = {"tmp", "temp", "out", "output", "outputs", "file", "files", "data", "image", "images", "attachments",
            "cache", "backup", "backups", "log", "logs", "txt", "json", "png", "jpg", "jpeg", "md", "pdf", "zip",
            "html", "sessions", "webui", "run", "journal", "workspaces", "the", "and"}


def _connect(scope: Scope) -> sqlite3.Connection | None:
    db = scope.home / "state.db"
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        conn.execute("select 1 from messages_fts limit 1")
        return conn
    except sqlite3.Error:
        return None


def _session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    row = conn.execute("select title, display_name, started_at from sessions where id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    first = conn.execute("select content from messages where session_id = ? and role = 'user' and content is not null "
                         "order by timestamp limit 1", (session_id,)).fetchone()
    about = _clean(first[0]) if first else None
    title = row[0] or row[1] or (about[:60] if about else None)
    return {"sessionId": session_id, "title": title, "startedAt": row[2], "about": about}


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"\[(?:CONTEXT|IMPORTANT|OUT-OF-BAND)[^\]]*\]", " ", str(text))
    text = re.sub(r"@(?:file|image):\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:157] + "…") if len(text) > 160 else (text or None)


def _query_for(name: str) -> tuple[str, str] | None:
    """FTS phrase built from the name's words, and the exact needle that must appear."""
    stem = name.rsplit("/", 1)[-1]
    words = [w for w in re.split(r"[^A-Za-z0-9]+", stem) if w]
    useful = [w for w in words if w.lower() not in _GENERIC and len(w) >= 3]
    if not useful or (len(useful) == 1 and len(useful[0]) < 6):
        return None  # too generic to link reliably
    return '"' + " ".join(words) + '"', stem


def _by_mention(conn: sqlite3.Connection, name: str) -> str | None:
    query = _query_for(name)
    if query is None:
        return None
    phrase, needle = query
    try:
        row = conn.execute(
            "select m.session_id from messages_fts f join messages m on m.id = f.rowid "
            "where messages_fts match ? and (instr(coalesce(m.content, ''), ?) > 0 or instr(coalesce(m.tool_calls, ''), ?) > 0) "
            "order by m.timestamp limit 1", (phrase, needle, needle)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _webui(scope: Scope, run_id: str, conn: sqlite3.Connection | None) -> dict[str, Any] | None:
    meta = scope.home / "webui" / "sessions" / f"{run_id}.json"
    try:
        data = json.loads(meta.read_text())
        started = float(data.get("created_at") or 0) or None
        return {"sessionId": run_id, "title": data.get("title"), "startedAt": started, "about": None, "app": "web chat"}
    except (OSError, ValueError, TypeError):
        pass
    return _session(conn, run_id) if conn else None


def _resolve(scope: Scope, conn: sqlite3.Connection | None, path: str) -> dict[str, Any] | None:
    parts = path.strip("/").split("/")
    if "_run_journal" in parts:
        index = parts.index("_run_journal")
        if index + 1 < len(parts):
            return _webui(scope, parts[index + 1], conn)
    if conn is None:
        return None
    name = parts[-1]
    match = re.match(r"(call_[A-Za-z0-9]+)", name)
    if match:
        # The assistant message that made the call carries its id in tool_calls (indexed); never scan.
        session_id = _by_mention(conn, match.group(1))
        return _session(conn, session_id) if session_id else None
    session_id = _by_mention(conn, name)
    if session_id is None and len(parts) > 2:
        session_id = _by_mention(conn, "/".join(parts[-2:]))
    return _session(conn, session_id) if session_id else None


def link(scope: Scope, items: list[dict[str, Any]], budget: float = BUDGET_SECONDS) -> dict[str, dict[str, Any] | None]:
    """{item path: chat or None}. Cached; stops after `budget` seconds (unreached items are omitted)."""
    cache = state.read_json(scope, CACHE, {})
    cache = cache if isinstance(cache, dict) else {}
    out: dict[str, dict[str, Any] | None] = {}
    conn = None
    started = time.monotonic()
    changed = False
    try:
        for item in items:
            path = str(item.get("path") or item.get("root") or "")
            if not path:
                continue
            key = f"{path}|{item.get('modifiedAt')}"
            if key in cache:
                out[path] = cache[key]
                continue
            if time.monotonic() - started > budget:
                break
            if conn is None:
                conn = _connect(scope) or False
            result = _resolve(scope, conn or None, path)
            cache[key] = result
            out[path] = result
            changed = True
    finally:
        if conn:
            conn.close()
    if changed:
        if len(cache) > 5000:
            cache = dict(list(cache.items())[-4000:])
        state.write_json(scope, CACHE, cache)
    return out


_KINDS = [
    (r"(^|/)_run_journal/", "Step-by-step record of a conversation in the separate web chat"),
    (r"(^|/)cache/spillover/", "A long tool result saved from a chat"),
    (r"(^|/)cache/terminal-output/", "Saved output of a command Hermes ran"),
    (r"(^|/)cache/web/browser-snapshot", "Snapshot of a web page Hermes visited"),
    (r"(^|/)cache/generated/", "Pictures Hermes generated"),
    (r"(^|/)cache/", "Saved result from one of Hermes's tools"),
    (r"(^|/)attachments/upload_[^/]*\.(png|jpe?g|gif|webp|heic)$", "Picture you sent in a chat"),
    (r"(^|/)images/upload_", "Picture you sent in a chat"),
    (r"(^|/)attachments/Pasted content", "Text you pasted into a chat"),
    (r"(^|/)attachments/[^/]+\.(zip|tar|gz|tgz)$", "Download package Hermes made"),
    (r"(^|/)attachments/[^/]+\.(pdf|docx?|xlsx?|pptx?|csv)$", "Document from a chat"),
    (r"(^|/)attachments/[^/]+$", "Files Hermes saved for you during a chat"),
    (r"(^|/)outputs/", "Something Hermes produced for you (a report, audio, export…)"),
    (r"(^|/)state-snapshots/", "Safety copy of Hermes's settings before a change"),
    (r"(^|/)backups/", "Backup Hermes made before a change"),
    (r"(^|/)workspaces/", "Work folder for a project"),
    (r"before-[^/]*-save|\.bak$", "Older saved copy (\"before I changed it\")"),
    (r"node_modules|\.venv|/venv/|__pycache__|/dist/|/build/", "Installed building blocks a project can download again"),
]


def what_is(path: str) -> str | None:
    for pattern, text in _KINDS:
        if re.search(pattern, path):
            return text
    return None
