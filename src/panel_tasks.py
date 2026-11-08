#!/usr/bin/env python3
"""Tasks panel: open BACKLOG.md items of the repo of this tmux window only.

  ● green ready   ◆ yellow to validate (Enter asks "what do I need to validate in item <id>?")   ○ dim needs refinement   ◌ paused   ⊘ blocked by an open dependency   ✖ cycle
A BACKLOG.md outside the unified format shows as "no format"; Enter on an item sends it to Claude; Right/Left expands/collapses its description (▸/▾).
"""
import os
import shutil
import textwrap

import backlog
import glyphs
import i18n
import lib
import tmux
from tree_state import Expander
from viewer import AZUL, B, CINZA, DIM, R, VERDE, AMARELO, Linha, Viewer, ellipsize



def t(key, **kw):
    return i18n.t(f"tasks.{key}", **kw)




def effective(i):
    """Display status: a ready item with an open dependency is blocked; cycles are invalid."""
    if i["cycle"]:
        return "cycle"
    return "blocked" if i["blocked"] and i["status"] == "ready" else i["status"]
CURRENT = lib.repo_atual()
ITENS = Expander()    # items whose description is expanded (Right opens, Left closes)
GRUPOS = Expander()    # categories whose items are shown (Right opens, Left closes); closed by default


def collect(v):
    file = os.path.join(CURRENT, "BACKLOG.md")
    items = backlog.parse(file) if os.path.exists(file) else None
    head = []   # title is in the top bar; the repo name is already in the repos list and the status line
    body = []
    if not os.path.exists(file):
        body.append(Linha(f" {DIM}{t('none')}{R}"))
    elif items is None:
        body.append(Linha(f" {DIM}{t('no_format')}{R}"))
    else:
        eff = [effective(i) for i in items]
        ready, blocked = eff.count("ready"), sum(e in ("blocked", "cycle") for e in eff)
        validate = eff.count("validate")
        refine = len(items) - ready - blocked - validate
        cols = shutil.get_terminal_size((80, 24))[0]
        parts = [(VERDE, ready, "ready"), (CINZA, refine, "refine"), (AMARELO, validate, "validate"),
                 (AMARELO, blocked, "blocked")]
        row, used = "", 0                                   # pack the counters, wrapping when they don't fit
        for color, n, key in parts:
            if key in ("validate", "blocked") and not n:
                continue
            plain = f"{n} {t(key)}"
            if used and used + 2 + len(plain) + 1 > cols:
                body.append(Linha(row))
                row, used = "", 0
            row += ("  " if used else " ") + f"{color}{n}{R} {DIM}{t(key)}{R}"
            used += (2 if used else 1) + len(plain)
        body.append(Linha(row))
        body.append(Linha())                                # blank line between counters and tasks
        body.append(Linha(f" {DIM}{glyphs.arrow(False)}{R} {t('expand_all')}", ("b", CURRENT, "expand")))
        body.append(Linha(f" {DIM}{glyphs.arrow(True)}{R} {t('collapse_all')}", ("b", CURRENT, "collapse")))
        body.append(Linha())
        rail = {}                                           # id -> tree prefix for its children/lines
        rows = []                                           # (group header | tree row), grouped by category
        for cat, members in backlog.groups(items):
            rows.append(("g", cat, members))
            if GRUPOS.is_open(cat):
                rows.extend(("i", *r) for r in backlog.tree(members))
        for row in rows:
            if row[0] == "g":
                _, cat, members = row
                opened = GRUPOS.is_open(cat)
                n_ready = sum(effective(m) == "ready" for m in members)
                counts = f"{len(members)}" + (f", {n_ready} {t('ready')}" if n_ready else "")
                name = cat or t("no_category")
                room = cols - len(counts) - 7
                name = ellipsize(name, room)
                body.append(Linha(f" {DIM}{glyphs.arrow(opened)}{R} {B}{name}{R} {DIM}({counts}){R}",
                                  ("g", CURRENT, cat)))
                continue
            _, i, par, last, kids = row
            st = effective(i)
            key = ("i", CURRENT, i["id"], st)
            base = rail.get(par, " ") if par is not None else " "
            lead = base + (("└─ " if last else "├─ ") if par is not None else "")
            rail[i["id"]] = base + (("   " if last else "│  ") if par is not None else "")
            deps = ", ".join(map(str, i["deps"]))
            dep = f" ←{deps}" if len(i["deps"]) > 1 or (i["deps"] and par is None) else ""
            if i["typo"] or i["cross"]:
                dep += f" {glyphs.warn()}"
            pad = len(lead) + 5 + len(str(i["id"]))   # "<lead>▸ ● 12 "
            opened = ITENS.is_open(i["id"])
            first = f"{DIM}{lead}{glyphs.arrow(opened)}{R} {glyphs.task_mark(st)} {DIM}{i['id']}{R} "
            if not opened:
                title = i["title"]
                room = cols - pad - len(dep) - 1
                title = ellipsize(title, room)
                body.append(Linha(f"{first}{title}{DIM}{dep}{R}", key))
                continue
            # selected item: full title wrapped, plus deps and acceptance
            width = max(10, cols - pad - 1)
            lines = textwrap.wrap(i["title"], width) or [""]
            body.append(Linha(f"{first}{B}{lines[0]}{R}", key))
            ind = f"{DIM}{rail[i['id']]}{'│' if kids else ' '}{R}" + " " * (pad - len(rail[i["id"]]) - 1)
            for ln in lines[1:]:
                body.append(Linha(f"{ind}{B}{ln}{R}", cont=True))
            if i["deps"]:
                body.append(Linha(f"{ind}{DIM}←{deps}{R}", cont=True))
            if i["cycle"]:
                body.append(Linha(f"{ind}{glyphs.cross()} {t('cycle')}{R}", cont=True))
            if i["cross"]:
                body.append(Linha(f"{ind}{AMARELO}{glyphs.warn()} {t('cross', ids=', '.join(map(str, i['cross'])))}{R}", cont=True))
            if i["typo"]:
                body.append(Linha(f"{ind}{AMARELO}{glyphs.warn()} {t('typo', ids=', '.join(map(str, i['typo'])))}{R}", cont=True))
            for n, ln in enumerate(textwrap.wrap(i["accept"], width - 2) if i["accept"] else []):
                body.append(Linha(f"{ind}{DIM}{'✓ ' if not n else '  '}{ln}{R}", cont=True))
    return head, body


def enter(v, key, k):
    path = key[1]
    if key[0] == "b":          # expand/collapse all categories (collapse also closes item details)
        file = os.path.join(CURRENT, "BACKLOG.md")
        items = backlog.parse(file) if os.path.exists(file) else None
        if key[2] == "expand":
            for cat, _ in backlog.groups(items or []):
                GRUPOS.open(cat)
        else:
            for cat, _ in backlog.groups(items or []):
                GRUPOS.close(cat)
            for i in items or []:
                ITENS.close(i["id"])
        v.recoletar.set()
        return
    if key[0] == "g":          # category header: Right/Left open and close it
        return
    if key[3] == "cycle":      # invalid until the cycle is undone
        return
    # ready -> implement; blocked -> implement its open dependencies first; to validate -> ask what to
    # check; anything else (to refine, paused) -> refine
    kinds = {"ready": "ready", "blocked": "blocked", "validate": "validate"}
    msg = i18n.t("prompt.tasks." + kinds.get(key[3], "refine"), id=key[2])
    tmux.confirm_then(t("confirm", msg=msg), [lib.WS, "send", path, msg])


def on_key(v, k):
    """Right expands the item or category under the cursor, Left collapses it (like the files tree)."""
    if k not in ("left", "right") or not v.chave or v.chave[0] not in ("i", "g"):
        return
    (GRUPOS if v.chave[0] == "g" else ITENS).handle_arrow(k, v.chave[2])
    v.recoletar.set()


if __name__ == "__main__":
    Viewer(collect, intervalo=3.0, cursor=True, ao_enter=enter, ao_tecla=on_key, chave_inicial=None).rodar()
