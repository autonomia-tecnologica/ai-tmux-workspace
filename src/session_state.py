#!/usr/bin/env python3
"""Persistent session tabs per repo: which tabs exist, their profiles, their session ids and their
agent state.

Each repo has one JSON file under `$WS_STATE/tabs/`, named after the sha1 of its real path:

    {"repo": "<realpath>", "active": "<tab id>",
     "tabs": [{"id": "<stable id>", "profile": "<name>", "session": "<id>",
               "state": "working", "pid": 1234, "harness": "claude", "ts": 1700000000.0}]}

The list order is the tab order in the strip. The id is stable across a tmux server restart, so it
is also the `WS_SESSION_TAB` value exported to the session and the suffix of
`$WS_STATE/tabs/session-<tab>`, where the Claude hook and the OpenCode plugin record the real
session id. `session_for` prefers that file (the live id) and falls back to the JSON value.

The agent state (`state`, `pid`, `harness`, `ts`) is written only here, by `record_agent`, from what
the agent declares: the adapters in `src/agents/` translate the hook or plugin event, and this module
stores the result. `tab_states` reads it back and turns it into one of `STATES`: a tab whose agent
process died (its `pid` is gone) reads as `unknown`, so a tab never stays `working` forever.

A missing or corrupt state file reads as no tabs, so one bad write never hides the repo's tabs.
This module is stdlib-only and also runs as a small CLI (see `main`), which `bin/workspace` uses
from shell code.
"""
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path

import paths

try:
    import fcntl
except ImportError:     # no fcntl (Windows): updates are not serialized
    fcntl = None

# The one vocabulary of an agent's state, shared by the Claude hook, the OpenCode plugin, the panels
# and `workspace status`. `blocked`: the agent declared it waits for an approval or an answer.
# `done`: the agent ended its turn. `working`: the agent is running. `idle`: nothing to do yet.
# `unknown`: the agent process died (or cannot be tested); it is derived, never stored.
BLOCKED = "blocked"
WORKING = "working"
DONE = "done"
IDLE = "idle"
UNKNOWN = "unknown"
STATES = (BLOCKED, WORKING, DONE, IDLE, UNKNOWN)
DECLARED = (BLOCKED, WORKING, DONE, IDLE)   # what an adapter may store; `unknown` is only derived
# The worst state of a repo wins: blocked, then working, then done, then unknown, then idle.
PRIORITY = {IDLE: 0, UNKNOWN: 1, DONE: 2, WORKING: 3, BLOCKED: 4}
# The states whose process must be alive: without a live pid they read as unknown.
LIVE_STATES = (BLOCKED, WORKING)


def _is_darwin():
    """True on macOS. Kept as a function so tests can patch it and run the Darwin paths on Linux."""
    return sys.platform == "darwin"


def path_key(path):
    """Comparison key for a path: byte-exact on Linux, NFC and casefolded on macOS.

    APFS is case-insensitive and normalization-insensitive, so `~/Repo` and `~/repo` (or the NFC
    and NFD spellings of the same name) name one directory and must share one state file. The key
    is only for comparing and hashing; the real path stays as the user wrote it."""
    text = str(path)
    if not _is_darwin():
        return text
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())


def _real(repo):
    """Normalized real path of a repo, so the same directory always maps to the same file."""
    return os.path.realpath(os.path.expanduser(str(repo)))


def pid_alive(pid):
    """True when `pid` is a running process (not a zombie); False when it died or cannot be seen."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:     # it exists, but belongs to another user
        pass
    except OSError:
        return False
    if _is_darwin():
        try:
            proc = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                                  text=True, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        stat = proc.stdout.strip()
        return proc.returncode == 0 and bool(stat) and not stat.startswith("Z")
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return not os.path.isdir("/proc/self")     # no /proc to ask: trust the signal test


def state_dir():
    """Directory that holds the per-repo tab files (overridable with WS_STATE)."""
    return paths.state_dir() / "tabs"


def repo_key(repo):
    """sha1 hex digest of the normalized repo path, used as the state file name.

    The digest is taken over `path_key`, so on macOS two spellings of the same directory (case or
    Unicode normalization) get the same file while Linux keeps byte-exact keys."""
    return hashlib.sha1(path_key(_real(repo)).encode("utf-8")).hexdigest()


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


def _agent_fields(tab):
    """The agent keys of a stored tab that are valid; anything else is dropped."""
    out = {}
    if tab.get("state") in DECLARED:
        out["state"] = tab["state"]
        try:
            out["pid"] = max(0, int(tab.get("pid") or 0))
        except (TypeError, ValueError):
            out["pid"] = 0
        out["harness"] = str(tab.get("harness", ""))
        try:
            out["ts"] = float(tab.get("ts") or 0)
        except (TypeError, ValueError):
            out["ts"] = 0.0
    return out


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
        entry = {
            "id": tab_id,
            "profile": str(tab.get("profile", "")),
            "session": str(tab.get("session", "")),
        }
        entry.update(_agent_fields(tab))
        result.append(entry)
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


def record_agent(repo, tab_id, state, pid=0, harness="", ts=None):
    """Stores the agent state a tab declared (`state` is one of `DECLARED`) with the agent's pid.

    Returns False, storing nothing, when the tab is not in the repo's state (a session opened by
    hand, or a tab already closed). Raises ValueError for a state outside `DECLARED`: `unknown` is
    derived from a dead pid by `tab_states`, never declared."""
    if state not in DECLARED:
        raise ValueError(f"not a declarable agent state: {state!r}")
    tab_id = str(tab_id)
    with _update(repo) as data:
        tab = _tab_of(data, tab_id)
        if tab is None:
            return False
        tab["state"] = state
        try:
            tab["pid"] = max(0, int(pid or 0))
        except (TypeError, ValueError):
            tab["pid"] = 0
        tab["harness"] = str(harness or "")
        tab["ts"] = float(time.time() if ts is None else ts)
    return True


def _resolve(tab, alive=pid_alive):
    """The state a tab reads as now: its declared state, or `unknown` when its agent process is gone.

    A tab with no declared state is `idle`. A dead pid turns any declared state into `unknown`, and
    so does a working or blocked state without a pid (there is nothing to watch it by)."""
    state = tab.get("state")
    if state not in DECLARED:
        return IDLE
    pid = tab.get("pid") or 0
    if pid > 0 and not alive(pid):
        return UNKNOWN
    if pid <= 0 and state in LIVE_STATES:
        return UNKNOWN
    return state


def tab_states(repo, alive=pid_alive):
    """Tabs of a repo in strip order, each as {"id", "profile", "state", "declared", "pid", "harness",
    "ts"}: `state` is the resolved one (see `_resolve`), `declared` is what the agent last said."""
    out = []
    for tab in read(repo)["tabs"]:
        out.append({
            "id": tab["id"],
            "profile": tab["profile"],
            "state": _resolve(tab, alive),
            "declared": tab.get("state", ""),
            "pid": tab.get("pid", 0),
            "harness": tab.get("harness", ""),
            "ts": tab.get("ts", 0.0),
        })
    return out


def repo_state(repo, alive=pid_alive):
    """Worst resolved state of a repo's tabs (`PRIORITY`), or `idle` when it has no tab."""
    states = [tab["state"] for tab in tab_states(repo, alive)]
    return max(states, key=PRIORITY.get, default=IDLE)


def all_repo_files():
    """Paths of every repo state file under `tabs/` (the per-tab session files are not JSON)."""
    try:
        return sorted(p for p in state_dir().glob("*.json") if not p.name.startswith("."))
    except OSError:
        return []


def save(repo, tabs, active=""):
    """Replaces the whole tab list with `(id, profile)` pairs, preserving known session ids and the
    agent state of the tabs that stay.

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
            tab.update(_agent_fields(known.get(tab["id"], {})))
        data["tabs"] = normalized
        data["active"] = str(active)


def status_lines():
    """One line per tab of every repo: `state<TAB>repo<TAB>tab<TAB>profile<TAB>harness`, in repo order.

    `state` is the resolved one, so a dead agent shows as `unknown`. `workspace status` prints this."""
    lines = []
    for path in all_repo_files():
        try:
            repo = json.loads(path.read_text(encoding="utf-8")).get("repo", "")
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(repo, str) or not repo:
            continue
        for tab in tab_states(repo):
            lines.append("\t".join([tab["state"], repo, tab["id"], tab["profile"], tab["harness"]]))
    return lines


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
    elif command == "status" and not args:
        for line in status_lines():
            print(line)
    else:
        print("usage: session_state.py {new-id|add|remove|active|session|set-session|"
              "reset-session|save|list|status} ...", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
