#!/usr/bin/env python3
"""Profile menu for a new session: every profile of config.yaml["profiles"] plus claude.

`session-menu <window>` (the + on the session tab strip, Enter, or ctrl+b t) shows a blocking tmux
menu; each item runs `workspace session-new <window> <profile>` in the background. The configured
agent is marked with a filled dot, the others with an open one.
"""
import sys

import i18n
import lib
import tmux


def keys():
    """Menu keys: 1..9, then a..z."""
    return [str(i) for i in range(1, 10)] + [chr(c) for c in range(ord("a"), ord("z") + 1)]


def menu_items(window):
    """[(label, key, command)] of every profile, default first marked with ●, others ○."""
    items = []
    default = lib.agent_name()
    for profile, key in zip(lib.profile_names(), keys()):
        marker = "●" if profile == default else "○"
        command = "run-shell -b " + tmux.quote_cmd([lib.WS, "session-new", window, profile])
        items.append((f"{marker} {profile}", key, command))
    return items


def main(window):
    tmux.menu(i18n.t("session.menu.title"), menu_items(window))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
