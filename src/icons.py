"""File-type icons (Nerd Font glyphs) for the tree panel.

Small hand-picked table. The mapping of file types to glyphs and colors follows the one from
nvim-web-devicons (https://github.com/nvim-tree/nvim-web-devicons, MIT), which deserves the credit.
License checked on GitHub on 2026-11-05. See NOTICE. A verbatim copy of more of it must keep its
license text.

Why it is embedded here instead of installed as a dependency: this repo takes no third-party
packages at runtime (no network, no daemon), which removes the supply-chain attack surface (a
compromised or typosquatted package, a hijacked update). A table of constants is cheap to embed
and to review.

Requires a Nerd Font in the terminal; set "icons": false in settings.json to turn it off.
"""
import json
import os

import lib

# name or extension -> (glyph, 256-color code)
BY_NAME = {
    "dockerfile": ("", 33), ".gitignore": ("", 202), ".gitattributes": ("", 202),
    "package.json": ("", 41), "package-lock.json": ("", 244), "makefile": ("", 244),
    "license": ("", 220), "readme.md": ("", 75), "claude.md": ("", 173),
    ".env": ("", 220), "tmux.conf": ("", 41),
}
BY_EXT = {
    "py": ("", 220), "js": ("", 221), "mjs": ("", 221), "ts": ("", 74),
    "jsx": ("", 80), "tsx": ("", 80), "json": ("", 221), "md": ("", 75),
    "html": ("", 202), "css": ("", 75), "scss": ("", 168), "vue": ("", 41),
    "sh": ("", 41), "zsh": ("", 41), "bash": ("", 41), "yml": ("", 167),
    "yaml": ("", 167), "toml": ("", 244), "ini": ("", 244), "conf": ("", 244),
    "rs": ("", 173), "go": ("", 74), "java": ("", 167), "kt": ("", 135),
    "c": ("", 75), "h": ("", 75), "cpp": ("", 75), "rb": ("", 160),
    "php": ("", 105), "lua": ("", 75), "swift": ("", 208), "sql": ("", 244),
    "txt": ("", 252), "lock": ("", 244), "log": ("", 244),
    "png": ("", 135), "jpg": ("", 135), "jpeg": ("", 135), "gif": ("", 135),
    "svg": ("", 220), "webp": ("", 135), "pdf": ("", 167), "zip": ("", 220),
    "gz": ("", 220), "tar": ("", 220),
}
FILE = ("", 244)
DIR_CLOSED, DIR_OPEN = "", ""


def enabled():
    try:
        with open(lib.config_path(), encoding="utf-8") as f:
            return bool(json.load(f).get("icons", True))
    except Exception:
        return True


def file_icon(name):
    low = name.lower()
    g, c = BY_NAME.get(low) or BY_EXT.get(low.rsplit(".", 1)[-1] if "." in low else "") or FILE
    return f"\x1b[38;5;{c}m{g}\x1b[0m "


def dir_icon(is_open):
    return f"\x1b[38;5;75m{DIR_OPEN if is_open else DIR_CLOSED}\x1b[0m "
