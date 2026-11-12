#!/usr/bin/env python3
"""Lifecycle owner of the workspace tmux servers: which profile each server serves, and which are up.

A workspace server is a tmux server on the socket `ws-<profile>`, with a session named `<profile>`.
The profile is the agent (`claude`, `opencode-claude`...); the server is the tmux side. This module
maps one to the other and lists the live servers. It starts and kills nothing: `bin/workspace`
(restart, stop, reload) does that, once for each profile named here.

    python3 lifecycle.py profiles [profile|ws-server]   profiles to act on: the one named, or every live server
"""
import os
import stat
import sys

import tmux

PREFIX = "ws-"


def server_of(profile):
    """tmux socket name of a profile's server."""
    return PREFIX + profile


def profile_of(server):
    """Profile served by a `ws-*` server, or "" when the name is not a workspace server."""
    if not server.startswith(PREFIX) or len(server) == len(PREFIX):
        return ""
    return server[len(PREFIX):]


def profile_of_argument(arg):
    """Profile named by a command argument: a profile (`claude`) or its server (`ws-claude`)."""
    arg = str(arg or "").strip()
    if arg.startswith(PREFIX):
        return profile_of(arg)
    return arg


def socket_dir():
    """Directory where tmux keeps the sockets of this user (`$TMUX_TMPDIR`, else /tmp)."""
    return os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}")


def _is_socket(path):
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _sessions(server):
    """Session names the server holds; empty when it is down."""
    return set(tmux.run("-L", server, "list-sessions", "-F", "#{session_name}").split())


def live_profiles():
    """Profiles whose workspace server is up now, sorted. A socket whose session is gone is not live."""
    folder = socket_dir()
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    found = []
    for name in names:
        profile = profile_of(name)
        if not profile or not _is_socket(os.path.join(folder, name)):
            continue
        if profile in _sessions(name):
            found.append(profile)
    return found


def profiles(arg=""):
    """Profiles a lifecycle command acts on: the one named by `arg` (empty when `arg` names no profile),
    or every live server when no argument is given."""
    if str(arg or "").strip():
        name = profile_of_argument(arg)
        return [name] if name else []
    return live_profiles()


def main(argv):
    if len(argv) < 2 or argv[1] != "profiles":
        print("usage: lifecycle.py profiles [profile|ws-server]", file=sys.stderr)
        return 2
    for name in profiles(argv[2] if len(argv) > 2 else ""):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
