"""The one door for opening a file outside the editor (item 44).

Every caller (the tree panel, `workspace view`) asks `open_file(path)` and nothing else opens files.
Order: the viewer configured for the kind of file (`viewer:` in config.yaml: html, image, video,
audio; a program with a window of its own, not the full application), then the OS default program
for document/archive types (macOS `open`, Linux `xdg-open`, WSL `wslview`/`explorer.exe`, Windows
`start`). Text and code, and anything else, return False and the caller offers the editor.
The programs come from config: nothing here names chromium, mpv or imv.
"""
import glob
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys

import lib
from i18n import t

SYSTEM_EXTENSIONS = {
    # documents
    "pdf", "doc", "docx", "odt", "rtf", "xls", "xlsx", "ods", "ppt", "pptx", "odp", "epub", "mobi",
    # web pages open in the default browser
    "html", "htm", "xhtml",
    # images (svg stays in the editor: it is text)
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "ico", "heic", "heif", "avif", "psd",
    # video
    "mp4", "mkv", "mov", "avi", "webm", "wmv", "flv", "m4v", "mpg", "mpeg",
    # audio
    "mp3", "wav", "flac", "ogg", "m4a", "aac", "opus", "wma",
    # archives and binaries that only make sense outside the editor
    "zip", "7z", "rar", "tar", "gz", "xz", "bz2", "dmg", "iso",
}


# Kinds a configured viewer opens (`viewer:` in config.yaml). SVG stays in the editor (it is text).
VIEWER_KINDS = {
    "html": {"html", "htm", "xhtml"},
    "image": {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "ico", "heic", "heif", "avif"},
    "video": {"mp4", "mkv", "mov", "avi", "webm", "wmv", "flv", "m4v", "mpg", "mpeg"},
    "audio": {"mp3", "wav", "flac", "ogg", "m4a", "aac", "opus", "wma"},
}
# Placeholders in a `viewer` command: absolute path, file:// URL (for browsers) and base name.
_PLACEHOLDER = re.compile(r"\{(file|url|name)\}")

# Extensions opened in Blender (the OS has no sensible default for them).
BLENDER_EXTENSIONS = {"ifc", "ifczip"}
IFC_EXTENSIONS = {"ifc", "ifczip"}

# Prose types get the prose editor list (micro first by default); everything else is code.
PROSE_EXTENSIONS = {"md", "markdown", "mdx", "rst", "txt"}

# Used when the `editor` key/type is absent or has no command available.
EDITOR_FALLBACKS = {
    "prose": [("micro", "micro", True), ("nvim", "nvim", False)],
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
    config = lib.read_config().get("editor")
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


def kind_of(path):
    """Viewer kind (html, image, video, audio) of `path`, or None when no viewer covers it."""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    for kind, exts in VIEWER_KINDS.items():
        if ext in exts:
            return kind
    return None


def _viewer_template(kind):
    """argv template from `viewer.<kind>` (a command string or a list of strings), or None."""
    config = lib.read_config().get("viewer")
    if not isinstance(config, dict):
        return None
    raw = config.get(kind)
    try:
        if isinstance(raw, str) and raw.strip():
            return shlex.split(raw)
    except ValueError:      # unbalanced quotes: the config is wrong, so this kind has no viewer
        return None
    if isinstance(raw, list) and raw and all(isinstance(x, str) and x.strip() for x in raw):
        return list(raw)
    return None


def viewer_command(path):
    """argv that opens `path` in the configured viewer for its kind, or None when there is none.

    The command is split before the placeholders are filled, so a path with spaces stays one
    argument and no shell runs. The program must exist in PATH. A command without a placeholder
    gets the file as its last argument.
    """
    kind = kind_of(path)
    template = _viewer_template(kind) if kind else None
    if not template or not shutil.which(template[0]):
        return None
    absolute = os.path.abspath(path)
    values = {"file": absolute, "url": pathlib.Path(absolute).as_uri(),
              "name": os.path.basename(absolute)}
    argv = [_PLACEHOLDER.sub(lambda m: values[m.group(1)], token) for token in template]
    if not any(_PLACEHOLDER.search(token) for token in template):
        argv.append(absolute)
    return argv


def _spawn(cmd):
    """Runs `cmd` detached from the workspace. Returns False when it cannot start."""
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


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
    return _spawn(cmd)


def open_file(path):
    """Opens `path` outside the editor: the configured viewer, then the OS default program.

    Returns False when neither can open it; the caller then offers the editor menu.
    """
    cmd = viewer_command(path)
    if cmd and _spawn(cmd):
        return True
    return use_system(path) and open_system(path)


def main(argv=None):
    """`workspace view <file>`: opens the file the same way the tree does. Exit 0 when opened."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(t("view.usage"), file=sys.stderr)
        return 2
    path = args[0]
    if not os.path.isfile(path):
        print(t("view.not_found", path=path), file=sys.stderr)
        return 1
    if open_file(path):
        return 0
    print(t("view.none", name=os.path.basename(path)), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
