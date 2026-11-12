"""Tests for the ACP-shaped agent layer (item 71).

One adapter per harness sits behind the same `initialize`/`session`/`prompt`/`permission`/`update`
surface; the window profile chooses it, the conference worker never builds a harness command line,
and both adapters ask the same central policy engine.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import agents  # noqa: E402
import conference  # noqa: E402
import conference_worker  # noqa: E402
import lib  # noqa: E402
import policy  # noqa: E402


class AgentAdapterTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "README.md").write_text("readme\n", encoding="utf-8")
        (self.repo / "BACKLOG.md").write_text("backlog\n", encoding="utf-8")
        self.state = self.root / "state"
        self.state.mkdir()
        self.folder = self.root / "conference"
        self.cfg = self.root / "config.yaml"
        self.cfg.write_text(
            "agent: claude\n"
            "profiles:\n"
            "  claude: claude\n"
            "  oc:\n"
            "    harness: opencode\n"
            "    command: opencode\n",
            encoding="utf-8")
        env = mock.patch.dict(os.environ, {
            "AT_WS_CONFIG": str(self.cfg),
            "WS_STATE": str(self.state),
            "WS_CONFERENCE": str(self.folder),
            "AT_HARNESS_DIR": str(self.root / "no-harness"),
        })
        env.start()
        self.addCleanup(env.stop)

    # --- interface and selection ---------------------------------------------------------------
    def test_both_adapters_expose_the_same_acp_shaped_surface(self):
        for name, harness in (("claude", "claude"), ("oc", "opencode")):
            with self.subTest(profile=name):
                adapter = lib.profile_adapter(name)
                self.assertEqual(adapter.harness, harness)
                info = adapter.initialize()
                self.assertEqual(info["protocol"], agents.PROTOCOL)
                self.assertEqual(info["harness"], harness)
                for method in ("initialize", "session", "prompt", "permission", "update"):
                    self.assertTrue(callable(getattr(adapter, method)))
                self.assertIn("stream", info)

    def test_the_profile_harness_selects_the_adapter(self):
        self.assertIsInstance(lib.profile_adapter("claude"), agents.ClaudeAdapter)
        self.assertIsInstance(lib.profile_adapter("oc"), agents.OpenCodeAdapter)
        self.assertIsInstance(lib.agent_adapter("oc"), agents.OpenCodeAdapter)
        self.assertIsInstance(lib.adapter_for("oc"), agents.OpenCodeAdapter)
        self.assertEqual(lib.profile_adapter("oc").initialize()["stream"], "json")
        self.assertEqual(lib.profile_adapter("claude").initialize()["stream"], "stream-json")

    def test_session_and_prompt_are_the_adapter_contract(self):
        adapter = lib.profile_adapter("oc")
        session = adapter.session("oc", self.repo)
        self.assertEqual(session.profile, "oc")
        self.assertEqual(session.harness, "opencode")
        self.assertEqual(session.cwd, str(self.repo))
        run = adapter.prompt(session, "oi")
        self.assertEqual(run.stdin, "oi")
        self.assertEqual(run.cwd, str(self.repo))
        self.assertEqual(run.command, conference_worker.answer_command(profile="oc"))
        # The short form does not require building a session by hand.
        self.assertEqual(adapter.prompt("oi").command, run.command)

    # --- no harness flag leaks ------------------------------------------------------------------
    def test_the_command_lines_do_not_mix_harness_flags(self):
        claude = conference_worker.answer_command(profile="claude")
        self.assertIn("--output-format stream-json", claude)
        self.assertIn("--allowedTools", claude)
        self.assertNotIn("OPENCODE_CONFIG", claude)
        self.assertNotIn("run --format json", claude)

        opencode = conference_worker.answer_command(profile="oc")
        self.assertIn("run --format json", opencode)
        for flag in ("-p ", "--output-format", "--settings", "--permission-mode",
                     "--append-system-prompt", "--allowedTools", "--disallowedTools",
                     "ENABLE_TOOL_SEARCH"):
            with self.subTest(flag=flag):
                self.assertNotIn(flag, opencode)

        # An OpenCode adapter asked for a Claude profile still runs OpenCode, not Claude.
        cross = agents.adapter("opencode", "claude").prompt("x").command
        self.assertIn("opencode run --format json", cross)
        self.assertNotIn("--settings", cross)
        self.assertNotIn("--allowedTools", cross)

    def test_the_conference_worker_has_no_headless_command_assembly(self):
        source = (SRC / "conference_worker.py").read_text(encoding="utf-8")
        for fragment in ("--output-format stream-json", "run --format json --archived",
                         "OPENCODE_CONFIG=", "--append-system-prompt", "ENABLE_TOOL_SEARCH"):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)

    # --- the lifecycle contract (item 124) ------------------------------------------------------
    def test_both_adapters_declare_the_lifecycle_contract(self):
        for name, source in (("claude", agents.SESSION_ID_WORKSPACE),
                             ("oc", agents.SESSION_ID_AGENT)):
            with self.subTest(profile=name):
                adapter = lib.profile_adapter(name)
                for method in agents.LIFECYCLE:
                    self.assertTrue(callable(getattr(adapter, method)), method)
                self.assertIn(adapter.session_id_source, agents.SESSION_ID_SOURCES)
                self.assertEqual(adapter.session_id_source, source)
                info = adapter.initialize()
                self.assertEqual(info["lifecycle"], list(agents.LIFECYCLE))
                self.assertEqual(info["session_id_source"], source)

    def test_claude_declares_the_workspace_session_id_and_its_arguments(self):
        adapter = agents.ClaudeAdapter("claude")
        self.assertEqual(adapter.session_id_source, agents.SESSION_ID_WORKSPACE)
        self.assertTrue(adapter.records_permission_mode)
        self.assertEqual(adapter.fresh_args("abc"), ["--session-id", "abc"])
        self.assertEqual(adapter.resume_args("abc"), ["--resume", "abc"])
        self.assertEqual(adapter.continue_args(), ["--continue"])

    def test_opencode_declares_the_agent_session_id_and_a_declared_fresh_tab(self):
        adapter = agents.OpenCodeAdapter("oc")
        self.assertEqual(adapter.session_id_source, agents.SESSION_ID_AGENT)
        self.assertFalse(adapter.records_permission_mode)
        # A tab with no recorded id: fresh session by default, never an implicit `--continue`.
        self.assertEqual(adapter.fresh_args("abc"), [])
        self.assertEqual(adapter.resume_args("abc"), ["--session", "abc"])
        self.assertEqual(adapter.continue_args(), ["--continue"])

    def test_claude_prepare_writes_the_generated_settings(self):
        adapter = lib.profile_adapter("claude")
        path = adapter.prepare("claude")
        self.assertEqual(path, str(self.state / "settings" / "claude.json"))
        self.assertTrue(Path(path).is_file())

    def test_opencode_prepare_regenerates_the_plugin_and_the_generated_config(self):
        adapter = lib.profile_adapter("oc")
        path = adapter.prepare("oc")
        plugin = self.state / "settings" / "opencode-plugin-oc.js"
        self.assertEqual(plugin.read_text(encoding="utf-8"),
                         (ROOT / "security" / "opencode-policy.js").read_text(encoding="utf-8"))
        self.assertEqual(path, str(self.state / "settings" / "opencode-oc.json"))
        config = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(config["plugin"], [plugin.as_uri()])
        # `workspace restart` runs prepare again: a deleted plugin comes back from the source.
        plugin.unlink()
        self.assertEqual(adapter.prepare("oc"), path)
        self.assertTrue(plugin.is_file())

    def test_opencode_prepare_merges_the_declared_config(self):
        declared = self.root / "declared.json"
        declared.write_text(json.dumps({
            "model": "deepseek/deepseek-flash",
            "plugin": ["some-declared-plugin"],
        }), encoding="utf-8")
        self.cfg.write_text(
            "agent: oc\n"
            "profiles:\n"
            "  claude: claude\n"
            "  oc:\n"
            "    harness: opencode\n"
            "    command: opencode\n"
            f"    config: {declared}\n",
            encoding="utf-8")
        path = lib.profile_adapter("oc").prepare("oc")
        config = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(config["model"], "deepseek/deepseek-flash")
        plugin = self.state / "settings" / "opencode-plugin-oc.js"
        self.assertEqual(config["plugin"], ["some-declared-plugin", plugin.as_uri()])
        # The declared file is never rewritten.
        self.assertEqual(json.loads(declared.read_text(encoding="utf-8")),
                         {"model": "deepseek/deepseek-flash",
                          "plugin": ["some-declared-plugin"]})

    def test_opencode_prepare_keeps_a_declared_config_it_cannot_read(self):
        declared = self.root / "declared.jsonc"
        declared.write_text('{"model": "x"} // comment\n', encoding="utf-8")
        self.cfg.write_text(
            "agent: oc\n"
            "profiles:\n"
            "  claude: claude\n"
            "  oc:\n"
            "    harness: opencode\n"
            "    command: opencode\n"
            f"    config: {declared}\n",
            encoding="utf-8")
        adapter = lib.profile_adapter("oc")
        self.assertEqual(adapter.prepare("oc"), str(declared))
        self.assertEqual(adapter.launch("oc").endswith("opencode"), True)
        self.assertIn(f"OPENCODE_CONFIG={declared}", adapter.launch("oc"))
        # The plugin copy is still regenerated, so a restart never loses it.
        self.assertTrue((self.state / "settings" / "opencode-plugin-oc.js").is_file())

    # --- one central policy ---------------------------------------------------------------------
    def _events(self):
        return [
            {"tool_name": "Read", "tool_input": {"file_path": str(self.repo / "README.md")}},
            {"tool_name": "Read", "tool_input": {"file_path": str(self.repo / "src" / "x.py")}},
            {"tool_name": "Write", "tool_input": {"file_path": str(self.repo / "README.md")}},
            {"tool_name": "Write", "tool_input": {"file_path": str(self.repo / "src" / "x.py")}},
            {"tool_name": "Grep", "tool_input": {"pattern": "x"}},
            {"tool_name": "Bash", "tool_input": {"command": "git status"}},
            {"tool_name": "Bash", "tool_input": {"command": "sudo ls"}},
            {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}},
            {"tool": "read", "args": {"file_path": str(self.repo / "BACKLOG.md")}},
        ]

    def test_the_same_battery_gives_the_same_decision_in_both_adapters(self):
        with mock.patch.dict(os.environ, {
            "WS_SESSION_ROLE": "repo",
            "WS_SESSION_ROOT": str(self.repo),
            "WS_STATE": str(self.state),
        }):
            for event in self._events():
                with self.subTest(event=event):
                    claude = agents.ClaudeAdapter("claude").permission(event)
                    opencode = agents.OpenCodeAdapter("oc").permission(event)
                    expected = policy.check_event(agents.normalize_event(event))
                    for got in (claude, opencode):
                        self.assertEqual(got["decision"], expected["decision"])
                        self.assertEqual(got["capability"], expected["capability"])
                        self.assertEqual(got["reason"], expected["reason"])

    def test_the_opencode_permissions_derive_from_the_conference_scope(self):
        config_path = lib.profile_adapter("oc").session("oc", self.repo).settings
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        permissions = config["permissions"]
        readme, backlog = "README.md", "BACKLOG.md"
        for name in conference.STRUCTURAL_FILES:
            self.assertIn({"action": "read", "resource": name, "effect": "allow"}, permissions)
        self.assertNotIn({"action": "read", "resource": "HARNESS.md", "effect": "allow"}, permissions)
        self.assertIn({"action": "edit", "resource": readme, "effect": "ask"}, permissions)
        self.assertIn({"action": "edit", "resource": backlog, "effect": "allow"}, permissions)
        for name in ("SECURITY.md", "DESIGN.md", "ARCH.md"):
            self.assertNotIn({"action": "edit", "resource": name, "effect": "allow"}, permissions)
            self.assertNotIn({"action": "edit", "resource": name, "effect": "ask"}, permissions)
        self.assertIn({"action": "*", "resource": "*", "effect": "deny"}, permissions)

    # --- stream as an adapter concern -----------------------------------------------------------
    def test_each_adapter_reads_its_own_stream(self):
        claude_lines = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read",
                 "input": {"file_path": str(self.repo / "README.md")}}]}},
            {"type": "result", "subtype": "success", "result": "resposta final"},
        ]
        opencode_lines = [
            {"type": "tool_use", "part": {"tool": "read", "state": {
                "input": {"path": str(self.repo / "BACKLOG.md")}}}},
            {"type": "text", "part": {"text": "resposta final"}},
        ]
        claude_status = []
        opencode_status = []
        claude = agents.read_stream(
            io.StringIO("\n".join(json.dumps(line) for line in claude_lines)),
            self.repo, claude_status.append, profile="claude")
        opencode = agents.read_stream(
            io.StringIO("\n".join(json.dumps(line) for line in opencode_lines)),
            self.repo, opencode_status.append, profile="oc")
        self.assertEqual(claude, "resposta final")
        self.assertEqual(opencode, "resposta final")
        self.assertEqual(claude_status, [lib.profile_adapter("claude").status_label(
            self.repo, "Read", {"file_path": str(self.repo / "README.md")})])
        self.assertEqual(opencode_status, [lib.profile_adapter("oc").status_label(
            self.repo, {"tool": "read", "state": {
                "input": {"path": str(self.repo / "BACKLOG.md")}}})])

    def test_the_worker_compatibility_stream_helper_delegates(self):
        lines = [{"type": "result", "subtype": "success", "result": "pronto"}]
        out = conference_worker._stream_answer(
            io.StringIO("\n".join(json.dumps(line) for line in lines)), self.repo)
        self.assertEqual(out, "pronto")

    def test_the_worker_executes_each_profile_and_reads_its_own_stream(self):
        claude = self.root / "fake-claude"
        claude.write_text(
            "#!/bin/sh\ncat >/dev/null\n"
            "printf '%s\\n' '" + json.dumps(
                {"type": "result", "subtype": "success",
                 "result": "answer from claude"}) + "'\n", encoding="utf-8")
        opencode = self.root / "fake-opencode"
        opencode.write_text(
            "#!/bin/sh\ncat >/dev/null\n"
            "printf '%s\\n' '" + json.dumps(
                {"type": "text", "part": {"text": "answer from opencode"}}) + "'\n",
            encoding="utf-8")
        for path in (claude, opencode):
            path.chmod(0o755)
        self.cfg.write_text(
            "agent: fake-claude\n"
            "profiles:\n"
            "  claude: claude\n"
            f"  fake-claude: {claude}\n"
            "  oc:\n"
            "    harness: opencode\n"
            f"    command: {opencode}\n",
            encoding="utf-8")

        for profile, expected in (("fake-claude", "answer from claude"),
                                  ("oc", "answer from opencode")):
            with self.subTest(profile=profile):
                name, adapter = conference_worker._agent(profile)
                run = adapter.prompt(adapter.session(name, str(self.repo)), "oi")
                out, err, code = conference_worker._execute(
                    run.command, str(self.repo), run.stdin, profile=name, adapter=adapter)
                self.assertEqual(code, 0, err)
                self.assertEqual(out, expected)

    # --- the conference uses the profile adapter ------------------------------------------------
    def test_the_conference_answer_runs_the_window_profile_adapter(self):
        self.assertEqual(conference.start([str(self.repo)], profile="oc"), 0)
        created = conference.read_active()["created"]
        calls = []

        def execute(command, repo, prompt):
            calls.append((command, prompt))
            return "answer", "", 0

        ok = conference_worker.answer(str(self.repo), "pergunta", "msg-1", created,
                                      execute=execute, profile="oc")
        self.assertTrue(ok)
        self.assertEqual(calls[0][1], "pergunta")
        self.assertIn("run --format json", calls[0][0])
        self.assertNotIn("--allowedTools", calls[0][0])


if __name__ == "__main__":
    unittest.main()
