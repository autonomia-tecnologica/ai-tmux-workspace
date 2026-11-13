"""Tests for `workspace doctor` (src/drift.py) and the source.json written by the installer.

Every test uses a temporary HOME, installed harness, canonical source and code root; the real
environment ({HOME}, ~/.local/share, ~/.config) is never touched.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import i18n  # noqa: E402  (path set above)
DRIFT = ROOT / "src" / "drift.py"
WORKSPACE = ROOT / "bin" / "workspace"
BASH = shutil.which("bash") or "/bin/bash"
HARNESS = "# HARNESS\n\nRegra citada: `src/policy.py`.\n"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class DriftTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.ws = self.root / "code"
        self.source = self.root / "ambiente-de-trabalho" / "harness"
        self.installed = self.home / ".local" / "share" / "ai-tmux-workspace" / "harness"
        for path in (self.home, self.state, self.ws / "src", self.source, self.installed):
            path.mkdir(parents=True, exist_ok=True)
        (self.ws / "src" / "policy.py").write_text("# policy\n", encoding="utf-8")
        (self.source / "HARNESS.md").write_text(HARNESS, encoding="utf-8")
        (self.installed / "HARNESS.md").write_text(HARNESS, encoding="utf-8")
        self.write_record()

    def write_record(self, source=None, commit=None, source_sha=None, installed_sha=None):
        data = {
            "source": str(self.source if source is None else source),
            "commit": commit,
            "source_sha256": sha256(self.source / "HARNESS.md") if source_sha is None else source_sha,
            "installed_sha256": sha256(self.installed / "HARNESS.md") if installed_sha is None else installed_sha,
        }
        (self.installed / "source.json").write_text(json.dumps(data) + "\n", encoding="utf-8")

    def env(self, **overrides):
        env = {key: value for key, value in os.environ.items()
               if key not in ("AT_HARNESS_DIR", "WS_DIR", "WS_STATE", "AT_WS_CONFIG")}
        env.update(HOME=str(self.home), AT_HARNESS_DIR=str(self.installed), WS_DIR=str(self.ws),
                   WS_STATE=str(self.state), NO_COLOR="1")
        env.update(overrides)
        return env

    def run_doctor(self, *args, **overrides):
        return subprocess.run([sys.executable, str(DRIFT), *args], env=self.env(**overrides),
                              capture_output=True, text=True, check=False, timeout=30)

    # 1 ---------------------------------------------------------------------------------------
    def test_consistent_state_exits_zero(self):
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("doctor:", result.stdout)

    # 2 ---------------------------------------------------------------------------------------
    def test_edited_installed_copy_is_reported(self):
        with (self.installed / "HARNESS.md").open("a", encoding="utf-8") as handle:
            handle.write("\nnew rule in the installed copy\n")
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("HARNESS.md", result.stdout)
        self.assertIn("source.json", result.stdout)

    # 3 ---------------------------------------------------------------------------------------
    def test_changed_canonical_warns_until_reinstall(self):
        with (self.source / "HARNESS.md").open("a", encoding="utf-8") as handle:
            handle.write("\nnew rule in the canonical\n")
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("HARNESS.md", result.stdout)
        self.assertIn("source.json", result.stdout)

    # 4 ---------------------------------------------------------------------------------------
    def test_changed_source_commit_is_reported(self):
        self.write_record(commit="deadbeef")
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("deadbeef", result.stdout)

    # 5 ---------------------------------------------------------------------------------------
    def test_missing_cited_path_fails(self):
        (self.ws / "src" / "policy.py").unlink()
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("src/policy.py", result.stdout)

    # 6 ---------------------------------------------------------------------------------------
    def test_prose_and_anti_example_paths_are_ignored(self):
        text = HARNESS + "\nIgnore `docs/BACKLOG.md`, `TODO.md` or `script_2.py`.\n"
        (self.source / "HARNESS.md").write_text(text, encoding="utf-8")
        (self.installed / "HARNESS.md").write_text(text, encoding="utf-8")
        self.write_record()
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # 7 ---------------------------------------------------------------------------------------
    def test_missing_canonical_source_is_only_a_note(self):
        shutil.rmtree(self.source)
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(self.source), result.stdout)

    # 8 ---------------------------------------------------------------------------------------
    def test_warning_mode_never_fails(self):
        with (self.installed / "HARNESS.md").open("a", encoding="utf-8") as handle:
            handle.write("\nnew rule in the installed copy\n")
        result = self.run_doctor("--warn")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("doctor:", result.stdout)
        self.assertNotIn("doctor: harness", result.stdout)

    # 9 ---------------------------------------------------------------------------------------
    def test_missing_or_invalid_source_json_fails(self):
        (self.installed / "source.json").unlink()
        missing = self.run_doctor()
        self.assertEqual(missing.returncode, 1)
        self.assertIn("source.json", missing.stdout)
        (self.installed / "source.json").write_text("{", encoding="utf-8")
        invalid = self.run_doctor()
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("inválido", invalid.stdout)

    # 10 --------------------------------------------------------------------------------------
    def test_workspace_doctor_subcommand_is_wired(self):
        result = subprocess.run([BASH, str(WORKSPACE), "doctor"], env=self.env(),
                                capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("doctor:", result.stdout)

    # 11 --------------------------------------------------------------------------------------
    def test_every_literal_doctor_i18n_key_exists(self):
        source = DRIFT.read_text(encoding="utf-8")
        strings = json.loads((ROOT / "i18n" / "pt-BR.json").read_text(encoding="utf-8"))
        keys = set(re.findall(r't\(\s*"(doctor[^"]*)"\s*[,)]', source))
        self.assertTrue(keys)
        self.assertEqual(sorted(key for key in keys if key not in strings), [])

    # 12 (item 95) ---------------------------------------------------------------------------
    def test_install_dir_override_at_the_clone_resolves_the_harness_outside_app(self):
        """AT_WS_INSTALL_DIR=.../app (install.sh's clone) must not send drift into .../app/harness."""
        layout = self.home / ".local" / "share" / "ai-tmux-workspace"
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); import drift; "
                 "print(drift.harness_dir())")
        for override, expected in ((layout / "app", layout / "harness"),
                                   (layout, layout / "harness")):
            env = {key: value for key, value in os.environ.items()
                   if key not in ("AT_HARNESS_DIR", "WS_DIR", "WS_STATE", "AT_WS_CONFIG",
                                  "AT_WS_INSTALL_DIR")}
            env.update(HOME=str(self.home), AT_WS_INSTALL_DIR=str(override), NO_COLOR="1")
            result = subprocess.run([sys.executable, "-c", probe, str(ROOT / "src")],
                                    capture_output=True, text=True, env=env, cwd=str(self.root))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(result.stdout.strip()), expected, override)

    def test_harness_managed_false_reports_a_note_and_exits_zero(self):
        config = self.root / "config.yaml"
        config.write_text("harness_managed: false\n", encoding="utf-8")
        result = self.run_doctor(AT_WS_CONFIG=str(config))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(i18n.t("doctor.not_managed"), result.stdout)
        self.assertNotIn("doctor: falha", result.stdout)


if __name__ == "__main__":
    unittest.main()
