"""Tests for opener: prose detection and the editor chooser (config, PATH filtering, fallbacks)."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import opener  # noqa: E402


def which_for(*commands):
    """shutil.which stand-in that only finds `commands` (as /usr/bin/<command>)."""
    def which(name):
        return f"/usr/bin/{name}" if name in commands else None
    return which


class TypeTest(unittest.TestCase):
    def test_prose_extensions(self):
        for name in ("a.md", "a.markdown", "a.mdx", "a.rst", "a.txt", "A.MD"):
            self.assertTrue(opener.is_prose(f"/tmp/{name}"), name)

    def test_code_extensions(self):
        for name in ("a.py", "a.json", "a.svg", "a.png", "README", "a.text"):
            self.assertFalse(opener.is_prose(f"/tmp/{name}"), name)


class ChoicesTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"VISUAL": "", "EDITOR": ""})
        env.start()
        self.addCleanup(env.stop)

    def choices(self, path, config, found):
        with mock.patch.object(opener.lib, "read_config", return_value=config), \
                mock.patch.object(opener.shutil, "which", side_effect=which_for(*found)):
            return opener.choices(path)

    def test_config_drives_the_list_and_keeps_order(self):
        config = {"editor": {
            "prose": [{"name": "micro", "command": "micro", "fullscreen": True},
                      {"name": "nvim", "command": "nvim"}],
            "code": [{"name": "nvim", "command": "nvim"},
                     {"name": "micro", "command": "micro"}],
        }}
        prose = self.choices("/tmp/a.md", config, {"micro", "nvim"})
        self.assertEqual([e["command"] for e in prose], ["micro", "nvim"])
        self.assertTrue(prose[0]["fullscreen"])
        code = self.choices("/tmp/a.py", config, {"micro", "nvim"})
        self.assertEqual([e["command"] for e in code], ["nvim", "micro"])
        self.assertFalse(code[0]["fullscreen"])

    def test_missing_command_is_filtered_out(self):
        config = {"editor": {"code": [{"name": "micro", "command": "micro", "fullscreen": True},
                                      {"name": "nvim", "command": "nvim"}]}}
        code = self.choices("/tmp/a.py", config, {"nvim"})
        self.assertEqual(code, [{"name": "nvim", "command": "nvim", "fullscreen": False}])

    def test_prose_fallback_is_micro_then_nvim(self):
        result = self.choices("/tmp/a.md", {}, {"micro", "nvim", "vim", "vi"})
        self.assertEqual([e["name"] for e in result], ["micro", "nvim", "vim", "vi"])
        self.assertTrue(result[0]["fullscreen"])

    def test_code_fallback_is_nvim_then_micro(self):
        result = self.choices("/tmp/a.py", {}, {"nvim", "micro", "vim", "vi"})
        self.assertEqual([e["name"] for e in result], ["nvim", "micro", "vim", "vi"])

    def test_fallback_goes_to_env_then_standard_editors(self):
        with mock.patch.dict(os.environ, {"VISUAL": "code --wait", "EDITOR": "emacs"}):
            # nvim is missing, so $VISUAL and $EDITOR come before vim/vi
            result = self.choices("/tmp/a.py", {}, {"code", "emacs", "vim", "vi"})
        self.assertEqual([e["command"] for e in result], ["code --wait", "emacs", "vim", "vi"])
        self.assertFalse(result[0]["fullscreen"])

    def test_fallback_skips_missing_editors(self):
        result = self.choices("/tmp/a.py", {}, {"vim"})
        self.assertEqual([e["command"] for e in result], ["vim"])

    def test_default_choice_is_the_first_one(self):
        with mock.patch.object(opener.lib, "read_config", return_value={}), \
                mock.patch.object(opener.shutil, "which", side_effect=which_for("micro", "nvim")):
            self.assertEqual(opener.default_choice("/tmp/a.md")["name"], "micro")

    def test_no_editor_available_returns_empty(self):
        result = self.choices("/tmp/a.py", {}, set())
        self.assertEqual(result, [])
        with mock.patch.object(opener.lib, "read_config", return_value={}), \
                mock.patch.object(opener.shutil, "which", return_value=None):
            self.assertIsNone(opener.default_choice("/tmp/a.py"))


if __name__ == "__main__":
    unittest.main()
