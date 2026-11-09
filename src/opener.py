"""Decides how a file from the tree is opened: nvim (default) or the OS default program.

Extensions of types that normally have a default viewer/player go to the OS opener
(macOS `open`, Linux `xdg-open`, WSL `wslview`/`explorer.exe`, Windows `start`).
Everything else, including unknown extensions, stays in the editor.
"""
import glob
import os
import shutil
import subprocess
import sys

import lib

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


# Extensions opened in Blender (the OS has no sensible default for them).
BLENDER_EXTENSIONS = {"ifc", "ifczip"}
IFC_EXTENSIONS = {"ifc", "ifczip"}

# Prose types get the prose editor list (Dawn first by default); everything else is code.
PROSE_EXTENSIONS = {"md", "markdown", "mdx", "rst", "txt"}

# Used when the `editor` key/type is absent or has no command available.
EDITOR_FALLBACKS = {
    "prose": [("Dawn", "dawn", True), ("nvim", "nvim", False)],
    "code": [("nvim", "nvim", False), ("micro", "micro", False)],
}
EDITOR_TAIL = ("nvim", "vim", "vi")


def is_prose(path):
    """True when the file extension is a prose one (md, markdown, mdx, rst, txt)."""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext in PROSE_EXTENSIONS


def _command_exists(command):
    first = command.split()[0] if command and command.strip() else ""
    return bool(first) and bool(shutil.which(first))


def _entry(item):
    """Normalized {name, command, fullscreen} from a config item, or None when it has no command."""
    if not isinstance(item, dict):
        return None
    command = item.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    name = item.get("name")
    return {"name": str(name) if name else command.split()[0],
            "command": command,
            "fullscreen": bool(item.get("fullscreen", False))}


def _from_config(kind):
    """Configured editors of `kind` whose command exists, or None when the key/type is absent."""
    config = lib.ler_config().get("editor")
    if not isinstance(config, dict):
        return None
    raw = config.get(kind)
    if not isinstance(raw, list):
        return None
    entries = [_entry(item) for item in raw]
    return [e for e in entries if e and _command_exists(e["command"])]


def _fallback(kind):
    """Default order: prose/code editors, then $VISUAL/$EDITOR, then nvim/vim/vi."""
    items = [{"name": name, "command": command, "fullscreen": fullscreen}
             for name, command, fullscreen in EDITOR_FALLBACKS.get(kind, EDITOR_FALLBACKS["code"])]
    for var in ("VISUAL", "EDITOR"):
        command = os.environ.get(var, "")
        if command.strip():
            items.append({"name": command.split()[0], "command": command, "fullscreen": False})
    items += [{"name": command, "command": command, "fullscreen": False} for command in EDITOR_TAIL]
    return [e for e in items if _command_exists(e["command"])]


def choices(path):
    """Editors offered for `path`, in order, as dicts with name/command/fullscreen.

    The configured list for the file type wins when present; otherwise the fallback chain runs.
    Only entries whose command exists in PATH are returned; duplicates keep the first one.
    """
    kind = "prose" if is_prose(path) else "code"
    entries = _from_config(kind)
    if not entries:
        entries = _fallback(kind)
    seen, out = set(), []
    for entry in entries:
        if entry["command"] not in seen:
            seen.add(entry["command"])
            out.append(entry)
    return out


def default_choice(path):
    """First editor offered for `path`, or None when none is available."""
    entries = choices(path)
    return entries[0] if entries else None


def find_blender(which=shutil.which, environ=os.environ):
    """Path to the Blender binary: AT_WS_BLENDER, PATH, then common install dirs."""
    env = environ.get("AT_WS_BLENDER")
    if env and os.path.isfile(env):
        return env
    found = which("blender")
    if found:
        return found
    home = os.path.expanduser("~")
    patterns = [
        f"{home}/Applications/blender-*/blender",
        f"{home}/blender-*/blender",
        "/opt/blender*/blender",
        "/Applications/Blender.app/Contents/MacOS/Blender",
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    return None


def build_blender_command(path, blender):
    """argv that opens `path` in Blender; IFC goes through the Bonsai add-on when present."""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext in IFC_EXTENSIONS:
        expr = ("import bpy\n"
                "try:\n"
                f"    bpy.ops.bim.load_project(filepath={os.path.abspath(path)!r})\n"
                "except Exception as e:\n"
                "    print('Bonsai not available:', e)\n")
        return [blender, "--python-expr", expr]
    return [blender, path]


def use_blender(path):
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext in BLENDER_EXTENSIONS


def _is_wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def use_system(path):
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext in SYSTEM_EXTENSIONS or ext in BLENDER_EXTENSIONS


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
    cmd = None
    if use_blender(path):
        blender = find_blender()
        if blender:
            cmd = build_blender_command(path, blender)
    cmd = cmd or build_command(path)
    if not cmd:
        return False
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True
