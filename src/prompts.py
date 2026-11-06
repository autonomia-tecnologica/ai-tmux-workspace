#!/usr/bin/env python3
"""Prompt wheel (`ctrl+b e`): a popup list of the prompts you are tired of typing.

Long prompts wrap onto several lines, never truncated. Enter (or 1-9) types the prompt into the active Claude
tab of the window, without submitting it (you press Enter); Esc or q closes. The list is settings.json["prompts"] (strings) when present,
else the i18n defaults (prompt.wheel.*).
Usage: prompts.py <window id>
"""
import os
import subprocess
import sys
import textwrap

import i18n
import lib
from viewer import B, DIM, R, Linha, Viewer

ORANGE = "\x1b[38;5;208m"
MAX_W = 100


def prompts():
    custom = lib.ler_config().get("prompts")
    if isinstance(custom, list) and custom:
        return [p for p in custom if isinstance(p, str) and p.strip()]
    return [v for k, v in sorted(i18n._STRINGS.items()) if k.startswith("prompt.wheel.")]


def collect(v):
    cols = os.get_terminal_size().columns if sys.stdout.isatty() else 80
    w = min(MAX_W, cols - 4)
    body = []
    for n, text in enumerate(prompts()):
        num = f"{n + 1}" if n < 9 else " "
        lines = textwrap.wrap(" ".join(text.split()), w - 5) or [""]
        for i, ln in enumerate(lines):
            head = f" {ORANGE}{num}{R} " if i == 0 else "   "
            body.append(Linha(f"{head} {ln}", chave=n if i == 0 else None, cont=i > 0))
    if not body:
        body = [Linha(f" {DIM}{i18n.t('wheel.empty')}{R}")]
    bar = f"{B}{ORANGE} {i18n.t('wheel.bar')} {R}{DIM} {i18n.t('wheel.keys')}{R}"
    return [Linha(bar), Linha(f"{DIM}{'─' * cols}{R}")], [Linha()] + body


def send(v, n):
    items = prompts()
    if not 0 <= n < len(items):
        return
    subprocess.run([str(lib.BASE / "bin" / "workspace"), "type", WINDOW, items[n]], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    v.parar = True
    v._acordar()


def on_enter(v, key, k):
    send(v, key)


def on_key(v, k):
    if k in ("esc", "q"):
        v.parar = True
        v._acordar()
    elif len(k) == 1 and k in "123456789":
        send(v, int(k) - 1)


if __name__ == "__main__":
    WINDOW = sys.argv[1] if len(sys.argv) > 1 else ""
    if WINDOW:
        Viewer(collect, intervalo=2.0, cursor=True, ao_enter=on_enter, ao_tecla=on_key).rodar()
