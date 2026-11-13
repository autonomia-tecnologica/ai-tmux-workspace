#!/usr/bin/env python3
"""Active sessions of every workspace repo, grouped by repo: one row per tab, with its agent state
(blocked, working, done, unknown, idle), title, last answer, message count, model, context and cost.
Running subagents become a row inside the parent session. Enter focuses the session pane.

The state of each row is the tab's (`session_state.tab_states`), so Claude and OpenCode tabs read the
same way. A tab with no Claude transcript (an OpenCode tab, or one still starting) shows its profile.
A live Claude session that belongs to no tab (opened by hand) is listed after the tabs.

Transcripts are read incrementally (only what was appended since the last read); model, context and cost
come from the <sid>.status.json written by statusline.py."""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import glyphs
import i18n
import lib
import session_state
import statusbar
import tmux
from viewer import YELLOW, B, GRAY, DIM, R, GREEN, RED, Row, Viewer


CHUNK = 1 << 20          # bytes read at a time: the transcript is consumed in pieces, never whole
HEAD = 300_000           # the first user answer (fallback title) is only searched at the start
CACHE = {}               # transcript path -> read state (see _empty_state)


def text_of(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")


def _empty_state():
    return {"offset": 0, "n": 0, "title": None, "first": None, "last": "", "model": ""}


def _process(block, offset, state):
    """Update `state` with the complete lines of `block`, which starts at byte `offset` of the transcript."""
    for m in re.finditer(rb'"aiTitle"\s*:\s*"((?:[^"\\]|\\.)*)"', block):
        try:
            state["title"] = json.loads(b'"' + m.group(1) + b'"')
        except Exception:
            pass
    state["n"] += len(re.findall(rb'"type"\s*:\s*"(?:user|assistant)"', block))
    pos = offset
    for l in block.split(b"\n"):
        current, pos = pos, pos + len(l) + 1
        if not (b'"assistant"' in l or (state["first"] is None and current < HEAD and b'"user"' in l)):
            continue
        try:
            d = json.loads(l)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        if d.get("type") == "assistant":
            state["model"] = msg.get("model") or state["model"]       # the last non-empty model wins
            t = text_of(msg).strip()
            if t:
                state["last"] = t
        elif d.get("type") == "user" and state["first"] is None and current < HEAD:
            t = text_of(msg).strip()
            if t and not t.startswith("<"):
                state["first"] = t


def _advance(f, state):
    """Read only what was appended to `f` since `state["offset"]`, in pieces, never past complete lines."""
    if f.stat().st_size < state["offset"]:         # truncated or replaced: start over
        state.update(_empty_state())
    with open(f, "rb") as fh:
        fh.seek(state["offset"])
        rest = b""
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            data = rest + block
            end = data.rfind(b"\n") + 1
            if end:
                _process(data[:end], state["offset"], state)
                state["offset"] += end
            rest = data[end:]


def read(f):
    """Title, last answer, message count and model of transcript `f`, via CACHE (incremental)."""
    state = CACHE.setdefault(str(f), _empty_state())
    _advance(f, state)
    return {"title": state["title"] or state["first"] or i18n.t("sessions.untitled"),
            "last": " ".join(state["last"].split()), "n": state["n"],
            "model": str(state["model"] or "").removeprefix("claude-")}


def folder(cwd):
    return lib.HOME / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def transcript_of(entry, sid):
    """Session transcript: the entry `transcript_path` when present; else the project folder from the cwd."""
    tp = entry.get("transcript_path")
    if isinstance(tp, str) and tp.strip():
        return Path(os.path.expanduser(tp))
    return folder(entry["cwd"]) / f"{sid}.jsonl"


def _details(entry, sid):
    """(title, last answer, message count, model) of a live Claude session with a transcript."""
    path = transcript_of(entry, sid)
    try:
        d = read(path)
    except Exception:
        d = {"title": i18n.t("sessions.untitled"), "last": "", "n": 0, "model": ""}
    st = statusbar.status_of(sid)
    last = " ".join((statusbar.clean_text(entry.get("last_assistant_message")) or d["last"]).split())
    model = statusbar.clean_text(st.get("model")) or d["model"]
    return d, st, last, model, str(path)


def _rows_of_repo(repo_name, rpath, tabs, sessions, used):
    """Body rows of one repo: each tab with its state, then the live sessions that no tab owns."""
    rows = []
    by_sid = {sid: (entry, color) for sid, entry, color in sessions}
    owned = set()
    for tab in tabs:
        sid = session_state.session_for(rpath, tab["id"])
        hit = by_sid.get(sid) if sid else None
        if hit:
            owned.add(sid)
        rows.append((tab["state"], tab["profile"], sid, hit))
    for sid, entry, color in sessions:
        if sid not in owned:
            rows.append((color, entry.get("profile") or "", sid, (entry, color)))
    body = []
    for state, profile, sid, hit in rows:
        if hit:
            entry = hit[0]
            d, st, last, model, path = _details(entry, sid)
            used.add(path)
            title = d["title"]
        else:
            entry, st, last, model, d = None, {}, "", "", {"n": 0}
            title = profile or i18n.t("sessions.untitled")
        if repo_name:
            body.append(Row(f" {GRAY}{repo_name.upper()}{R}"))
            repo_name = None
        key = (entry["pane"], rpath) if entry and entry.get("pane") else None
        body.append(Row(f" {glyphs.dot(state)} {B}{title}{R}", key))
        if entry:
            body.append(Row(f"   {GRAY}{i18n.t('sessions.agent')}{R} {DIM}{last}{R}", cont=True))
            parts = [i18n.t("sessions.msgs", n=d["n"])] + ([model] if model else [])
            entry_profile = entry.get("profile")
            if isinstance(entry_profile, str) and entry_profile.strip() and entry_profile.strip() != "claude":
                parts.append(entry_profile.strip())
            pct = statusbar.as_number(st.get("context_pct"))
            if pct is not None:
                parts.append(i18n.t("status.context", pct=f"{pct:.0f}"))
            cost = statusbar.as_number(st.get("cost_usd"))
            if cost is not None:
                parts.append(i18n.t("sessions.cost", v=f"{cost:.2f}".replace(".", ",")))
            body.append(Row(f"   {GRAY}{' · '.join(parts)}{R}", cont=True))
            types = entry.get("agent_types") or {}
            for a in entry.get("agents") or []:
                body.append(Row(f"   {GREEN}↳{R} {GRAY}{types.get(a, 'agent')}{R}", cont=True))
        else:
            body.append(Row(f"   {GRAY}{i18n.t('sessions.no_transcript')}{R}", cont=True))
        body.append(Row(""))
    return body, len(rows)


def collect(v):
    now = time.time()
    main = lib.read_main()
    repos = ([main] if main else []) + [r for _, rs in lib.read_repos() for r in rs]
    by_repo = lib.live_sessions([p for _, p in repos], now)
    tabs_of = {rpath: session_state.tab_states(rpath) for _, rpath in repos}
    body = []
    used = set()
    total = 0
    for repo_name, rpath in repos:
        rows, count = _rows_of_repo(repo_name, rpath, tabs_of[rpath], by_repo.get(rpath, []), used)
        body += rows
        total += count
    header = [Row(f" {GRAY}{i18n.t('sessions.header', n=total)}{R}"), Row("")]
    for key in [c for c in CACHE if c not in used]:      # session left the list: release the read state
        del CACHE[key]
    if total == 0:
        body.append(Row(f" {GRAY}{i18n.t('sessions.empty')}{R}"))
    return header, body


def enter(v, key, _pressed):
    """Focus the session's pane: bring it forward in its repo window (it may be a hidden tab)."""
    if not key:     # a tab whose agent has no live pane (an OpenCode tab) has nothing to focus
        return
    pane, repo = key
    session = tmux.run("display", "-p", "-t", pane, "#{session_name}")
    win = ""
    for line in tmux.run("list-windows", "-t", session, "-F", "#{window_id}|#{@ws_repo}").splitlines():
        wid, _, r = line.partition("|")
        if r == repo:
            win = wid
            break
    if win:
        subprocess.run([lib.WS, "tab-show", win, pane],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        tmux.run("select-window", "-t", win)
    else:
        tmux.run("select-window", "-t", pane)
    tmux.run("select-pane", "-t", pane)


if __name__ == "__main__":
    Viewer(collect, interval=3.0, cursor=True, on_enter=enter).run()
