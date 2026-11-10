"""Plain-language descriptions of well-known Hermes home paths.

Deterministic "what is this?" help for the Explorer. Descriptions explain
purpose only; they never declare anything safe to delete.
"""
from __future__ import annotations

_EXACT: dict[str, str] = {
    "": "Your Hermes home folder for this profile.",
    "state.db": "Session database: every conversation, message and the search index. Dead space inside it can be reclaimed by compaction (see Cleanup).",
    "state.db-wal": "Write-ahead log for the session database. Shrinks automatically when Hermes checkpoints it.",
    "state.db-shm": "Shared-memory index for the session database while Hermes is running.",
    "attachments": "Files you attached to chats or that were saved from conversations.",
    "outputs": "Files produced by tools and agents.",
    "images": "Images generated or saved by Hermes.",
    "tools": "Package store: bundled Python/Node runtimes and tools installed by Hermes's package manager.",
    "webui": "Web UI build and its assets.",
    "installs": "Hermes dependency environments managed by `hermes pm`. Old generations can be garbage-collected.",
    "node": "Node.js runtime used by Hermes tools.",
    "bin": "Helper executables installed by Hermes.",
    "lsp": "Language servers used for code intelligence.",
    "runtimes": "Optional runtimes downloaded on demand.",
    "tts": "Text-to-speech voices and models.",
    "cache": "Rebuildable caches: package downloads, temporary scratch space and web fetches.",
    "cache/uv": "Python package download cache. Rebuildable; packages are re-downloaded when needed.",
    "cache/scratch": "Temporary working files. Hermes deletes entries that have been idle for 24 hours.",
    "cache/web": "Cached web pages and fetch results.",
    "cache/spillover": "Large tool outputs that did not fit in a conversation.",
    "cache/terminal-output": "Saved output from long terminal commands.",
    "cache/exec": "Saved output from code execution.",
    "cache/delegation": "Transcripts of delegated sub-agent runs.",
    "workspaces": "Project folders created by agents and boards. May contain your work.",
    "sessions": "Gateway routing index and per-session transcript files.",
    "logs": "Hermes log files. They rotate automatically.",
    "backups": "Backups created by Hermes tools before risky changes.",
    "state-snapshots": "Snapshots of Hermes state taken before migrations or experiments.",
    ".curator_backups": "Backups made by the skill curator before it edits skills.",
    "checkpoints": "Rollback history for file edits (/rollback). Managed by `hermes checkpoints`.",
    "skills": "Installed and learned skills.",
    "plugins": "Installed Hermes plugins.",
    "desktop-plugins": "Desktop/Webapp UI plugins.",
    "cron": "Scheduled job definitions and their output.",
    "kanban": "Kanban boards, tasks and worker logs.",
    "memories": "Long-term memory notes.",
    "storage-utility": "This Storage plugin's own small state (settings, snapshots, Trash receipts).",
    "config.yaml": "Main Hermes settings.",
    "designs": "Design artifacts produced by Hermes.",
    "projects": "Named multi-folder projects.",
}

_PREFIX: tuple[tuple[str, str], ...] = (
    ("installs/", "Part of a Hermes dependency environment managed by `hermes pm`."),
    ("tools/", "Part of Hermes's package store."),
    ("cache/uv/", "Part of the Python package download cache (rebuildable)."),
    ("cache/scratch/", "Temporary scratch entry; removed automatically after 24 idle hours."),
    ("attachments/", "A file or folder saved from a conversation."),
    ("outputs/", "Output produced by a tool or agent."),
    ("workspaces/", "An agent/project workspace. May contain your work."),
    ("backups/", "A Hermes backup."),
    ("state-snapshots/", "A Hermes state snapshot."),
    ("logs/", "A Hermes log file."),
    ("skills/", "Part of an installed skill."),
    ("kanban/", "Kanban board data or worker logs."),
)


def describe(relative: str) -> str | None:
    """Return a short description for a home-relative path, if known."""
    key = relative.strip("/")
    if key in _EXACT:
        return _EXACT[key]
    for prefix, text in _PREFIX:
        if key.startswith(prefix):
            return text
    return None
