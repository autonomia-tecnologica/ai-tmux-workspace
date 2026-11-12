"""`workspace config` and `workspace config update` (src/config_cmd.py).

Everything runs under a temporary HOME, AT_WS_CONFIG, WS_STATE and AT_HARNESS_DIR: nothing touches the
real ~/.config, ~/.local/share or ~/.claude. The CLI cases run the script as the launcher does.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
CONFIG_CMD = SRC / "config_cmd.py"
DRIFT = SRC / "drift.py"
EXAMPLE = ROOT / "config" / "config.example.yaml"
sys.path.insert(0, str(SRC))

import config_cmd  # noqa: E402
import installer  # noqa: E402
import yaml_min  # noqa: E402
from i18n import t  # noqa: E402

EXAMPLE_TEXT = EXAMPLE.read_text(encoding="utf-8")
LAST_PROMPT = [line for line in EXAMPLE_TEXT.splitlines() if line.startswith('  - "')][-1]


def without_line(text, line):
    return "\n".join(item for item in text.splitlines() if item != line) + "\n"


def without_block(text, start, stop):
    """Text with the top-level block from `start` up to (not including) `stop` removed."""
    head, _, rest = text.partition("\n" + start)
    _, _, tail = rest.partition("\n" + stop)
    return head + "\n" + stop + tail


class PlanConfigTests(unittest.TestCase):
    def test_missing_prompt_is_listed_and_added_once(self):
        user = without_line(EXAMPLE_TEXT, LAST_PROMPT)
        new_text, plan = config_cmd.plan_config(user, EXAMPLE_TEXT)
        self.assertEqual([c["kind"] for c in plan.changes], ["item"])
        self.assertEqual(plan.changes[0]["value"], yaml_min.loads(EXAMPLE_TEXT)["prompts"][-1])
        self.assertIn(LAST_PROMPT, new_text.splitlines())
        again, second = config_cmd.plan_config(new_text, EXAMPLE_TEXT)
        self.assertEqual(second.changes, [])
        self.assertEqual(again, new_text)

    def test_existing_values_are_kept_and_custom_items_stay(self):
        user = EXAMPLE_TEXT.replace("notifications: true", "notifications: false")
        user = user.replace("prompts:\n", "prompts:\n  - \"Meu prompt\"\n", 1)
        new_text, _plan = config_cmd.plan_config(user, EXAMPLE_TEXT)
        new = yaml_min.loads(new_text)
        self.assertIs(new["notifications"], False)
        self.assertEqual(new["prompts"][0], "Meu prompt")

    def test_removed_pattern_does_not_come_back(self):
        user = without_line(EXAMPLE_TEXT, LAST_PROMPT)
        removed = config_cmd._change("item", ["prompts"], yaml_min.loads(EXAMPLE_TEXT)["prompts"][-1])
        new_text, plan = config_cmd.plan_config(user, EXAMPLE_TEXT,
                                                offered={config_cmd.identity(removed)})
        self.assertEqual(plan.changes, [])
        self.assertEqual(plan.skipped, 1)
        self.assertEqual(new_text, user)

    def test_personal_keys_are_never_added(self):
        user = without_block(EXAMPLE_TEXT, "profiles:", "prompts:")
        user = without_block(user, "groups:", "relations:")
        absent = [key for key in config_cmd.PERSONAL_KEYS if key not in yaml_min.loads(user)]
        self.assertIn("profiles", absent)
        self.assertIn("groups", absent)
        new_text, plan = config_cmd.plan_config(user, EXAMPLE_TEXT)
        added = {".".join(c["path"]) for c in plan.changes}
        self.assertFalse(added & set(config_cmd.PERSONAL_KEYS))
        new = yaml_min.loads(new_text)
        for key in absent:
            self.assertNotIn(key, new)

    def test_missing_top_level_key_is_added_with_its_comment(self):
        user = without_line(EXAMPLE_TEXT, "icons: true")
        self.assertNotIn("icons", yaml_min.loads(user))
        new_text, plan = config_cmd.plan_config(user, EXAMPLE_TEXT)
        self.assertEqual([(c["kind"], c["path"]) for c in plan.changes], [("key", ["icons"])])
        self.assertIn("icons: true", new_text.splitlines())
        self.assertEqual(yaml_min.loads(new_text)["icons"], True)

    def test_empty_inline_list_is_opened_and_filled(self):
        user = without_block(EXAMPLE_TEXT, "prompts:", "options:")
        user = user.replace("\noptions:", "\nprompts: []\noptions:", 1)
        new_text, plan = config_cmd.plan_config(user, EXAMPLE_TEXT)
        self.assertEqual([c["kind"] for c in plan.changes], ["open"])
        self.assertEqual(yaml_min.loads(new_text)["prompts"], yaml_min.loads(EXAMPLE_TEXT)["prompts"])

    def test_inline_non_empty_list_is_reported_not_rewritten(self):
        user = without_block(EXAMPLE_TEXT, "prompts:", "options:")
        user = user.replace("\noptions:", '\nprompts: ["so um"]\noptions:', 1)
        new_text, plan = config_cmd.plan_config(user, EXAMPLE_TEXT)
        self.assertEqual(plan.manual, ["prompts"])
        self.assertEqual(new_text, user)

    def test_missing_subkey_of_a_nested_map_is_added_inside_it(self):
        user = EXAMPLE_TEXT.replace("    - name: micro\n      command: micro\n      fullscreen: false\n", "", 1)
        user = user.replace("  code:\n    - name: nvim\n      command: nvim\n      fullscreen: false\n", "", 1)
        new_text, plan = config_cmd.plan_config(user, EXAMPLE_TEXT)
        self.assertTrue(any(c["path"][:1] == ["editor"] for c in plan.changes))
        self.assertEqual(yaml_min.loads(new_text)["editor"], yaml_min.loads(EXAMPLE_TEXT)["editor"])

    def test_the_example_against_itself_has_nothing_to_add(self):
        new_text, plan = config_cmd.plan_config(EXAMPLE_TEXT, EXAMPLE_TEXT)
        self.assertEqual((plan.changes, plan.manual), ([], []))
        self.assertEqual(new_text, EXAMPLE_TEXT)

    def test_unreadable_config_raises_without_writing(self):
        with self.assertRaises(ValueError):
            config_cmd.plan_config("notifications: [\n", EXAMPLE_TEXT)


class OfferedRegistryTests(unittest.TestCase):
    def test_round_trip_under_the_config_folder(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {"AT_WS_CONFIG": str(Path(tmp) / "config.yaml")}):
            self.assertEqual(config_cmd.load_offered(), set())
            config_cmd.save_offered(["a"])
            config_cmd.save_offered(["b", "a"])
            self.assertEqual(config_cmd.load_offered(), {"a", "b"})
            self.assertEqual(config_cmd.state_path(), Path(tmp) / "state" / "config-offered.json")

    def test_unreadable_registry_refuses(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {"AT_WS_CONFIG": str(Path(tmp) / "config.yaml")}):
            config_cmd.state_path().parent.mkdir(parents=True)
            config_cmd.state_path().write_text("nao e json", encoding="utf-8")
            with self.assertRaises(ValueError):
                config_cmd.load_offered()


class ProfileStepTests(unittest.TestCase):
    def test_drifted_profile_is_regenerated_and_then_clean(self):
        expected = {"env": {"ANTHROPIC_BASE_URL": "https://example.invalid"}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"WS_STATE": tmp}), \
                mock.patch.object(config_cmd.lib, "manage_enabled", return_value=True), \
                mock.patch.object(config_cmd.lib, "profile_names", return_value=["claude"]), \
                mock.patch.object(config_cmd.lib, "expected_profile_settings", return_value=expected):
            path = config_cmd.lib.profile_settings_path("claude")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"stale": True}), encoding="utf-8")
            status, pending = config_cmd._profiles_step(apply=False)
            self.assertTrue(pending)
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"stale": True})
            config_cmd._profiles_step(apply=True)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)
            self.assertEqual(config_cmd._profiles_step(apply=False), (0, False))


class CliTests(unittest.TestCase):
    """The launcher's path: `python3 src/config_cmd.py ...` under a temporary HOME."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.config = base / "config" / "config.yaml"
        self.config.parent.mkdir()
        self.harness = base / "installed-harness"
        self.source = base / "canonical" / "harness"
        self.source.mkdir(parents=True)
        (self.source / "HARNESS.md").write_text("# Harness\n\nRegra de teste.\n", encoding="utf-8")
        (self.source / "forcado.yaml").write_text("regras: []\n", encoding="utf-8")
        self.env = dict(os.environ, HOME=str(self.home), AT_WS_CONFIG=str(self.config),
                        WS_STATE=str(base / "state-cache"), AT_HARNESS_DIR=str(self.harness),
                        AT_HARNESS_SOURCE=str(self.source), AT_WS_INSTALL_STATE=str(base / "install-state"))
        self.env.pop("WS_DIR", None)

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(CONFIG_CMD), *args], env=self.env,
                              capture_output=True, text=True, timeout=120)

    def run_doctor(self):
        return subprocess.run([sys.executable, str(DRIFT)], env=self.env, capture_output=True, text=True,
                              timeout=120)

    def test_show_prints_the_path_and_the_effective_text(self):
        self.config.write_text("notifications: false\n", encoding="utf-8")
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.config), result.stdout)
        self.assertIn("notifications: false", result.stdout)

    def test_unknown_argument_is_a_usage_error(self):
        result = self.run_cli("nope")
        self.assertEqual(result.returncode, 2)
        self.assertIn("workspace config", result.stderr)

    def test_config_update_adds_backs_up_and_is_idempotent(self):
        self.config.write_text(without_line(EXAMPLE_TEXT, LAST_PROMPT), encoding="utf-8")
        shown = self.run_cli("update")
        self.assertIn(LAST_PROMPT[len('  - "'):-1][:20], shown.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), without_line(EXAMPLE_TEXT, LAST_PROMPT))

        applied = self.run_cli("update", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn(LAST_PROMPT, self.config.read_text(encoding="utf-8").splitlines())
        backups = list(self.config.parent.glob("config.yaml.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), without_line(EXAMPLE_TEXT, LAST_PROMPT))
        self.assertTrue((self.config.parent / "state" / "config-offered.json").is_file())

        text_after_first_apply = self.config.read_text(encoding="utf-8")
        again = self.run_cli("update", "--apply")
        self.assertIn(t("config.update.none"), again.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), text_after_first_apply)
        self.assertEqual(len(list(self.config.parent.glob("config.yaml.bak-*"))), 1)

    def test_prompt_removed_by_the_person_stays_removed(self):
        self.config.write_text(without_line(EXAMPLE_TEXT, LAST_PROMPT), encoding="utf-8")
        self.run_cli("update", "--apply")
        without_it = without_line(self.config.read_text(encoding="utf-8"), LAST_PROMPT)
        self.config.write_text(without_it, encoding="utf-8")
        result = self.run_cli("update", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), without_it)

    def test_harness_drift_is_shown_and_fixed_by_apply(self):
        installer.copy_harness(self.source, self.harness)
        self.assertEqual(self.run_doctor().returncode, 0)
        os.chmod(self.harness / "HARNESS.md", 0o644)
        (self.harness / "HARNESS.md").write_text("# editado na mao\n", encoding="utf-8")

        shown = self.run_cli("update")
        self.assertIn(t("doctor.installed_edited"), shown.stdout)
        self.assertNotEqual(self.run_doctor().returncode, 0)

        applied = self.run_cli("update", "--apply")
        self.assertIn(t("config.harness.applied", path=str(self.harness)), applied.stdout)
        doctor = self.run_doctor()
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        lean = ROOT / "harness" / "HARNESS.md"
        self.assertEqual((self.harness / "HARNESS.md").read_bytes(), lean.read_bytes())

    def test_hooks_drift_is_shown_and_registered_by_apply(self):
        claude = self.home / ".claude" / "settings.json"
        shown = self.run_cli("update")
        self.assertIn(t("config.hooks.drift"), shown.stdout)
        self.assertFalse(claude.exists())

        applied = self.run_cli("update", "--apply")
        self.assertIn(t("config.hooks.applied"), applied.stdout)
        self.assertIn("hooks", json.loads(claude.read_text(encoding="utf-8")))

        again = self.run_cli("update")
        self.assertIn(t("config.hooks.none"), again.stdout)


if __name__ == "__main__":
    unittest.main()
