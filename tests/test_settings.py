"""Tests for the workspace-generated settings: the harness base (hooks + deny) merged with each
profile's own settings, the `--settings` launch flag, and the refusal to write a literal access key.

Each test runs `src/lib.py` in a child process with temporary HOME, WS_STATE, AT_WS_CONFIG and
AT_HARNESS_DIR, so the real config, cache and installed harness are never read or written. The
committed harness/settings.json is the explicit fallback until item 33 installs the harness."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "src" / "lib.py"

FAKE_KEY = "sk-" + "fake-" + "0" * 30      # assembled at runtime: no key-like literal in the repo

HARNESS_POLICY = """\
versao: 1
itens:
  - id: F03
    regra: "Root uses doas, never sudo"
    tipo: deny
    alvo: Bash
    padrao: "doas *"

  - id: F07
    regra: "Read the harness before writing"
    texto: regras/agents.md
    tipo: lembrete
    alvo: "Write|Edit|NotebookEdit"
"""


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.home.mkdir()
        self.state.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text("{}", encoding="utf-8")

    def configure(self, config):
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def env(self, harness=None):
        return dict(os.environ, HOME=str(self.home), WS_STATE=str(self.state),
                    AT_WS_CONFIG=str(self.config),
                    AT_HARNESS_DIR=str(harness if harness is not None else self.root / "no-harness"))

    def run_lib(self, *args, harness=None):
        return subprocess.run([sys.executable, str(LIB), *args], env=self.env(harness),
                              capture_output=True, text=True, check=False)

    def ensure(self, profile="", harness=None):
        return self.run_lib("ensure-settings", profile, harness=harness)

    def generated(self, profile="deepseek"):
        return json.loads((self.state / "settings" / f"{profile}.json").read_text(encoding="utf-8"))

    def harness(self, policy=HARNESS_POLICY):
        folder = self.root / "installed-harness"
        folder.mkdir(exist_ok=True)
        (folder / "forcado.yaml").write_text(textwrap.dedent(policy), encoding="utf-8")
        (folder / "harness.py").write_text("# installed hook\n", encoding="utf-8")
        return folder

    def mapping_profile(self, settings, agent="deepseek", name="deepseek"):
        self.configure({"agent": agent, "profiles": {name: {"command": "claude", "settings": settings}}})

    # 1 ---------------------------------------------------------------------------------------
    def test_profile_settings_are_merged_over_the_harness_base(self):
        self.mapping_profile({
            "env": {"ANTHROPIC_BASE_URL": "https://example.invalid/anthropic",
                    "ANTHROPIC_MODEL": "deepseek-flash"},
            "model": "deepseek-flash",
            "apiKeyHelper": "pass show deepseek/api-key",
            "permissions": {"defaultMode": "bypassPermissions", "deny": ["Bash(rm -rf:*)"]},
            "hooks": {"SessionStart": []},          # a profile cannot replace the forced hooks
        })
        proc = self.ensure("deepseek")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), str(self.state / "settings" / "deepseek.json"))
        settings = self.generated()

        self.assertIn("Bash(sudo:*)", settings["permissions"]["deny"])       # committed base
        self.assertIn("Bash(rm -rf:*)", settings["permissions"]["deny"])     # profile union
        self.assertEqual(settings["permissions"]["defaultMode"], "bypassPermissions")
        self.assertEqual(settings["env"]["ANTHROPIC_MODEL"], "deepseek-flash")
        self.assertEqual(settings["model"], "deepseek-flash")
        self.assertEqual(settings["apiKeyHelper"], "pass show deepseek/api-key")
        # the installed harness is absent: the hook points at this repo's existing script, never a
        # missing path (a non-zero hook would block every prompt)
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
                         f'python3 "{ROOT / "harness" / "harness.py"}"')
        self.assertIn("SessionStart", settings["hooks"])
        self.assertTrue(settings["hooks"]["SessionStart"])

    def test_generated_settings_has_no_literal_key_and_is_private(self):
        self.mapping_profile({
            "env": {"ANTHROPIC_BASE_URL": "https://example.invalid/anthropic"},
            "apiKeyHelper": "pass show deepseek/api-key",
        })
        self.assertEqual(self.ensure("deepseek").returncode, 0)
        path = self.state / "settings" / "deepseek.json"
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", text)
        self.assertNotIn("ANTHROPIC_API_KEY", text)
        self.assertIn("apiKeyHelper", text)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_installed_harness_policy_is_the_base_when_present(self):
        folder = self.harness()
        self.mapping_profile({})
        proc = self.ensure("deepseek", harness=folder)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        settings = self.generated()
        self.assertIn("Bash(doas:*)", settings["permissions"]["deny"])
        self.assertNotIn("Bash(sudo:*)", settings["permissions"]["deny"])
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
                         f'python3 "{folder / "harness.py"}"')

    def test_missing_installed_harness_uses_the_committed_base(self):
        self.mapping_profile({})
        proc = self.ensure("deepseek", harness=self.root / "absent")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        deny = self.generated()["permissions"]["deny"]
        self.assertIn("Bash(sudo:*)", deny)
        self.assertIn("Write(./**/.env)", deny)

    # 2 ---------------------------------------------------------------------------------------
    def test_literal_env_key_is_refused_and_nothing_is_written(self):
        self.mapping_profile({"env": {"ANTHROPIC_AUTH_TOKEN": FAKE_KEY}})
        proc = self.ensure("deepseek")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("apiKeyHelper", proc.stderr)         # the message points to the vault helper
        self.assertFalse((self.state / "settings" / "deepseek.json").exists())

    def test_literal_key_in_any_env_name_is_refused(self):
        keys = ("OPENAI_API_KEY", "PROVIDER_ACCESS_TOKEN", "SERVICE_PASSWORD",
                "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN")
        for key in keys:
            with self.subTest(key=key):
                self.mapping_profile({"env": {key: "literal-value"}})
                proc = self.ensure("deepseek")
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertIn("apiKeyHelper", proc.stderr)         # the message points to the vault helper

    def test_non_secret_env_names_are_not_mistaken_for_keys(self):
        self.mapping_profile({"env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
                                      "ENABLE_TOOL_SEARCH": "false"}})
        self.assertEqual(self.ensure("deepseek").returncode, 0)
        self.assertEqual(self.generated()["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "1000000")

    def test_literal_key_inside_api_key_helper_is_refused(self):
        self.mapping_profile({"apiKeyHelper": f"printf '%s' {FAKE_KEY}"})
        proc = self.ensure("deepseek")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("apiKeyHelper", proc.stderr)         # the message points to the vault helper

    def test_literal_key_in_the_profile_command_is_refused(self):
        self.configure({"agent": "deepseek", "profiles": {
            "deepseek": f"claude --api-key {FAKE_KEY}"}})
        proc = self.run_lib("profile-cmd", "deepseek")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("apiKeyHelper", proc.stderr)         # the message points to the vault helper

    def test_bad_settings_shape_is_refused(self):
        self.configure({"agent": "deepseek", "profiles": {"deepseek": {"command": "claude",
                                                                       "settings": ["not", "a", "map"]}}})
        proc = self.ensure("deepseek")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("não é um mapa", proc.stderr)

    # 3 ---------------------------------------------------------------------------------------
    def test_launch_command_appends_the_generated_settings(self):
        self.mapping_profile({})
        proc = self.run_lib("profile-cmd", "deepseek")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout.strip()
        self.assertTrue(out.startswith("env ENABLE_TOOL_SEARCH=false claude --settings "))
        self.assertIn(str(self.state / "settings" / "deepseek.json"), out)

    def test_default_claude_profile_gets_the_base_without_a_model(self):
        self.configure({"agent": "claude", "profiles": {"claude": "claude"}})
        proc = self.run_lib("profile-cmd", "claude")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout.strip()
        self.assertTrue(out.startswith("claude --settings "))
        self.assertNotIn("ENABLE_TOOL_SEARCH", out)
        settings = self.generated("claude")
        self.assertNotIn("model", settings)
        self.assertIn("Bash(sudo:*)", settings["permissions"]["deny"])

    def test_legacy_string_profile_with_settings_is_left_untouched(self):
        self.configure({"agent": "deepseek", "profiles": {
            "deepseek": "claude --settings ~/.claude/settings.deepseek.json"}})
        proc = self.run_lib("profile-cmd", "deepseek")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("claude --settings ~/.claude/settings.deepseek.json", proc.stdout)
        self.assertNotIn(str(self.state / "settings"), proc.stdout)
        self.assertFalse((self.state / "settings" / "deepseek.json").exists())

    def test_profile_names_lists_every_profile_plus_the_chosen_agent(self):
        self.configure({"agent": "deepseek", "profiles": {"claude": "claude", "deepseek": {"command": "claude"}}})
        proc = self.run_lib("profile-names")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ["claude", "deepseek"])

    def test_mapping_profile_switch_is_a_different_identity_and_mode(self):
        repo = self.root / "repo"
        repo.mkdir()
        self.configure({"agent": "deepseek", "profiles": {
            "claude": "claude", "deepseek": {"command": "claude"}}})
        first = self.run_lib("profile-cmd", "deepseek", str(repo))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("--permission-mode acceptEdits", first.stdout)     # Claude harness, never bypass
        self.assertNotIn("bypassPermissions", first.stdout)
        saved = json.loads((self.state / "permission.json").read_text(encoding="utf-8"))
        self.assertEqual(saved[str(repo.resolve())]["command"], "claude [deepseek]")

        self.configure({"agent": "claude", "profiles": {
            "claude": "claude", "deepseek": {"command": "claude"}}})
        second = self.run_lib("profile-cmd", "claude", str(repo))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("--permission-mode acceptEdits", second.stdout)     # the old profile's mode is not reapplied

    # --- manage (item 93) -------------------------------------------------------------------
    def test_manage_false_launches_pure_claude_and_generates_no_settings(self):
        self.configure({"manage": False, "agent": "claude", "profiles": {"claude": "claude"}})
        proc = self.run_lib("profile-cmd", "claude")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "claude")
        ensured = self.run_lib("ensure-settings", "claude")
        self.assertEqual(ensured.returncode, 0, ensured.stderr)
        self.assertEqual(ensured.stdout.strip(), "")
        self.assertFalse((self.state / "settings" / "claude.json").exists())

    def test_manage_false_keeps_continue_without_claude_flags(self):
        self.configure({"manage": False, "agent": "claude", "profiles": {"claude": "claude"}})
        repo = self.root / "repo"
        repo.mkdir()
        first = self.run_lib("opening-command", str(repo))
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_lib("opening-command", str(repo))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("--continue", second.stdout)
        for flag in ("--settings", "--permission-mode", "ENABLE_TOOL_SEARCH"):
            self.assertNotIn(flag, second.stdout)

    def test_manage_missing_keeps_the_historical_managed_behavior(self):
        self.configure({"agent": "claude", "profiles": {"claude": "claude"}})
        proc = self.run_lib("profile-cmd", "claude")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--settings", proc.stdout)
        self.assertTrue((self.state / "settings" / "claude.json").is_file())

    def test_claude_deepseek_settings_uses_the_endpoint_and_the_vault_without_bypass(self):
        self.configure({"manage": True, "agent": "claude-deepseek", "profiles": {
            "claude-deepseek": {"command": "claude", "settings": {
                "env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"},
                "apiKeyHelper": "pass show deepseek/api-key"}}}})
        proc = self.ensure("claude-deepseek")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        settings = self.generated("claude-deepseek")
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"],
                         "https://api.deepseek.com/anthropic")
        self.assertEqual(settings["apiKeyHelper"], "pass show deepseek/api-key")
        text = json.dumps(settings)
        self.assertNotIn("bypassPermissions", text)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", text)

    def test_claude_deepseek_opens_in_accept_edits_and_never_bypasses(self):
        repo = self.root / "repo"
        repo.mkdir()
        self.configure({"manage": True, "agent": "claude-deepseek", "profiles": {
            "claude-deepseek": {"command": "claude", "settings": {
                "env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"},
                "apiKeyHelper": "pass show deepseek/api-key"}}}})
        proc = self.run_lib("opening-command", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--permission-mode acceptEdits", proc.stdout)
        self.assertNotIn("bypassPermissions", proc.stdout)
        settings = (self.state / "settings" / "claude-deepseek.json").read_text(encoding="utf-8")
        self.assertNotIn("bypassPermissions", settings)

    def test_opencode_deepseek_profile_keeps_no_claude_flag(self):
        self.configure({"manage": True, "agent": "opencode-deepseek", "profiles": {
            "opencode-deepseek": {"harness": "opencode", "command": "opencode",
                                  "config": "~/.config/ai-tmux-workspace/opencode-deepseek.json",
                                  "model": "deepseek/deepseek-flash"}}})
        proc = self.run_lib("profile-cmd")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout.strip()
        self.assertEqual(out, "env OPENCODE_CONFIG=" +
                         str(self.home / ".config" / "ai-tmux-workspace" / "opencode-deepseek.json") +
                         " opencode")
        for flag in ("--settings", "--permission-mode", "--continue", "ENABLE_TOOL_SEARCH"):
            self.assertNotIn(flag, out)
        ensured = self.run_lib("ensure-settings", "opencode-deepseek")
        self.assertEqual(ensured.returncode, 0, ensured.stderr)
        self.assertEqual(ensured.stdout.strip(), "")
        self.assertFalse((self.state / "settings" / "opencode-deepseek.json").exists())

    # --- OpenCode harness ------------------------------------------------------------------
    def opencode_profile(self, **extra):
        profile = {"harness": "opencode", "command": "opencode",
                   "config": "~/.config/ai-tmux-workspace/opencode.json"}
        profile.update(extra)
        self.configure({"agent": "opencode", "profiles": {"opencode": profile}})

    def test_opencode_launch_exports_the_config_and_adds_no_claude_flag(self):
        self.opencode_profile()
        proc = self.run_lib("profile-cmd", "opencode")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout.strip()
        self.assertEqual(out, "env OPENCODE_CONFIG=" +
                         str(self.home / ".config" / "ai-tmux-workspace" / "opencode.json") + " opencode")
        for flag in ("--settings", "--permission-mode", "--continue", "ENABLE_TOOL_SEARCH"):
            self.assertNotIn(flag, out)

    def test_opencode_profile_defaults_to_the_opencode_command(self):
        self.configure({"agent": "opencode", "profiles": {"opencode": {"harness": "opencode"}}})
        proc = self.run_lib("profile-cmd", "opencode")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "opencode")

    def test_opencode_profile_generates_no_settings(self):
        self.opencode_profile()
        proc = self.ensure("opencode")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertFalse((self.state / "settings" / "opencode.json").exists())

    def test_agent_harness_reports_the_chosen_profile_harness(self):
        self.opencode_profile()
        proc = self.run_lib("agent-harness")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "opencode")
        self.configure({"agent": "claude", "profiles": {"claude": "claude"}})
        self.assertEqual(self.run_lib("agent-harness").stdout.strip(), "claude")


if __name__ == "__main__":
    unittest.main()
