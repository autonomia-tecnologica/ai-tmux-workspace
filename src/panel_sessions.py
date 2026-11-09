#!/usr/bin/env python3
"""Active sessions (open Claudes) of every workspace repo, grouped by repo: title, state, last answer, message count,
model, context and cost. Running subagents become a row inside the parent session. Enter focuses the session pane.

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


def collect(v):
    now = time.time()
    main = lib.read_main()
    repos = ([main] if main else []) + [r for _, rs in lib.read_repos() for r in rs]
    by_repo = lib.live_sessions([p for _, p in repos], now)
    total = sum(len(x) for x in by_repo.values())
    header = [Row(f" {GRAY}{i18n.t('sessions.header', n=total)}{R}"), Row("")]
    body = []
    used = set()
    for repo_name, rpath in repos:
        for sid, e, color in by_repo.get(rpath, []):
            path = transcript_of(e, sid)
            used.add(str(path))
            try:
                d = read(path)
            except Exception:
                d = {"title": i18n.t("sessions.untitled"), "last": "", "n": 0, "model": ""}
            st = statusbar.status_of(sid)
            last = " ".join((statusbar.clean_text(e.get("last_assistant_message")) or d["last"]).split())
            model = statusbar.clean_text(st.get("model")) or d["model"]
            if repo_name:
                body.append(Row(f" {GRAY}{repo_name.upper()}{R}"))
                repo_name = None
            k = (e["pane"], rpath)
            body.append(Row(f" {glyphs.dot(color)} {B}{d['title']}{R}", k))
            body.append(Row(f"   {GRAY}{i18n.t('sessions.agent')}{R} {DIM}{last}{R}", cont=True))
            parts = [i18n.t("sessions.msgs", n=d["n"])] + ([model] if model else [])
            pct = statusbar.as_number(st.get("context_pct"))
            if pct is not None:
                parts.append(i18n.t("status.context", pct=f"{pct:.0f}"))
            cost = statusbar.as_number(st.get("cost_usd"))
            if cost is not None:
                parts.append(i18n.t("sessions.cost", v=f"{cost:.2f}".replace(".", ",")))
            body.append(Row(f"   {GRAY}{' · '.join(parts)}{R}", cont=True))
            types = e.get("agent_types") or {}
            for a in e.get("agents") or []:
                body.append(Row(f"   {GREEN}↳{R} {GRAY}{types.get(a, 'agent')}{R}", cont=True))
            body.append(Row(""))
    for key in [c for c in CACHE if c not in used]:      # session left the list: release the read state
        del CACHE[key]
    return header, body


def enter(v, key, _pressed):
    """Focus the session's pane: bring it forward in its repo window (it may be a hidden tab)."""
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
