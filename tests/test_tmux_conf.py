"""Regression tests for the hooks that keep the layout fitted to the active client, and for the
single owner of the geometry: `layout_apply` is the only place that resizes, zooms or refreshes.

The phone (SSH) layout is applied by `workspace fit`, which the tmux hooks call. Switching repo
windows resizes the new window but fires no client hook, so without a window/session hook the phone
kept the desktop pane layout squeezed into its width (no zoom) until some client event happened.
These tests read config/tmux.conf and bin/workspace; they need no tmux server.
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


class StatusBarProfileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conf = CONF.read_text(encoding="utf-8")

    def test_statusbar_receives_session_profile_with_fallback_to_window_profile(self):
        self.assertIn(
            "statusbar.py '#{@ws_repo}' '#{?#{@ws_session_profile},#{@ws_session_profile},#{@ws_profile}}'",
            self.conf,
        )


BIN = Path(__file__).resolve().parent.parent / "bin" / "workspace"
FUNC_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{")


def functions(text):
    """name -> body of every top-level bash function (the file closes each with `}` at column 0)."""
    blocks, name, body = {}, None, []
    for line in text.splitlines():
        if name is None:
            match = FUNC_RE.match(line)
            if not match:
                continue
            if line.count("{") <= line.count("}"):   # one-line function
                blocks[match.group(1)] = line
                continue
            name, body = match.group(1), []
            continue
        if line == "}":
            blocks[name] = "\n".join(body)
            name = None
        else:
            body.append(line)
    return blocks


class LayoutOwnerTest(unittest.TestCase):
    """One single owner of the geometry: a session action never resizes tmux."""

    @classmethod
    def setUpClass(cls):
        cls.text = BIN.read_text(encoding="utf-8")
        cls.functions = functions(cls.text)

    def test_layout_apply_replaced_the_old_helpers(self):
        self.assertIn("layout_apply", self.functions)
        self.assertNotIn("mobile_apply", self.functions)
        self.assertNotIn("fit_window", self.functions)

    def test_only_layout_apply_resizes_zooms_or_refreshes(self):
        for name, body in self.functions.items():
            if name == "layout_apply":
                continue
            for command in ("resize-pane", "select-pane -Z", "refresh-client"):
                with self.subTest(function=name, command=command):
                    self.assertNotIn(command, body)

    def test_layout_apply_owns_every_geometry_change(self):
        body = self.functions["layout_apply"]
        for command in ("resize-pane", "select-pane -Z", "refresh-client", "@ws_mzoom"):
            self.assertIn(command, body)

    def test_session_paths_do_not_reapply_the_layout(self):
        for name in ("tab_show", "tab_new", "tab_exit", "tab_step", "session_new", "session_exit",
                     "session_run", "resume", "open_sessions", "session_restore", "side", "left"):
            with self.subTest(function=name):
                self.assertNotIn("layout_apply", self.functions[name])

    def test_tab_switch_keeps_the_phone_zoom_without_resizing(self):
        # -Z keeps the window zoomed, so the phone stays inside the geometry applied by layout_apply
        self.assertIn("swap-pane -dZ", self.functions["tab_show"])

    def test_only_the_entries_that_change_the_target_apply_the_layout_again(self):
        for name in ("build_window", "ensure_window", "open_repo", "mview", "fit"):
            with self.subTest(function=name):
                self.assertIn("layout_apply", self.functions[name])

    def test_fit_applies_once_without_repeating_passes(self):
        body = self.functions["fit"]
        self.assertIn("layout_apply", body)
        self.assertNotIn("again", body)
        self.assertNotIn("sleep", body)

    def test_strips_are_signalled_instead_of_respawned(self):
        for name in ("tab_render", "side", "left"):
            with self.subTest(function=name):
                self.assertIn("panel_signal", self.functions[name])
        self.assertNotIn("respawn-pane", self.functions["tab_render"])

    def test_reload_does_not_refresh_the_client(self):
        self.assertNotIn("refresh-client", self.functions["reload"])


if __name__ == "__main__":
    unittest.main()
