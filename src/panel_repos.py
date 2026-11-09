#!/usr/bin/env python3
"""Repos panel: config.yaml groups, each repo with the agent state dot.

  ○ empty    session with no text            ● green   agent (or subagent) working
  ● yellow   stopped, but running something  ● red     stopped with text / question

Next to the name, two icons always present:
  branch  = local git: dim without git · normal clean · yellow with uncommitted work
  GitHub  = remote:    dim not on the remote · normal up to date · yellow ahead/behind the upstream
                       (fetch every 5 min)
"""
import subprocess
import sys

import glyphs
import lib
from tree_state import Expander
from viewer import BLUE, B, GRAY, DIM, R, GREEN, YELLOW, RED, Row, Viewer



def git_icon(repo):
    """Two icons, always present: branch (local git) and the GitHub logo (remote)."""
    state, dirty = lib.git_state(repo)
    return glyphs.git_icons(state, dirty)


GROUPS = Expander(lib.STATE / "collapsed.json", default_open=True)   # groups start open; the file keeps the closed ones
CURRENT = lib.current_repo()


def collect(v):
    groups, state = lib.read_repos(), None
    main = lib.read_main()
    state = lib.states_by_repo(groups + ([("", [main])] if main else []))
    header = []   # the REPOS title lives in the panel's top bar (panel_tabs.py repos)
    body = []
    if main:   # main repo: outside the groups, always on top, bold in the default foreground (blue when it is this window's repo)
        repo_name, p = main
        color = BLUE if p == CURRENT else ""
        body.append(Row(f" {glyphs.dot(state.get(p, 'idle'))} {color}{B}{repo_name}{R} {git_icon(p)}", ("r", p)))
    for name, repos in groups:
        opened = GROUPS.is_open(name)
        arrow = "▾" if opened else "▸"
        # the closed group dot shows the worst state inside
        worst = max((state.get(p, "idle") for _, p in repos), key=lib.PRIORITY.get, default="idle")
        mark = f" {glyphs.dot(worst)}" if not opened and worst != "idle" else ""
        body.append(Row(f" {GRAY}{arrow} {name.upper()}{R}{mark}", ("g", name)))
        if not opened:
            continue
        for repo_name, p in repos:
            sel = f"{BLUE}{B}" if p == CURRENT else ""
            body.append(Row(f"   {glyphs.dot(state.get(p, 'idle'))} {sel}{repo_name}{R} {git_icon(p)}", ("r", p)))
    return header, body


def on_key(v, k):
    """Right expands the group under the cursor, Left collapses it (like the files tree)."""
    if k not in ("left", "right") or not v.key or v.key[0] != "g":
        return
    GROUPS.handle_arrow(k, v.key[1])
    v.redraw.set()


def enter(v, key, _pressed):
    kind, value = key
    if kind == "g":
        GROUPS.toggle(value)
        v.redraw.set()
    else:
        subprocess.run([lib.WS, "open", value], timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def focus_lost(v):
    """Leaving the pane (or window) puts the cursor back on this window's own repo."""
    v.key = ("r", CURRENT)
    v.redraw.set()
    v._wake()


if __name__ == "__main__":
    Viewer(collect, interval=1.0, cursor=True, on_enter=enter, on_key=on_key, initial_key=("r", CURRENT),
           on_focus_lost=focus_lost).run()
