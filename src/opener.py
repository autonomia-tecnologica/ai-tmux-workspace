"""Decides how a file from the tree is opened: nvim (default) or the OS default program.

Extensions of types that normally have a default viewer/player go to the OS opener
(macOS `open`, Linux `xdg-open`, WSL `wslview`/`explorer.exe`, Windows `start`).
Everything else, including unknown extensions, stays in the editor.
"""
import os
import shutil
import subprocess
import sys

SYSTEM_EXTENSIONS = {
    # documents
    "pdf", "doc", "docx", "odt", "rtf", "xls", "xlsx", "ods", "ppt", "pptx", "odp", "epub", "mobi",
    # images (svg stays in the editor: it is text)
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "ico", "heic", "heif", "avif", "psd",
    # video
    "mp4", "mkv", "mov", "avi", "webm", "wmv", "flv", "m4v", "mpg", "mpeg",
    # audio
    "mp3", "wav", "flac", "ogg", "m4a", "aac", "opus", "wma",
    # archives and binaries that only make sense outside the editor
    "zip", "7z", "rar", "tar", "gz", "xz", "bz2", "dmg", "iso",
}


def _is_wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def use_system(path):
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext in SYSTEM_EXTENSIONS


def build_command(path, platform=None, wsl=None, which=shutil.which):
    """argv list that opens `path` with the OS default program, or None if unsupported."""
    platform = platform or sys.platform
    if platform == "darwin":
        return ["open", path]
    if platform == "win32":
        return ["cmd", "/c", "start", "", path]
    if platform.startswith("linux"):
        wsl = _is_wsl() if wsl is None else wsl
        if wsl:
            if which("wslview"):
                return ["wslview", path]
            if which("wslpath"):
                win = subprocess.run(["wslpath", "-w", path], capture_output=True, text=True).stdout.strip()
                return ["explorer.exe", win] if win else None
        if which("xdg-open"):
            return ["xdg-open", path]
    return None


def open_system(path):
    """Spawns the OS opener detached. Returns False when there is no opener available."""
    cmd = build_command(path)
    if not cmd:
        return False
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True
