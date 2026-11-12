"""Tests for the file viewer door (item 44): kinds, the configured viewer command, the order of
open_file (viewer, OS default, editor), the `workspace view` entry, and the rule that no other
module opens files by itself. The GUI programs are never run: _spawn and open_system are mocked."""
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import opener  # noqa: E402

CONFIG = {"viewer": {
    "html": "chromium --app={url} --window-size=1000,760",
    "image": "imv {file}",
    "video": "mpv --force-window=yes --geometry=640x360 --title={name} {file}",
    "audio": ["mpv", "--no-video", "{file}"],
}}


def which_for(*commands):
    def which(name):
        return f"/usr/bin/{name}" if name in commands else None
    return which


class KindTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(opener.kind_of("/a/x.html"), "html")
        self.assertEqual(opener.kind_of("/a/x.htm"), "html")
        self.assertEqual(opener.kind_of("/a/x.PNG"), "image")
        self.assertEqual(opener.kind_of("/a/x.mkv"), "video")
        self.assertEqual(opener.kind_of("/a/x.flac"), "audio")

    def test_text_svg_and_documents_have_no_viewer_kind(self):
        for name in ("a.py", "a.md", "a.svg", "a.pdf", "a.zip", "README"):
            self.assertIsNone(opener.kind_of(f"/a/{name}"), name)


class ViewerCommandTest(unittest.TestCase):
    def command(self, path, config=CONFIG, found=("chromium", "imv", "mpv")):
        with mock.patch.object(opener.lib, "read_config", return_value=config), \
                mock.patch.object(opener.shutil, "which", side_effect=which_for(*found)):
            return opener.viewer_command(path)

    def test_html_runs_chromium_in_app_mode_with_a_file_url(self):
        path = "/tmp/viewer-test/Relatório final.html"
        cmd = self.command(path)
        self.assertEqual(cmd, ["chromium",
                               "--app=" + Path(path).as_uri(),
                               "--window-size=1000,760"])
        self.assertTrue(cmd[1].startswith("--app=file:///"))
        self.assertIn("%20", cmd[1])     # spaces are escaped in the URL, the argv keeps one token

    def test_image_video_and_audio_use_their_own_lines(self):
        self.assertEqual(self.command("/tmp/a.png"), ["imv", os.path.abspath("/tmp/a.png")])
        video = self.command("/tmp/clip one.mp4")
        self.assertEqual(video, ["mpv", "--force-window=yes", "--geometry=640x360",
                                 "--title=clip one.mp4", os.path.abspath("/tmp/clip one.mp4")])
        self.assertEqual(self.command("/tmp/a.mp3", found=("mpv",)),
                         ["mpv", "--no-video", os.path.abspath("/tmp/a.mp3")])

    def test_placeholder_text_inside_a_file_name_is_not_expanded(self):
        cmd = self.command("/tmp/{url}.png")
        self.assertEqual(cmd[1], os.path.abspath("/tmp/{url}.png"))

    def test_command_without_placeholder_gets_the_file_last(self):
        config = {"viewer": {"image": "feh --scale-down"}}
        self.assertEqual(self.command("/tmp/a.jpg", config=config, found=("feh",)),
                         ["feh", "--scale-down", os.path.abspath("/tmp/a.jpg")])

    def test_no_line_for_the_kind_means_no_viewer(self):
        self.assertIsNone(self.command("/tmp/a.mp4", config={"viewer": {"html": "chromium"}}))
        self.assertIsNone(self.command("/tmp/a.html", config={}))
        self.assertIsNone(self.command("/tmp/a.html", config={"viewer": "chromium"}))

    def test_program_missing_from_path_means_no_viewer(self):
        self.assertIsNone(self.command("/tmp/a.html", found=()))

    def test_unbalanced_quotes_mean_no_viewer(self):
        config = {"viewer": {"image": "imv \"{file}"}}
        self.assertIsNone(self.command("/tmp/a.png", config=config))

    def test_text_never_has_a_viewer(self):
        self.assertIsNone(self.command("/tmp/a.py"))
        self.assertIsNone(self.command("/tmp/a.svg"))


class OpenFileTest(unittest.TestCase):
    def open(self, path, config=CONFIG, found=("chromium", "imv", "mpv"), spawn=True, system=True):
        with mock.patch.object(opener.lib, "read_config", return_value=config), \
                mock.patch.object(opener.shutil, "which", side_effect=which_for(*found)), \
                mock.patch.object(opener, "_spawn", return_value=spawn) as sp, \
                mock.patch.object(opener, "open_system", return_value=system) as sy:
            result = opener.open_file(path)
        return result, sp, sy

    def test_configured_viewer_wins_over_the_os_default(self):
        result, spawn, system = self.open("/tmp/page.html")
        self.assertTrue(result)
        spawn.assert_called_once()
        system.assert_not_called()

    def test_html_falls_back_to_the_os_default_without_a_viewer(self):
        result, spawn, system = self.open("/tmp/page.html", config={})
        self.assertTrue(result)
        spawn.assert_not_called()
        system.assert_called_once_with("/tmp/page.html")

    def test_failed_viewer_start_falls_back_to_the_os_default(self):
        result, spawn, system = self.open("/tmp/page.html", spawn=False)
        self.assertTrue(result)
        spawn.assert_called_once()
        system.assert_called_once()

    def test_documents_go_to_the_os_default_without_a_viewer_line(self):
        result, spawn, system = self.open("/tmp/a.pdf")
        self.assertTrue(result)
        spawn.assert_not_called()
        system.assert_called_once()

    def test_text_is_not_opened_here_so_the_editor_takes_it(self):
        result, spawn, system = self.open("/tmp/a.py")
        self.assertFalse(result)
        spawn.assert_not_called()
        system.assert_not_called()

    def test_nothing_available_returns_false(self):
        result, _, _ = self.open("/tmp/page.html", config={}, system=False)
        self.assertFalse(result)


class MainTest(unittest.TestCase):
    def test_usage_error_exits_2(self):
        with mock.patch.object(opener.sys, "stderr"):
            self.assertEqual(opener.main([]), 2)
            self.assertEqual(opener.main(["a", "b"]), 2)

    def test_missing_file_exits_1(self):
        with mock.patch.object(opener.sys, "stderr"), \
                mock.patch.object(opener, "open_file") as opened:
            self.assertEqual(opener.main(["/nao/existe/x.html"]), 1)
            opened.assert_not_called()

    def test_opened_file_exits_0_and_uses_the_one_door(self):
        here = os.path.abspath(__file__)
        with mock.patch.object(opener, "open_file", return_value=True) as opened:
            self.assertEqual(opener.main([here]), 0)
            opened.assert_called_once_with(here)

    def test_no_program_exits_1(self):
        here = os.path.abspath(__file__)
        with mock.patch.object(opener.sys, "stderr"), \
                mock.patch.object(opener, "open_file", return_value=False):
            self.assertEqual(opener.main([here]), 1)


class OneDoorTest(unittest.TestCase):
    """Only opener.py starts the OS opener or a viewer; the other modules and bin/workspace ask it."""
    OPENERS = re.compile(r"xdg-open|wslview|explorer\.exe|\[\s*\"open\"|\"start\"")
    ALLOWED = {"opener.py", "clipboard.py", "notify.py"}   # clipboard and notification, not files

    def test_no_other_module_names_an_opener_program(self):
        offenders = []
        for path in sorted(list(SRC.glob("*.py")) + list((SRC / "agents").glob("*.py"))):
            if path.name in self.ALLOWED:
                continue
            if self.OPENERS.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_the_tree_panel_calls_the_door(self):
        text = (SRC / "panel_tree.py").read_text(encoding="utf-8")
        self.assertIn("opener.open_file(path)", text)
        self.assertNotIn("open_system", text)


if __name__ == "__main__":
    unittest.main()
