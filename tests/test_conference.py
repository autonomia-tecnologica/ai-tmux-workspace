"""Tests for conference mode: state, log, isolated workers and the conversation screen.

Every test points WS_STATE (and, for the config cases, AT_WS_CONFIG) at a temporary folder, so
the real ~/.cache/workspace is never touched. Launching and running the isolated Claude is
injected: no subprocess and no tmux needed.
"""
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import conference  # noqa: E402
import conference_worker  # noqa: E402
import i18n  # noqa: E402


class ConferenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.a = self.tmp / "repo-a"
        self.b = self.tmp / "repo-b"
        self.a.mkdir()
        self.b.mkdir()
        self.repos = [str(self.a.resolve()), str(self.b.resolve())]
        env = mock.patch.dict(os.environ, {"WS_STATE": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)

    def fake_launch(self, calls):
        def launch(repo, prompt, message_id, created, others=None):
            calls.append((repo, prompt, message_id, created, others))
            return True
        return launch

    def test_start_with_explicit_repos_writes_state(self):
        self.assertEqual(conference.start([str(self.a), str(self.b)]), 0)
        active = conference.read_active()
        self.assertEqual(active["repos"], self.repos)
        self.assertEqual(active["awaiting"], {})
        self.assertTrue(conference.log_path().exists())
        self.assertEqual(conference.read_log(), [])

    def test_start_replaces_an_active_conference_that_never_received_a_message(self):
        self.assertEqual(conference.start([str(self.a)]), 0)
        self.assertEqual(conference.start([str(self.b)]), 0)
        self.assertEqual(conference.read_active()["repos"], [self.repos[1]])
        self.assertEqual(conference.read_log(), [])

    def test_start_twice_is_refused_when_the_active_conference_has_messages(self):
        self.assertEqual(conference.start([str(self.a)]), 0)
        conference.say("hi", launch=self.fake_launch([]))
        self.assertEqual(conference.start([str(self.b)]), 2)
        self.assertEqual(conference.read_active()["repos"], [self.repos[0]])
        self.assertEqual(len(conference.read_log()), 1)

    def test_start_without_repos_uses_the_window_repo_and_its_siblings(self):
        cfg = self.tmp / "config.yaml"
        cfg.write_text(
            "groups:\n"
            "  Pair:\n"
            f"    - path: {self.a}\n"
            f"      siblings: [{self.b}]\n"
            f"    - {self.b}\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"AT_WS_CONFIG": str(cfg)}), \
                mock.patch("lib.current_repo", return_value=str(self.a)):
            self.assertEqual(conference.start(), 0)
        self.assertEqual(conference.read_active()["repos"], [self.repos[0], self.repos[1]])

    def test_start_does_not_treat_the_rest_of_the_group_as_siblings(self):
        cfg = self.tmp / "config.yaml"
        cfg.write_text(
            "groups:\n"
            "  Pair:\n"
            f"    - {self.a}\n"
            f"    - {self.b}\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"AT_WS_CONFIG": str(cfg)}), \
                mock.patch("lib.current_repo", return_value=str(self.a)):
            self.assertEqual(conference.start(), 2)
        self.assertEqual(conference.read_active(), {})

    def test_start_uses_the_repo_passed_by_the_options_screen(self):
        cfg = self.tmp / "config.yaml"
        cfg.write_text(
            "groups:\n"
            "  Pair:\n"
            f"    - path: {self.a}\n"
            f"      siblings: [{self.b}]\n"
            f"    - {self.b}\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"AT_WS_CONFIG": str(cfg)}), \
                mock.patch("lib.current_repo", side_effect=AssertionError("must not read the tmux window")):
            self.assertEqual(conference.start(repo=str(self.a)), 0)
        self.assertEqual(conference.read_active()["repos"], [self.repos[0], self.repos[1]])

    def test_start_without_repos_and_without_siblings_is_refused(self):
        outside = self.tmp / "home"
        outside.mkdir()
        cfg = self.tmp / "config.yaml"
        cfg.write_text(
            "groups:\n"
            "  Other:\n"
            f"    - {self.a}\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"AT_WS_CONFIG": str(cfg)}), \
                mock.patch("lib.current_repo", return_value=str(outside)):
            self.assertEqual(conference.start(), 2)
        self.assertEqual(conference.read_active(), {})

    def test_start_rejects_a_missing_repo(self):
        self.assertEqual(conference.start([str(self.tmp / "nope")]), 2)
        self.assertEqual(conference.read_active(), {})

    def test_say_appends_the_message_and_starts_one_claude_per_repo(self):
        conference.start([str(self.a), str(self.b)])
        calls = []
        self.assertEqual(conference.say("my message", launch=self.fake_launch(calls)), 0)

        log = conference.read_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["sender"], "rafael")
        self.assertEqual(log[0]["text"], "my message")
        message_id = log[0]["id"]
        self.assertIsInstance(message_id, str)
        self.assertEqual(conference.read_active()["awaiting"],
                         {self.repos[0]: message_id, self.repos[1]: message_id})
        self.assertEqual([c[0] for c in calls], self.repos)
        self.assertTrue(all(c[2] == message_id for c in calls))
        self.assertTrue(all(str(c[3]) == str(conference.read_active()["created"])
                            for c in calls))
        self.assertTrue(all(c[4] == self.repos for c in calls))   # the siblings each may read
        self.assertEqual(conference.read_active()["status"],
                         {self.repos[0]: i18n.t("conference.status.thinking"),
                          self.repos[1]: i18n.t("conference.status.thinking")})
        participants = ", ".join(f"{conference._sender(p)} ({p})" for p in self.repos)
        expected = dict(text="my message", no_answer=conference.NO_ANSWER,
                        participants=participants, max_files=conference.MAX_FILES_READ,
                        max_steps=conference.MAX_TOOL_STEPS)
        self.assertEqual(calls[0][1], i18n.t(
            "conference.prompt", you=conference._sender(self.repos[0]), **expected))
        self.assertEqual(calls[1][1], i18n.t(
            "conference.prompt", you=conference._sender(self.repos[1]), **expected))
        self.assertIn("my message", calls[0][1])
        self.assertIn(str(conference.MAX_FILES_READ), calls[0][1])
        self.assertIn(str(conference.MAX_TOOL_STEPS), calls[0][1])
        self.assertIn("BACKLOG.md", calls[0][1])              # the only allowed write
        for repo in self.repos:                            # every participant is named
            self.assertIn(f"{conference._sender(repo)} ({repo})", calls[0][1])
        self.assertIn(conference_worker.NO_ANSWER, calls[0][1])
        self.assertNotIn(str(conference.log_path()), calls[0][1])   # no read outside the repo

    def test_the_prompt_carries_only_the_message_being_sent(self):
        conference.start([str(self.a)])
        calls = []
        conference.say("first", launch=self.fake_launch([]))
        conference.say("second", launch=self.fake_launch(calls))
        self.assertIn("second", calls[0][1])
        self.assertNotIn("first", calls[0][1])             # no history in the prompt

    def test_say_without_an_active_conference_is_refused(self):
        calls = []
        self.assertEqual(conference.say("hi", launch=self.fake_launch(calls)), 2)
        self.assertEqual(calls, [])
        self.assertEqual(conference.read_log(), [])

    def test_say_drops_from_awaiting_the_repos_that_did_not_start(self):
        conference.start([str(self.a), str(self.b)])
        calls = []
        first, second = self.repos

        def launch(repo, prompt, message_id, created, others=None):
            calls.append(repo)
            return repo == second     # the first repo's Claude fails to start

        self.assertEqual(conference.say("hi", launch=launch), 1)
        self.assertEqual(calls, [first, second])
        message_id = conference.read_log()[0]["id"]
        self.assertEqual(conference.read_active()["awaiting"], {second: message_id})
        self.assertEqual(conference.read_active()["status"],
                         {second: i18n.t("conference.status.thinking")})

    def test_stop_closes_the_conference_and_keeps_the_log(self):
        conference.start([str(self.a)])
        conference.say("save", launch=self.fake_launch([]))
        self.assertEqual(conference.stop(), 0)
        self.assertEqual(conference.read_active(), {})
        self.assertEqual(len(conference.read_log()), 1)
        self.assertEqual(conference.stop(), 2)

    def test_a_new_conference_starts_empty_and_the_previous_log_does_not_come_back(self):
        conference.start([str(self.a)])
        conference.say("first", launch=self.fake_launch([]))
        self.assertEqual(conference.stop(), 0)

        conference.start([str(self.b)])
        self.assertEqual(conference.read_log(), [])     # no persistence on screen
        archives = list((self.tmp / "conference").glob("log-*.jsonl"))
        self.assertEqual(len(archives), 1)              # the old log is only a file
        self.assertIn("first", archives[0].read_text(encoding="utf-8"))

    def test_first_paragraph_keeps_only_the_first_block(self):
        self.assertEqual(conference.first_paragraph("one.\n\n two.\n\nthree"), "one.")
        self.assertEqual(conference.first_paragraph("one paragraph\nwith a line break"),
                         "one paragraph\nwith a line break")
        self.assertEqual(conference.first_paragraph("\n \n"), "")

    def test_format_log_has_sender_and_time(self):
        text = conference.format_log([
            {"ts": 0, "sender": "rafael", "text": "question"},
            {"ts": 0, "sender": "repo-a", "text": "resposta"},
        ])
        self.assertIn("Rafael", text)
        self.assertIn("repo-a", text)
        self.assertIn("question", text)
        self.assertIn("resposta", text)
        self.assertEqual(conference.format_log([]), i18n.t("conference.log_empty"))


class WorkerTest(unittest.TestCase):
    """The isolated Claude run: one per participant, read-only and bounded by the budget hook."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.a = self.tmp / "repo-a"
        self.b = self.tmp / "repo-b"
        self.a.mkdir()
        self.b.mkdir()
        self.repos = [str(self.a.resolve()), str(self.b.resolve())]
        self.cfg = self.tmp / "config.yaml"
        self.cfg.write_text("profiles:\n  claude: claude\nagent: claude\n", encoding="utf-8")
        env = mock.patch.dict(os.environ, {"WS_STATE": str(self.tmp), "AT_WS_CONFIG": str(self.cfg),
                                           "AT_HARNESS_DIR": str(self.tmp / "no-harness")})
        env.start()
        self.addCleanup(env.stop)

    def launch(self):
        calls = []

        def launch(repo, prompt, message_id, created, others=None):
            calls.append((repo, message_id, created))
            return True
        return calls, launch

    def say(self, text="oi"):
        calls, launch = self.launch()
        conference.say(text, launch=launch)
        repo, message_id, created = calls[-1]
        return repo, message_id, created

    def test_answer_appends_the_first_paragraph_and_clears_the_waiting_mark(self):
        conference.start(self.repos)
        _, message_id, created = self.say()

        ok = conference_worker.answer(
            self.repos[0], "prompt", message_id, created,
            execute=lambda cmd, repo, prompt: ("First.\n\nSecond.", "", 0))

        self.assertTrue(ok)
        log = conference.read_log()
        self.assertEqual(len(log), 2)                 # Rafael's message + repo-a's answer
        self.assertEqual(log[1]["sender"], "repo-a")
        self.assertEqual(log[1]["repo"], self.repos[0])
        self.assertEqual(log[1]["reply_to"], message_id)
        self.assertEqual(log[1]["text"], "First.")
        self.assertEqual(conference.read_active()["awaiting"], {self.repos[1]: message_id})
        self.assertEqual(conference.read_active()["status"],
                         {self.repos[1]: i18n.t("conference.status.thinking")})

    def test_a_failed_run_writes_a_visible_note_and_keeps_the_details_out_of_the_log(self):
        conference.start(self.repos)
        _, message_id, created = self.say()

        ok = conference_worker.answer(
            self.repos[0], "prompt", message_id, created,
            execute=lambda cmd, repo, prompt: ("", "endpoint is down", 1))

        self.assertFalse(ok)
        log = conference.read_log()
        self.assertEqual(log[1]["text"], i18n.t("conference.answer_failed"))
        self.assertNotIn("endpoint is down", log[1]["text"])
        errors = (self.tmp / "conference" / "errors.log").read_text(encoding="utf-8")
        self.assertIn("endpoint is down", errors)
        self.assertEqual(conference.read_active()["awaiting"], {self.repos[1]: message_id})
        self.assertEqual(conference.read_active()["status"],
                         {self.repos[1]: i18n.t("conference.status.thinking")})

    def test_a_message_that_is_not_this_repo_gets_no_log_entry_and_no_waiting_mark(self):
        conference.start(self.repos)
        _, message_id, created = self.say("message from another repo")

        ok = conference_worker.answer(
            self.repos[0], "prompt", message_id, created,
            execute=lambda cmd, repo, prompt: (conference_worker.NO_ANSWER, "", 0))

        self.assertFalse(ok)
        self.assertEqual(len(conference.read_log()), 1)   # only Rafael's message
        self.assertEqual(conference.read_active()["awaiting"], {self.repos[1]: message_id})
        self.assertEqual(conference.read_active()["status"],
                         {self.repos[1]: i18n.t("conference.status.thinking")})
        self.assertFalse((self.tmp / "conference" / "errors.log").exists())

    def test_an_answer_from_a_previous_conference_is_dropped(self):
        conference.start([self.repos[0]])
        _, message_id, created = self.say()
        conference.stop()
        conference.start([self.repos[1]])            # fresh log, new `created`

        conference_worker.answer(
            self.repos[0], "prompt", message_id, created,
            execute=lambda cmd, repo, prompt: ("tarde demais", "", 0))

        self.assertEqual(conference.read_log(), [])

    def test_an_answer_never_clears_the_waiting_mark_of_a_newer_message(self):
        conference.start([self.repos[0]])
        _, first_id, created = self.say("primeira")
        _, second_id, _ = self.say("segunda")

        conference_worker.answer(
            self.repos[0], "prompt", first_id, created,
            execute=lambda cmd, repo, prompt: ("resposta da primeira", "", 0))

        self.assertEqual(conference.read_log()[-1]["text"], "resposta da primeira")
        self.assertEqual(conference.read_active()["awaiting"], {self.repos[0]: second_id})
        self.assertEqual(conference.read_active()["status"],
                         {self.repos[0]: i18n.t("conference.status.thinking")})

    def test_a_stale_worker_does_not_overwrite_the_status_of_a_newer_message(self):
        conference.start([self.repos[0]])
        _, first_id, created = self.say("primeira")
        _, second_id, _ = self.say("segunda")

        conference_worker._set_status(self.repos[0], first_id, created, "lendo coisa velha")

        self.assertEqual(conference.read_active()["status"],
                         {self.repos[0]: i18n.t("conference.status.thinking")})
        self.assertEqual(conference.read_active()["awaiting"], {self.repos[0]: second_id})

    def test_the_stream_reports_tool_steps_and_returns_the_final_text(self):
        lines = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read",
                 "input": {"file_path": str(Path(self.repos[0]) / "docs" / "x.md")}}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Grep", "input": {"pattern": "conflito"}}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Glob", "input": {"pattern": "**/*.md"}}]}},
            {"type": "result", "subtype": "success", "result": "resposta final"},
        ]
        statuses = []
        out = conference_worker._stream_answer(
            io.StringIO("\n".join(json.dumps(line) for line in lines)),
            self.repos[0], statuses.append)

        self.assertEqual(out, "resposta final")
        self.assertEqual(statuses, [
            i18n.t("conference.status.reading", path="docs/x.md"),
            i18n.t("conference.status.searching", pattern="conflito"),
            i18n.t("conference.status.looking", pattern="**/*.md"),
        ])

    def test_an_error_result_is_not_an_answer(self):
        lines = [{"type": "result", "subtype": "error", "is_error": True,
                  "result": "deu ruim"}]
        out = conference_worker._stream_answer(
            io.StringIO("\n".join(json.dumps(line) for line in lines)), self.repos[0])
        self.assertEqual(out, "")

    def test_the_run_is_headless_isolated_restricted_and_budgeted(self):
        cmd = conference_worker.answer_command([self.repos[1]])
        self.assertIn("-p ", cmd + " ")
        self.assertIn("--add-dir", cmd)              # siblings are readable for cross-validation
        self.assertIn(self.repos[1], cmd)
        self.assertIn("--output-format stream-json", cmd)
        self.assertIn("--verbose", cmd)
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--permission-mode default", cmd)
        self.assertIn("--append-system-prompt", cmd)
        self.assertIn(conference_worker.NO_ANSWER, cmd)
        self.assertIn("--max-turns", cmd)            # last-resort exit above the hook budget
        argv = shlex.split(cmd)
        self.assertIn("--settings", argv)            # the generated settings carries the profile env
        settings_path = Path(argv[argv.index("--settings") + 1])
        self.assertEqual(settings_path, self.tmp / "settings" / "claude.json")
        self.assertTrue(settings_path.is_file())
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Grep,Glob,Edit")
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        for tool in ("Bash", "Task", "Write", "NotebookEdit", "MultiEdit", "WebFetch", "WebSearch"):
            self.assertIn(tool, denied)              # Edit is the only write, path-checked by the hook
        self.assertNotIn("Edit", denied)
        self.assertNotIn("--resume", cmd)
        self.assertNotIn("--continue", cmd)

    def test_the_installed_harness_is_readable_so_its_write_gate_can_be_satisfied(self):
        harness = self.tmp / "harness"
        harness.mkdir()
        with mock.patch.dict(os.environ, {"AT_HARNESS_DIR": str(harness)}):
            cmd = conference_worker.answer_command([self.repos[1]])
        self.assertIn(f"--add-dir {shlex.quote(str(harness))}", cmd)

    def test_mapping_profile_env_and_model_reach_the_conference_run(self):
        """A mapping profile keeps its URL, model and apiKeyHelper in the generated settings; without
        `--settings` the worker would fall back to the person's Anthropic settings and have no key."""
        self.cfg.write_text(
            "agent: deepseek\n"
            "profiles:\n"
            "  deepseek:\n"
            "    command: claude\n"
            "    settings:\n"
            "      env:\n"
            "        ANTHROPIC_BASE_URL: https://example.invalid/anthropic\n"
            "        ANTHROPIC_MODEL: deepseek-flash\n"
            "      model: deepseek-flash\n"
            "      apiKeyHelper: \"printf from-vault\"\n"
            "      permissions:\n"
            "        defaultMode: bypassPermissions\n",
            encoding="utf-8")
        cmd = conference_worker.answer_command([self.repos[1]])
        argv = shlex.split(cmd)
        self.assertIn("--settings", argv)
        settings_path = Path(argv[argv.index("--settings") + 1])
        self.assertEqual(settings_path, self.tmp / "settings" / "deepseek.json")
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "https://example.invalid/anthropic")
        self.assertEqual(settings["env"]["ANTHROPIC_MODEL"], "deepseek-flash")
        self.assertEqual(settings["model"], "deepseek-flash")
        self.assertEqual(settings["apiKeyHelper"], "printf from-vault")

        # the harness base still comes along; the command keeps the write controls
        self.assertIn("Bash(sudo:*)", settings["permissions"]["deny"])
        self.assertIn("--permission-mode default", cmd)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Grep,Glob,Edit")
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        for tool in ("Bash", "Write", "NotebookEdit"):
            self.assertIn(tool, denied)
        self.assertNotIn("Edit", denied)


BUDGET_HOOK = SRC / "conference_budget.py"


class BudgetHookTest(unittest.TestCase):
    """The PreToolUse hook that turns the conference budget into a mechanism, not a prompt request."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "budget.json"

    def hook(self, tool="Read", file_path=None, max_steps=20, max_files=12, with_state=True,
             tool_use_id=None, repo=None):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}}
        if file_path is not None:
            payload["tool_input"]["file_path"] = file_path
        if tool_use_id is not None:
            payload["tool_use_id"] = tool_use_id
        env = dict(os.environ, WS_CONFERENCE_MAX_STEPS=str(max_steps),
                   WS_CONFERENCE_MAX_FILES=str(max_files))
        env.pop("WS_CONFERENCE_REPO", None)
        if repo is not None:
            env["WS_CONFERENCE_REPO"] = str(repo)
        if with_state:
            env["WS_CONFERENCE_BUDGET"] = str(self.state)
        proc = subprocess.run([sys.executable, str(BUDGET_HOOK)], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        return out.get("hookSpecificOutput") or {}

    def test_a_file_counts_once_and_a_new_file_is_denied_past_the_limit(self):
        self.assertEqual(self.hook(file_path="/x/a.md", max_files=2), {})
        self.assertEqual(self.hook(file_path="/x/a.md", max_files=2), {})   # already read
        self.assertEqual(self.hook(file_path="/x/b.md", max_files=2), {})
        denied = self.hook(file_path="/x/c.md", max_files=2)
        self.assertEqual(denied.get("permissionDecision"), "deny")
        self.assertIn(i18n.t("conference.budget.files", max_files=2),
                      denied.get("permissionDecisionReason"))

    def test_the_step_budget_denies_more_tools_and_asks_for_an_answer(self):
        self.assertEqual(self.hook(tool="Grep", max_steps=2), {})
        self.assertEqual(self.hook(tool="Grep", max_steps=2), {})
        denied = self.hook(tool="Glob", max_steps=2)
        self.assertEqual(denied.get("permissionDecision"), "deny")
        self.assertIn(i18n.t("conference.budget.steps", max_steps=2),
                      denied.get("permissionDecisionReason"))

    def test_the_same_event_counted_twice_charges_one_step(self):
        self.assertEqual(self.hook(tool="Grep", max_steps=1, tool_use_id="tool-1"), {})
        self.assertEqual(self.hook(tool="Grep", max_steps=1, tool_use_id="tool-1"), {})  # duplicate
        denied = self.hook(tool="Grep", max_steps=1, tool_use_id="tool-2")   # a real second step
        self.assertEqual(denied.get("permissionDecision"), "deny")

    def test_uncounted_tools_and_a_run_without_state_are_not_blocked(self):
        self.assertEqual(self.hook(tool="Bash"), {})
        self.assertEqual(self.hook(file_path="/x/a.md", with_state=False), {})
        self.assertFalse(self.state.exists())

    def test_a_write_to_the_repos_own_backlog_is_allowed(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        self.assertEqual(self.hook(tool="Edit", file_path=str(repo / "BACKLOG.md"), repo=repo), {})
        self.assertEqual(self.hook(tool="Edit", file_path="BACKLOG.md", repo=repo), {})

    def test_a_write_to_any_other_file_is_denied(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        denied = self.hook(tool="Edit", file_path=str(repo / "src" / "x.py"), repo=repo)
        self.assertEqual(denied.get("permissionDecision"), "deny")
        self.assertIn(i18n.t("conference.write_denied"),
                      denied.get("permissionDecisionReason"))

    def test_a_write_to_a_sibling_backlog_is_denied(self):
        repo, sibling = self.tmp / "repo", self.tmp / "sibling"
        repo.mkdir()
        sibling.mkdir()
        denied = self.hook(tool="Edit", file_path=str(sibling / "BACKLOG.md"), repo=repo)
        self.assertEqual(denied.get("permissionDecision"), "deny")

    def test_the_write_restriction_holds_without_a_budget_state(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        denied = self.hook(tool="Edit", file_path=str(repo / "README.md"), repo=repo,
                           with_state=False)
        self.assertEqual(denied.get("permissionDecision"), "deny")
        self.assertFalse(self.state.exists())

    def test_a_write_counts_as_a_step(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        self.assertEqual(self.hook(tool="Edit", file_path=str(repo / "BACKLOG.md"), repo=repo,
                                   max_steps=1), {})
        denied = self.hook(tool="Edit", file_path=str(repo / "BACKLOG.md"), repo=repo, max_steps=1)
        self.assertEqual(denied.get("permissionDecision"), "deny")
        self.assertIn(i18n.t("conference.budget.steps", max_steps=1),
                      denied.get("permissionDecisionReason"))

    def test_a_broken_state_file_does_not_block_the_run(self):
        self.state.write_text("not json", encoding="utf-8")
        self.assertEqual(self.hook(file_path="/x/a.md", max_files=1), {})
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["files"], ["/x/a.md"])

    def test_the_workspace_hook_applies_the_budget_to_a_conference_run(self):
        payload = {"hook_event_name": "PreToolUse", "session_id": "sess-1",
                   "tool_name": "Read", "tool_input": {"file_path": "/x/a.md"}}
        env = dict(os.environ, WS_STATE=str(self.tmp), WS_CONFERENCE_BUDGET=str(self.state),
                   WS_CONFERENCE_MAX_STEPS="20", WS_CONFERENCE_MAX_FILES="1")

        def run_hook():
            proc = subprocess.run([sys.executable, str(SRC / "hook.py")],
                                  input=json.dumps(payload), capture_output=True, text=True,
                                  env=env, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout) if proc.stdout.strip() else {}

        self.assertEqual(run_hook(), {})                       # first file: allowed
        self.assertEqual(run_hook(), {})                       # same file: not charged again
        payload["tool_input"]["file_path"] = "/x/b.md"
        denied = run_hook()["hookSpecificOutput"]              # second file: over the limit
        self.assertEqual(denied.get("permissionDecision"), "deny")
        self.assertIn(i18n.t("conference.budget.files", max_files=1),
                      denied.get("permissionDecisionReason"))

    def test_the_workspace_hook_ignores_runs_without_a_conference_budget(self):
        payload = {"hook_event_name": "PreToolUse", "session_id": "sess-1",
                   "tool_name": "Read", "tool_input": {"file_path": "/x/a.md"}}
        env = dict(os.environ, WS_STATE=str(self.tmp))
        env.pop("WS_CONFERENCE_BUDGET", None)
        proc = subprocess.run([sys.executable, str(SRC / "hook.py")],
                              input=json.dumps(payload), capture_output=True, text=True,
                              env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_the_workspace_hook_restricts_writes_without_a_budget_file(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        payload = {"hook_event_name": "PreToolUse", "session_id": "sess-1",
                   "tool_name": "Edit", "tool_input": {"file_path": str(repo / "README.md")}}
        env = dict(os.environ, WS_STATE=str(self.tmp), WS_CONFERENCE_REPO=str(repo))
        env.pop("WS_CONFERENCE_BUDGET", None)

        def run():
            proc = subprocess.run([sys.executable, str(SRC / "hook.py")],
                                  input=json.dumps(payload), capture_output=True, text=True,
                                  env=env, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout.strip()

        denied = json.loads(run())["hookSpecificOutput"]
        self.assertEqual(denied.get("permissionDecision"), "deny")
        self.assertIn(i18n.t("conference.write_denied"),
                      denied.get("permissionDecisionReason"))

        payload["tool_input"]["file_path"] = str(repo / "BACKLOG.md")
        self.assertEqual(run(), "")


class FakeViewer:
    """Key script applied in order right after the first frame is collected."""

    keys = []
    last = None

    def __init__(self, collect, interval=2.0, **kwargs):
        self.collect = collect
        self.on_key, self.on_input, self.footer = (kwargs.get("on_key"), kwargs.get("on_input"),
                                                    kwargs.get("footer"))
        self.top, self.stopped, self.frame = 0, False, None
        self.redraw = mock.Mock()
        type(self).last = self

    def _wake(self):
        pass

    def run(self):
        self.frame = self.collect(self)
        for k in type(self).keys:
            if self.on_input and self.on_input(self, k):
                continue
            if self.on_key:
                self.on_key(self, k)
            if self.stopped:
                break


class ConverseTest(unittest.TestCase):
    """The conversation screen: the footer is the message box, no command for Rafael to type."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.a = self.tmp / "repo-a"
        self.b = self.tmp / "repo-b"
        self.a.mkdir()
        self.b.mkdir()
        self.repos = [str(self.a.resolve()), str(self.b.resolve())]
        cfg = self.tmp / "config.yaml"
        cfg.write_text(
            "groups:\n"
            "  Pair:\n"
            f"    - path: {self.a}\n"
            f"      siblings: [{self.b}]\n"
            f"    - {self.b}\n",
            encoding="utf-8",
        )
        env = mock.patch.dict(os.environ, {"WS_STATE": str(self.tmp), "AT_WS_CONFIG": str(cfg)})
        env.start()
        self.addCleanup(env.stop)

    def run_screen(self, keys, repo=None):
        FakeViewer.keys, FakeViewer.last = list(keys), None
        with mock.patch("viewer.Viewer", FakeViewer), \
                mock.patch("conference._launch_answer", return_value=True):
            return conference.converse(repo=repo or str(self.a))

    def test_typing_and_enter_starts_with_the_window_repo_and_siblings_and_sends(self):
        self.assertEqual(self.run_screen(list("my message") + ["\r", "esc"]), 0)
        self.assertEqual(conference.read_active(), {})   # Esc closes the conference with the screen
        log = conference.read_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["sender"], "rafael")
        self.assertEqual(log[0]["text"], "my message")

    def test_esc_ends_the_open_conference_and_keeps_the_log(self):
        conference.start([str(self.a), str(self.b)])
        conference.say("hi", launch=lambda *a, **k: True)
        self.assertEqual(self.run_screen(["esc"], repo=str(self.a)), 0)
        self.assertEqual(conference.read_active(), {})
        self.assertEqual(len(conference.read_log()), 1)

    def test_the_header_shows_what_each_repo_is_doing(self):
        conference.start(self.repos)
        conference.say("hi", launch=lambda *a, **k: True)
        message_id = conference.read_log()[0]["id"]
        created = conference.read_active()["created"]
        conference_worker._set_status(self.repos[0], message_id, created, "lendo docs/x.md")

        self.assertEqual(self.run_screen(["esc"], repo=str(self.a)), 0)
        header = FakeViewer.last.frame[0][0].text
        body = "\n".join(l.text for l in FakeViewer.last.frame[1])
        self.assertIn("repo-a (lendo docs/x.md)", header)
        self.assertIn("repo-b (analisando…)", header)
        self.assertIn(i18n.t("conference.screen.hint"), body)

    def test_esc_on_the_inactive_screen_changes_nothing(self):
        self.assertEqual(self.run_screen(["esc"]), 0)
        self.assertEqual(conference.read_active(), {})
        self.assertEqual(conference.read_log(), [])

    def test_a_closed_conference_does_not_come_back_on_the_next_screen(self):
        self.run_screen(list("oi") + ["\r", "esc"])     # starts, sends and closes
        self.run_screen(["esc"])                         # reopening: nothing from the previous one
        text = "\n".join(l.text for l in FakeViewer.last.frame[1])
        self.assertNotIn("oi", text)
        self.assertIn(i18n.t("conference.not_active"), text)

    def test_j_and_x_are_text_and_backspace_edits_the_box(self):
        keys = list("jx errado") + ["\x7f"] * len("errado") + list("certo") + ["\r", "esc"]
        self.run_screen(keys)
        self.assertEqual(conference.read_log()[0]["text"], "jx certo")

    def test_a_conference_whose_screen_process_is_gone_reads_as_inactive(self):
        gone = subprocess.Popen([sys.executable, "-c", ""])
        gone.wait()                                # reaped: its pid no longer exists
        self.assertEqual(conference.start([str(self.a)], screen=gone.pid), 0)
        self.assertEqual(conference.read_active(), {})

    def test_ctrl_x_ends_the_conference_and_keeps_the_log(self):
        conference.start([str(self.a)])
        conference.say("guardar", launch=lambda *a, **k: True)
        self.assertEqual(self.run_screen(["\x18", "esc"]), 0)
        self.assertEqual(conference.read_active(), {})
        self.assertEqual(len(conference.read_log()), 1)

    def test_enter_keeps_the_typed_text_when_there_are_no_siblings(self):
        lone = self.tmp / "lone"
        lone.mkdir()
        self.run_screen(list("oi") + ["\r", "esc"], repo=str(lone))
        self.assertEqual(conference.read_active(), {})
        self.assertIn("oi", FakeViewer.last.footer(FakeViewer.last))


if __name__ == "__main__":
    unittest.main()
