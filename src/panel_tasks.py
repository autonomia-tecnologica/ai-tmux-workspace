#!/usr/bin/env python3
"""Tasks panel: open BACKLOG.md items of the repo of this tmux window only.

  ● green ready   ◆ yellow to validate (Enter asks "what do I need to validate in item <id>?")   ○ dim needs refinement   ◌ paused   ⊘ blocked by an open dependency   ✖ cycle
A BACKLOG.md outside the unified format shows as "no format"; Enter on an item sends it to Claude; Right/Left expands/collapses its description (▸/▾).
"""
import os
import shlex
import shutil
import subprocess
import textwrap

import backlog
import i18n
import lib
from viewer import AZUL, B, CINZA, DIM, R, VERDE, AMARELO, Linha, Viewer



def t(key, **kw):
    return i18n.t(f"tasks.{key}", **kw)


MARK = {"ready": f"{VERDE}●{R}", "refine": f"{CINZA}○{R}", "paused": f"{DIM}◌{R}",
        "blocked": f"{AMARELO}⊘{R}", "validate": f"{AMARELO}◆{R}", "cycle": f"\x1b[31m✖{R}"}


def effective(i):
    """Display status: a ready item with an open dependency is blocked; cycles are invalid."""
    if i["cycle"]:
        return "cycle"
    return "blocked" if i["blocked"] and i["status"] == "ready" else i["status"]
CURRENT = lib.repo_atual()
OPEN = set()    # ids of items whose description is expanded (Right opens, Left closes)


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
        rail = {}                                           # id -> tree prefix for its children/lines
        for i, par, last, kids in backlog.tree(items):
            st = effective(i)
            key = ("i", CURRENT, i["id"], st)
            base = rail.get(par, " ") if par is not None else " "
            lead = base + (("└─ " if last else "├─ ") if par is not None else "")
            rail[i["id"]] = base + (("   " if last else "│  ") if par is not None else "")
            deps = ", ".join(map(str, i["deps"]))
            dep = f" ←{deps}" if len(i["deps"]) > 1 or (i["deps"] and par is None) else ""
            if i["typo"]:
                dep += " ⚠"
            pad = len(lead) + 5 + len(str(i["id"]))         # "<lead>▸ ● 12 "
            opened = i["id"] in OPEN
            first = f"{DIM}{lead}{'▾' if opened else '▸'}{R} {MARK[st]} {DIM}{i['id']}{R} "
            if not opened:
                title = i["title"]
                room = cols - pad - len(dep) - 1
                if len(title) > room:
                    title = title[:max(1, room - 1)].rstrip() + "…"
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
                body.append(Linha(f"{ind}\x1b[31m✖ {t('cycle')}{R}", cont=True))
            if i["typo"]:
                body.append(Linha(f"{ind}{AMARELO}⚠ {t('typo', ids=', '.join(map(str, i['typo'])))}{R}", cont=True))
            for n, ln in enumerate(textwrap.wrap(i["accept"], width - 2) if i["accept"] else []):
                body.append(Linha(f"{ind}{DIM}{'✓ ' if not n else '  '}{ln}{R}", cont=True))
    return head, body


def enter(v, key, k):
    path = key[1]
    if key[3] in ("blocked", "cycle"):      # never dispatched while a dependency is open
        return
    # ready -> implement; to validate -> ask what to check; anything else (to refine, paused) -> refine
    msg = i18n.t("prompt.tasks." + {"ready": "ready", "validate": "validate"}.get(key[3], "refine"), id=key[2])
    send = " ".join(shlex.quote(a) for a in (str(lib.BASE / "bin" / "workspace"), "send", path, msg))
    # tmux command string: double quotes, with \\ " $ escaped and # doubled (format expansion)
    tq = '"' + send.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("#", "##") + '"'
    prompt = t("confirm", msg=msg).replace("#", "##")
    subprocess.run(["tmux", "confirm-before", "-p", prompt, f"run-shell -b {tq}"], timeout=20,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def on_key(v, k):
    """Right expands the item under the cursor, Left collapses it (like the files tree)."""
    if k not in ("\x1b[C", "\x1bOC", "\x1b[D", "\x1bOD") or not v.chave or v.chave[0] != "i":
        return
    (OPEN.add if k in ("\x1b[C", "\x1bOC") else OPEN.discard)(v.chave[2])
    v.recoletar.set()


if __name__ == "__main__":
    Viewer(collect, intervalo=3.0, cursor=True, ao_enter=enter, ao_tecla=on_key, chave_inicial=None).rodar()
