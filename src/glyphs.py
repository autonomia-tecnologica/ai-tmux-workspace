"""Central place for every symbol the panels draw (status dots, task marks, git icons, tree icons).

Panels never hardcode a symbol: they ask this module, so swapping the glyph set later (for example
an emoji set for a terminal without a Nerd Font) is a change in one file. Needs a Nerd Font in the
terminal for the git and file icons.
"""
import icons
from viewer import AMARELO, CINZA, DIM, R, VERDE, VERMELHO

# --- status dots (repos, sessions) -------------------------------------------------------------
_DOT = {"ocioso": (CINZA, "○"), "trabalha": (VERDE, "●"),
        "espera": (AMARELO, "●"), "pergunta": (VERMELHO, "●")}


def dot(state):
    color, ch = _DOT[state]
    return f"{color}{ch}{R}"


def ci_dot(color):
    """CI run result dot; color is an ANSI color from viewer."""
    return f"{color}●{R}"


# --- task markers ------------------------------------------------------------------------------
_MARK = {"ready": (VERDE, "●"), "refine": (CINZA, "○"), "paused": (DIM, "◌"),
         "blocked": (AMARELO, "⊘"), "validate": (AMARELO, "◆"), "cycle": ("\x1b[31m", "✖")}


def task_mark(state):
    color, ch = _MARK[state]
    return f"{color}{ch}{R}"


def warn():
    return "⚠"


def cross():
    return f"\x1b[31m✖{R}"


def arrow(opened):
    return "▾" if opened else "▸"


def dirty_mark():
    return f"{AMARELO}•{R}"


# --- git icons (repos panel) -------------------------------------------------------------------
def git_icons(est, dirty):
    """est: None (no git) | "local" | "dessinc" | other (in sync with the remote)."""
    dim = "\x1b[38;5;238m"
    gh, br = "", ""
    if est is None:
        return f"{dim}{br} {gh}{R}"
    branch = f"{AMARELO}{br}{R}" if dirty else br
    remote = f"{dim}{gh}{R}" if est == "local" else f"{AMARELO}{gh}{R}" if est == "dessinc" else gh
    return f"{branch} {remote}"


# --- file tree icons ---------------------------------------------------------------------------
def file_icon(name, dim=False):
    return icons.file_icon(name, dim)


def dir_icon(is_open, dim=False):
    return icons.dir_icon(is_open, dim)
