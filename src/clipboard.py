"""Puts a file on the OS clipboard as a file (not as text), so Ctrl+V/Cmd+V in a chat, mail,
a browser upload, etc. attaches it without going through the file manager. `copy_text` puts plain
text on the clipboard instead (the absolute path, to paste as text).

macOS: osascript `set the clipboard to POSIX file`; text goes through pbcopy. Linux: wl-copy
(Wayland) or xclip (X11) with `text/uri-list` for files and `text/plain` for text.
`copy_file` returns "file" or None; `copy_text` returns "text" or None.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _applescript_str(value):
    """AppleScript string literal for `value`: backslash, quote and line/tab controls are escaped."""
    return '"' + (value.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")) + '"'


def build_command(path, platform=None, wayland=None, which=shutil.which):
    """(argv, stdin_bytes) that copies `path` as a file, or None if no tool is available.

    The path is made absolute first, so the clipboard entry and the URI name the same file whatever
    the caller's working directory is.
    """
    platform = platform or sys.platform
    absolute = os.path.abspath(path)
    if platform == "darwin":
        return ["osascript", "-e",
                f"set the clipboard to (POSIX file {_applescript_str(absolute)})"], None
    uri = (Path(absolute).as_uri() + "\r\n").encode()
    if platform.startswith("linux"):
        if which("wl-copy"):
            return ["wl-copy", "--type", "text/uri-list"], uri
        if which("xclip"):
            return ["xclip", "-selection", "clipboard", "-t", "text/uri-list"], uri
    return None


def build_text_command(text, platform=None, wayland=None, which=shutil.which):
    """(argv, stdin_bytes) that copies `text` as plain text, or None if no tool is available.

    macOS: pbcopy. Linux: wl-copy (Wayland) or xclip (X11), type text/plain.
    """
    platform = platform or sys.platform
    data = text.encode("utf-8")
    if platform == "darwin":
        return ["pbcopy"], data
    if platform.startswith("linux"):
        if which("wl-copy"):
            return ["wl-copy", "--type", "text/plain"], data
        if which("xclip"):
            return ["xclip", "-selection", "clipboard", "-t", "text/plain"], data
    return None


def _run(cmd, data):
    try:
        subprocess.run(cmd, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def copy_file(path):
    built = build_command(path)
    if built and _run(*built):
        return "file"
    return None


def copy_text(text):
    """Puts `text` on the clipboard as plain text. Returns "text" or None (no tool, or it failed)."""
    built = build_text_command(text)
    if built and _run(*built):
        return "text"
    return None
