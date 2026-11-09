"""Central place for every symbol the panels draw (status dots, task marks, git icons, tree icons).

Panels never hardcode a symbol: they ask this module, so swapping the glyph set later (for example
an emoji set for a terminal without a Nerd Font) is a change in one file. Needs a Nerd Font in the
terminal for the git and file icons.
"""
import icons
from colors import YELLOW, GRAY, DIM, GRAY_DARK, R, GREEN, RED

# --- status dots (repos, sessions) -------------------------------------------------------------
_DOT = {"idle": (GRAY, "○"), "working": (GREEN, "●"),
        "waiting": (YELLOW, "●"), "question": (RED, "●")}


def dot(state):
    color, ch = _DOT[state]
    return f"{color}{ch}{R}"


def ci_dot(color):
    """CI run result dot; color is an ANSI color from viewer."""
    return f"{color}●{R}"


# --- task markers ------------------------------------------------------------------------------
_MARK = {"ready": (GREEN, "●"), "refine": (GRAY, "○"), "paused": (DIM, "◌"),
         "blocked": (YELLOW, "⊘"), "validate": (YELLOW, "◆"), "cycle": (RED, "✖")}


def task_mark(state):
    color, ch = _MARK[state]
    return f"{color}{ch}{R}"


def warn():
    return "⚠"


def cross():
    return f"{RED}✖{R}"


def arrow(opened):
    return "▾" if opened else "▸"


def dirty_mark():
    return f"{YELLOW}•{R}"


# --- git icons (repos panel) -------------------------------------------------------------------
def git_icons(state, dirty):
    """est: None (no git) | "local" | "diverged" | other (in sync with the remote)."""
    dim = GRAY_DARK
    gh, br = "", ""
    if state is None:
        return f"{dim}{br} {gh}{R}"
    branch = f"{YELLOW}{br}{R}" if dirty else br
    remote = f"{dim}{gh}{R}" if state == "local" else f"{YELLOW}{gh}{R}" if state == "diverged" else gh
    return f"{branch} {remote}"


# --- file tree icons ---------------------------------------------------------------------------
def file_icon(name, dim=False):
    return icons.file_icon(name, dim)


def dir_icon(is_open, dim=False):
    return icons.dir_icon(is_open, dim)
