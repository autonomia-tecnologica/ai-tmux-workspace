#!/usr/bin/env python3
"""Placeholder shown in the main pane when a repo window has no open session.

Draws the SESSÕES title and hint and redraws them on SIGWINCH; it never spawns a shell. It reads
only the two session.placeholder i18n keys (with a literal fallback) and does not import lib/tmux,
so it keeps working while the workspace panels are reloading.
"""
import signal
import sys
import time

TITLE = "SESSÕES"
HINT = "Abra uma sessão pelo + na tira de abas acima."


def _text(key, fallback):
    try:
        import i18n
        return i18n.t(key)
    except Exception:
        return fallback


def texts():
    """(title, hint) from i18n, falling back to the literal when a key is missing."""
    return _text("session.placeholder.title", TITLE), _text("session.placeholder.hint", HINT)


def draw(title, hint):
    sys.stdout.write(f"\x1b[2J\x1b[H{title}\n\n{hint}\n")
    sys.stdout.flush()


def main():
    title, hint = texts()
    draw(title, hint)

    def redraw(*_):
        draw(title, hint)

    signal.signal(signal.SIGWINCH, redraw)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
