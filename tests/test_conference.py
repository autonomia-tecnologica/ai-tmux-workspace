"""Tests for conference mode: state, log, isolated workers and the conversation screen.

Every test points WS_ESTADO (and, for the config cases, AT_WS_CONFIG) at a temporary folder, so
the real ~/.cache/workspace is never touched. Launching and running the isolated Claude is
injected: no subprocess and no tmux needed.
"""
import io
import json
import os
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
        env = mock.patch.dict(os.environ, {"WS_ESTADO": str(self.tmp)})
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
        conference.say("oi", launch=self.fake_launch([]))
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
                mock.patch("lib.repo_atual", return_value=str(self.a)):
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
                mock.patch("lib.repo_atual", return_value=str(self.a)):
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
                mock.patch("lib.repo_atual", side_effect=AssertionError("must not read the tmux window")):
            self.assertEqual(conference.start(repo=str(self.a)), 0)
        self.assertEqual(conference.read_active()["repos"], [self.repos[0], self.repos[1]])

    def test_start_without_repos_and_without_siblings_is_refused(self):
        fora = self.tmp / "home"
        fora.mkdir()
        cfg = self.tmp / "config.yaml"
        cfg.write_text(
            "groups:\n"
            "  Other:\n"
            f"    - {self.a}\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"AT_WS_CONFIG": str(cfg)}), \
                mock.patch("lib.repo_atual", return_value=str(fora)):
            self.assertEqual(conference.start(), 2)
        self.assertEqual(conference.read_active(), {})

    def test_start_rejects_a_missing_repo(self):
        self.assertEqual(conference.start([str(self.tmp / "nope")]), 2)
        self.assertEqual(conference.read_active(), {})

    def test_say_appends_the_message_and_starts_one_claude_per_repo(self):
        conference.start([str(self.a), str(self.b)])
        calls = []
        self.assertEqual(conference.say("minha mensagem", launch=self.fake_launch(calls)), 0)

        log = conference.read_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["sender"], "rafael")
        self.assertEqual(log[0]["text"], "minha mensagem")
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
        expected = dict(text="minha mensagem", no_answer=conference.NO_ANSWER,
                        participants=participants, max_files=conference.MAX_FILES_READ,
                        max_steps=conference.MAX_TOOL_STEPS)
        self.assertEqual(calls[0][1], i18n.t(
            "conference.prompt", you=conference._sender(self.repos[0]), **expected))
        self.assertEqual(calls[1][1], i18n.t(
            "conference.prompt", you=conference._sender(self.repos[1]), **expected))
        self.assertIn("um parágrafo", calls[0][1])
        self.assertIn("minha mensagem", calls[0][1])
        self.assertIn("conferência", calls[0][1])           # not long work
        self.assertIn("apenas sobre este repo", calls[0][1])
        self.assertIn("leia o harness", calls[0][1])
        self.assertIn("validação cruzada", calls[0][1])
        self.assertIn("repos irmãos", calls[0][1])
        self.assertIn("análise mais profunda", calls[0][1])
        self.assertIn("dirigida a outro participante", calls[0][1])
        self.assertIn(f"{conference.MAX_FILES_READ} arquivos", calls[0][1])
        self.assertIn(f"{conference.MAX_TOOL_STEPS} passos", calls[0][1])
        self.assertNotIn("registre no BACKLOG", calls[0][1])   # the backlog is Rafael's call
        self.assertIn("somente leitura", calls[0][1])
        for repo in self.repos:                            # every participant is named
            self.assertIn(f"{conference._sender(repo)} ({repo})", calls[0][1])
        self.assertIn(conference_worker.NO_ANSWER, calls[0][1])
        self.assertNotIn("independente do assunto", calls[0][1])
        self.assertNotIn(str(conference.log_path()), calls[0][1])   # no read outside the repo

    def test_the_prompt_carries_only_the_message_being_sent(self):
        conference.start([str(self.a)])
        calls = []
        conference.say("primeira", launch=self.fake_launch([]))
        conference.say("segunda", launch=self.fake_launch(calls))
        self.assertIn("segunda", calls[0][1])
        self.assertNotIn("primeira", calls[0][1])          # no history in the prompt

    def test_say_without_an_active_conference_is_refused(self):
        calls = []
        self.assertEqual(conference.say("oi", launch=self.fake_launch(calls)), 2)
        self.assertEqual(calls, [])
        self.assertEqual(conference.read_log(), [])

    def test_say_drops_from_awaiting_the_repos_that_did_not_start(self):
        conference.start([str(self.a), str(self.b)])
        calls = []
        first, second = self.repos

        def launch(repo, prompt, message_id, created, others=None):
            calls.append(repo)
            return repo == second     # the first repo's Claude fails to start

        self.assertEqual(conference.say("oi", launch=launch), 1)
        self.assertEqual(calls, [first, second])
        message_id = conference.read_log()[0]["id"]
        self.assertEqual(conference.read_active()["awaiting"], {second: message_id})
        self.assertEqual(conference.read_active()["status"],
                         {second: i18n.t("conference.status.thinking")})

    def test_stop_closes_the_conference_and_keeps_the_log(self):
        conference.start([str(self.a)])
        conference.say("guardar", launch=self.fake_launch([]))
        self.assertEqual(conference.stop(), 0)
        self.assertEqual(conference.read_active(), {})
        self.assertEqual(len(conference.read_log()), 1)
        self.assertEqual(conference.stop(), 2)

    def test_a_new_conference_starts_empty_and_the_previous_log_does_not_come_back(self):
        conference.start([str(self.a)])
        conference.say("primeira", launch=self.fake_launch([]))
        self.assertEqual(conference.stop(), 0)

        conference.start([str(self.b)])
        self.assertEqual(conference.read_log(), [])     # no persistence on screen
        archives = list((self.tmp / "conference").glob("log-*.jsonl"))
        self.assertEqual(len(archives), 1)              # the old log is only a file
        self.assertIn("primeira", archives[0].read_text(encoding="utf-8"))

    def test_first_paragraph_keeps_only_the_first_block(self):
        self.assertEqual(conference.first_paragraph("um.\n\n dois.\n\ntrês"), "um.")
        self.assertEqual(conference.first_paragraph("só um parágrafo\ncom quebra de linha"), "só um parágrafo\ncom quebra de linha")
        self.assertEqual(conference.first_paragraph("\n \n"), "")

    def test_format_log_has_sender_and_time(self):
        text = conference.format_log([
            {"ts": 0, "sender": "rafael", "text": "pergunta"},
            {"ts": 0, "sender": "repo-a", "text": "resposta"},
        ])
        self.assertIn("Rafael", text)
        self.assertIn("repo-a", text)
        self.assertIn("pergunta", text)
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
        cfg = self.tmp / "config.yaml"
        cfg.write_text("profiles:\n  claude: claude\nagent: claude\n", encoding="utf-8")
        env = mock.patch.dict(os.environ, {"WS_ESTADO": str(self.tmp), "AT_WS_CONFIG": str(cfg)})
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
            execute=lambda cmd, repo, prompt: ("Primeiro.\n\nSegundo.", "", 0))

        self.assertTrue(ok)
        log = conference.read_log()
        self.assertEqual(len(log), 2)                 # Rafael's message + repo-a's answer
        self.assertEqual(log[1]["sender"], "repo-a")
        self.assertEqual(log[1]["repo"], self.repos[0])
        self.assertEqual(log[1]["reply_to"], message_id)
        self.assertEqual(log[1]["text"], "Primeiro.")
        self.assertEqual(conference.read_active()["awaiting"], {self.repos[1]: message_id})
        self.assertEqual(conference.read_active()["status"],
                         {self.repos[1]: i18n.t("conference.status.thinking")})

    def test_a_failed_run_writes_a_visible_note_and_keeps_the_details_out_of_the_log(self):
        conference.start(self.repos)
        _, message_id, created = self.say()

        ok = conference_worker.answer(
            self.repos[0], "prompt", message_id, created,
            execute=lambda cmd, repo, prompt: ("", "endpoint fora do ar", 1))

        self.assertFalse(ok)
        log = conference.read_log()
        self.assertEqual(log[1]["text"], i18n.t("conference.answer_failed"))
        self.assertNotIn("endpoint fora do ar", log[1]["text"])
        errors = (self.tmp / "conference" / "errors.log").read_text(encoding="utf-8")
        self.assertIn("endpoint fora do ar", errors)
        self.assertEqual(conference.read_active()["awaiting"], {self.repos[1]: message_id})
        self.assertEqual(conference.read_active()["status"],
                         {self.repos[1]: i18n.t("conference.status.thinking")})

    def test_a_message_that_is_not_this_repo_gets_no_log_entry_and_no_waiting_mark(self):
        conference.start(self.repos)
        _, message_id, created = self.say("mensagem de outro repo")

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

    def test_the_run_is_headless_isolated_read_only_and_budgeted(self):
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
        self.assertNotIn("--settings", cmd)          # never override the profile's settings
        self.assertIn("Read,Grep,Glob", cmd)         # the only allowed tools
        denied = cmd.split("--disallowedTools", 1)[1]
        for tool in ("Bash", "Task", "Edit", "Write", "WebFetch", "WebSearch"):
            self.assertIn(tool, denied)              # read-only: nothing is written
        self.assertNotIn("--resume", cmd)
        self.assertNotIn("--continue", cmd)


BUDGET_HOOK = SRC / "conference_budget.py"


class BudgetHookTest(unittest.TestCase):
    """The PreToolUse hook that turns the conference budget into a mechanism, not a prompt request."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "budget.json"

    def hook(self, tool="Read", file_path=None, max_steps=20, max_files=12, with_state=True,
             tool_use_id=None):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}}
        if file_path is not None:
            payload["tool_input"]["file_path"] = file_path
        if tool_use_id is not None:
            payload["tool_use_id"] = tool_use_id
        env = dict(os.environ, WS_CONFERENCE_MAX_STEPS=str(max_steps),
                   WS_CONFERENCE_MAX_FILES=str(max_files))
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
        self.assertIn("responda agora", denied.get("permissionDecisionReason"))

    def test_the_same_event_counted_twice_charges_one_step(self):
        self.assertEqual(self.hook(tool="Grep", max_steps=1, tool_use_id="tool-1"), {})
        self.assertEqual(self.hook(tool="Grep", max_steps=1, tool_use_id="tool-1"), {})  # duplicate
        denied = self.hook(tool="Grep", max_steps=1, tool_use_id="tool-2")   # a real second step
        self.assertEqual(denied.get("permissionDecision"), "deny")

    def test_uncounted_tools_and_a_run_without_state_are_not_blocked(self):
        self.assertEqual(self.hook(tool="Bash"), {})
        self.assertEqual(self.hook(file_path="/x/a.md", with_state=False), {})
        self.assertFalse(self.state.exists())

    def test_a_broken_state_file_does_not_block_the_run(self):
        self.state.write_text("not json", encoding="utf-8")
        self.assertEqual(self.hook(file_path="/x/a.md", max_files=1), {})
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["files"], ["/x/a.md"])

    def test_the_workspace_hook_applies_the_budget_to_a_conference_run(self):
        payload = {"hook_event_name": "PreToolUse", "session_id": "sess-1",
                   "tool_name": "Read", "tool_input": {"file_path": "/x/a.md"}}
        env = dict(os.environ, WS_ESTADO=str(self.tmp), WS_CONFERENCE_BUDGET=str(self.state),
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
        env = dict(os.environ, WS_ESTADO=str(self.tmp))
        env.pop("WS_CONFERENCE_BUDGET", None)
        proc = subprocess.run([sys.executable, str(SRC / "hook.py")],
                              input=json.dumps(payload), capture_output=True, text=True,
                              env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")


class FakeViewer:
    """Key script applied in order right after the first frame is collected."""

    keys = []
    last = None

    def __init__(self, coletar, intervalo=2.0, **kwargs):
        self.coletar = coletar
        self.ao_tecla, self.entrada, self.rodape = (kwargs.get("ao_tecla"), kwargs.get("entrada"),
                                                    kwargs.get("rodape"))
        self.top, self.parar, self.frame = 0, False, None
        self.recoletar = mock.Mock()
        type(self).last = self

    def _acordar(self):
        pass

    def rodar(self):
        self.frame = self.coletar(self)
        for k in type(self).keys:
            if self.entrada and self.entrada(self, k):
                continue
            if self.ao_tecla:
                self.ao_tecla(self, k)
            if self.parar:
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
        env = mock.patch.dict(os.environ, {"WS_ESTADO": str(self.tmp), "AT_WS_CONFIG": str(cfg)})
        env.start()
        self.addCleanup(env.stop)

    def run_screen(self, keys, repo=None):
        FakeViewer.keys, FakeViewer.last = list(keys), None
        with mock.patch("viewer.Viewer", FakeViewer), \
                mock.patch("conference._launch_answer", return_value=True):
            return conference.converse(repo=repo or str(self.a))

    def test_typing_and_enter_starts_with_the_window_repo_and_siblings_and_sends(self):
        self.assertEqual(self.run_screen(list("minha mensagem") + ["\r", "esc"]), 0)
        self.assertEqual(conference.read_active(), {})   # Esc closes the conference with the screen
        log = conference.read_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["sender"], "rafael")
        self.assertEqual(log[0]["text"], "minha mensagem")

    def test_esc_ends_the_open_conference_and_keeps_the_log(self):
        conference.start([str(self.a), str(self.b)])
        conference.say("oi", launch=lambda *a, **k: True)
        self.assertEqual(self.run_screen(["esc"], repo=str(self.a)), 0)
        self.assertEqual(conference.read_active(), {})
        self.assertEqual(len(conference.read_log()), 1)

    def test_the_header_shows_what_each_repo_is_doing(self):
        conference.start(self.repos)
        conference.say("oi", launch=lambda *a, **k: True)
        message_id = conference.read_log()[0]["id"]
        created = conference.read_active()["created"]
        conference_worker._set_status(self.repos[0], message_id, created, "lendo docs/x.md")

        self.assertEqual(self.run_screen(["esc"], repo=str(self.a)), 0)
        header = FakeViewer.last.frame[0][0].texto
        body = "\n".join(l.texto for l in FakeViewer.last.frame[1])
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
        texto = "\n".join(l.texto for l in FakeViewer.last.frame[1])
        self.assertNotIn("oi", texto)
        self.assertIn(i18n.t("conference.not_active"), texto)

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
        self.assertIn("oi", FakeViewer.last.rodape(FakeViewer.last))


if __name__ == "__main__":
    unittest.main()
