"""Tests for the forced harness mechanism: the Claude Code hook and the settings renderer.

Every test builds a fixture harness (forcado.yaml, CLAUDE.md and regras/*.md) in a temporary
directory and points AT_HARNESS_DIR and AT_HARNESS_STATE at it, so neither the development harness
nor the real cache is read or written. The hook is exercised as a child process with simulated
stdin, the same way Claude Code invokes it."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "security" / "gate.py"
REPO_SETTINGS = ROOT / "security" / "settings.json"
PRODUCTION_COMMAND = '"${WS_PYTHON:-python3}" "$HOME/.local/share/ai-tmux-workspace/security/gate.py"'

CLAUDE_MD = "# Harness entry\n\nRead the rules before working.\n"
AGENTS_MD = "# Agents\n\nOne slice at a time.\n"

HARNESS_POLICY = """\
versao: 1
itens:
  - id: F03
    regra: "Root uses pkexec, never sudo"
    texto: regras/conversation.md
    tipo: deny
    alvo: Bash
    padrao: "sudo *"

  - id: F05
    regra: "No plaintext secret in the repo"
    texto: regras/secrets.md
    tipo: deny
    alvo: "Write|Edit"
    padrao:
      - "**/.env"
      - "**/*.pem"

  - id: F07
    regra: "Read the harness before writing"
    texto: regras/agents.md
    tipo: lembrete
    alvo: "Write|Edit|NotebookEdit"
    failure: "open"
"""
HARNESS_POLICY_NO_F07 = """\
versao: 1
itens:
  - id: F03
    regra: "Root uses pkexec, never sudo"
    tipo: deny
    alvo: Bash
    padrao: "sudo *"
"""
HARNESS_POLICY_OTHER_RULE = """\
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


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.harness = self.root / "harness"
        self.state = self.root / "state"
        self.outside = self.root / "outside"
        for folder in (self.home, self.harness / "regras", self.state, self.outside):
            folder.mkdir(parents=True)
        (self.harness / "CLAUDE.md").write_text(CLAUDE_MD, encoding="utf-8")
        (self.harness / "regras" / "agents.md").write_text(AGENTS_MD, encoding="utf-8")
        (self.harness / "regras" / "conversation.md").write_text("# Conversation\n", encoding="utf-8")
        self.write_harness(HARNESS_POLICY)

    def write_harness(self, text):
        (self.harness / "forcado.yaml").write_text(text, encoding="utf-8")

    def env(self):
        return dict(os.environ, HOME=str(self.home), AT_HARNESS_DIR=str(self.harness),
                    AT_HARNESS_STATE=str(self.state))

    def run_hook(self, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload)
        return subprocess.run([sys.executable, str(HARNESS)], input=data, env=self.env(),
                              capture_output=True, text=True, check=False)

    def hook_output(self, payload, raw=None):
        proc = self.run_hook(payload, raw)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc, json.loads(proc.stdout) if proc.stdout.strip() else None

    def render(self, *args):
        return subprocess.run([sys.executable, str(HARNESS), "render", *args], env=self.env(),
                              capture_output=True, text=True, check=False)

    def session_start(self, session_id="sess-1"):
        return {"hook_event_name": "SessionStart", "session_id": session_id, "source": "startup"}

    def prompt(self, session_id="sess-1"):
        return {"hook_event_name": "UserPromptSubmit", "session_id": session_id, "prompt": "hi"}

    def read(self, path, session_id="sess-1"):
        return {"hook_event_name": "PostToolUse", "session_id": session_id, "tool_name": "Read",
                "tool_input": {"file_path": str(path)}}

    def write(self, session_id="sess-1", tool="Write", **extra):
        payload = {"hook_event_name": "PreToolUse", "session_id": session_id, "tool_name": tool,
                   "tool_input": {"file_path": str(self.outside / "file.txt")}}
        payload.update(extra)
        return payload

    def decision(self, payload):
        _, output = self.hook_output(payload)
        return (output or {}).get("hookSpecificOutput", {})

    # 1 ---------------------------------------------------------------------------------------
    def test_session_start_injects_installed_harness_texts(self):
        _, output = self.hook_output(self.session_start())
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "SessionStart")
        context = specific["additionalContext"]
        self.assertIn("Harness entry", context)
        self.assertIn("Read the rules before working.", context)
        self.assertIn("One slice at a time.", context)
        self.assertIn("## CLAUDE.md", context)
        self.assertIn("## regras/agents.md", context)

    def test_session_start_prefers_harness_md(self):
        (self.harness / "HARNESS.md").write_text("# HARNESS.md\n\nSingle file rule.\n",
                                                 encoding="utf-8")
        _, output = self.hook_output(self.session_start())
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("## HARNESS.md", context)
        self.assertIn("Single file rule.", context)
        self.assertNotIn("## CLAUDE.md", context)
        self.assertNotIn("## regras/agents.md", context)

    # 2 ---------------------------------------------------------------------------------------
    def test_user_prompt_submit_injects_reminder_from_f07(self):
        _, output = self.hook_output(self.prompt())
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
        context = specific["additionalContext"]
        self.assertIn("Read the harness before writing", context)
        self.assertIn("regras/agents.md", context)
        self.assertIn("not read", context)

    def test_user_prompt_submit_reports_read_session(self):
        self.run_hook(self.read(self.harness / "CLAUDE.md"))
        _, output = self.hook_output(self.prompt())
        self.assertIn("read in this session", output["hookSpecificOutput"]["additionalContext"])

    # 3 ---------------------------------------------------------------------------------------
    def test_write_is_denied_before_reading_the_harness(self):
        for tool in ("Write", "Edit", "NotebookEdit"):
            with self.subTest(tool=tool):
                specific = self.decision(self.write(tool=tool))
                self.assertEqual(specific.get("permissionDecision"), "deny")
                self.assertIn("Read", specific.get("permissionDecisionReason", ""))

    # 4 ---------------------------------------------------------------------------------------
    def test_reading_a_harness_file_unlocks_the_next_write(self):
        proc, _ = self.hook_output(self.read(self.harness / "regras" / "agents.md"))
        self.assertEqual(proc.stdout.strip(), "")
        proc, output = self.hook_output(self.write())
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIsNone(output)

    # 5 ---------------------------------------------------------------------------------------
    def test_reading_outside_the_harness_does_not_mark(self):
        target = self.outside / "note.md"
        target.write_text("not the harness\n", encoding="utf-8")
        self.run_hook(self.read(target))
        specific = self.decision(self.write())
        self.assertEqual(specific.get("permissionDecision"), "deny")

    def test_subagent_is_exempt_from_the_gate(self):
        specific = self.decision(self.write(agent_id="agent-1"))
        self.assertEqual(specific, {})

    # 6 ---------------------------------------------------------------------------------------
    def test_removing_f07_in_yaml_disables_the_gate_and_reminder(self):
        self.write_harness(HARNESS_POLICY_NO_F07)
        proc, output = self.hook_output(self.prompt())
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIsNone(output)
        proc, output = self.hook_output(self.write())
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIsNone(output)

    def test_changing_f03_in_yaml_changes_the_rendered_deny(self):
        self.write_harness(HARNESS_POLICY_OTHER_RULE)
        target = self.root / "settings.json"
        proc = self.render("--out", str(target))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        deny = json.loads(target.read_text(encoding="utf-8"))["permissions"]["deny"]
        self.assertIn("Bash(doas:*)", deny)
        self.assertNotIn("Bash(sudo:*)", deny)

    # 7 ---------------------------------------------------------------------------------------
    def test_invalid_input_fails_open(self):
        cases = [
            ("not json", None),
            ("", None),
            ("[]", None),
            (json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write"}), None),
            (json.dumps({"hook_event_name": "PreToolUse", "session_id": "../escape",
                         "tool_name": "Write"}), None),
            (json.dumps({"hook_event_name": "Nope", "session_id": "sess-1"}), None),
        ]
        for raw, _ in cases:
            with self.subTest(raw=raw):
                proc = self.run_hook(None, raw=raw)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout.strip(), "")

    def test_missing_harness_fails_open(self):
        (self.harness / "forcado.yaml").unlink()
        proc = self.run_hook(self.write())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    # 8 ---------------------------------------------------------------------------------------
    def test_render_registers_hooks_and_every_deny(self):
        target = self.root / "settings.json"
        proc = self.render("--out", str(target))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        settings = json.loads(target.read_text(encoding="utf-8"))

        hooks = settings["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "UserPromptSubmit", "PostToolUse", "PreToolUse"})
        for event in ("SessionStart", "UserPromptSubmit"):
            self.assertEqual(hooks[event][0]["hooks"][0]["command"], PRODUCTION_COMMAND)
        self.assertEqual(hooks["PostToolUse"][0]["matcher"], "Read")
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Write|Edit|NotebookEdit")
        self.assertEqual(hooks["PostToolUse"][0]["hooks"][0]["command"], PRODUCTION_COMMAND)
        self.assertEqual(hooks["PreToolUse"][0]["hooks"][0]["command"], PRODUCTION_COMMAND)

        deny = settings["permissions"]["deny"]
        for entry in ("Bash(sudo:*)", "Edit(./**/.env)", "Edit(./**/*.pem)"):
            self.assertIn(entry, deny)
        self.assertFalse(any(entry.startswith("Write(") for entry in deny))
        self.assertFalse(any(".example" in entry for entry in deny))

    def test_render_is_deterministic(self):
        first, second = self.root / "a.json", self.root / "b.json"
        self.assertEqual(self.render("--out", str(first)).returncode, 0)
        self.assertEqual(self.render("--out", str(second)).returncode, 0)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    # 9 ---------------------------------------------------------------------------------------
    def test_artifacts_have_no_fixed_machine_path(self):
        for path in (HARNESS, REPO_SETTINGS):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("/home/", text)
                self.assertNotIn("/mnt/", text)

    def test_repository_settings_is_the_rendered_production_harness(self):
        """The committed settings.json must be reproducible from the installed forcado.yaml."""
        self.assertTrue(REPO_SETTINGS.is_file())
        settings = json.loads(REPO_SETTINGS.read_text(encoding="utf-8"))
        self.assertIn("Bash(sudo:*)", settings["permissions"]["deny"])
        self.assertIn("Edit(./**/.env)", settings["permissions"]["deny"])
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"], PRODUCTION_COMMAND)


class YamlSubsetTest(unittest.TestCase):
    def test_harness_fixture_parses(self):
        sys.path.insert(0, str(ROOT / "security"))
        try:
            import gate
        finally:
            sys.path.pop(0)
        document = gate.yaml_loads(textwrap.dedent(HARNESS_POLICY))
        self.assertEqual(document["versao"], 1)
        self.assertEqual([item["id"] for item in document["itens"]], ["F03", "F05", "F07"])
        self.assertEqual(document["itens"][1]["padrao"], ["**/.env", "**/*.pem"])
        self.assertEqual(document["itens"][2]["alvo"], "Write|Edit|NotebookEdit")


if __name__ == "__main__":
    unittest.main()
