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
                         ["● opencode-free", "○ claude", "○ deepseek"])
        keys = [key for _, key, _ in items]
        self.assertEqual(keys, ["1", "2", "3"])
        self.assertEqual(len(keys), len(set(keys)))

    def test_command_runs_session_new_in_the_background_and_is_quoted(self):
        with mock.patch.object(session_menu.lib, "profile_names", return_value=["claude"]), \
                mock.patch.object(session_menu.lib, "agent_name", return_value="claude"), \
                mock.patch.object(session_menu.lib, "WS", "/bin/workspace"):
            items = session_menu.menu_items("%7")

        _, _, command = items[0]
        line = "/bin/workspace session-new %7 claude >/dev/null 2>&1"
        self.assertEqual(command, "run-shell -b " + tmux.quote_cmd(["sh", "-c", line]))
        self.assertTrue(command.startswith("run-shell -b "))

    def test_item_output_is_discarded_so_no_message_covers_the_new_session(self):
        with mock.patch.object(session_menu.lib, "profile_names", return_value=["claude", "opencode"]), \
                mock.patch.object(session_menu.lib, "agent_name", return_value="claude"), \
                mock.patch.object(session_menu.lib, "WS", "/bin/workspace"):
            items = session_menu.menu_items("%7")

        for _, _, command in items:
            with self.subTest(command=command):
                self.assertIn(">/dev/null 2>&1", command)


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


class PanelStateTest(unittest.TestCase):
    def test_load_rereads_the_state_and_keeps_a_live_cursor(self):
        view = {"cursor": "%2"}
        with mock.patch.object(panel_tabs, "state",
                               return_value=("%9", [("%1", "A"), ("%2", "B"), ("%3", "C")], "%1")):
            panel_tabs.load(view)

        self.assertEqual(view["win"], "%9")
        self.assertEqual(view["items"], ["%1", "%2", "%3", "new"])
        self.assertEqual(view["cursor"], "%2")

    def test_cursor_falls_back_to_the_active_tab_when_its_tab_is_gone(self):
        view = {"cursor": "%2"}
        with mock.patch.object(panel_tabs, "state",
                               return_value=("%9", [("%1", "A"), ("%3", "C")], "%3")):
            panel_tabs.load(view)

        self.assertEqual(view["cursor"], "%3")

    def test_cursor_index_is_clamped_without_an_active_tab(self):
        self.assertEqual(panel_tabs.cursor_index(["%1", "new"], "gone"), 0)
        self.assertEqual(panel_tabs.cursor_index(["%1", "new"], "gone", "%1"), 0)


class TabStylesTest(unittest.TestCase):
    ITEMS = ["%1", "%2", "new"]

    def test_cursor_on_the_active_tab_is_blue_and_the_plus_is_gray(self):
        styles = panel_tabs.item_styles(self.ITEMS, "%1", "%1", focused=True)
        self.assertEqual(styles, {"%1": "active", "%2": "idle", "new": "idle"})

    def test_cursor_on_another_tab_moves_the_blue_and_the_active_turns_gray(self):
        styles = panel_tabs.item_styles(self.ITEMS, "%1", "%2", focused=True)
        self.assertEqual(styles, {"%1": "idle", "%2": "active", "new": "idle"})

    def test_cursor_on_the_plus_is_blue_and_the_active_stays_gray(self):
        styles = panel_tabs.item_styles(self.ITEMS, "%1", "new", focused=True)
        self.assertEqual(styles, {"%1": "idle", "%2": "idle", "new": "active"})

    def test_lost_focus_leaves_only_the_active_tab_blue(self):
        styles = panel_tabs.item_styles(self.ITEMS, "%1", "%2", focused=False)
        self.assertEqual(styles, {"%1": "active", "%2": "idle", "new": "idle"})

    def test_losing_focus_sends_the_cursor_back_to_the_active_tab(self):
        view = {"items": self.ITEMS, "active": "%1", "cursor": "%2", "focused": True}
        panel_tabs.set_focus(view, False)
        self.assertFalse(view["focused"])
        self.assertEqual(view["cursor"], "%1")

    def test_gaining_focus_starts_the_cursor_on_the_active_tab(self):
        view = {"items": self.ITEMS, "active": "gone", "cursor": "new", "focused": False}
        panel_tabs.set_focus(view, True)
        self.assertTrue(view["focused"])
        self.assertEqual(view["cursor"], "%1")

    def test_activation_moves_the_cursor_to_the_new_active_tab(self):
        view = {"cursor": "new", "follow": True}
        with mock.patch.object(panel_tabs, "state",
                               return_value=("%9", [("%1", "A"), ("%2", "B")], "%2")):
            panel_tabs.load(view)
        self.assertEqual(view["cursor"], "%2")
        self.assertNotIn("follow", view)

    def test_plus_at_rest_uses_the_idle_gray_not_the_new_tab_blue(self):
        text, _ = panel_tabs.draw([("%1", "A")], "%1", cursor="%1", focused=False)
        self.assertIn(f"{panel_tabs.TAB_IDLE} + {panel_tabs.R}", text)
        self.assertIn(f"{panel_tabs.TAB_ACTIVE} A {panel_tabs.R}", text)

    def test_focused_cursor_on_the_plus_paints_it_blue(self):
        text, _ = panel_tabs.draw([("%1", "A")], "%1", cursor="new", focused=True)
        self.assertIn(f"{panel_tabs.TAB_ACTIVE} + {panel_tabs.R}", text)
        self.assertIn(f"{panel_tabs.TAB_IDLE} A {panel_tabs.R}", text)


class _Out:
    def __init__(self):
        self.writes = []

    def write(self, text):
        self.writes.append(text)

    def flush(self):
        pass


class PanelPaintTest(unittest.TestCase):
    def test_unchanged_state_draws_nothing(self):
        view = {"tabs": [("%1", "A")], "active": "%1", "cursor": "%1"}
        out = _Out()
        with mock.patch.object(panel_tabs.sys, "stdout", out):
            self.assertTrue(panel_tabs.paint(view, first=True))
            self.assertFalse(panel_tabs.paint(view))

        self.assertEqual(len(out.writes), 1)

    def test_changed_state_draws_again(self):
        view = {"tabs": [("%1", "A")], "active": "%1", "cursor": "%1"}
        out = _Out()
        with mock.patch.object(panel_tabs.sys, "stdout", out):
            panel_tabs.paint(view, first=True)
            view["tabs"] = [("%1", "A"), ("%2", "B")]
            self.assertTrue(panel_tabs.paint(view))

        self.assertEqual(len(out.writes), 2)


if __name__ == "__main__":
    unittest.main()
