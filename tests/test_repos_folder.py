"""Tests for the repos folder (config "repos_folder") and the per-repo switch ("repos_disabled").

Every test points AT_WS_CONFIG and WS_STATE at a temporary folder, so the real
~/.config/ai-tmux-workspace and ~/.cache/workspace are never read or written. The repos are real
(empty) git repositories created by the test, so no network is used.
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

import lib  # noqa: E402
import yaml_min  # noqa: E402
from i18n import _STRINGS, t  # noqa: E402


def make_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


class ReposFolderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.folder = self.root / "Codes"
        self.folder.mkdir()
        self.config = self.root / "config.yaml"
        old = {name: os.environ.get(name) for name in ("AT_WS_CONFIG", "WS_STATE")}
        os.environ["AT_WS_CONFIG"] = str(self.config)
        os.environ["WS_STATE"] = str(self.root / "state")

        def restore():
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        for name in ("alpha", "beta", "gamma"):
            make_repo(self.folder / name)
        (self.folder / "notes").mkdir()            # not a git repo: not listed
        (self.folder / ".hidden").mkdir()          # hidden folder: not listed
        (self.folder / "file.txt").write_text("x", encoding="utf-8")
        self.write_config({"repos_folder": str(self.folder), "profiles": {"claude": "claude"}})

    def write_config(self, data):
        self.config.write_text(yaml_min.dumps(data), encoding="utf-8")

    def read_config(self):
        return yaml_min.loads(self.config.read_text(encoding="utf-8"))

    def names(self, repos):
        return [name for name, _ in repos]

    # folder reading
    def test_folder_lists_only_git_repos_sorted(self):
        self.assertEqual(self.names(lib.folder_repos()), ["alpha", "beta", "gamma"])
        self.assertEqual(lib.repos_folder(), str(self.folder))

    def test_read_repos_uses_the_folder_as_the_only_source(self):
        self.write_config({
            "repos_folder": str(self.folder),
            "groups": {"Old": [str(self.root / "elsewhere")]},   # outside the folder: not supported
        })
        groups = lib.read_repos()
        self.assertEqual([name for name, _ in groups], [t("repos.group.uncategorized")])
        self.assertEqual(self.names(groups[0][1]), ["alpha", "beta", "gamma"])

    def test_declared_groups_classify_the_folder_repos_and_the_rest_is_uncategorized(self):
        self.write_config({
            "repos_folder": str(self.folder),
            "groups": {"Work": [str(self.folder / "beta")]},
        })
        groups = lib.read_repos()
        self.assertEqual([name for name, _ in groups], ["Work", t("repos.group.uncategorized")])
        self.assertEqual(self.names(groups[0][1]), ["beta"])
        self.assertEqual(self.names(groups[1][1]), ["alpha", "gamma"])

    def test_without_folder_groups_are_read_as_before(self):
        repo = self.root / "solo"
        make_repo(repo)
        self.write_config({"groups": {"Team": [str(repo)]}})
        self.assertIsNone(lib.repos_folder())
        self.assertEqual(lib.read_repos(), [("Team", [("solo", str(repo.resolve()))])])
        self.assertTrue(lib.repo_enabled(str(repo)))

    def test_empty_folder_gives_no_group(self):
        self.write_config({"repos_folder": str(self.root / "missing")})
        self.assertEqual(lib.read_repos(), [])

    # the switch
    def test_every_folder_repo_is_on_by_default(self):
        self.assertEqual(lib.disabled_repos(), set())
        for _, path in lib.folder_repos():
            self.assertTrue(lib.repo_enabled(path))

    def test_switch_off_and_on_writes_the_config(self):
        lib.set_repo_enabled("beta", False)
        self.assertEqual(self.read_config()["repos_disabled"], ["beta"])
        self.assertFalse(lib.repo_enabled(str(self.folder / "beta")))
        self.assertTrue(lib.repo_enabled(str(self.folder / "alpha")))
        lib.set_repo_enabled("beta", True)
        self.assertNotIn("repos_disabled", self.read_config())   # empty list is dropped, not kept
        self.assertTrue(lib.repo_enabled(str(self.folder / "beta")))

    def test_write_keeps_other_keys_and_leaves_a_backup(self):
        before = self.read_config()
        lib.set_repo_enabled("alpha", False)
        lib.set_repo_enabled("gamma", False)
        after = self.read_config()
        self.assertEqual(after["repos_disabled"], ["alpha", "gamma"])
        self.assertEqual(after["repos_folder"], before["repos_folder"])
        self.assertEqual(after["profiles"], before["profiles"])
        backup = self.config.with_name("config.yaml.bak")
        self.assertTrue(backup.is_file())
        self.assertEqual(yaml_min.loads(backup.read_text(encoding="utf-8"))["repos_disabled"], ["alpha"])
        self.assertFalse(self.config.with_name("config.yaml.tmp").exists())

    def test_name_outside_the_folder_is_refused_and_nothing_is_written(self):
        before = self.config.read_text(encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            lib.set_repo_enabled("notes", False)     # a folder, not a git repo
        self.assertIn("notes", str(caught.exception))
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_unreadable_config_is_never_overwritten(self):
        self.config.write_text("repos_folder: [\n", encoding="utf-8")
        before = self.config.read_text(encoding="utf-8")
        with self.assertRaises(lib.ConfigError):
            lib.set_repo_enabled("alpha", False)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_switch_survives_a_reload(self):
        lib.set_repo_enabled("beta", False)
        # a reload re-reads the config in a new process: nothing lives in the tmux server or in memory
        out = subprocess.run([sys.executable, str(SRC / "lib.py"), "repo", "list"],
                             env=dict(os.environ), capture_output=True, text=True, check=True).stdout
        self.assertIn("beta\tdesligado", out)
        self.assertIn("alpha\tligado", out)

    def test_outside_paths_are_not_governed_by_the_switch(self):
        other = self.root / "outside"
        make_repo(other)
        self.assertTrue(lib.repo_enabled(str(other)))
        self.assertTrue(lib.repo_enabled(str(self.folder / "alpha" / "nested")))

    # agent gate
    def test_disabled_repo_builds_no_agent_command(self):
        lib.set_repo_enabled("beta", False)
        repo = str(self.folder / "beta")
        with mock.patch.object(lib, "profile_command", side_effect=AssertionError("must not run")):
            with self.assertRaises(lib.RepoDisabledError) as caught:
                lib.session_command("claude", repo, "")
            self.assertIn("beta", str(caught.exception))
            with self.assertRaises(lib.RepoDisabledError):
                lib.agent_launch_command(repo=repo, profile="claude")

    def test_disabled_repo_records_nothing(self):
        lib.set_repo_enabled("beta", False)
        with mock.patch.object(lib.permission_state, "record_launch", side_effect=AssertionError("recorded")):
            with self.assertRaises(lib.RepoDisabledError):
                lib.session_command("claude", str(self.folder / "beta"), "")

    # command line
    def run_lib(self, *args):
        return subprocess.run([sys.executable, str(SRC / "lib.py"), *args],
                              env=dict(os.environ), capture_output=True, text=True)

    def test_cli_repo_on_off_and_repo_enabled_exit_codes(self):
        self.assertEqual(self.run_lib("repo", "off", "alpha").returncode, 0)
        off = self.run_lib("repo-enabled", str(self.folder / "alpha"))
        self.assertEqual(off.returncode, 1)
        self.assertIn("alpha", off.stderr)
        self.assertEqual(self.run_lib("repo-enabled", str(self.folder / "beta")).returncode, 0)
        self.assertEqual(self.run_lib("repo", "on", "alpha").returncode, 0)
        self.assertEqual(self.run_lib("repo-enabled", str(self.folder / "alpha")).returncode, 0)
        bad = self.run_lib("repo", "off", "nope")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("nope", bad.stderr)

    def test_cli_repo_list_and_no_folder_message(self):
        self.run_lib("repo", "off", "gamma")
        out = self.run_lib("repo", "list").stdout.splitlines()
        self.assertEqual(out, ["alpha\tligado", "beta\tligado", "gamma\tdesligado"])
        self.write_config({"profiles": {"claude": "claude"}})
        missing = self.run_lib("repo", "list")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("repos_folder", missing.stderr)

    def test_cli_session_command_refuses_disabled_repo_with_message(self):
        self.run_lib("repo", "off", "beta")
        result = self.run_lib("session-command", "claude", str(self.folder / "beta"), "")
        self.assertEqual(result.returncode, 2)
        self.assertIn("beta", result.stderr)
        self.assertEqual(result.stdout, "")


class PanelReposTest(unittest.TestCase):
    """The repos panel filters by switched on/off and marks the switched-off repos."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.folder = self.root / "Codes"
        self.folder.mkdir()
        for name in ("alpha", "beta"):
            make_repo(self.folder / name)
        self.config = self.root / "config.yaml"
        old = {name: os.environ.get(name) for name in ("AT_WS_CONFIG", "WS_STATE", "TMUX_PANE")}
        os.environ["AT_WS_CONFIG"] = str(self.config)
        os.environ["WS_STATE"] = str(self.root / "state")
        os.environ.pop("TMUX_PANE", None)

        def restore():
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        self.config.write_text(yaml_min.dumps({"repos_folder": str(self.folder)}), encoding="utf-8")
        import panel_repos
        self.panel = panel_repos
        panel_repos.VIEW.update({"filter": "all", "note": "", "note_until": 0.0})
        self.addCleanup(panel_repos.VIEW.update, {"filter": "all", "note": "", "note_until": 0.0})

    def rows(self):
        class Viewer:
            key = None
            redraw = mock.Mock()
        _header, body = self.panel.collect(Viewer())
        return _header, [row.text for row in body]

    def has(self, lines, name):
        return any(re.search(rf"\b{name}\b", line) for line in lines)

    def test_filter_cycles_all_on_off(self):
        lib.set_repo_enabled("beta", False)
        header, lines = self.rows()
        self.assertIn("todos", header[0])
        self.assertTrue(self.has(lines, "alpha") and self.has(lines, "beta"))
        self.panel.on_key(mock.Mock(key=None), "f")
        header, lines = self.rows()
        self.assertIn("ligados", header[0])
        self.assertTrue(self.has(lines, "alpha"))
        self.assertFalse(self.has(lines, "beta"))
        self.panel.on_key(mock.Mock(key=None), "f")
        header, lines = self.rows()
        self.assertIn("desligados", header[0])
        self.assertFalse(self.has(lines, "alpha"))
        self.assertTrue(self.has(lines, "beta"))
        self.assertIn(t("repos.disabled"), " ".join(lines))
        self.panel.on_key(mock.Mock(key=None), "f")
        self.assertEqual(self.panel.VIEW["filter"], "all")

    def test_t_switches_the_repo_under_the_cursor(self):
        viewer = mock.Mock(key=("r", str(self.folder / "alpha")))
        viewer._wake = mock.Mock()
        self.panel.on_key(viewer, "t")
        self.assertEqual(lib.disabled_repos(), {"alpha"})
        self.assertIn("alpha", self.panel.VIEW["note"])
        self.panel.on_key(viewer, "t")
        self.assertEqual(lib.disabled_repos(), set())

    def test_enter_on_disabled_repo_opens_nothing_and_says_why(self):
        lib.set_repo_enabled("beta", False)
        viewer = mock.Mock()
        with mock.patch.object(self.panel.subprocess, "run") as run:
            self.panel.enter(viewer, ("r", str(self.folder / "beta")), "enter")
            run.assert_not_called()
        self.assertIn(t("repos.open_disabled", repo="beta"), self.panel.VIEW["note"])

    def test_disabled_repo_reads_no_git_state(self):
        lib.set_repo_enabled("beta", False)
        with mock.patch.object(self.panel, "git_icon", side_effect=lambda p: "") as icon:
            self.rows()
        called = [c.args[0] for c in icon.call_args_list]
        self.assertNotIn(str(self.folder / "beta"), called)
        self.assertIn(str(self.folder / "alpha"), called)


class TextsTest(unittest.TestCase):
    def test_every_new_text_exists_in_pt_br(self):
        keys = ["repo.disabled", "repo.config_error", "repo.not_in_folder", "repo.no_folder", "repo.state.on",
                "repo.state.off", "repo.set", "repo.usage", "repos.filter.line", "repos.filter.all",
                "repos.filter.on", "repos.filter.off", "repos.disabled", "repos.toggled", "repos.open_disabled"]
        for key in keys:
            self.assertIn(key, _STRINGS)
        self.assertIn("alpha", t("repo.disabled", repo="alpha"))


if __name__ == "__main__":
    unittest.main()
