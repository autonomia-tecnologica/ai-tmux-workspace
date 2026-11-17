"""The options screen exposes the two conference start modes: group repos and selected repos."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import conference  # noqa: E402
import i18n  # noqa: E402
import options  # noqa: E402


class FakePickViewer:
    """Applies (key, value) pairs in order; None as key keeps the current row."""

    keys = []
    last = None

    def __init__(self, collect, **kwargs):
        self.collect = collect
        self.key = kwargs.get("initial_key")
        self.on_input = kwargs.get("on_input")
        self.on_key = kwargs.get("on_key")
        self.footer = kwargs.get("footer")
        self.stopped = False
        self.redraw = mock.Mock()
        self.top, self.frame = 0, None
        type(self).last = self

    def _wake(self):
        pass

    def run(self):
        self.frame = self.collect(self)
        for key, k in type(self).keys:
            if key is not None:
                self.key = key
            if k is None:
                continue
            if self.on_input and self.on_input(self, k):
                if self.stopped:
                    break
                continue
            if self.on_key and self.on_key(self, k):
                break


class OptionsConferenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo-a"
        self.mate = self.tmp / "repo-b"
        self.repo.mkdir()
        self.mate.mkdir()
        self.cfg = self.tmp / "config.yaml"
        self.cfg.write_text(
            "profiles:\n  claude: claude\n"
            "groups:\n  Pair:\n"
            f"    - {self.repo}\n"
            f"    - {self.mate}\n",
            encoding="utf-8",
        )
        # Stub of lib.group_mates (the group contract): the two repos share one group.
        self.mates = {os.path.realpath(self.repo): [os.path.realpath(self.mate)],
                      os.path.realpath(self.mate): [os.path.realpath(self.repo)]}
        stub = mock.patch("lib.group_mates",
                          side_effect=lambda path: list(self.mates.get(os.path.realpath(path), [])))
        stub.start()
        self.addCleanup(stub.stop)
        env = mock.patch.dict(os.environ, {"AT_WS_CONFIG": str(self.cfg), "WS_STATE": str(self.tmp),
                                           "WS_CONFERENCE": str(self.tmp / "conference")})
        env.start()
        self.addCleanup(env.stop)

    def paths(self, *repos):
        return [str(Path(repo).resolve()) for repo in repos]

    def test_the_conference_folder_offers_the_two_start_modes(self):
        folder = next(n for n in options.tree() if n.get("label") == i18n.t("options.conference"))
        self.assertEqual([c["conference"] for c in folder["children"]], ["group", "selected"])

    def test_the_conference_leaves_are_visible_on_the_flat_screen(self):
        actions = [node["conference"] for _key, _depth, node in options.flatten(options.tree())
                   if "conference" in node]
        self.assertEqual(actions, ["group", "selected"])

    def test_group_start_opens_the_conversation_screen(self):
        with mock.patch.object(options.tmux, "display", return_value=str(self.repo)), \
                mock.patch("conference.converse") as converse:
            options.run_conference({"label": "Grupo", "conference": "group"}, "%1")
        self.assertEqual(conference.read_active()["repos"],
                         self.paths(self.repo, self.mate))
        converse.assert_called_once_with(repo=str(self.repo))

    def test_selected_start_uses_the_picked_repos(self):
        picked = self.paths(self.repo, self.mate)
        with mock.patch.object(options.tmux, "display", return_value=str(self.repo)), \
                mock.patch.object(options, "choose_repos", return_value=picked) as pick, \
                mock.patch("conference.converse") as converse:
            options.run_conference({"label": "Selecionados", "conference": "selected"}, "%1")
        pick.assert_called_once_with(str(self.repo))
        self.assertEqual(conference.read_active()["repos"], picked)
        converse.assert_called_once_with(repo=str(self.repo))

    # The picker keys its rows by resolved path (macOS: /var is a link to /private/var).
    def test_the_picker_prechecks_the_window_repo_and_returns_the_marked_paths(self):
        FakePickViewer.keys = [(self.paths(self.mate)[0], " "), (None, "\r")]
        with mock.patch.object(options, "Viewer", FakePickViewer):
            picked = options.choose_repos(str(self.repo))
        self.assertEqual(picked, self.paths(self.repo, self.mate))

    def test_the_picker_returns_only_the_repos_left_marked(self):
        FakePickViewer.keys = [(self.paths(self.repo)[0], " "), (self.paths(self.mate)[0], " "),
                               (None, "\r")]
        with mock.patch.object(options, "Viewer", FakePickViewer):
            picked = options.choose_repos(str(self.repo))
        self.assertEqual(picked, self.paths(self.mate))

    def test_the_picker_cancel_returns_nothing(self):
        FakePickViewer.keys = [(self.paths(self.mate)[0], " "), (None, "esc")]
        with mock.patch.object(options, "Viewer", FakePickViewer):
            picked = options.choose_repos(str(self.repo))
        self.assertEqual(picked, [])          # no Enter: cancelled even with marks

    def test_the_picker_with_nothing_marked_stays_open_and_returns_nothing(self):
        FakePickViewer.keys = [(self.paths(self.repo)[0], " "), (None, "\r")]
        with mock.patch.object(options, "Viewer", FakePickViewer):
            picked = options.choose_repos(str(self.repo))
        self.assertEqual(picked, [])
        frame = FakePickViewer.last.collect(FakePickViewer.last)   # redraw after the notice
        body = "\n".join(l.text for l in frame[1])
        self.assertIn(i18n.t("options.conference.pick_none"), body)

    def test_the_picker_lists_the_window_repo_first(self):
        entries = options.workspace_repos(str(self.repo))
        self.assertEqual(entries[0][1][0], (self.repo.name, self.paths(self.repo)[0]))
        paths = [path for _, repos in entries for _, path in repos]
        self.assertEqual(paths, self.paths(self.repo, self.mate))

    # --- OpenCode harness: Claude-only surfaces degrade with a warning ----------------------
    def opencode_workspace(self):
        self.cfg.write_text(
            "profiles:\n  opencode:\n    harness: opencode\nagent: opencode\n"
            "groups:\n  Pair:\n"
            f"    - {self.repo}\n"
            f"    - {self.mate}\n",
            encoding="utf-8")

    def display(self, profile, session=""):
        def display(option, **_kwargs):
            if option == "#{@ws_session_profile}":
                return session
            if option == "#{@ws_profile}":
                return profile
            return str(self.repo)
        return display

    def test_a_window_opencode_profile_runs_the_conference_on_opencode(self):
        self.opencode_workspace()
        with mock.patch.object(options.tmux, "display", side_effect=self.display("opencode")), \
                mock.patch("conference.converse") as converse:
            options.run_conference({"label": "Grupo", "conference": "group"}, "%1")
        active = conference.read_active()
        self.assertEqual(active["profile"], "opencode")
        self.assertEqual(active["repos"], self.paths(self.repo, self.mate))
        converse.assert_called_once()

    def test_a_claude_window_profile_is_recorded_for_the_participants(self):
        with mock.patch.object(options.tmux, "display", side_effect=self.display("claude")), \
                mock.patch("conference.converse"):
            options.run_conference({"label": "Grupo", "conference": "group"}, "%1")
        self.assertEqual(conference.read_active()["profile"], "claude")

    def test_without_a_window_profile_an_opencode_agent_is_refused(self):
        self.opencode_workspace()
        with mock.patch.object(options.tmux, "display", side_effect=self.display("")), \
                mock.patch.object(options, "show") as show, \
                mock.patch.object(options, "choose_repos") as pick:
            options.run_conference({"label": "Grupo", "conference": "group"}, "%1")
        show.assert_called_once()
        self.assertIn(i18n.t("agent.surface.conference"), show.call_args[0][1])
        pick.assert_not_called()
        self.assertEqual(conference.read_active(), {})

    def test_an_active_conference_is_resumed_instead_of_started_again(self):
        conference.start(self.paths(self.repo, self.mate), profile="claude")
        with mock.patch.object(options.tmux, "display", side_effect=self.display("claude")), \
                mock.patch.object(options, "choose_repos") as pick, \
                mock.patch("conference.converse") as converse:
            options.run_conference({"label": "Selecionados", "conference": "selected"}, "%1")
        pick.assert_not_called()
        converse.assert_called_once()

    def test_ask_degrades_on_an_opencode_profile(self):
        self.opencode_workspace()
        with mock.patch.object(options.tmux, "display", side_effect=self.display("", session="")), \
                mock.patch.object(options, "show") as show, \
                mock.patch.object(options, "repo_and_command") as repo_and_command:
            options.run_leaf({"label": "Haiku", "ask": "haiku"}, "%1")
        show.assert_called_once()
        self.assertIn(i18n.t("agent.surface.ask"), show.call_args[0][1])
        repo_and_command.assert_not_called()

    def test_the_ask_leaf_refuses_an_opencode_session_profile(self):
        self.opencode_workspace()
        with mock.patch.object(options.tmux, "display",
                               side_effect=self.display("claude", session="opencode")), \
                mock.patch.object(options, "show") as show, \
                mock.patch.object(options, "repo_and_command") as repo_and_command:
            options.run_leaf({"label": "Haiku", "ask": "haiku"}, "%1")
        show.assert_called_once()
        self.assertIn(i18n.t("agent.surface.ask"), show.call_args[0][1])
        repo_and_command.assert_not_called()

    # --- window profile: the active session wins over the window ------------------------------
    def test_window_profile_reads_the_session_profile_first(self):
        self.opencode_workspace()
        with mock.patch.object(options.tmux, "display",
                               side_effect=self.display("claude", session="opencode")):
            self.assertEqual(options.window_profile("%1"), "opencode")

    def test_window_profile_falls_back_when_the_session_profile_is_empty(self):
        self.opencode_workspace()
        with mock.patch.object(options.tmux, "display",
                               side_effect=self.display("opencode", session="")):
            self.assertEqual(options.window_profile("%1"), "opencode")

    def test_window_profile_falls_back_when_the_session_profile_is_invalid(self):
        self.opencode_workspace()
        with mock.patch.object(options.tmux, "display",
                               side_effect=self.display("opencode", session="nope")):
            self.assertEqual(options.window_profile("%1"), "opencode")

    def test_repo_and_command_passes_the_window_profile(self):
        self.opencode_workspace()
        with mock.patch.object(options.tmux, "display",
                               side_effect=self.display("opencode", session="")), \
                mock.patch.object(options.lib, "agent_launch_command",
                                  return_value="opencode") as launch:
            repo, argv = options.repo_and_command("%1")
        self.assertEqual(repo, str(self.repo))
        launch.assert_called_once_with(repo=str(self.repo), profile="opencode")
        self.assertEqual(argv, ["opencode"])

    def test_switch_agent_saves_without_restarting_the_workspace(self):
        self.opencode_workspace()
        with mock.patch.object(options, "confirm_security", return_value=True), \
                mock.patch.object(options.lib, "set_agent") as set_agent, \
                mock.patch.object(options, "show") as show, \
                mock.patch.object(options.subprocess, "Popen") as popen:
            options.switch_agent("claude", "%1")
        set_agent.assert_called_once_with("claude")
        popen.assert_not_called()
        show.assert_called_once_with(i18n.t("options.agent"),
                                     i18n.t("options.agent.saved", profile="claude"))


if __name__ == "__main__":
    unittest.main()
