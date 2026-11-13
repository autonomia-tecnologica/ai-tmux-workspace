"""Tests for the single owner of the workspace paths (`src/paths.py`).

Every test points HOME, WS_STATE, WS_CONFERENCE, AT_WS_CONFIG and the install overrides at a
temporary folder, so the real ~/.config, ~/.cache and ~/.local/share are never read or written.
The functions read the environment at call time, which is what lets one process test both layouts.
"""
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import paths  # noqa: E402

ENV_KEYS = (
    "HOME", "WS_STATE", "WS_CONFERENCE", "AT_WS_CONFIG", "AT_HARNESS_DIR", "AT_SECURITY_DIR",
    "AT_WS_INSTALL_DIR",
)


@contextlib.contextmanager
def patched_env(**values):
    saved = {name: os.environ.get(name) for name in ENV_KEYS}
    for name in ENV_KEYS:
        os.environ.pop(name, None)
    for name, value in values.items():
        if value is not None:
            os.environ[name] = str(value)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class PathsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()

    def env(self, **overrides):
        values = {"HOME": self.home}
        values.update(overrides)
        return patched_env(**values)

    # --- defaults and overrides ----------------------------------------------------------------
    def test_defaults_use_the_control_plane_and_the_installed_copy(self):
        config = self.home / ".config" / "ai-tmux-workspace"
        install = self.home / ".local" / "share" / "ai-tmux-workspace"
        with self.env():
            self.assertEqual(paths.home(), self.home)
            self.assertEqual(paths.config_dir(), config)
            self.assertEqual(paths.config_path(), config / "config.yaml")
            self.assertEqual(paths.legacy_config_path(), config / "settings.json")
            self.assertEqual(paths.state_dir(), config / "state")
            self.assertEqual(paths.settings_dir(), config / "state" / "settings")
            self.assertEqual(paths.conference_dir(), config / "conference")
            self.assertEqual(paths.install_dir(), install)
            self.assertEqual(paths.harness_dir(), install / "harness")
            self.assertEqual(paths.security_dir(), install / "security")
            self.assertEqual(paths.legacy_state_dir(), self.home / ".cache" / "workspace")

    def test_overrides_win_and_are_read_at_call_time(self):
        state = self.root / "state"
        conference = self.root / "conference"
        install = self.root / "install"
        harness = self.root / "harness"
        security = self.root / "security"
        config = self.root / "cfg" / "config.yaml"
        with self.env():
            self.assertEqual(paths.state_dir(), self.home / ".config" / "ai-tmux-workspace" / "state")
            self.assertEqual(paths.config_path(),
                             self.home / ".config" / "ai-tmux-workspace" / "config.yaml")
        with self.env(WS_STATE=str(state), WS_CONFERENCE=str(conference),
                      AT_WS_INSTALL_DIR=str(install), AT_HARNESS_DIR=str(harness),
                      AT_SECURITY_DIR=str(security), AT_WS_CONFIG=str(config)):
            self.assertEqual(paths.state_dir(), state)
            self.assertEqual(paths.settings_dir(), state / "settings")
            self.assertEqual(paths.conference_dir(), conference)
            self.assertEqual(paths.install_dir(), install)
            self.assertEqual(paths.harness_dir(), harness)
            self.assertEqual(paths.security_dir(), security)
            self.assertEqual(paths.config_dir(), config.parent)
            self.assertEqual(paths.config_path(), config)
            self.assertEqual(paths.legacy_config_path(), config.with_suffix(".json"))

    def test_install_dir_pointing_at_the_clone_resolves_to_the_layout_root(self):
        root = self.root / "layout"
        clone = root / "app"
        for override in (root, clone):
            with self.env(AT_WS_INSTALL_DIR=str(override)):
                self.assertEqual(paths.layout_root(), root, override)
                self.assertEqual(paths.install_dir(), root, override)
                self.assertEqual(paths.harness_dir(), root / "harness", override)
                self.assertEqual(paths.security_dir(), root / "security", override)
        with self.env(AT_WS_INSTALL_DIR=str(self.root / "elsewhere" / "copy")):
            self.assertEqual(paths.layout_root(), self.root / "elsewhere" / "copy")

    def test_config_dir_is_the_parent_of_at_ws_config(self):
        config = self.root / "custom" / "config.yaml"
        with self.env(AT_WS_CONFIG=str(config)):
            self.assertEqual(paths.config_dir(), config.parent)
            self.assertEqual(paths.state_dir(), config.parent / "state")
            self.assertEqual(paths.conference_dir(), config.parent / "conference")
            self.assertEqual(paths.legacy_config_path(), config.with_suffix(".json"))

    def test_security_dir_is_the_harness_sibling_when_only_the_harness_is_declared(self):
        harness = self.root / "installed" / "harness"
        with self.env(AT_HARNESS_DIR=str(harness)):
            self.assertEqual(paths.security_dir(), harness.parent / "security")
        security = self.root / "explicit-security"
        with self.env(AT_HARNESS_DIR=str(harness), AT_SECURITY_DIR=str(security)):
            self.assertEqual(paths.security_dir(), security)

    # --- migration ----------------------------------------------------------------------------
    def create_legacy(self, **files):
        legacy = self.home / ".cache" / "workspace"
        for name, text in files.items():
            path = legacy / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return legacy

    def test_migrate_moves_each_child_without_overwriting(self):
        legacy = self.create_legacy(**{"tabs/a.json": "old", "policy.jsonl": "old"})
        destination = self.home / ".config" / "ai-tmux-workspace" / "state"
        destination.mkdir(parents=True)
        (destination / "tabs").mkdir()
        (destination / "tabs" / "a.json").write_text("new", encoding="utf-8")

        with self.env():
            moved = paths.migrate_state()

        self.assertEqual(moved, ["policy.jsonl"])
        self.assertEqual((destination / "tabs" / "a.json").read_text(encoding="utf-8"), "new")
        self.assertEqual((legacy / "tabs" / "a.json").read_text(encoding="utf-8"), "old")
        self.assertFalse((legacy / "policy.jsonl").exists())
        self.assertEqual((destination / "policy.jsonl").read_text(encoding="utf-8"), "old")

    def test_migrate_is_idempotent_and_keeps_the_legacy_folder(self):
        legacy = self.create_legacy(**{"sessions/s.json": "x", "tabs/b.json": "y"})
        with self.env():
            self.assertEqual(paths.migrate_state(), ["sessions", "tabs"])
            self.assertEqual(paths.migrate_state(), [])
        self.assertTrue(legacy.is_dir())
        state = self.home / ".config" / "ai-tmux-workspace" / "state"
        self.assertEqual((state / "sessions" / "s.json").read_text(encoding="utf-8"), "x")
        self.assertEqual((state / "tabs" / "b.json").read_text(encoding="utf-8"), "y")

    def test_migrate_does_nothing_when_ws_state_is_declared(self):
        legacy = self.create_legacy(**{"tabs/a.json": "x"})
        declared = self.root / "declared-state"
        with self.env(WS_STATE=str(declared)):
            self.assertEqual(paths.migrate_state(), [])
        self.assertTrue((legacy / "tabs" / "a.json").is_file())
        self.assertFalse((declared / "tabs").exists())

    def test_migrate_never_moves_the_legacy_into_itself(self):
        legacy = self.create_legacy(**{"tabs/a.json": "x"})
        with self.env(WS_STATE=str(legacy)):
            self.assertEqual(paths.migrate_state(), [])
        self.assertTrue((legacy / "tabs" / "a.json").is_file())
        with self.env(WS_STATE=str(legacy / "nested")):
            self.assertEqual(paths.migrate_state(), [])
        self.assertFalse((legacy / "nested").exists())

    def test_migrate_reports_an_oserror(self):
        self.create_legacy(**{"tabs/a.json": "x"})
        with self.env(), mock.patch("paths.shutil.move", side_effect=OSError("disk full")):
            with self.assertRaises(OSError) as raised:
                paths.migrate_state()
        self.assertIn("disk full", str(raised.exception))

    # --- layout and CLI -----------------------------------------------------------------------
    def test_ensure_layout_creates_config_state_and_conference_and_migrates(self):
        legacy = self.create_legacy(**{"tabs/a.json": "x"})
        install = self.home / ".local" / "share" / "ai-tmux-workspace"
        with self.env():
            moved = paths.ensure_layout()
        config = self.home / ".config" / "ai-tmux-workspace"
        self.assertEqual(moved, ["tabs"])
        self.assertTrue(config.is_dir())
        self.assertTrue((config / "state").is_dir())
        self.assertTrue((config / "conference").is_dir())
        self.assertTrue((config / "state" / "tabs" / "a.json").is_file())
        self.assertFalse((legacy / "tabs").exists())
        self.assertFalse(install.exists(), "the installer owns the installed copy")

    def test_ensure_layout_skips_migration_when_asked(self):
        self.create_legacy(**{"tabs/a.json": "x"})
        with self.env():
            self.assertEqual(paths.ensure_layout(migrate=False), [])
        self.assertTrue((self.home / ".config" / "ai-tmux-workspace" / "state").is_dir())
        self.assertTrue((self.home / ".cache" / "workspace" / "tabs" / "a.json").is_file())

    def test_main_migrate_returns_zero_and_reports_oserror(self):
        self.create_legacy(**{"tabs/a.json": "x"})
        with self.env():
            self.assertEqual(paths.main(["migrate"]), 0)
        self.assertTrue((self.home / ".config" / "ai-tmux-workspace" / "state"
                         / "tabs" / "a.json").is_file())

        self.create_legacy(**{"tabs/b.json": "y"})
        with self.env(), mock.patch("paths.ensure_layout", side_effect=OSError("boom")), \
                mock.patch("sys.stderr") as stderr:
            self.assertEqual(paths.main(["migrate"]), 1)
            self.assertIn("boom", "".join(str(call) for call in stderr.write.call_args_list))
        with mock.patch("sys.stderr"):
            self.assertEqual(paths.main([]), 2)


if __name__ == "__main__":
    unittest.main()
