"""Risk warning before opening a Claude profile pointed at the DeepSeek API (item 101).

Two commands, both run from inside a tmux popup by `risk_check` in bin/workspace:

  session_risk.py --check <profile>                    exit 0 when the profile is risky, 1 otherwise
  session_risk.py --confirm <profile> [--status file]  show the warning, read one key, write yes/no

The module never imports tmux directly: it must run inside a popup with no tmux client available.
It imports lib (profile/harness resolution) and i18n (user-facing texts) only.
"""
import sys
from pathlib import Path

import lib
from i18n import t

DEEPSEEK_NAME = "claude-deepseek"


def risky(profile):
    """True when `profile` is a Claude harness pointed at the DeepSeek API.

    A missing profile, a normal Claude profile and every OpenCode profile return False."""
    profiles = lib.read_config().get("profiles")
    if not isinstance(profiles, dict) or profile not in profiles:
        return False
    if not lib.is_claude_harness(profile):
        return False
    if profile == DEEPSEEK_NAME:
        return True
    value = profiles.get(profile)
    if not isinstance(value, dict):
        return False
    settings = value.get("settings")
    if not isinstance(settings, dict):
        return False
    env = settings.get("env")
    if not isinstance(env, dict):
        return False
    base = env.get("ANTHROPIC_BASE_URL")
    return isinstance(base, str) and "deepseek" in base.lower()


def _read_key(prompt):
    """Print `prompt` and read ONE key. Raw mode when stdin is a tty, else a plain read(1)."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if not sys.stdin.isatty():
        return sys.stdin.read(1)
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def confirm(status_path=""):
    """Show the warning, read one key and record the decision.

    Only s/S/y/Y mean yes; any other key, Enter, Esc, Ctrl+C and EOF mean no. The decision is
    written to `status_path` (when given) on every path, including cancellation and error."""
    result = False
    try:
        print(t("session.risk.deepseek.title"))
        print()
        print(t("session.risk.deepseek.summary"))
        print()
        key = _read_key(t("session.risk.confirm"))
        result = key in ("s", "S", "y", "Y")
    except Exception:
        result = False
    finally:
        if status_path:
            try:
                Path(status_path).write_text("yes" if result else "no", encoding="utf-8")
            except OSError:
                pass
    return result


def _usage():
    print("usage: session_risk.py --check <profile>", file=sys.stderr)
    print("       session_risk.py --confirm <profile> [--status <file>]", file=sys.stderr)
    return 2


def main(argv):
    args = argv[1:]
    if len(args) == 2 and args[0] == "--check":
        return 0 if risky(args[1]) else 1
    if len(args) >= 2 and args[0] == "--confirm":
        profile = args[1]
        status = ""
        if len(args) >= 4 and args[2] == "--status":
            status = args[3]
        elif len(args) > 2:
            return _usage()
        if not profile:
            return _usage()
        return 0 if confirm(status) else 1
    return _usage()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
