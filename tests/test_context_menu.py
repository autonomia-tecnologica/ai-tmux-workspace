"""Tests for the repos context menu (src/context_menu.py) and the config writes behind it.

The repos are real (empty) git repositories in a temporary folder, and AT_WS_CONFIG/WS_STATE point
at that folder, so the person's real config is never read or written. The config keeps comments in
the fixtures: the textual edit must preserve them.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import context_menu  # noqa: E402
import lib  # noqa: E402
import yaml_min  # noqa: E402
from i18n import _STRINGS, t  # noqa: E402


def make_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


class FakeViewer:
    """Records the last submenu the menu layer asked the viewer to open."""

    def __init__(self):
        self.redraw = mock.Mock()
        self.key = None
        self.items = None
        self.menu_key = None

    def open_menu(self, items, key=None):
        self.items, self.menu_key = list(items), key
        return True


class ContextMenuTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.folder = self.root / "Codes"
        self.folder.mkdir()
        for name in ("alpha", "beta", "gamma"):
            make_repo(self.folder / name)
        self.main = self.root / "main"
        make_repo(self.main)
        self.config = self.root / "config.yaml"
        env = mock.patch.dict(os.environ, {"AT_WS_CONFIG": str(self.config),
                                           "WS_STATE": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TMUX_PANE", None)
        self.write_config((
            "# topo\n"
            f"repos_folder: {self.folder}\n"
            f"main: {self.main}\n"
            "profiles:\n  claude: claude\n"
            "# grupos\n"
            "groups:\n"
            "  Team:\n"
            f"    - {self.folder / 'beta'}   # repo beta\n"
            "\n"
            "# cauda\n"
            "notifications: true\n"
        ))
        lib.CONFIG_ERROR = None
        context_menu._STATE.update({"prompt": None, "note": "", "until": 0.0})

    def tearDown(self):
        context_menu._STATE.update({"prompt": None, "note": "", "until": 0.0})

    def write_config(self, text):
        self.config.write_text(text, encoding="utf-8")

    def text(self):
        return self.config.read_text(encoding="utf-8")

    def groups(self):
        return lib.read_repos()

    def names(self, repos):
        return [name for name, _ in repos]

    def paths(self, *names):
        return [str(self.folder / name) for name in names]

    # --- the implicit group -----------------------------------------------------------------
    def test_repo_without_group_is_uncategorized(self):
        groups = self.groups()
        self.assertEqual([name for name, _ in groups],
                         ["Team", t("repos.group.uncategorized")])
        self.assertEqual(self.names(groups[0][1]), ["beta"])
        self.assertEqual(self.names(groups[1][1]), ["alpha", "gamma"])

    def test_groups_outside_the_folder_do_not_classify_anything(self):
        self.write_config(
            f"repos_folder: {self.folder}\n"
            "groups:\n"
            "  Elsewhere:\n"
            f"    - {self.root / 'main'}\n"
        )
        groups = self.groups()
        self.assertEqual([name for name, _ in groups], [t("repos.group.uncategorized")])
        self.assertEqual(self.names(groups[0][1]), ["alpha", "beta", "gamma"])

    # --- move to group ----------------------------------------------------------------------
    def test_move_to_existing_group_leaves_uncategorized(self):
        lib.move_repo_to_group(self.paths("alpha")[0], "Team")
        groups = self.groups()
        self.assertEqual([name for name, _ in groups],
                         ["Team", t("repos.group.uncategorized")])
        self.assertEqual(self.names(groups[0][1]), ["beta", "alpha"])
        self.assertEqual(self.names(groups[1][1]), ["gamma"])

    def test_move_creates_the_group_and_preserves_comments_and_a_backup(self):
        before = self.text()
        lib.move_repo_to_group(self.paths("gamma")[0], "New")
        text = self.text()
        self.assertIn("# topo", text)
        self.assertIn("# grupos", text)
        self.assertIn("# cauda", text)
        self.assertIn("notifications: true", text)
        self.assertEqual(yaml_min.loads(text)["repos_folder"], str(self.folder))
        self.assertIn("New", lib.group_names())
        self.assertEqual(self.names(self.groups()[-1][1]), ["alpha"])
        backups = sorted(self.config.parent.glob("config.yaml.bak-*"))
        self.assertTrue(backups)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), before)

    def test_move_from_one_group_to_another_removes_the_old_entry(self):
        lib.move_repo_to_group(self.paths("beta")[0], "Other")
        cfg = yaml_min.loads(self.text())
        self.assertIn("# repo beta", self.text())       # the entry keeps its comment
        self.assertFalse(cfg["groups"].get("Team"))     # left empty by the move
        self.assertEqual([lib.expand_path(item) for item in cfg["groups"]["Other"]],
                         [lib.expand_path(self.paths("beta")[0])])
        self.assertEqual([name for name, _ in self.groups()],
                         ["Other", t("repos.group.uncategorized")])
        self.assertEqual(self.names(self.groups()[0][1]), ["beta"])

    def test_move_to_the_same_group_is_a_no_op(self):
        before = self.text()
        lib.move_repo_to_group(self.paths("beta")[0], "Team")
        self.assertEqual(self.text(), before)
        self.assertFalse(list(self.config.parent.glob("config.yaml.bak-*")))

    # --- mark as sibling --------------------------------------------------------------------
    def test_sibling_is_symmetric_and_preserves_comments(self):
        before = self.text()
        lib.mark_repo_sibling(self.paths("gamma")[0], self.paths("alpha")[0])
        text = self.text()
        self.assertIn("# topo", text)
        self.assertIn("# cauda", text)
        self.assertTrue(backups := list(self.config.parent.glob("config.yaml.bak-*")))
        self.assertEqual(backups[0].read_text(encoding="utf-8"), before)
        self.assertEqual(lib.siblings(self.paths("gamma")[0]), self.paths("alpha"))
        self.assertEqual(lib.siblings(self.paths("alpha")[0]), self.paths("gamma"))

    def test_sibling_of_uncategorized_repo_does_not_change_its_group(self):
        lib.mark_repo_sibling(self.paths("alpha")[0], self.paths("gamma")[0])
        self.assertEqual(self.names(self.groups()[-1][1]), ["alpha", "gamma"])
        self.assertEqual(lib.siblings(self.paths("alpha")[0]), self.paths("gamma"))

    def test_sibling_is_idempotent_on_both_sides(self):
        lib.mark_repo_sibling(self.paths("alpha")[0], self.paths("gamma")[0])
        text = self.text()
        lib.mark_repo_sibling(self.paths("alpha")[0], self.paths("gamma")[0])
        lib.mark_repo_sibling(self.paths("gamma")[0], self.paths("alpha")[0])
        self.assertEqual(self.text(), text)

    # --- the menu ---------------------------------------------------------------------------
    def test_menu_has_only_the_two_repo_actions(self):
        viewer = FakeViewer()
        items = context_menu.actions(viewer, ("r", self.paths("alpha")[0]))
        self.assertEqual([label for label, _active, _action in items],
                         [t("repos.menu.move_group"), t("repos.menu.sibling")])
        self.assertTrue(all(active for _label, active, _action in items))

    def test_other_items_have_no_menu(self):
        self.assertEqual(context_menu.actions(FakeViewer(), ("g", "Team")), [])
        self.assertEqual(context_menu.actions(FakeViewer(), None), [])

    def test_main_repo_cannot_move_to_a_group(self):
        items = context_menu.actions(FakeViewer(), ("r", str(self.main)))
        self.assertFalse(items[0][1])
        self.assertTrue(items[1][1])

    def test_move_submenu_lists_groups_and_offers_a_new_one(self):
        viewer = FakeViewer()
        key = ("r", self.paths("alpha")[0])
        context_menu.open_groups(viewer, key)
        self.assertEqual([label for label, _active, _action in viewer.items],
                         ["Team", t("repos.menu.new_group")])
        self.assertEqual(viewer.menu_key, key)
        next(item for item in viewer.items if item[0] == "Team")[2](viewer, key)
        self.assertEqual(self.names(self.groups()[0][1]), ["beta", "alpha"])

    def test_new_group_prompt_writes_the_group(self):
        viewer = FakeViewer()
        key = ("r", self.paths("gamma")[0])
        context_menu.open_groups(viewer, key)
        viewer.items[-1][2](viewer, key)
        self.assertIn(t("repos.group.new_prompt"), context_menu.header()[0])
        for char in "Novo":
            self.assertTrue(context_menu.on_input(viewer, char))
        self.assertTrue(context_menu.on_input(viewer, "\r"))
        self.assertEqual(context_menu._STATE["prompt"], None)
        self.assertEqual(lib.group_names(), ["Team", "Novo"])
        groups = dict(self.groups())
        self.assertEqual(self.names(groups["Novo"]), ["gamma"])

    def test_escape_cancels_the_new_group_prompt_without_writing(self):
        before = self.text()
        viewer = FakeViewer()
        key = ("r", self.paths("gamma")[0])
        context_menu.ask_group(viewer, key)
        context_menu.on_input(viewer, "x")
        self.assertTrue(context_menu.on_input(viewer, "esc"))
        self.assertEqual(context_menu.header(), [])
        self.assertEqual(self.text(), before)

    def test_sibling_submenu_excludes_the_repo_itself(self):
        viewer = FakeViewer()
        key = ("r", self.paths("gamma")[0])
        context_menu.open_siblings(viewer, key)
        labels = [label for label, _active, _action in viewer.items]
        self.assertNotIn("gamma", labels)
        self.assertEqual(labels, ["main", "beta", "alpha"])   # main, declared groups, then uncategorized
        next(item for item in viewer.items if item[0] == "alpha")[2](viewer, key)
        self.assertEqual(lib.siblings(self.paths("gamma")[0]), self.paths("alpha"))

    def test_no_other_repo_keeps_the_menu_with_an_inactive_note(self):
        self.write_config(f"repos_folder: {self.root / 'empty'}\n")
        viewer = FakeViewer()
        context_menu.open_siblings(viewer, ("r", self.paths("alpha")[0]))
        self.assertEqual(viewer.items, [(t("repos.menu.no_siblings"), False, None)])

    def test_invalid_group_name_writes_nothing(self):
        before = self.text()
        with self.assertRaises(ValueError):
            lib.move_repo_to_group(self.paths("alpha")[0], "  ")
        self.assertEqual(self.text(), before)


class ContextMenuTextsTest(unittest.TestCase):
    def test_every_new_text_exists_in_pt_br(self):
        keys = ["repos.group.uncategorized", "repos.menu.move_group", "repos.menu.sibling",
                "repos.menu.new_group", "repos.menu.no_siblings", "repos.group.new_prompt",
                "repos.moved", "repos.sibling_added", "repos.err.write", "repos.err.group_name",
                "repos.err.same_repo"]
        for key in keys:
            with self.subTest(key=key):
                self.assertIn(key, _STRINGS)
        self.assertEqual(t("repos.group.uncategorized"), "Sem categoria")


if __name__ == "__main__":
    unittest.main()
