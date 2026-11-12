"""Tests for free folders (item 82): the config reader, the navigation panel and the home tab.

A free folder ("free_folders" in config.yaml) is a navigation-only type: it gets no repo window and
no agent session. The panel returns `collect(viewer)` rows and opens files through `opener.open_file`
(the item 44 door). No GUI and no tmux: `lib.read_config` reads a temporary config and the viewer is
a mock with a `redraw.set`.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import lib  # noqa: E402
import panel_folders  # noqa: E402
import panel_tabs  # noqa: E402
import yaml_min  # noqa: E402
from i18n import t  # noqa: E402


class FolderBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.docs = self.root / "Documentos"
        self.pics = self.root / "Imagens"
        self.sub = self.docs / "sub"
        self.deep = self.sub / "fundo"
        for folder in (self.docs, self.pics, self.sub, self.deep):
            folder.mkdir(parents=True)
        (self.docs / "nota.txt").write_text("x", encoding="utf-8")
        (self.docs / ".oculto").write_text("x", encoding="utf-8")
        (self.sub / "foto.png").write_text("x", encoding="utf-8")
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
        panel_folders.OPEN = panel_folders.Expander()   # fresh expansion state per test
        panel_folders.VIEW.update(note="", note_until=0.0)

    def write_config(self, data):
        self.config.write_text(yaml_min.dumps(data), encoding="utf-8")

    def free(self, *entries):
        self.write_config({"free_folders": list(entries)})

    def viewer(self):
        return mock.Mock()

    def keys(self, body):
        return [row.key for row in body if row.key]

    def row(self, body, key):
        return next((row for row in body if row.key == key), None)


class FreeFoldersConfigTest(FolderBase):
    def test_reads_paths_labels_and_keeps_config_order(self):
        missing = self.root / "NaoExiste"
        self.free(str(self.docs), {"path": str(self.pics), "label": "Fotos"}, str(missing))
        self.assertEqual(lib.free_folders(), [
            ("Documentos", str(self.docs)),
            ("Fotos", str(self.pics)),
            ("NaoExiste", str(missing)),
        ])

    def test_expands_home_and_skips_bad_or_repeated_entries(self):
        self.write_config({"free_folders": [
            "~/Documentos", 42, "", {"label": "sem caminho"}, {"path": ""},
            "~/Imagens", "~/Documentos",
        ]})
        expected = os.path.expanduser("~/Documentos")
        expected = os.path.realpath(expected)
        pictures = os.path.realpath(os.path.expanduser("~/Imagens"))
        self.assertEqual(lib.free_folders(), [
            (os.path.basename(expected) or expected, expected),
            (os.path.basename(pictures) or pictures, pictures),
        ])

    def test_without_the_key_there_are_no_free_folders(self):
        self.write_config({})
        self.assertEqual(lib.free_folders(), [])

    def test_a_free_folder_is_not_a_repo(self):
        repo = self.root / "projeto"
        repo.mkdir()
        (repo / ".git").mkdir()      # even a git folder stays outside the repo list
        self.write_config({"main": str(self.root / "home"), "free_folders": [str(repo)]})
        self.assertEqual([path for _, path in lib.free_folders()], [str(repo)])
        self.assertEqual(lib.read_repos(), [])


class CollectTest(FolderBase):
    def test_roots_are_listed_closed_with_their_labels(self):
        self.free({"path": str(self.docs), "label": "Documentos"}, str(self.pics))
        header, body = panel_folders.collect(None)
        self.assertIn(t("folders.hint"), " ".join(row.text for row in header))
        self.assertEqual(self.keys(body), [("d", str(self.docs)), ("d", str(self.pics))])
        self.assertIn("Documentos", self.row(body, ("d", str(self.docs))).text)
        self.assertNotIn(("f", str(self.docs / "nota.txt")), self.keys(body))

    def test_an_open_root_lists_folders_first_then_files_without_dot_files(self):
        self.free(str(self.docs))
        panel_folders.OPEN.open(str(self.docs))
        _, body = panel_folders.collect(None)
        self.assertEqual(self.keys(body)[1:], [
            ("d", str(self.sub)),
            ("f", str(self.docs / "nota.txt")),
        ])
        self.assertNotIn(("f", str(self.docs / ".oculto")), self.keys(body))

    def test_a_nested_folder_shows_its_children_only_when_open(self):
        self.free(str(self.docs))
        panel_folders.OPEN.open(str(self.docs))
        _, body = panel_folders.collect(None)
        self.assertNotIn(("f", str(self.sub / "foto.png")), self.keys(body))
        panel_folders.OPEN.open(str(self.sub))
        _, body = panel_folders.collect(None)
        self.assertIn(("f", str(self.sub / "foto.png")), self.keys(body))

    def test_a_missing_folder_is_listed_as_not_found(self):
        missing = self.root / "NaoExiste"
        self.free(str(missing))
        _, body = panel_folders.collect(None)
        row = self.row(body, ("d", str(missing)))
        self.assertIsNotNone(row)
        self.assertIn(t("folders.missing"), row.text)

    def test_without_free_folders_the_panel_says_so(self):
        self.write_config({})
        _, body = panel_folders.collect(None)
        self.assertEqual([row.key for row in body], [None])
        self.assertIn(t("folders.none"), body[0].text)

    def test_footer_shows_the_absolute_path_of_the_selection(self):
        v = self.viewer()
        v.key = ("d", str(self.docs))
        self.assertEqual(panel_folders.footer(v), f"\x1b[1m{self.docs}/\x1b[0m")
        v.key = ("f", str(self.docs / "nota.txt"))
        self.assertIn(str(self.docs / "nota.txt"), panel_folders.footer(v))
        v.key = None
        self.assertEqual(panel_folders.footer(v), "")


class EnterTest(FolderBase):
    def test_enter_folds_a_folder_without_opening_a_window(self):
        self.free(str(self.docs))
        v = self.viewer()
        panel_folders.enter(v, ("d", str(self.docs)), False)
        self.assertTrue(panel_folders.OPEN.is_open(str(self.docs)))
        v.redraw.set.assert_called_once()
        panel_folders.enter(v, ("d", str(self.docs)), False)
        self.assertFalse(panel_folders.OPEN.is_open(str(self.docs)))

    def test_enter_on_a_file_goes_through_the_one_door(self):
        self.free(str(self.docs))
        target = str(self.docs / "nota.txt")
        with mock.patch.object(panel_folders.opener, "open_file", return_value=True) as opened:
            panel_folders.enter(self.viewer(), ("f", target), False)
        opened.assert_called_once_with(target)
        self.assertEqual(panel_folders.VIEW["note"], "")

    def test_a_file_without_viewer_leaves_a_note(self):
        self.free(str(self.docs))
        target = str(self.docs / "nota.txt")
        with mock.patch.object(panel_folders.opener, "open_file", return_value=False):
            panel_folders.enter(self.viewer(), ("f", target), False)
        self.assertEqual(panel_folders.VIEW["note"], t("folders.open_failed", name="nota.txt"))

    def test_a_missing_folder_leaves_a_note_and_does_not_open(self):
        missing = str(self.root / "NaoExiste")
        self.free(missing)
        with mock.patch.object(panel_folders.opener, "open_file") as opened:
            panel_folders.enter(self.viewer(), ("d", missing), False)
        opened.assert_not_called()
        self.assertEqual(panel_folders.VIEW["note"], t("folders.missing_note", path=missing))

    def test_panel_only_opens_through_opener_and_never_starts_a_session(self):
        text = (SRC / "panel_folders.py").read_text(encoding="utf-8")
        self.assertIn("opener.open_file(path)", text)
        self.assertNotIn("open_system", text)
        for forbidden in ("open_repo", "session_new", "import session", "agents"):
            self.assertNotIn(forbidden, text)


class OnKeyTest(FolderBase):
    def test_right_opens_and_left_closes_the_folder_under_the_cursor(self):
        self.free(str(self.docs))
        v = self.viewer()
        v.key = ("d", str(self.docs))
        panel_folders.on_key(v, "right")
        self.assertTrue(panel_folders.OPEN.is_open(str(self.docs)))
        panel_folders.on_key(v, "left")
        self.assertFalse(panel_folders.OPEN.is_open(str(self.docs)))

    def test_left_on_a_closed_child_climbs_to_the_parent(self):
        self.free(str(self.docs))
        v = self.viewer()
        v.key = ("d", str(self.deep))
        panel_folders.on_key(v, "left")
        self.assertEqual(v.key, ("d", str(self.sub)))

    def test_left_on_a_root_stays_at_the_root(self):
        self.free(str(self.docs))
        v = self.viewer()
        v.key = ("d", str(self.docs))
        panel_folders.on_key(v, "left")
        self.assertEqual(v.key, ("d", str(self.docs)))

    def test_arrows_on_a_file_do_not_fold_it(self):
        self.free(str(self.docs))
        v = self.viewer()
        v.key = ("f", str(self.docs / "nota.txt"))
        panel_folders.on_key(v, "right")
        self.assertFalse(panel_folders.OPEN.is_open(str(self.docs / "nota.txt")))


class HomeTabTest(unittest.TestCase):
    def test_only_the_home_window_gets_the_folders_tab(self):
        self.assertEqual(panel_tabs.left_tabs(False), [
            ("repos", t("tabs.repos")), ("sessions", t("tabs.sessions")),
        ])
        self.assertEqual(panel_tabs.left_tabs(True), [
            ("repos", t("tabs.repos")), ("sessions", t("tabs.sessions")),
            ("folders", t("tabs.folders")),
        ])
        self.assertEqual(t("tabs.folders"), "PASTAS")

    def test_is_home_compares_the_window_repo_with_the_main_repo(self):
        with mock.patch.object(panel_tabs.lib, "read_main", return_value=("home", "/repos/home")), \
                mock.patch.object(panel_tabs.tmux, "show_w", return_value="/repos/home"):
            self.assertTrue(panel_tabs.is_home("%1"))
        with mock.patch.object(panel_tabs.lib, "read_main", return_value=("home", "/repos/home")), \
                mock.patch.object(panel_tabs.tmux, "show_w", return_value="/repos/other"):
            self.assertFalse(panel_tabs.is_home("%1"))
        with mock.patch.object(panel_tabs.lib, "read_main", return_value=None):
            self.assertFalse(panel_tabs.is_home("%1"))


if __name__ == "__main__":
    unittest.main()
