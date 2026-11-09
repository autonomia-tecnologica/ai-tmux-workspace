"""Tests for the files tree panel: rename (validation, git mv/os.rename, cursor, cancel)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import i18n  # noqa: E402
import panel_tree  # noqa: E402


class RenameTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = str(Path(tmp.name).resolve())
        self._old_repo = panel_tree.REPO
        self.addCleanup(setattr, panel_tree, "REPO", self._old_repo)
        panel_tree.REPO = self.repo
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.git("config", "core.hooksPath", os.devnull)   # no global commit-msg hook in tests

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args],
                              capture_output=True, text=True, check=True).stdout

    def write(self, name, content="x\n"):
        path = os.path.join(self.repo, name)
        Path(path).write_text(content, encoding="utf-8")
        return path

    def commit(self, name):
        self.git("add", name)
        self.git("commit", "-qm", "add")

    def viewer(self, path):
        return SimpleNamespace(key=("f", path), redraw=mock.Mock())

    def test_tracked_file_uses_git_mv(self):
        old = self.write("old.txt")
        self.commit("old.txt")
        v = self.viewer(old)
        with mock.patch.object(panel_tree.tmux, "message") as message:
            ok = panel_tree.rename_to(v, ("f", old), "new.txt")
        self.assertTrue(ok)
        new = os.path.join(self.repo, "new.txt")
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        status = self.git("status", "--porcelain").strip()
        self.assertTrue(status.startswith("R"), status)     # staged rename, as `git mv` does
        self.assertEqual(v.key, ("f", new))
        v.redraw.set.assert_called_once()
        message.assert_called_once_with(i18n.t("tree.renamed", name="new.txt"))

    def test_untracked_file_uses_os_rename(self):
        old = self.write("old.txt")
        v = self.viewer(old)
        with mock.patch.object(panel_tree.tmux, "message"):
            ok = panel_tree.rename_to(v, ("f", old), "new.txt")
        self.assertTrue(ok)
        new = os.path.join(self.repo, "new.txt")
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        self.assertEqual(self.git("status", "--porcelain").strip(), "?? new.txt")
        self.assertEqual(v.key, ("f", new))
        v.redraw.set.assert_called_once()

    def test_existing_destination_is_refused(self):
        old = self.write("old.txt")
        self.write("other.txt")
        v = self.viewer(old)
        with mock.patch.object(panel_tree.tmux, "message") as message:
            ok = panel_tree.rename_to(v, ("f", old), "other.txt")
        self.assertFalse(ok)
        self.assertTrue(os.path.exists(old))
        self.assertEqual(v.key, ("f", old))
        v.redraw.set.assert_not_called()
        message.assert_called_once_with(i18n.t("tree.rename_exists", name="other.txt"))

    def test_invalid_names_are_refused(self):
        old = self.write("old.txt")
        for name in ("", "a/b", "a\\b", ".", "..", "/abs/path"):
            v = self.viewer(old)
            with mock.patch.object(panel_tree.tmux, "message") as message:
                ok = panel_tree.rename_to(v, ("f", old), name)
            self.assertFalse(ok, name)
            self.assertTrue(os.path.exists(old), name)
            self.assertEqual(v.key, ("f", old), name)
            message.assert_called_once()

    def test_unchanged_name_is_a_noop(self):
        old = self.write("old.txt")
        v = self.viewer(old)
        with mock.patch.object(panel_tree.tmux, "message") as message:
            ok = panel_tree.rename_to(v, ("f", old), "old.txt")
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(old))
        message.assert_not_called()

    def tearDown(self):
        panel_tree.RENAME.update(active=False, key=None, name="")

    def test_inline_input_cancels_on_esc(self):
        old = self.write("old.txt")
        self.commit("old.txt")
        v = self.viewer(old)
        panel_tree.rename(v, ("f", old))
        self.assertTrue(panel_tree.RENAME["active"])
        self.assertEqual(panel_tree.RENAME["name"], "old.txt")
        with mock.patch.object(panel_tree.tmux, "message") as message:
            self.assertTrue(panel_tree.on_input(v, "esc"))
        self.assertFalse(panel_tree.RENAME["active"])
        self.assertTrue(os.path.exists(old))
        self.assertEqual(v.key, ("f", old))
        message.assert_not_called()
        self.assertEqual(self.git("status", "--porcelain").strip(), "")

    def test_inline_input_commits_on_enter(self):
        old = self.write("old.txt")
        self.commit("old.txt")
        v = self.viewer(old)
        panel_tree.rename(v, ("f", old))
        for _ in "old.txt":
            panel_tree.on_input(v, "\x7f")
        for ch in "new.txt":
            panel_tree.on_input(v, ch)
        with mock.patch.object(panel_tree.tmux, "message"):
            self.assertTrue(panel_tree.on_input(v, "\r"))
        new = os.path.join(self.repo, "new.txt")
        self.assertFalse(panel_tree.RENAME["active"])
        self.assertEqual(v.key, ("f", new))
        self.assertTrue(os.path.exists(new))
        self.assertFalse(os.path.exists(old))

    def test_input_passes_through_when_not_renaming(self):
        old = self.write("old.txt")
        v = self.viewer(old)
        self.assertFalse(panel_tree.on_input(v, "x"))


if __name__ == "__main__":
    unittest.main()
