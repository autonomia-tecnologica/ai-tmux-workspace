"""Tests for the session tab strip and the profile menu (no tmux server needed)."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import i18n  # noqa: E402
import panel_tabs  # noqa: E402
import session_menu  # noqa: E402
import session_placeholder  # noqa: E402
import tmux  # noqa: E402


class MenuItemsTest(unittest.TestCase):
    def test_default_profile_is_marked_and_keys_are_unique(self):
        with mock.patch.object(session_menu.lib, "profile_names", return_value=["claude", "opencode-free", "deepseek"]), \
                mock.patch.object(session_menu.lib, "agent_name", return_value="opencode-free"), \
                mock.patch.object(session_menu.lib, "WS", "/bin/workspace"):
            items = session_menu.menu_items("%7")

        self.assertEqual([label for label, _, _ in items],
                         ["○ claude", "● opencode-free", "○ deepseek"])
        keys = [key for _, key, _ in items]
        self.assertEqual(keys, ["1", "2", "3"])
        self.assertEqual(len(keys), len(set(keys)))

    def test_command_runs_session_new_in_the_background_and_is_quoted(self):
        with mock.patch.object(session_menu.lib, "profile_names", return_value=["claude"]), \
                mock.patch.object(session_menu.lib, "agent_name", return_value="claude"), \
                mock.patch.object(session_menu.lib, "WS", "/bin/workspace"):
            items = session_menu.menu_items("%7")

        _, _, command = items[0]
        self.assertEqual(
            command, "run-shell -b " + tmux.quote_cmd(["/bin/workspace", "session-new", "%7", "claude"]))
        self.assertTrue(command.startswith("run-shell -b "))


class SessionLabelsTest(unittest.TestCase):
    def test_labels_are_profiles_and_repeated_profiles_are_numbered(self):
        profile_of = {"%1": "claude", "%2": "opencode-free", "%3": "claude", "%4": "deepseek"}.get
        labels = panel_tabs.session_labels(["%1", "%2", "%3", "%4"], profile_of, alive=lambda p: True)
        self.assertEqual(labels, [("%1", "CLAUDE"), ("%2", "OPENCODE-FREE"),
                                  ("%3", "CLAUDE (2)"), ("%4", "DEEPSEEK")])

    def test_dead_pane_is_ignored(self):
        profile_of = {"%1": "claude", "%2": "opencode"}.get
        labels = panel_tabs.session_labels(["%1", "%2"], profile_of, alive=lambda p: p == "%1")
        self.assertEqual(labels, [("%1", "CLAUDE")])

    def test_pane_without_profile_uses_the_session_label(self):
        profile_of = {"%1": "claude"}.get
        labels = panel_tabs.session_labels(["%1", "%2"], profile_of, alive=lambda p: True)
        self.assertEqual(labels, [("%1", "CLAUDE"), ("%2", i18n.t("tabs.session"))])


class PlaceholderTest(unittest.TestCase):
    def test_texts_come_from_i18n(self):
        self.assertEqual(session_placeholder.texts(),
                         (i18n.t("session.placeholder.title"), i18n.t("session.placeholder.hint")))


if __name__ == "__main__":
    unittest.main()
