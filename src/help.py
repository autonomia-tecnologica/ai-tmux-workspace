#!/usr/bin/env python3
"""Read-only help viewer: renders HELP.md with colors, boxed tables and wrapped text.

Used by `ctrl+b h` (tmux popup). Scroll with arrows/j/k/PgUp/PgDn/g/G/mouse; Esc or q closes.
Supports the Markdown subset HELP.md uses: headings, paragraphs, `-` lists, tables, indented code
blocks, `inline code` and **bold**.
"""
import os
import re
import sys

import lib
from viewer import ANSI, AMARELO, AZUL, B, CINZA, DIM, R, VERDE, VERMELHO, Linha, Viewer, _larg, cortar

HELP = lib.BASE / "HELP.md"
CODE = "\x1b[38;5;215m"            # inline code: soft orange
ORANGE = "\x1b[38;5;208m"
MAX_W = 100                        # text column never gets wider than this
TOKEN = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*)")
SYMBOL_COLOR = {"verde": VERDE, "amarelo": AMARELO, "vermelho": VERMELHO, "cinza": CINZA, "apagada": DIM}
SYMBOL_DEFAULT = {"◆": AMARELO, "⊘": AMARELO, "⚠": AMARELO, "✖": VERMELHO, "◌": DIM, "○": CINZA}


def width(s):
    return sum(_larg(c) for c in ANSI.sub("", s))


def words(text, symbol_color=None):
    """Inline markup -> list of styled, unbreakable words (code spans stay in one piece).
    Text touching a styled span without a space (e.g. "(`x`)") is glued to it."""
    out, glue = [], False
    for part in TOKEN.split(text):
        if not part:
            continue
        if part.startswith("`"):
            body = part[1:-1]
            color = symbol_color if symbol_color and len(body) == 1 else CODE
            new = [f"{color}{body}{R}"]
        elif part.startswith("**"):
            new = [f"{B}{w}{R}" for w in part[2:-2].split()]
        else:
            new = part.split()
            if new and not part[0].isspace() and out and glue:
                out[-1] += new.pop(0)
            glue = False
            out.extend(new)
            glue = bool(out) and not part[-1].isspace()
            continue
        if new and out and glue:
            out[-1] += new.pop(0)
        out.extend(new)
        glue = True
    return out


def wrap(text, w, indent="", first="", symbol_color=None):
    """Greedy ANSI-aware word wrap. `first` prefixes line 1, `indent` the rest."""
    lines, cur, used = [], first, width(first)
    pad = width(indent)
    empty = True
    for word in words(text, symbol_color):
        n = width(word)
        if not empty and used + 1 + n > w:
            lines.append(cur)
            cur, used, empty = indent + word, pad + n, False
            continue
        cur += ("" if empty else " ") + word
        used += n + (0 if empty else 1)
        empty = False
    lines.append(cur)
    return lines


def cell_color(cells):
    """Marker cells like "`●` verde": the symbol takes the color named by the word after it."""
    m = re.match(r"`(.)`\s*(\w+)?", cells[0])
    if not m:
        return None
    return SYMBOL_COLOR.get((m.group(2) or "").lower()) or SYMBOL_DEFAULT.get(m.group(1))


def table(rows, w):
    """Boxed table; columns shrink proportionally and cells wrap when the terminal is narrow."""
    head, body = rows[0], rows[1:]
    n = len(head)
    natural = [max(width(" ".join(words(r[i]))) if r[i] else 0 for r in rows) for i in range(n)]
    room = w - (3 * n + 1)
    if sum(natural) > room:
        scale = room / sum(natural)
        cols = [max(8, int(c * scale)) for c in natural]
    else:
        cols = natural
    border = lambda l, m, r: DIM + l + m.join("─" * (c + 2) for c in cols) + r + R

    def render(cells, bold=False, color=None):
        wrapped = [wrap(c, cols[i], symbol_color=color if i == 0 else None) for i, c in enumerate(cells)]
        out = []
        for ln in range(max(len(x) for x in wrapped)):
            parts = []
            for i, x in enumerate(wrapped):
                t = x[ln] if ln < len(x) else ""
                t = f"{B}{AZUL}{ANSI.sub('', t)}{R}" if bold else t
                parts.append(" " + t + " " * (cols[i] - width(t)) + " ")
            out.append(f"{DIM}│{R}" + f"{DIM}│{R}".join(parts) + f"{DIM}│{R}")
        return out

    lines = [border("┌", "┬", "┐")] + render(head, bold=True) + [border("├", "┼", "┤")]
    for r in body:
        lines += render(r, color=cell_color(r))
    return lines + [border("└", "┴", "┘")]


def code_block(block, w):
    inner = max([width(l) for l in block] + [10])
    inner = min(inner, w - 4)
    top = f"{DIM}╭{'─' * (inner + 2)}╮{R}"
    bottom = f"{DIM}╰{'─' * (inner + 2)}╯{R}"
    out = [top]
    for l in block:
        t, n = cortar(l, inner)
        out.append(f"{DIM}│{R} {CINZA}{t}{R}{' ' * (inner - n)} {DIM}│{R}")
    return out + [bottom]


def render(md, cols):
    w = min(MAX_W, cols - 4)
    ml = " " * max(2, (cols - w) // 2)           # left margin: centered column
    out, lines, i = [], md.splitlines(), 0

    def emit(rows):
        out.extend(Linha(ml + r) for r in rows)

    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            if out and out[-1].texto != "":
                out.append(Linha())
            i += 1
        elif ln.startswith("# "):
            emit([f"{B}{ORANGE}{ln[2:]}{R}", f"{ORANGE}{'━' * min(w, width(ln[2:]) + 4)}{R}"])
            i += 1
        elif ln.startswith("## "):
            if out and out[-1].texto != "":
                out.append(Linha())
            emit([f"{B}{AZUL}▌ {ln[3:]}{R}"])
            i += 1
        elif ln.startswith("    "):
            block = []
            while i < len(lines) and (lines[i].startswith("    ") or not lines[i].strip()):
                block.append(lines[i][4:])
                i += 1
            while block and not block[-1].strip():
                block.pop()
            emit(code_block(block, w))
            out.append(Linha())
        elif ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            emit(table(rows, w))
            out.append(Linha())
        elif ln.startswith("- "):
            while i < len(lines) and lines[i].startswith("- "):
                emit(wrap(lines[i][2:], w, indent="  ", first=f"{ORANGE}•{R} "))
                i += 1
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"(#|\||- |    )", lines[i]):
                para.append(lines[i].strip())
                i += 1
            emit(wrap(" ".join(para), w))
    return out


_cache = {}


def collect(v):
    cols = os.get_terminal_size().columns if sys.stdout.isatty() else 80
    try:
        stamp = (HELP.stat().st_mtime, cols)
    except OSError:
        return [], [Linha(f" {VERMELHO}HELP.md not found{R}")]
    if _cache.get("stamp") != stamp:
        _cache["stamp"], _cache["body"] = stamp, render(HELP.read_text(), cols)
    bar = f"{B}{ORANGE} AJUDA {R}{DIM} ↑↓ rolar · PgUp/PgDn · g/G início/fim · Esc fecha{R}"
    return [Linha(bar), Linha(f"{DIM}{'─' * cols}{R}")], [Linha()] + _cache["body"]


def on_key(v, k):
    if k in ("esc", "q"):
        v.parar = True
        v._acordar()


if __name__ == "__main__":
    Viewer(collect, intervalo=1.0, ao_tecla=on_key).rodar()
