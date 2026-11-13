"""Tests for the worktree rows of the repos panel (src/panel_repos.py).

Every test points AT_WS_CONFIG, WS_STATE and WS_WORKTREES at a temporary folder, so the real config,
state and worktree root are never read or written. The repos are real git repositories created by the
test (no network); the window state is mocked through session_state, so no tmux is needed.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import glyphs  # noqa: E402
import lib  # noqa: E402
import session_state  # noqa: E402
import worktrees  # noqa: E402
import yaml_min  # noqa: E402
from i18n import _STRINGS, t  # noqa: E402

import panel_repos  # noqa: E402


def make_repo(path):
    """A git repo with one commit on main (a worktree needs a commit to branch from)."""
    path.mkdir(parents=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    (path / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                    "commit", "-q", "-m", "init"], check=True, env=env)


class PanelWorktreesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.folder = self.root / "Codes"
        self.folder.mkdir()
        for name in ("alpha", "beta"):
            make_repo(self.folder / name)
        self.config = self.root / "config.yaml"
        self.config.write_text(yaml_min.dumps({"repos_folder": str(self.folder)}), encoding="utf-8")
        old = {name: os.environ.get(name) for name in ("AT_WS_CONFIG", "WS_STATE", "WS_WORKTREES", "TMUX_PANE")}
        os.environ["AT_WS_CONFIG"] = str(self.config)
        os.environ["WS_STATE"] = str(self.root / "state")
        os.environ["WS_WORKTREES"] = str(self.root / "worktrees")
        os.environ.pop("TMUX_PANE", None)

        def restore():
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        panel_repos._WT_CACHE.clear()
        self.addCleanup(panel_repos._WT_CACHE.clear)
        panel_repos.VIEW.update({"filter": "all", "note": "", "note_until": 0.0})
        self.addCleanup(panel_repos.VIEW.update, {"filter": "all", "note": "", "note_until": 0.0})
        self.alpha = str(self.folder / "alpha")
        self.beta = str(self.folder / "beta")

    def body(self):
        _header, body = panel_repos.collect(mock.Mock())
        return body

    def keyed(self, body, kind):
        return [row for row in body if row.key and row.key[0] == kind]

    def index_of_repo(self, body, path):
        return next(i for i, row in enumerate(body) if row.key == ("r", path))

    # listing ---------------------------------------------------------------------------------
    def test_worktrees_of_lists_extra_worktrees_and_leaves_the_main_checkout_out(self):
        worktrees.create(self.alpha, "feature")
        found = panel_repos.worktrees_of(self.alpha)
        self.assertEqual([branch for branch, _ in found], ["feature"])
        self.assertEqual(os.path.realpath(found[0][1]), os.path.realpath(str(self.root / "worktrees" / "alpha" / "feature")))

    def test_worktrees_of_a_path_that_is_not_a_repo_is_empty(self):
        self.assertEqual(panel_repos.worktrees_of(str(self.root / "nowhere")), [])

    def test_listing_is_cached_for_the_ttl(self):
        with mock.patch.object(worktrees, "list_worktrees", return_value=[]) as listing:
            panel_repos.worktrees_of(self.alpha)
            panel_repos.worktrees_of(self.alpha)
        self.assertEqual(listing.call_count, 1)

    # rows ------------------------------------------------------------------------------------
    def test_repo_without_extra_worktree_has_no_child_rows(self):
        body = self.body()
        self.assertEqual(self.keyed(body, "w"), [])
        self.assertFalse(any("↳" in row.text for row in body))

    def test_worktree_row_sits_under_its_repo_indented_and_dim_without_window(self):
        path = worktrees.create(self.alpha, "feature")
        body = self.body()
        at = self.index_of_repo(body, self.alpha)
        child = body[at + 1]
        self.assertEqual(child.key, ("w", str(path)))
        self.assertTrue(child.text.startswith("     "))
        self.assertIn("↳ feature", child.text)
        self.assertNotIn(glyphs.dot("idle"), child.text)

    def test_worktree_row_shows_the_agent_dot_when_its_window_has_tabs(self):
        path = str(worktrees.create(self.alpha, "feature"))
        tabs = {path: [{"id": "tab-1"}]}
        states = {path: "working"}
        with mock.patch.object(session_state, "tab_states", side_effect=lambda repo, *a, **k: tabs.get(repo, [])), \
                mock.patch.object(session_state, "repo_state", side_effect=lambda repo, *a, **k: states.get(repo, "idle")):
            child = next(row for row in self.body() if row.key == ("w", path))
        self.assertIn(glyphs.dot("working"), child.text)
        self.assertIn("feature", child.text)
        self.assertNotIn("↳", child.text)

    def test_detached_worktree_reads_with_the_detached_label(self):
        detached = self.root / "detached-wt"
        detached.mkdir()
        entries = [{"path": str(self.alpha), "branch": "main"}, {"path": str(detached), "branch": None}]
        with mock.patch.object(worktrees, "list_worktrees", return_value=entries):
            child = next(row for row in self.body() if row.key == ("w", str(detached)))
        self.assertIn(t("repos.worktree.detached"), child.text)

    def test_missing_worktree_directory_is_not_listed(self):
        entries = [{"path": str(self.alpha), "branch": "main"},
                   {"path": str(self.root / "gone"), "branch": "old"}]
        with mock.patch.object(worktrees, "list_worktrees", return_value=entries):
            self.assertEqual(panel_repos.worktrees_of(self.alpha), [])

    def test_git_error_keeps_the_repo_without_children(self):
        with mock.patch.object(worktrees, "list_worktrees", side_effect=worktrees.WorktreeError("boom")):
            body = self.body()
        self.assertEqual(self.keyed(body, "w"), [])
        self.assertIn(("r", self.alpha), [row.key for row in body])
        self.assertIn(("r", self.beta), [row.key for row in body])

    def test_switched_off_repo_reads_no_worktrees(self):
        lib.set_repo_enabled("alpha", False)
        with mock.patch.object(worktrees, "list_worktrees", return_value=[]) as listing:
            self.body()
        called = [os.path.realpath(call.args[0]) for call in listing.call_args_list]
        self.assertNotIn(os.path.realpath(self.alpha), called)
        self.assertIn(os.path.realpath(self.beta), called)

    def test_worktree_rows_appear_under_the_main_repo_too(self):
        main_folder = self.root / "main-repo"
        make_repo(main_folder)
        self.config.write_text(yaml_min.dumps({"repos_folder": str(self.folder), "main": str(main_folder)}),
                               encoding="utf-8")
        worktrees.create(str(main_folder), "side")
        body = self.body()
        at = self.index_of_repo(body, str(main_folder))
        self.assertIn("↳ side", body[at + 1].text)
        self.assertTrue(body[at + 1].text.startswith("   "))

    # enter -----------------------------------------------------------------------------------
    def test_enter_on_a_worktree_opens_or_focuses_its_window(self):
        path = str(worktrees.create(self.alpha, "feature"))
        with mock.patch.object(panel_repos.subprocess, "run") as run:
            panel_repos.enter(mock.Mock(), ("w", path), "enter")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [lib.WS, "open", path])

    def test_t_on_a_worktree_switches_nothing(self):
        path = str(worktrees.create(self.alpha, "feature"))
        with mock.patch.object(panel_repos, "toggle") as toggle:
            panel_repos.on_key(mock.Mock(key=("w", path)), "t")
        toggle.assert_not_called()


class TextsTest(unittest.TestCase):
    def test_worktree_text_exists_in_pt_br(self):
        self.assertIn("repos.worktree.detached", _STRINGS)


if __name__ == "__main__":
    unittest.main()
