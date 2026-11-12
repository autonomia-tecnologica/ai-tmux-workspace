#!/usr/bin/env python3
"""Repos panel: config.yaml groups, each repo with the agent state dot.

  ○ empty    session with no text            ● green   agent (or subagent) working
  ● yellow   stopped, but running something  ● red     stopped with text / question

Next to the name, two icons always present:
  branch  = local git: dim without git · normal clean · yellow with uncommitted work
  GitHub  = remote:    dim not on the remote · normal up to date · yellow ahead/behind the upstream
                       (fetch every 5 min)

With "repos_folder" the repos are the git repos of that folder. F cycles the filter (all, switched on,
switched off); T switches the repo under the cursor on or off ("repos_disabled" in config.yaml). A
switched-off repo shows no git state and opens no window and no agent.
"""
import os
import subprocess
import sys
import time

import glyphs
import lib
from i18n import t
from tree_state import Expander
from viewer import BLUE, B, GRAY, DIM, R, GREEN, YELLOW, RED, Row, Viewer


def git_icon(repo):
    """Two icons, always present: branch (local git) and the GitHub logo (remote)."""
    state, dirty = lib.git_state(repo)
    return glyphs.git_icons(state, dirty)


GROUPS = Expander(lib.STATE / "collapsed.json", default_open=True)   # groups start open; the file keeps the closed ones
CURRENT = lib.current_repo()
FILTERS = ("all", "on", "off")
VIEW = {"filter": "all", "note": "", "note_until": 0.0}   # per panel run: a restart shows every repo again
NOTE_S = 4   # seconds a note stays in the header (the panel redraws every second)


def note(text):
    VIEW["note"], VIEW["note_until"] = text, time.time() + NOTE_S


def switched_off():
    """Names of the folder repos that are off; without a repos folder nothing is switched."""
    return lib.disabled_repos() if lib.repos_folder() else set()


def shown(name, off):
    """Filter: all repos, only the switched-on ones, or only the switched-off ones."""
    f = VIEW["filter"]
    return f == "all" or (f == "off") == (name in off)


def collect(v):
    groups, state = lib.read_repos(), None
    main = lib.read_main()
    state = lib.states_by_repo(groups + ([("", [main])] if main else []))
    off = switched_off()
    header = []   # the REPOS title lives in the panel's top bar (panel_tabs.py repos)
    if lib.repos_folder():
        header.append(f" {DIM}{t('repos.filter.line', filter=t('repos.filter.' + VIEW['filter']))}{R}")
    if time.time() < VIEW["note_until"]:
        header.append(f" {VIEW['note']}")
    body = []
    if main:   # main repo: outside the groups, always on top, bold in the default foreground (blue when it is this window's repo)
        repo_name, p = main
        color = BLUE if p == CURRENT else ""
        body.append(Row(f" {glyphs.dot(state.get(p, 'idle'))} {color}{B}{repo_name}{R} {git_icon(p)}", ("r", p)))
    for name, all_repos in groups:
        repos = [(n, p) for n, p in all_repos if shown(n, off)]
        opened = GROUPS.is_open(name)
        arrow = "▾" if opened else "▸"
        # the closed group dot shows the worst state inside
        worst = max((state.get(p, "idle") for _, p in repos), key=lib.PRIORITY.get, default="idle")
        mark = f" {glyphs.dot(worst)}" if not opened and worst != "idle" else ""
        body.append(Row(f" {GRAY}{arrow} {name.upper()}{R}{mark}", ("g", name)))
        if not opened:
            continue
        for repo_name, p in repos:
            if repo_name in off:   # a switched-off repo is not worked on: no git state is read for it
                body.append(Row(f"   {GRAY}○ {repo_name} · {t('repos.disabled')}{R}", ("r", p)))
                continue
            sel = f"{BLUE}{B}" if p == CURRENT else ""
            body.append(Row(f"   {glyphs.dot(state.get(p, 'idle'))} {sel}{repo_name}{R} {git_icon(p)}", ("r", p)))
    return header, body


def on_key(v, k):
    """F cycles the filter; T switches the repo under the cursor; Left/Right fold a group (like the files tree)."""
    if k == "f":
        VIEW["filter"] = FILTERS[(FILTERS.index(VIEW["filter"]) + 1) % len(FILTERS)]
        v.redraw.set()
    elif k == "t" and v.key and v.key[0] == "r":
        toggle(v, v.key[1])
    elif k in ("left", "right") and v.key and v.key[0] == "g":
        GROUPS.handle_arrow(k, v.key[1])
        v.redraw.set()


def toggle(v, path):
    """Switches the repo on or off in config.yaml; the next redraw reads it back (no tmux state, no restart)."""
    name = os.path.basename(path)
    enable = name in lib.disabled_repos()
    try:
        lib.set_repo_enabled(name, enable)
        note(t("repos.toggled", repo=name, state=t("repo.state.on" if enable else "repo.state.off")))
    except ValueError as error:   # config unreadable, or not a repo of the folder
        note(str(error))
    v.redraw.set()
    v._wake()


def enter(v, key, _pressed):
    kind, value = key
    if kind == "g":
        GROUPS.toggle(value)
        v.redraw.set()
    elif not lib.repo_enabled(value):   # a switched-off repo opens nothing: say so in the header
        note(t("repos.open_disabled", repo=os.path.basename(value)))
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
