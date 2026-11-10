"""Tests for conference mode: persistent workspace state, log, isolated workers and the screen.

Every test points WS_CONFERENCE (the conference folder) and WS_STATE (the workspace state) at a
temporary folder, so the real ~/.config/ai-tmux-workspace is never touched. Launching and running
the isolated agent is injected: no subprocess and no tmux needed.
"""
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import conference  # noqa: E402
import conference_worker  # noqa: E402
import i18n  # noqa: E402


def real(path):
    return os.path.realpath(str(path))


def editing(repo, path, old, new, replace_all=False):
    """PreToolUse event of an Edit call, like the agent sends to the worker hook."""
    return {"hook_event_name": "PreToolUse", "tool_name": "Edit",
            "tool_input": {"file_path": str(path), "old_string": old, "new_string": new,
                           "replace_all": replace_all}, "cwd": str(repo)}


def reading(repo, path):
    return {"hook_event_name": "PreToolUse", "tool_name": "Read",
            "tool_input": {"file_path": str(path)}, "cwd": str(repo)}


class ConferenceTest(unittest.TestCase):
    """One conference per workspace: the state survives the screen and the process."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.a = self.tmp / "repo-a"
        self.b = self.tmp / "repo-b"
        self.a.mkdir()
        self.b.mkdir()
        self.repos = [real(self.a), real(self.b)]
        self.folder = self.tmp / "conference"
        self.cfg = self.tmp / "config.yaml"
        env = mock.patch.dict(os.environ, {"WS_CONFERENCE": str(self.folder),
                                           "WS_STATE": str(self.tmp),
                                           "AT_WS_CONFIG": str(self.cfg)})
        env.start()
        self.addCleanup(env.stop)

    def siblings_config(self):
        self.cfg.write_text(
            "profiles:\n  claude: claude\n"
            "groups:\n"
            "  Pair:\n"
            f"    - path: {self.a}\n"
            f"      siblings: [{self.b}]\n"
            f"    - {self.b}\n",
            encoding="utf-8",
        )

    def fake_launch(self, calls):
        def launch(repo, prompt, message_id, created, others=None):
            calls.append((repo, prompt, message_id, created, others))
            return len(calls)
        return launch

    def test_the_conference_lives_in_the_workspace_config_folder_not_in_a_session(self):
        self.assertEqual(conference.state_dir(), self.folder)
        self.assertEqual(conference.active_path(), self.folder / "active.json")
        self.assertEqual(conference.log_path(), self.folder / "log.jsonl")
        self.assertEqual(conference.archive_dir(), self.folder / "archive")
        self.assertEqual(conference.start(self.repos), 0)
        self.assertTrue(conference.active_path().is_file())
        saved = json.loads(conference.active_path().read_text(encoding="utf-8"))
        self.assertEqual(saved["repos"], self.repos)

    def test_the_state_survives_another_process(self):
        self.assertEqual(conference.start(self.repos, profile="claude"), 0)
        conference.say("guardado", launch=self.fake_launch([]))
        code = (
            "import json, sys\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "import conference\n"
            "print(json.dumps({'active': conference.read_active(),"
            " 'log': conference.read_log()}))\n"
        )
        env = dict(os.environ, WS_CONFERENCE=str(self.folder))
        proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                              text=True, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["active"]["repos"], self.repos)
        self.assertEqual(data["active"]["profile"], "claude")
        self.assertEqual([e["text"] for e in data["log"]], ["guardado"])

    def test_start_with_explicit_repos_writes_state(self):
        self.assertEqual(conference.start([str(self.a), str(self.b)]), 0)
        active = conference.read_active()
        self.assertEqual(active["repos"], self.repos)
        self.assertEqual(active["awaiting"], {})
        self.assertEqual(active["status"], {})
        self.assertEqual(active["workers"], {})
        self.assertIsInstance(active["created"], float)
        self.assertTrue(conference.log_path().exists())
        self.assertEqual(conference.read_log(), [])

    def test_start_is_refused_while_a_conference_is_active(self):
        self.assertEqual(conference.start([str(self.a)]), 0)
        self.assertEqual(conference.start([str(self.b)]), 2)
        self.assertEqual(conference.read_active()["repos"], [self.repos[0]])

    def test_start_without_repos_uses_the_window_repo_and_its_siblings(self):
        self.siblings_config()
        with mock.patch("lib.current_repo", return_value=str(self.a)):
            self.assertEqual(conference.start(), 0)
        self.assertEqual(conference.read_active()["repos"], [self.repos[0], self.repos[1]])

    def test_start_does_not_treat_the_rest_of_the_group_as_siblings(self):
        self.cfg.write_text(
            "groups:\n  Pair:\n"
            f"    - {self.a}\n"
            f"    - {self.b}\n",
            encoding="utf-8")
        with mock.patch("lib.current_repo", return_value=str(self.a)):
            self.assertEqual(conference.start(), 2)
        self.assertEqual(conference.read_active(), {})

    def test_start_uses_the_repo_passed_by_the_options_screen(self):
        self.siblings_config()
        with mock.patch("lib.current_repo",
                        side_effect=AssertionError("must not read the tmux window")):
            self.assertEqual(conference.start(repo=str(self.a)), 0)
        self.assertEqual(conference.read_active()["repos"], [self.repos[0], self.repos[1]])

    def test_start_without_repos_and_without_siblings_is_refused(self):
        outside = self.tmp / "home"
        outside.mkdir()
        self.cfg.write_text(f"groups:\n  Other:\n    - {self.a}\n", encoding="utf-8")
        with mock.patch("lib.current_repo", return_value=str(outside)):
            self.assertEqual(conference.start(), 2)
        self.assertEqual(conference.read_active(), {})

    def test_start_rejects_a_missing_repo(self):
        self.assertEqual(conference.start([str(self.tmp / "nope")]), 2)
        self.assertEqual(conference.read_active(), {})

    def test_start_records_the_window_profile(self):
        self.assertEqual(conference.start(self.repos, profile="opencode"), 0)
        self.assertEqual(conference.read_active()["profile"], "opencode")

    def test_say_appends_the_message_and_starts_one_agent_per_repo(self):
        conference.start(self.repos, profile="claude")
        calls = []
        self.assertEqual(conference.say("my message", launch=self.fake_launch(calls)), 0)

        log = conference.read_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["sender"], "rafael")
        self.assertEqual(log[0]["text"], "my message")
        message_id = log[0]["id"]
        self.assertEqual(conference.read_active()["awaiting"],
                         {self.repos[0]: message_id, self.repos[1]: message_id})
        self.assertEqual([c[0] for c in calls], self.repos)
        self.assertTrue(all(c[2] == message_id for c in calls))
        self.assertTrue(all(str(c[3]) == str(conference.read_active()["created"])
                            for c in calls))
        thinking = i18n.t("conference.status.thinking")
        self.assertEqual(conference.read_active()["status"],
                         {self.repos[0]: thinking, self.repos[1]: thinking})
        self.assertEqual(conference.read_active()["workers"],
                         {self.repos[0]: 1, self.repos[1]: 2})
        for call, repo in zip(calls, self.repos):
            self.assertEqual(call[1], i18n.t(
                "conference.prompt", text="my message", log=i18n.t("conference.log_empty"),
                no_answer=conference.NO_ANSWER, you=conference._sender(repo),
                profile="claude", max_files=conference.MAX_FILES_READ,
                max_steps=conference.MAX_TOOL_STEPS))
            self.assertIn("README.md", call[1])
            self.assertIn("BACKLOG.md", call[1])
            self.assertIn("só quando Rafael pedir", call[1])       # reads only when asked
            self.assertIn(conference_worker.NO_ANSWER, call[1])

    def test_the_prompt_carries_the_whole_log_until_the_cap(self):
        conference.start([str(self.a)], profile="claude")
        conference.say("primeira", launch=self.fake_launch([]))
        conference._append({"ts": time.time(), "sender": "repo-a", "text": "resposta um"})
        calls = []
        conference.say("segunda", launch=self.fake_launch(calls))
        prompt = calls[0][1]
        self.assertIn("segunda", prompt)
        self.assertIn("primeira", prompt)          # the earlier message goes in the context
        self.assertIn("resposta um", prompt)       # so does the earlier answer

    def test_a_long_log_is_capped_and_the_truncation_is_visible(self):
        entries = [{"ts": i, "sender": "rafael", "text": f"mensagem {i} " + "x" * 400}
                   for i in range(80)]
        text, omitted = conference.prompt_log(entries, limit=800)
        self.assertLessEqual(len(text), 800)
        self.assertGreater(omitted, 0)
        self.assertIn(i18n.t("conference.log_truncated", omitted=omitted), text)
        self.assertNotIn("mensagem 0 ", text)                 # the oldest messages were dropped
        self.assertIn("mensagem 79", text)                    # the newest one is still there

    def test_the_default_cap_is_hard(self):
        entries = [{"ts": i, "sender": "rafael", "text": "y" * 1200} for i in range(40)]
        text, omitted = conference.prompt_log(entries)
        self.assertGreater(omitted, 0)
        self.assertLessEqual(len(text), conference.MAX_PROMPT_CHARS)

    def test_a_short_log_goes_in_whole(self):
        entries = [{"ts": 0, "sender": "rafael", "text": "curta"},
                   {"ts": 1, "sender": "repo-a", "text": "resposta"}]
        text, omitted = conference.prompt_log(entries)
        self.assertEqual(omitted, 0)
        self.assertIn("curta", text)
        self.assertIn("resposta", text)
        self.assertEqual(conference.prompt_log([]), (i18n.t("conference.log_empty"), 0))

    def test_say_without_an_active_conference_is_refused(self):
        calls = []
        self.assertEqual(conference.say("hi", launch=self.fake_launch(calls)), 2)
        self.assertEqual(calls, [])
        self.assertEqual(conference.read_log(), [])

    def test_say_drops_from_awaiting_the_repos_that_did_not_start(self):
        conference.start(self.repos)
        calls = []
        first, second = self.repos

        def launch(repo, prompt, message_id, created, others=None):
            calls.append(repo)
            return 0 if repo == first else 4242     # the first repo's agent fails to start

        self.assertEqual(conference.say("hi", launch=launch), 1)
        self.assertEqual(calls, [first, second])
        message_id = conference.read_log()[0]["id"]
        self.assertEqual(conference.read_active()["awaiting"], {second: message_id})
        self.assertEqual(conference.read_active()["status"],
                         {second: i18n.t("conference.status.thinking")})
        self.assertEqual(conference.read_active()["workers"], {second: 4242})

    def test_a_secret_in_the_readme_blocks_that_participant(self):
        secret = "AKIA" + "ABCDEFGHIJKLMNOP"
        (self.a / "README.md").write_text(f"chave {secret}\n", encoding="utf-8")
        conference.start(self.repos)
        calls = []
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(conference.say("oi", launch=self.fake_launch(calls)), 1)
        self.assertEqual([c[0] for c in calls], [self.repos[1]])
        self.assertNotIn(self.repos[0], conference.read_active()["awaiting"])
        log = conference.read_log()
        note = [e for e in log if e["sender"] == conference.SYSTEM_SENDER]
        self.assertEqual(len(note), 1)
        self.assertIn("README.md", note[0]["text"])
        self.assertIn("aws", note[0]["text"])
        self.assertNotIn(secret, note[0]["text"])
        self.assertNotIn(secret, err.getvalue())

    def test_a_secret_in_the_backlog_blocks_that_participant(self):
        (self.b / "BACKLOG.md").write_text("token sk-ant-abcdefghijklmnop\n", encoding="utf-8")
        conference.start(self.repos)
        calls = []
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(conference.say("oi", launch=self.fake_launch(calls)), 1)
        self.assertEqual([c[0] for c in calls], [self.repos[0]])
        self.assertIn("BACKLOG.md", conference.read_log()[-1]["text"])

    def test_stop_only_happens_explicitly_and_keeps_the_log(self):
        conference.start([str(self.a)])
        conference.say("save", launch=self.fake_launch([]))
        self.assertEqual(conference.stop(), 0)
        self.assertEqual(conference.read_active(), {})
        self.assertEqual(len(conference.read_log()), 1)
        self.assertEqual(conference.stop(), 2)

    def test_a_new_conference_after_stop_starts_empty_and_archives_the_old_log(self):
        conference.start([str(self.a)])
        conference.say("first", launch=self.fake_launch([]))
        self.assertEqual(conference.stop(), 0)

        conference.start([str(self.b)])
        self.assertEqual(conference.read_log(), [])
        archives = list((self.folder / "archive").glob("log-*.jsonl"))
        self.assertEqual(len(archives), 1)
        self.assertIn("first", archives[0].read_text(encoding="utf-8"))

    def test_clear_archives_the_log_resets_the_context_and_keeps_the_participants(self):
        conference.start(self.repos, profile="claude")
        conference.say("primeira", launch=self.fake_launch([]))
        before = conference.read_active()
        self.assertEqual(conference.clear(), 0)

        active = conference.read_active()
        self.assertEqual(active["repos"], before["repos"])     # participants are kept
        self.assertEqual(active["profile"], "claude")
        self.assertEqual(active["awaiting"], {})
        self.assertEqual(active["status"], {})
        self.assertEqual(active["workers"], {})
        self.assertGreaterEqual(active["created"], before["created"])
        self.assertEqual(conference.read_log(), [])            # the context is gone
        archives = list((self.folder / "archive").glob("log-*.jsonl"))
        self.assertEqual(len(archives), 1)
        self.assertIn("primeira", archives[0].read_text(encoding="utf-8"))

    def test_clear_without_a_log_keeps_the_participants(self):
        conference.start(self.repos)
        self.assertEqual(conference.clear(), 0)
        self.assertEqual(conference.read_active()["repos"], self.repos)
        self.assertEqual(list((self.folder / "archive").glob("log-*.jsonl")), [])

    def test_clear_without_a_conference_is_refused(self):
        self.assertEqual(conference.clear(), 2)

    def test_pruning_keeps_the_newest_archive_even_when_it_is_old(self):
        folder = self.folder / "archive"
        folder.mkdir(parents=True)
        old = folder / "log-0001.jsonl"
        newest = folder / "log-0002.jsonl"
        old.write_text("old", encoding="utf-8")
        newest.write_text("new", encoding="utf-8")
        ancient = time.time() - conference.ARCHIVE_RETENTION_S - 60
        os.utime(old, (ancient, ancient))
        os.utime(newest, (ancient + 1, ancient + 1))
        conference._prune_archives(folder)
        self.assertFalse(old.exists())
        self.assertTrue(newest.exists())        # the last log is always kept

    def test_an_archive_inside_the_retention_window_is_kept(self):
        folder = self.folder / "archive"
        folder.mkdir(parents=True)
        recent = folder / "log-0001.jsonl"
        recent.write_text("recent", encoding="utf-8")
        conference._prune_archives(folder)
        self.assertTrue(recent.exists())

    def test_resume_clears_a_waiting_mark_whose_worker_is_gone(self):
        conference.start([str(self.a)])
        conference.say("oi", launch=self.fake_launch([]))
        gone = subprocess.Popen([sys.executable, "-c", ""])
        gone.wait()
        active = conference.read_active()
        active["workers"][self.repos[0]] = gone.pid
        conference._write_active(active)
        conference.resume()
        self.assertEqual(conference.read_active()["awaiting"], {})

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
    """The isolated agent run: one per participant, scoped to two files and bounded by the hook."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.a = self.tmp / "repo-a"
        self.b = self.tmp / "repo-b"
        self.a.mkdir()
        self.b.mkdir()
        self.repos = [real(self.a), real(self.b)]
        self.folder = self.tmp / "conference"
        self.cfg = self.tmp / "config.yaml"
        self.cfg.write_text("profiles:\n  claude: claude\nagent: claude\n", encoding="utf-8")
        env = mock.patch.dict(os.environ, {"WS_CONFERENCE": str(self.folder),
                                           "WS_STATE": str(self.tmp),
                                           "AT_WS_CONFIG": str(self.cfg),
                                           "AT_HARNESS_DIR": str(self.tmp / "no-harness")})
        env.start()
        self.addCleanup(env.stop)

    def launch(self):
        calls = []

        def launch(repo, prompt, message_id, created, others=None):
            calls.append((repo, message_id, created))
            return 1000 + len(calls)
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
        self.assertEqual(len(log), 2)
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
        errors = (self.folder / "errors.log").read_text(encoding="utf-8")
        self.assertIn("endpoint is down", errors)
        self.assertEqual(conference.read_active()["awaiting"], {self.repos[1]: message_id})

    def test_a_message_that_is_not_this_repo_gets_no_log_entry_and_no_waiting_mark(self):
        conference.start(self.repos)
        _, message_id, created = self.say("message from another repo")

        ok = conference_worker.answer(
            self.repos[0], "prompt", message_id, created,
            execute=lambda cmd, repo, prompt: (conference_worker.NO_ANSWER, "", 0))

        self.assertFalse(ok)
        self.assertEqual(len(conference.read_log()), 1)
        self.assertEqual(conference.read_active()["awaiting"], {self.repos[1]: message_id})
        self.assertFalse((self.folder / "errors.log").exists())

    def test_a_secret_blocks_the_run_before_it_reaches_the_provider(self):
        (self.a / "README.md").write_text("sk-ant-abcdefghijklmnop\n", encoding="utf-8")
        conference.start(self.repos)
        _, message_id, created = self.say()
        execute = mock.Mock()

        ok = conference_worker.answer(self.repos[0], "prompt", message_id, created,
                                      execute=execute)

        self.assertFalse(ok)
        execute.assert_not_called()
        self.assertEqual(conference.read_log()[-1]["sender"], conference.SYSTEM_SENDER)
        self.assertIn("README.md", conference.read_log()[-1]["text"])
        self.assertNotIn("sk-ant-abcdefghijklmnop", conference.read_log()[-1]["text"])
        self.assertEqual(conference.read_active()["awaiting"], {self.repos[1]: message_id})

    def test_an_answer_from_a_previous_generation_is_dropped(self):
        conference.start([self.repos[0]])
        _, message_id, created = self.say()
        conference.clear()                       # same participants, new generation

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
                 "input": {"file_path": str(Path(self.repos[0]) / "README.md")}}]}},
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
            i18n.t("conference.status.reading", path="README.md"),
            i18n.t("conference.status.searching", pattern="conflito"),
            i18n.t("conference.status.looking", pattern="**/*.md"),
        ])

    def test_the_opencode_stream_returns_the_last_text_part(self):
        lines = [
            {"type": "tool_use", "part": {"tool": "read", "state": {
                "input": {"path": str(Path(self.repos[0]) / "BACKLOG.md")}}}},
            {"type": "text", "part": {"text": "primeira parte"}},
            {"type": "text", "part": {"text": "resposta final"}},
        ]
        statuses = []
        out = conference_worker._stream_answer(
            io.StringIO("\n".join(json.dumps(line) for line in lines)),
            self.repos[0], statuses.append)
        self.assertEqual(out, "primeira parte\n\nresposta final")
        self.assertEqual(statuses, [i18n.t("conference.status.reading", path="BACKLOG.md")])

    def test_an_error_result_is_not_an_answer(self):
        lines = [{"type": "result", "subtype": "error", "is_error": True,
                  "result": "deu ruim"}]
        out = conference_worker._stream_answer(
            io.StringIO("\n".join(json.dumps(line) for line in lines)), self.repos[0])
        self.assertEqual(out, "")

    def test_the_claude_run_is_headless_isolated_restricted_and_budgeted(self):
        cmd = conference_worker.answer_command(profile="claude")
        self.assertIn("-p ", cmd + " ")
        self.assertIn("--output-format stream-json", cmd)
        self.assertIn("--verbose", cmd)
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--permission-mode default", cmd)
        self.assertIn("--append-system-prompt", cmd)
        self.assertIn(conference_worker.NO_ANSWER, cmd)
        self.assertIn(str(conference.MAX_FILES_READ), cmd)
        self.assertIn(str(conference.MAX_TOOL_STEPS), cmd)
        argv = shlex.split(cmd)
        settings_path = Path(argv[argv.index("--settings") + 1])
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        hook = settings["hooks"]["PreToolUse"][-1]["hooks"][0]["command"]
        self.assertIn("conference_worker.py", hook)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Edit")
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        for tool in ("Bash", "Task", "Write", "NotebookEdit", "MultiEdit", "Grep", "Glob"):
            self.assertIn(tool, denied)
        self.assertNotIn("Edit", denied)
        # no sibling, no harness and no second turn
        self.assertNotIn("--add-dir", cmd)
        for path in (self.repos[1], str(self.tmp / "no-harness")):
            self.assertNotIn(path, cmd)
        self.assertNotIn("--resume", cmd)
        self.assertNotIn("--continue", cmd)

    def test_the_opencode_run_uses_the_profile_config_and_two_file_permissions(self):
        self.cfg.write_text(
            "agent: oc\n"
            "profiles:\n"
            "  oc:\n"
            "    harness: opencode\n"
            "    command: opencode\n",
            encoding="utf-8")
        cmd = conference_worker.answer_command(profile="oc")
        self.assertIn("run --format json --archived", cmd)
        self.assertNotIn("-p ", cmd + " ")
        self.assertNotIn("--allowedTools", cmd)
        config_path = cmd.split("OPENCODE_CONFIG=")[1].split()[0].strip("'\"")
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        permissions = config["permissions"]
        self.assertIn({"action": "*", "resource": "*", "effect": "deny"}, permissions)
        self.assertIn({"action": "read", "resource": "README.md", "effect": "allow"},
                      permissions)
        self.assertIn({"action": "read", "resource": "BACKLOG.md", "effect": "allow"},
                      permissions)
        self.assertIn({"action": "edit", "resource": "README.md", "effect": "ask"},
                      permissions)
        self.assertIn({"action": "edit", "resource": "BACKLOG.md", "effect": "allow"},
                      permissions)

    def test_the_active_conference_profile_decides_the_agent(self):
        self.cfg.write_text(
            "agent: claude\n"
            "profiles:\n"
            "  claude: claude\n"
            "  oc:\n"
            "    harness: opencode\n"
            "    command: opencode\n",
            encoding="utf-8")
        conference.start(self.repos, profile="oc")
        self.assertIn("run --format json", conference_worker.answer_command([self.repos[1]]))

    def test_mapping_profile_env_and_model_reach_the_conference_run(self):
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
            "      apiKeyHelper: \"printf from-vault\"\n",
            encoding="utf-8")
        cmd = conference_worker.answer_command(profile="deepseek")
        argv = shlex.split(cmd)
        settings_path = Path(argv[argv.index("--settings") + 1])
        self.assertEqual(settings_path,
                         self.folder / "settings" / "conference-deepseek.json")
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "https://example.invalid/anthropic")
        self.assertEqual(settings["env"]["ANTHROPIC_MODEL"], "deepseek-flash")
        self.assertEqual(settings["model"], "deepseek-flash")
        self.assertEqual(settings["apiKeyHelper"], "printf from-vault")
        self.assertIn("Bash(sudo:*)", settings["permissions"]["deny"])
        self.assertIn("conference_worker.py",
                      settings["hooks"]["PreToolUse"][-1]["hooks"][0]["command"])
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Edit")

    # --- the worker hook: two files, no siblings, no search/bash/turns -------------------------

    def test_the_hook_allows_only_the_two_permitted_files(self):
        (self.a / "README.md").write_text("readme", encoding="utf-8")
        (self.a / "BACKLOG.md").write_text("backlog", encoding="utf-8")
        self.assertIsNone(conference_worker.check_tool(
            reading(self.a, self.a / "README.md"), repo=str(self.a)))
        self.assertIsNone(conference_worker.check_tool(
            reading(self.a, self.a / "BACKLOG.md"), repo=str(self.a)))
        self.assertIsNone(conference_worker.check_tool(
            reading(self.a, "README.md"), repo=str(self.a)))      # relative to the repo

    def test_the_hook_denies_code_siblings_and_the_harness(self):
        harness = self.tmp / "no-harness"
        harness.mkdir()
        for target in (self.a / "src" / "x.py", self.b / "README.md", harness / "HARNESS.md"):
            with self.subTest(target=str(target)):
                reason = conference_worker.check_tool(
                    reading(self.a, target), repo=str(self.a))
                self.assertEqual(reason, i18n.t("conference.read_denied"))

    def test_the_hook_denies_grep_glob_bash_and_every_other_tool(self):
        for tool in ("Grep", "Glob", "Bash", "Task", "WebFetch", "WebSearch"):
            with self.subTest(tool=tool):
                reason = conference_worker.check_tool(
                    {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}},
                    repo=str(self.a))
                self.assertEqual(reason, i18n.t("conference.tool_denied"))

    def test_the_hook_denies_a_write_outside_the_two_files(self):
        reason = conference_worker.check_tool(
            editing(self.a, self.a / "src" / "x.py", "a", "b"), repo=str(self.a))
        self.assertEqual(reason, i18n.t("conference.write_denied"))
        for tool in ("Write", "MultiEdit", "NotebookEdit"):
            with self.subTest(tool=tool):
                reason = conference_worker.check_tool(
                    {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}},
                    repo=str(self.a))
                self.assertEqual(reason, i18n.t("conference.write_denied"))

    def test_editing_the_backlog_is_allowed(self):
        (self.a / "BACKLOG.md").write_text("backlog", encoding="utf-8")
        self.assertIsNone(conference_worker.check_tool(
            editing(self.a, self.a / "BACKLOG.md", "backlog", "backlog novo"),
            repo=str(self.a)))

    def test_a_readme_edit_becomes_a_pending_diff_and_waits_for_confirmation(self):
        readme = self.a / "README.md"
        readme.write_text("# Projeto\n\nantigo\n", encoding="utf-8")

        reason = conference_worker.check_tool(
            editing(self.a, readme, "antigo", "novo"), repo=str(self.a))

        self.assertEqual(reason, i18n.t("conference.readme_pending"))
        self.assertEqual(readme.read_text(encoding="utf-8"), "# Projeto\n\nantigo\n")
        pending = conference.read_pending()
        self.assertIn(str(self.a), pending)
        self.assertIn("-antigo", pending[str(self.a)]["diff"])
        self.assertIn("+novo", pending[str(self.a)]["diff"])
        self.assertEqual(pending[str(self.a)]["before"], "# Projeto\n\nantigo\n")
        self.assertEqual(pending[str(self.a)]["after"], "# Projeto\n\nnovo\n")

    def test_a_readme_edit_with_a_stale_old_string_is_not_captured(self):
        readme = self.a / "README.md"
        readme.write_text("atual\n", encoding="utf-8")
        reason = conference_worker.check_tool(
            editing(self.a, readme, "antigo", "novo"), repo=str(self.a))
        self.assertEqual(reason, i18n.t("conference.readme_stale"))
        self.assertEqual(conference.read_pending(), {})

    def test_confirming_the_readme_proposal_writes_the_new_content(self):
        readme = self.a / "README.md"
        readme.write_text("antigo\n", encoding="utf-8")
        conference_worker.check_tool(editing(self.a, readme, "antigo", "novo"),
                                     repo=str(self.a))
        self.assertEqual(conference.confirm_pending(), 0)
        self.assertEqual(readme.read_text(encoding="utf-8"), "novo\n")
        self.assertEqual(conference.read_pending(), {})
        self.assertIn("README.md", conference.read_log()[-1]["text"])

    def test_rejecting_the_readme_proposal_leaves_the_file_untouched(self):
        readme = self.a / "README.md"
        readme.write_text("antigo\n", encoding="utf-8")
        conference_worker.check_tool(editing(self.a, readme, "antigo", "novo"),
                                     repo=str(self.a))
        self.assertEqual(conference.reject_pending(), 0)
        self.assertEqual(readme.read_text(encoding="utf-8"), "antigo\n")
        self.assertEqual(conference.read_pending(), {})
        self.assertEqual(conference.read_log()[-1]["text"],
                         i18n.t("conference.readme_rejected"))

    def test_a_proposal_whose_file_changed_is_discarded_as_stale(self):
        readme = self.a / "README.md"
        readme.write_text("antigo\n", encoding="utf-8")
        conference_worker.check_tool(editing(self.a, readme, "antigo", "novo"),
                                     repo=str(self.a))
        readme.write_text("mudou por fora\n", encoding="utf-8")
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(conference.confirm_pending(), 1)
        self.assertEqual(readme.read_text(encoding="utf-8"), "mudou por fora\n")

    def test_the_hook_budget_caps_the_tool_steps(self):
        budget = self.tmp / "budget.json"
        (self.a / "README.md").write_text("readme", encoding="utf-8")
        env = mock.patch.dict(os.environ, {"WS_CONFERENCE_BUDGET": str(budget),
                                           "WS_CONFERENCE_MAX_STEPS": "1",
                                           "WS_CONFERENCE_MAX_FILES": "2"})
        with env:
            self.assertIsNone(conference_worker.check_tool(
                reading(self.a, self.a / "README.md"), repo=str(self.a)))
            denied = conference_worker.check_tool(
                reading(self.a, self.a / "README.md"), repo=str(self.a))
        self.assertEqual(denied, i18n.t("conference.budget.steps", max_steps=1))

    def test_the_hook_budget_caps_the_distinct_files_read(self):
        budget = self.tmp / "budget.json"
        (self.a / "README.md").write_text("readme", encoding="utf-8")
        (self.a / "BACKLOG.md").write_text("backlog", encoding="utf-8")
        env = mock.patch.dict(os.environ, {"WS_CONFERENCE_BUDGET": str(budget),
                                           "WS_CONFERENCE_MAX_STEPS": "20",
                                           "WS_CONFERENCE_MAX_FILES": "1"})
        with env:
            self.assertIsNone(conference_worker.check_tool(
                reading(self.a, self.a / "README.md"), repo=str(self.a)))
            denied = conference_worker.check_tool(
                reading(self.a, self.a / "BACKLOG.md"), repo=str(self.a))
        self.assertEqual(denied, i18n.t("conference.budget.files", max_files=1))

    def test_the_hook_reports_a_permitted_tool_call_as_json(self):
        (self.a / "README.md").write_text("readme", encoding="utf-8")
        payload = reading(self.a, self.a / "README.md")
        proc = subprocess.run([sys.executable, str(SRC / "conference_worker.py"), "hook"],
                              input=json.dumps(payload), capture_output=True, text=True,
                              env=dict(os.environ, WS_CONFERENCE=str(self.folder),
                                       WS_CONFERENCE_ROOT=str(self.a)),
                              check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_the_hook_denies_out_of_scope_and_says_why(self):
        payload = reading(self.a, self.a / "src" / "x.py")
        proc = subprocess.run([sys.executable, str(SRC / "conference_worker.py"), "hook"],
                              input=json.dumps(payload), capture_output=True, text=True,
                              env=dict(os.environ, WS_CONFERENCE=str(self.folder),
                                       WS_CONFERENCE_ROOT=str(self.a)),
                              check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertEqual(out["permissionDecisionReason"], i18n.t("conference.read_denied"))


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
                if self.stopped:
                    break
                continue
            if self.on_key:
                self.on_key(self, k)
            if self.stopped:
                break


class ConverseTest(unittest.TestCase):
    """The conversation screen: the footer is the message box; closing it does not end anything."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.a = self.tmp / "repo-a"
        self.b = self.tmp / "repo-b"
        self.a.mkdir()
        self.b.mkdir()
        self.repos = [real(self.a), real(self.b)]
        self.folder = self.tmp / "conference"
        self.cfg = self.tmp / "config.yaml"
        self.cfg.write_text(
            "profiles:\n  claude: claude\n"
            "groups:\n"
            "  Pair:\n"
            f"    - path: {self.a}\n"
            f"      siblings: [{self.b}]\n"
            f"    - {self.b}\n",
            encoding="utf-8",
        )
        env = mock.patch.dict(os.environ, {"WS_CONFERENCE": str(self.folder),
                                           "WS_STATE": str(self.tmp),
                                           "AT_WS_CONFIG": str(self.cfg)})
        env.start()
        self.addCleanup(env.stop)

    def run_screen(self, keys, repo=None):
        FakeViewer.keys, FakeViewer.last = list(keys), None
        with mock.patch("viewer.Viewer", FakeViewer), \
                mock.patch("conference._launch_answer", return_value=999):
            return conference.converse(repo=repo or str(self.a))

    def body(self):
        return "\n".join(l.text for l in FakeViewer.last.frame[1])

    def header(self):
        return "\n".join(l.text for l in FakeViewer.last.frame[0])

    def test_typing_and_enter_starts_with_the_window_repo_and_siblings_and_sends(self):
        self.assertEqual(self.run_screen(list("my message") + ["\r", "\x18"]), 0)
        active = conference.read_active()
        self.assertEqual(active["repos"], self.repos)
        log = conference.read_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["sender"], "rafael")
        self.assertEqual(log[0]["text"], "my message")

    def test_esc_only_closes_the_screen_and_the_conference_stays(self):
        conference.start([str(self.a), str(self.b)])
        conference.say("hi", launch=lambda *a, **k: 999)
        self.assertEqual(self.run_screen(["esc"], repo=str(self.a)), 0)
        self.assertTrue(conference.read_active())          # Esc does not stop the conference
        self.assertEqual(len(conference.read_log()), 1)

    def test_ctrl_x_only_closes_the_screen_and_the_conference_stays(self):
        conference.start([str(self.a)])
        conference.say("guardar", launch=lambda *a, **k: 999)
        self.assertEqual(self.run_screen(["\x18"]), 0)
        self.assertTrue(conference.read_active())
        self.assertEqual(len(conference.read_log()), 1)

    def test_reopening_the_screen_shows_the_persisted_log(self):
        self.run_screen(list("oi") + ["\r", "\x18"])
        self.run_screen(["esc"])
        self.assertIn("oi", self.body())
        self.assertIn("CONFERÊNCIA", self.header())
        self.assertIn("repo-a", self.header())

    def test_clear_archives_the_log_and_resets_the_context_keeping_the_participants(self):
        self.run_screen(list("primeira") + ["\r", "\x18"])
        before = conference.read_active()
        self.run_screen(list("/clear") + ["\r", "\x18"], repo=str(self.a))
        active = conference.read_active()
        self.assertEqual(active["repos"], before["repos"])
        self.assertEqual(conference.read_log(), [])
        self.assertEqual(len(list((self.folder / "archive").glob("log-*.jsonl"))), 1)

    def test_the_header_shows_what_each_repo_is_doing(self):
        conference.start(self.repos)
        conference.say("hi", launch=lambda *a, **k: 999)
        message_id = conference.read_log()[0]["id"]
        created = conference.read_active()["created"]
        conference_worker._set_status(self.repos[0], message_id, created, "lendo README.md")

        self.run_screen(["esc"], repo=str(self.a))
        header = self.header()
        self.assertIn("repo-a (lendo README.md)", header)
        self.assertIn("repo-b (analisando…)", header)
        self.assertIn(i18n.t("conference.screen.hint"), self.body())

    def test_esc_on_the_inactive_screen_changes_nothing(self):
        self.assertEqual(self.run_screen(["esc"]), 0)
        self.assertEqual(conference.read_active(), {})
        self.assertEqual(conference.read_log(), [])

    def test_j_and_x_are_text_and_backspace_edits_the_box(self):
        keys = list("jx errado") + ["\x7f"] * len("errado") + list("certo") + ["\r", "\x18"]
        self.run_screen(keys)
        self.assertEqual(conference.read_log()[0]["text"], "jx certo")

    def test_enter_keeps_the_typed_text_when_there_are_no_siblings(self):
        lone = self.tmp / "lone"
        lone.mkdir()
        self.run_screen(list("oi") + ["\r", "esc"], repo=str(lone))
        self.assertEqual(conference.read_active(), {})
        self.assertIn("oi", FakeViewer.last.footer(FakeViewer.last))

    def test_the_pending_readme_diff_is_shown_and_y_confirms_it(self):
        readme = self.a / "README.md"
        readme.write_text("# Projeto\n\nantigo\n", encoding="utf-8")
        conference_worker.check_tool(editing(self.a, readme, "antigo", "novo"),
                                     repo=str(self.a))
        self.run_screen(["y", "\x18"], repo=str(self.a))
        self.assertEqual(readme.read_text(encoding="utf-8"), "# Projeto\n\nnovo\n")
        self.assertEqual(conference.read_pending(), {})

    def test_the_pending_readme_diff_is_shown_and_n_discards_it(self):
        readme = self.a / "README.md"
        readme.write_text("# Projeto\n\nantigo\n", encoding="utf-8")
        conference_worker.check_tool(editing(self.a, readme, "antigo", "novo"),
                                     repo=str(self.a))
        self.run_screen(["n", "\x18"], repo=str(self.a))
        self.assertEqual(readme.read_text(encoding="utf-8"), "# Projeto\n\nantigo\n")
        self.assertEqual(conference.read_pending(), {})

    def test_the_diff_is_visible_on_the_screen_before_the_confirmation(self):
        readme = self.a / "README.md"
        readme.write_text("antigo\n", encoding="utf-8")
        conference_worker.check_tool(editing(self.a, readme, "antigo", "novo"),
                                     repo=str(self.a))
        self.run_screen(["esc"], repo=str(self.a))
        self.assertIn(i18n.t("conference.pending.title"), self.body())
        self.assertIn("-antigo", self.body())
        self.assertIn("+novo", self.body())
        self.assertEqual(conference.read_pending() != {}, True)   # Esc does not decide it


if __name__ == "__main__":
    unittest.main()
