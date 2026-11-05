"""Puts a file on the OS clipboard as a file (not as text), so Ctrl+V/Cmd+V in Slack, mail,
a browser upload, etc. attaches it without going through the file manager.

macOS: osascript `set the clipboard to POSIX file`. Linux: wl-copy (Wayland) or xclip (X11) with
`text/uri-list`. Returns the kind of copy that happened ("file", "path") or None.
"""
import shutil
import subprocess
import sys
from pathlib import Path


def build_command(path, platform=None, wayland=None, which=shutil.which):
    """(argv, stdin_bytes) that copies `path` as a file, or None if no tool is available."""
    platform = platform or sys.platform
    if platform == "darwin":
        esc = path.replace("\\", "\\\\").replace('"', '\\"')
        return ["osascript", "-e", f'set the clipboard to (POSIX file "{esc}")'], None
    uri = (Path(path).as_uri() + "\r\n").encode()
    if platform.startswith("linux"):
        if which("wl-copy"):
            return ["wl-copy", "--type", "text/uri-list"], uri
        if which("xclip"):
            return ["xclip", "-selection", "clipboard", "-t", "text/uri-list"], uri
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
