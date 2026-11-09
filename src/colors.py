"""ANSI colors and SGR helpers for the whole workspace.

This is the only module with raw color escape sequences. Panels, popups and icons import the
names from here (viewer re-exports the base ones), so a palette change is a change in one file.
"""

R, B, DIM, REV = "\x1b[0m", "\x1b[1m", "\x1b[2m", "\x1b[7m"
GREEN, YELLOW, RED, BLUE, GRAY = "\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[38;5;75m", "\x1b[38;5;244m"
ORANGE = "\x1b[38;5;208m"
CODE = "\x1b[38;5;215m"          # inline code in the help viewer: soft orange
GRAY_DARK = "\x1b[38;5;238m"     # git icons when the repo is clean
BG = "\x1b[48;5;237m"
MENU_BG = "\x1b[48;5;239m"

# top tab bar of each panel (panel_tabs)
TAB_ACTIVE = "\x1b[48;5;75;38;5;16;1m"
TAB_NEW = "\x1b[38;5;75;1m"
TAB_IDLE = "\x1b[48;5;238;38;5;250m"
TAB_CURSOR = "\x1b[48;5;250;38;5;16m"


def fg(n, dim=False):
    """Foreground from the 256-color palette; dim adds the SGR 2 attribute."""
    return f"\x1b[{'2;' if dim else ''}38;5;{n}m"
