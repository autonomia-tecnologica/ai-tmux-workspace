"""Regression tests for the hooks that keep the layout fitted to the active client.

The phone (SSH) layout is applied by `workspace fit`, which the tmux hooks call. Switching repo
windows resizes the new window but fires no client hook, so without a window/session hook the phone
kept the desktop pane layout squeezed into its width (no zoom) until some client event happened.
These tests read config/tmux.conf; they need no tmux server.
"""
import re
import unittest
from pathlib import Path

CONF = Path(__file__).resolve().parent.parent / "config" / "tmux.conf"

# Events that must re-apply the layout: client events (attach/resize/switch/focus) and window events
# (tmux followed the latest client, or the current window changed).
FIT_EVENTS = (
    "client-attached",
    "client-resized",
    "client-active",
    "client-focus-in",
    "window-resized",
    "session-window-changed",
)

HOOK_RE = re.compile(r'^set-hook -g (\S+) "(.*)"\s*$')


def hooks(conf):
    """event -> list of hook commands, in file order (duplicates visible)."""
    found = {}
    for line in conf.splitlines():
        match = HOOK_RE.match(line.strip())
        if match:
            found.setdefault(match.group(1), []).append(match.group(2))
    return found


class FitHooksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conf = CONF.read_text(encoding="utf-8")
        cls.hooks = hooks(cls.conf)

    def test_window_size_follows_the_last_used_client(self):
        self.assertIn("set -g window-size latest", self.conf)

    def test_every_relevant_event_runs_one_fit(self):
        for event in FIT_EVENTS:
            with self.subTest(event=event):
                self.assertIn(event, self.hooks, f"{event} must call fit")
                self.assertEqual(len(self.hooks[event]), 1, f"{event} must have one fit hook")

    def test_fit_hooks_run_in_the_background_for_the_profile(self):
        for event in FIT_EVENTS:
            for command in self.hooks[event]:
                with self.subTest(event=event, command=command):
                    self.assertTrue(command.startswith("run-shell -b "), command)
                    self.assertIn("bin/workspace fit '#{@ws_profile}'", command)
                    self.assertIn(">/dev/null 2>&1", command)

    def test_fit_hooks_are_identical(self):
        commands = {command for event in FIT_EVENTS for command in self.hooks[event]}
        self.assertEqual(len(commands), 1, commands)


if __name__ == "__main__":
    unittest.main()
