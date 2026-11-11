#!/usr/bin/env python3
"""Persistent session tabs per repo: which tabs exist, their profiles and their session ids.

Each repo has one JSON file under `$WS_STATE/tabs/`, named after the sha1 of its real path:

    {"repo": "<realpath>", "active": "<tab id>",
     "tabs": [{"id": "<stable id>", "profile": "<name>", "session": "<id>"}]}

The list order is the tab order in the strip. The id is stable across a tmux server restart, so it
is also the `WS_SESSION_TAB` value exported to the session and the suffix of
`$WS_STATE/tabs/session-<tab>`, where the Claude hook and the OpenCode plugin record the real
session id. `session_for` prefers that file (the live id) and falls back to the JSON value.

A missing or corrupt state file reads as no tabs, so one bad write never hides the repo's tabs.
This module is stdlib-only and also runs as a small CLI (see `main`), which `bin/workspace` uses
from shell code.
"""
import contextlib
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:     # no fcntl (Windows): updates are not serialized
    fcntl = None


def _real(repo):
    """Normalized real path of a repo, so the same directory always maps to the same file."""
    return os.path.realpath(os.path.expanduser(str(repo)))


def state_dir():
    """Directory that holds the per-repo tab files (overridable with WS_STATE)."""
    return Path(os.environ.get("WS_STATE", Path.home() / ".cache" / "workspace")) / "tabs"


def repo_key(repo):
    """sha1 hex digest of the normalized repo path, used as the state file name."""
    return hashlib.sha1(_real(repo).encode("utf-8")).hexdigest()


def state_path(repo):
    """State file for one repo."""
    return state_dir() / f"{repo_key(repo)}.json"


def _empty(repo):
    return {"repo": _real(repo), "active": "", "tabs": []}


def read(repo):
    """Tab state of a repo; a missing or corrupt file reads as a fresh empty state."""
    data = _empty(repo)
    try:
        loaded = json.loads(state_path(repo).read_text(encoding="utf-8"))
    except Exception:
        return data
    if not isinstance(loaded, dict):
        return data
    tabs = loaded.get("tabs")
    data["tabs"] = _normalize_tabs(tabs) if isinstance(tabs, list) else []
    active = loaded.get("active")
    data["active"] = str(active) if isinstance(active, str) else ""
    return data


def _normalize_tabs(tabs):
    """Tabs with a non-empty id, as fresh dicts; duplicate ids keep their first position."""
    result, seen = [], set()
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        tab_id = str(tab.get("id", ""))
        if not tab_id or tab_id in seen:
            continue
        seen.add(tab_id)
        result.append({
            "id": tab_id,
            "profile": str(tab.get("profile", "")),
            "session": str(tab.get("session", "")),
        })
    return result


def _lock():
    """Exclusive lock while a read-modify-write happens (several tmux commands may land together)."""
    state_dir().mkdir(parents=True, exist_ok=True)
    return open(state_dir() / ".lock", "a")


def _write_unlocked(data):
    """Writes the state atomically (temp file + replace), so readers never see a partial file."""
    path = state_path(data.get("repo", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write(repo, data):
    """Replaces the state of a repo, keeping only the documented keys."""
    _write_unlocked({
        "repo": _real(repo),
        "active": str(data.get("active", "")),
        "tabs": _normalize_tabs(data.get("tabs") or []),
    })


@contextlib.contextmanager
def _update(repo):
    with _lock() as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = read(repo)
        yield data
        _write_unlocked(data)


def new_id():
    """A fresh stable tab id."""
    return str(uuid.uuid4())


def _tab_of(data, tab_id):
    for tab in data["tabs"]:
        if tab["id"] == str(tab_id):
            return tab
    return None


def _session_file(tab_id):
    return state_dir() / f"session-{tab_id}"


def _effective_session(data, tab_id):
    """Session id of a tab: the live `session-<tab>` file first, then the JSON value."""
    try:
        current = _session_file(tab_id).read_text(encoding="utf-8").strip()
    except OSError:
        current = ""
    if current:
        return current
    tab = _tab_of(data, tab_id)
    return tab["session"] if tab is not None else ""


def add(repo, tab_id, profile, session=None):
    """Adds a tab (or updates its profile) and makes it active.

    `session` is optional: with None, an existing tab keeps its recorded session id; with a string,
    it replaces it (an empty string clears it)."""
    tab_id = str(tab_id)
    with _update(repo) as data:
        tab = _tab_of(data, tab_id)
        if tab is None:
            tab = {"id": tab_id, "profile": "", "session": ""}
            data["tabs"].append(tab)
        tab["profile"] = str(profile)
        if session is not None:
            tab["session"] = str(session)
        data["active"] = tab_id


def remove(repo, tab_id):
    """Removes a tab and its live session file; the first remaining tab (or "") becomes active."""
    tab_id = str(tab_id)
    try:
        _session_file(tab_id).unlink()
    except OSError:
        pass
    with _update(repo) as data:
        data["tabs"] = [tab for tab in data["tabs"] if tab["id"] != tab_id]
        if data["active"] == tab_id:
            data["active"] = data["tabs"][0]["id"] if data["tabs"] else ""


def set_active(repo, tab_id):
    """Marks a tab as the active one."""
    with _update(repo) as data:
        data["active"] = str(tab_id)


def session_for(repo, tab_id):
    """Effective session id of a tab: the live `session-<tab>` file when non-empty, else the state."""
    return _effective_session(read(repo), str(tab_id))


def set_session(repo, tab_id, session):
    """Stores the session id of an existing tab (see `session_for` for the precedence)."""
    tab_id = str(tab_id)
    with _update(repo) as data:
        tab = _tab_of(data, tab_id)
        if tab is not None:
            tab["session"] = str(session)


def reset_session(repo, tab_id, session=""):
    """Clears the live `session-<tab>` file and records `session` (default: none).

    Used when a recorded id is no longer valid: the next launch starts a new session, and the old
    file cannot shadow the new id."""
    tab_id = str(tab_id)
    try:
        _session_file(tab_id).unlink()
    except OSError:
        pass
    with _update(repo) as data:
        tab = _tab_of(data, tab_id)
        if tab is not None:
            tab["session"] = str(session)


def save(repo, tabs, active=""):
    """Replaces the whole tab list with `(id, profile)` pairs, preserving known session ids.

    Used to sync the state with the live panes before a tmux server restart; the order of `tabs` is
    the order of the strip."""
    normalized = _normalize_tabs([{"id": tab_id, "profile": profile}
                                  for tab_id, profile in tabs])
    with _update(repo) as data:
        known = {tab["id"]: tab for tab in data["tabs"]}
        for tab in normalized:
            session = _effective_session(data, tab["id"])
            if not session:
                session = known.get(tab["id"], {}).get("session", "")
            tab["session"] = session
        data["tabs"] = normalized
        data["active"] = str(active)


def main(argv):
    """CLI used from shell code; prints only what the caller needs."""
    args = list(argv)
    command = args.pop(0) if args else ""
    if command == "new-id":
        print(new_id())
    elif command == "add" and len(args) in (3, 4):
        add(args[0], args[1], args[2], args[3] if len(args) == 4 else None)
    elif command == "remove" and len(args) == 2:
        remove(args[0], args[1])
    elif command == "active" and len(args) == 2:
        set_active(args[0], args[1])
    elif command == "session" and len(args) == 2:
        session = session_for(args[0], args[1])
        if session:
            print(session)
    elif command == "set-session" and len(args) == 3:
        set_session(args[0], args[1], args[2])
    elif command == "reset-session" and len(args) in (2, 3):
        reset_session(args[0], args[1], args[2] if len(args) == 3 else "")
    elif command == "save" and len(args) in (1, 2):
        tabs = []
        for line in sys.stdin:
            line = line.rstrip("\n")
            if not line:
                continue
            tab_id, _, profile = line.partition("\t")
            if tab_id:
                tabs.append((tab_id, profile))
        save(args[0], tabs, args[1] if len(args) == 2 else "")
    elif command == "list" and len(args) == 1:
        data = read(args[0])
        if not data["tabs"] and not data["active"]:
            return 0
        print(f"active\t{data['active']}")
        for tab in data["tabs"]:
            print(f"id\t{tab['id']}\t{tab['profile']}\t{_effective_session(data, tab['id'])}")
    else:
        print("usage: session_state.py {new-id|add|remove|active|session|set-session|"
              "reset-session|save|list} ...", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
