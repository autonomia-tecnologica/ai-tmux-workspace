"""OS notifications for the hook. Never raises, never blocks: the command is spawned detached."""
import base64
import subprocess
import sys

_PS_TOAST = """$ErrorActionPreference='Stop'
$d={param($s)[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($s))}
$title=& $d '%s'; $body=& $d '%s'
[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]
$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02')
$t=$x.GetElementsByTagName('text')
[void]$t.Item(0).AppendChild($x.CreateTextNode($title))
[void]$t.Item(1).AppendChild($x.CreateTextNode($body))
$n=[Windows.UI.Notifications.ToastNotification]::new($x)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Claude Code').Show($n)
"""


def _is_wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _b64(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _applescript_str(s):
    """AppleScript string literal for `s`: backslash, quote and line/tab controls are escaped, so a
    multi-line body neither breaks the script nor ends the literal early."""
    return '"' + (s.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")) + '"'


def build_command(title, body, platform=None, wsl=None):
    """argv list for the current platform (no shell involved), or None if unsupported."""
    platform = platform or sys.platform
    title, body = title.replace("\0", " "), body.replace("\0", " ")
    if platform == "darwin":
        script = f"display notification {_applescript_str(body)} with title {_applescript_str(title)}"
        return ["osascript", "-e", script]
    if wsl is None:
        wsl = platform.startswith("linux") and _is_wsl()
    if platform == "win32" or wsl:
        # title/body travel as base64 literals, so nothing user-controlled is parsed as code
        script = _PS_TOAST % (_b64(title), _b64(body))
        enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        exe = "powershell" if platform == "win32" else "powershell.exe"
        return [exe, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", enc]
    if platform.startswith("linux"):
        return ["notify-send", "--app-name=Claude Code", "--", title, body]
    return None


def notify(title, body):
    try:
        cmd = build_command(title, body)
        if not cmd:
            return
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = 0x00000008 | 0x08000000   # DETACHED_PROCESS | CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kw)
    except Exception:
        pass
