"""Agent state of each tab (item 137): one vocabulary, adapter translation, the tab state file, the
repo summary by worst state, dead agents as `unknown`, `workspace status`, and the two entry points
that store a state (the Claude hook and `hook.py agent` for OpenCode).

Every test points WS_STATE (and the config) at a temporary folder, so the real state is never read
or written. The Claude hook is run with its claude pid patched: a test has no claude process above it."""
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
sys.path.insert(0, str(SRC))

import session_state  # noqa: E402
from agents.claude import ClaudeAdapter  # noqa: E402
from agents.opencode import OpenCodeAdapter  # noqa: E402
from session_state import BLOCKED, DONE, IDLE, UNKNOWN, WORKING  # noqa: E402


def dead_pid():
    """A pid of a process that has already exited (and was reaped), so it is not alive."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class StateVocabularyTest(unittest.TestCase):
    def test_the_five_states_are_declared_once(self):
        self.assertEqual(session_state.STATES, (BLOCKED, WORKING, DONE, IDLE, UNKNOWN))
        self.assertEqual(set(session_state.DECLARED), {BLOCKED, WORKING, DONE, IDLE})

    def test_worst_order_is_blocked_working_done_then_unknown_then_idle(self):
        order = sorted(session_state.STATES, key=session_state.PRIORITY.get, reverse=True)
        self.assertEqual(order, [BLOCKED, WORKING, DONE, UNKNOWN, IDLE])


class ClaudeTranslationTest(unittest.TestCase):
    """Claude hook events (`hook_event_name`) to the declared state."""

    def state(self, name, current=IDLE, **fields):
        return ClaudeAdapter().state_of(dict(hook_event_name=name, **fields), current)

    def test_prompt_and_tool_steps_are_working(self):
        self.assertEqual(self.state("UserPromptSubmit"), WORKING)
        self.assertEqual(self.state("PreToolUse", tool_name="Bash"), WORKING)
        self.assertEqual(self.state("PostToolUse", tool_name="Bash"), WORKING)
        self.assertEqual(self.state("PostToolUseFailure", tool_name="Bash"), WORKING)
        self.assertEqual(self.state("PermissionDenied"), WORKING)

    def test_a_finished_turn_is_done(self):
        self.assertEqual(self.state("Stop", last_assistant_message="ok"), DONE)
        self.assertEqual(self.state("StopFailure"), DONE)

    def test_background_work_keeps_the_turn_working(self):
        self.assertEqual(self.state("Stop", background_tasks=["t1"]), WORKING)
        self.assertEqual(self.state("StopFailure", session_crons=["c1"]), WORKING)

    def test_only_what_the_agent_declares_is_blocked(self):
        self.assertEqual(self.state("PermissionRequest"), BLOCKED)
        self.assertEqual(self.state("Elicitation"), BLOCKED)
        self.assertEqual(self.state("Notification", notification_type="permission_prompt"), BLOCKED)
        self.assertEqual(self.state("Notification", notification_type="elicitation_dialog"), BLOCKED)
        self.assertEqual(self.state("PreToolUse", tool_name="AskUserQuestion"), BLOCKED)
        self.assertEqual(self.state("PreToolUse", tool_name="ExitPlanMode"), BLOCKED)

    def test_a_plain_notification_does_not_block(self):
        self.assertIsNone(self.state("Notification", notification_type="idle_prompt"))

    def test_a_subagent_tool_keeps_a_blocked_session_blocked(self):
        self.assertEqual(self.state("PreToolUse", current=BLOCKED, agent_id="a1", tool_name="Read"),
                         BLOCKED)
        self.assertEqual(self.state("PreToolUse", current=DONE, agent_id="a1", tool_name="Read"),
                         WORKING)

    def test_session_start_and_end(self):
        self.assertEqual(self.state("SessionStart", source="startup"), IDLE)
        self.assertIsNone(self.state("SessionStart", source="compact"))
        self.assertEqual(self.state("SessionEnd"), IDLE)

    def test_events_without_a_state_change_return_none(self):
        self.assertIsNone(self.state("SubagentStart", agent_id="a1"))
        self.assertIsNone(self.state("CwdChanged"))
        self.assertIsNone(ClaudeAdapter().state_of("not a dict"))


class OpenCodeTranslationTest(unittest.TestCase):
    """OpenCode plugin events (`event`, and `status` for session.status) to the declared state."""

    def state(self, event, status=""):
        return OpenCodeAdapter().state_of({"event": event, "status": status})

    def test_tool_steps_and_busy_sessions_are_working(self):
        self.assertEqual(self.state("tool.execute.before"), WORKING)
        self.assertEqual(self.state("tool.execute.after"), WORKING)
        self.assertEqual(self.state("session.status", "busy"), WORKING)
        self.assertEqual(self.state("permission.replied"), WORKING)

    def test_a_permission_request_is_blocked(self):
        self.assertEqual(self.state("permission.asked"), BLOCKED)
        self.assertEqual(self.state("permission.updated"), BLOCKED)

    def test_a_finished_turn_is_done(self):
        self.assertEqual(self.state("session.idle"), DONE)
        self.assertEqual(self.state("session.error"), DONE)
        self.assertEqual(self.state("session.status", "idle"), DONE)

    def test_other_events_change_nothing(self):
        self.assertIsNone(self.state("message.updated"))
        self.assertIsNone(self.state("session.status", "retry"))
        self.assertIsNone(OpenCodeAdapter().state_of(None))


class TabStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.state = self.root / "state"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state.mkdir()
        env = mock.patch.dict(os.environ, {"WS_STATE": str(self.state),
                                           "AT_WS_CONFIG": str(self.root / "config.yaml")})
        env.start()
        self.addCleanup(env.stop)
        session_state.add(str(self.repo), "tab-1", "claude")
        session_state.add(str(self.repo), "tab-2", "oc")

    def alive(self, pid):
        return True

    def test_a_tab_with_no_declared_state_is_idle(self):
        states = {t["id"]: t["state"] for t in session_state.tab_states(str(self.repo), self.alive)}
        self.assertEqual(states, {"tab-1": IDLE, "tab-2": IDLE})

    def test_a_declared_state_of_a_live_agent_is_shown(self):
        self.assertTrue(session_state.record_agent(str(self.repo), "tab-1", BLOCKED, pid=os.getpid(),
                                                   harness="claude"))
        tab = session_state.tab_states(str(self.repo))[0]
        self.assertEqual((tab["state"], tab["declared"], tab["harness"]), (BLOCKED, BLOCKED, "claude"))

    def test_a_dead_agent_reads_as_unknown_whatever_it_declared(self):
        pid = dead_pid()
        session_state.record_agent(str(self.repo), "tab-1", WORKING, pid=pid, harness="claude")
        session_state.record_agent(str(self.repo), "tab-2", DONE, pid=pid, harness="opencode")
        states = {t["id"]: t["state"] for t in session_state.tab_states(str(self.repo))}
        self.assertEqual(states, {"tab-1": UNKNOWN, "tab-2": UNKNOWN})
        # the declared state is kept on disk: what the agent said, not what the panel shows
        self.assertEqual(session_state.tab_states(str(self.repo))[0]["declared"], WORKING)

    def test_working_or_blocked_without_a_pid_is_unknown(self):
        session_state.record_agent(str(self.repo), "tab-1", WORKING, pid=0)
        self.assertEqual(session_state.tab_states(str(self.repo), self.alive)[0]["state"], UNKNOWN)

    def test_done_without_a_pid_stays_done(self):
        session_state.record_agent(str(self.repo), "tab-1", DONE, pid=0)
        self.assertEqual(session_state.tab_states(str(self.repo), self.alive)[0]["state"], DONE)

    def test_unknown_is_never_stored_and_an_unknown_tab_is_refused(self):
        with self.assertRaises(ValueError):
            session_state.record_agent(str(self.repo), "tab-1", UNKNOWN, pid=1)
        self.assertFalse(session_state.record_agent(str(self.repo), "gone", WORKING, pid=os.getpid()))

    def test_the_pid_of_a_zombie_or_missing_process_is_not_alive(self):
        self.assertFalse(session_state.pid_alive(dead_pid()))
        self.assertFalse(session_state.pid_alive(0))
        self.assertTrue(session_state.pid_alive(os.getpid()))

    def test_repo_summary_is_the_worst_state_of_its_tabs(self):
        session_state.record_agent(str(self.repo), "tab-1", DONE, pid=os.getpid())
        session_state.record_agent(str(self.repo), "tab-2", WORKING, pid=os.getpid())
        self.assertEqual(session_state.repo_state(str(self.repo), self.alive), WORKING)
        session_state.record_agent(str(self.repo), "tab-1", BLOCKED, pid=os.getpid())
        self.assertEqual(session_state.repo_state(str(self.repo), self.alive), BLOCKED)

    def test_a_repo_with_no_tab_is_idle_and_the_panel_summary_uses_the_same_rule(self):
        import lib
        other = self.root / "other"
        other.mkdir()
        session_state.record_agent(str(self.repo), "tab-2", DONE, pid=os.getpid())
        summary = lib.states_by_repo([("grupo", [("repo", str(self.repo)), ("other", str(other))])])
        self.assertEqual(summary[str(self.repo)], DONE)
        self.assertEqual(summary[str(other)], IDLE)

    def test_saving_the_tab_list_keeps_the_agent_state_of_the_tabs_that_stay(self):
        session_state.record_agent(str(self.repo), "tab-1", BLOCKED, pid=os.getpid(), harness="claude")
        session_state.save(str(self.repo), [("tab-1", "claude")], "tab-1")
        tab = session_state.tab_states(str(self.repo))[0]
        self.assertEqual((tab["id"], tab["declared"]), ("tab-1", BLOCKED))

    def test_removing_a_tab_drops_its_state(self):
        session_state.record_agent(str(self.repo), "tab-1", BLOCKED, pid=os.getpid())
        session_state.remove(str(self.repo), "tab-1")
        self.assertEqual([t["id"] for t in session_state.tab_states(str(self.repo))], ["tab-2"])


class WorkspaceStatusTest(unittest.TestCase):
    """`workspace status` prints one line per tab with its resolved state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.state = self.root / "state"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        env = mock.patch.dict(os.environ, {"WS_STATE": str(self.state),
                                           "AT_WS_CONFIG": str(self.root / "config.yaml")})
        env.start()
        self.addCleanup(env.stop)

    def run_status(self):
        proc = subprocess.run([sys.executable, str(SRC / "session_state.py"), "status"],
                              capture_output=True, text=True, check=False,
                              env=dict(os.environ, WS_STATE=str(self.state)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [line.split("\t") for line in proc.stdout.splitlines()]

    def test_status_prints_the_resolved_state_of_every_tab(self):
        session_state.add(str(self.repo), "tab-1", "claude")
        session_state.add(str(self.repo), "tab-2", "oc")
        session_state.record_agent(str(self.repo), "tab-1", BLOCKED, pid=os.getpid(), harness="claude")
        session_state.record_agent(str(self.repo), "tab-2", WORKING, pid=dead_pid(), harness="opencode")
        rows = self.run_status()
        real = str(self.repo.resolve())
        self.assertEqual(rows, [
            [BLOCKED, real, "tab-1", "claude", "claude"],
            [UNKNOWN, real, "tab-2", "oc", "opencode"],
        ])

    def test_status_prints_nothing_without_tabs(self):
        self.assertEqual(self.run_status(), [])

    def test_the_workspace_script_routes_status_to_the_state_cli(self):
        script = (ROOT / "bin" / "workspace").read_text(encoding="utf-8")
        self.assertIn('status) shift; "$PYTHON" "$SRC/session_state.py" status "$@" ;;', script)


class StoringEntryPointsTest(unittest.TestCase):
    """The two ways a state reaches the tab file: the Claude hook and `hook.py agent` (OpenCode)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.state = self.root / "state"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.config = self.root / "config.yaml"
        self.config.write_text("notifications: false\n", encoding="utf-8")
        self.env = dict(os.environ, WS_STATE=str(self.state), AT_WS_CONFIG=str(self.config),
                        WS_SESSION_TAB="tab-1", WS_SESSION_ROOT=str(self.repo))
        env = mock.patch.dict(os.environ, {"WS_STATE": str(self.state),
                                           "AT_WS_CONFIG": str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        session_state.add(str(self.repo), "tab-1", "claude")

    def test_opencode_event_through_the_entry_point_stores_the_tab_state(self):
        proc = subprocess.run([sys.executable, str(SRC / "hook.py"), "agent", "opencode", "tab-1",
                               str(os.getpid())], input=json.dumps({"event": "permission.asked"}),
                              capture_output=True, text=True, check=False, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        tab = session_state.tab_states(str(self.repo))[0]
        self.assertEqual((tab["state"], tab["harness"]), (BLOCKED, "opencode"))

    def test_opencode_entry_point_with_a_dead_pid_reads_unknown(self):
        subprocess.run([sys.executable, str(SRC / "hook.py"), "agent", "opencode", "tab-1",
                        str(dead_pid())], input=json.dumps({"event": "session.idle"}),
                       capture_output=True, text=True, check=False, env=self.env)
        self.assertEqual(session_state.tab_states(str(self.repo))[0]["state"], UNKNOWN)

    def test_opencode_entry_point_ignores_an_unknown_event_and_a_missing_tab(self):
        subprocess.run([sys.executable, str(SRC / "hook.py"), "agent", "opencode", "tab-1",
                        str(os.getpid())], input=json.dumps({"event": "message.updated"}),
                       capture_output=True, text=True, check=False, env=self.env)
        self.assertEqual(session_state.tab_states(str(self.repo))[0]["state"], IDLE)
        subprocess.run([sys.executable, str(SRC / "hook.py"), "agent", "opencode", "tab-x",
                        str(os.getpid())], input=json.dumps({"event": "session.idle"}),
                       capture_output=True, text=True, check=False, env=self.env)
        self.assertEqual([t["id"] for t in session_state.tab_states(str(self.repo))], ["tab-1"])

    def run_claude_hook(self, event, pid):
        """Runs the Claude hook main in a child process, with the claude pid fixed to `pid`."""
        driver = ("import sys, hook\n"
                  "from unittest import mock\n"
                  f"mock.patch.object(hook, '_claude_pid', return_value={int(pid)}).start()\n"
                  "hook.main()\n")
        proc = subprocess.run([sys.executable, "-c", driver], cwd=str(SRC), input=json.dumps(event),
                              capture_output=True, text=True, check=False, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def claude_event(self, name, **fields):
        return dict(hook_event_name=name, session_id="11111111-2222-3333-4444-555555555555",
                    cwd=str(self.repo), **fields)

    def test_claude_hook_stores_blocked_on_a_permission_request(self):
        self.run_claude_hook(self.claude_event("PermissionRequest", tool_name="Bash"), os.getpid())
        tab = session_state.tab_states(str(self.repo))[0]
        self.assertEqual((tab["state"], tab["harness"]), (BLOCKED, "claude"))

    def test_claude_hook_turns_a_finished_turn_into_done_and_a_prompt_into_working(self):
        self.run_claude_hook(self.claude_event("UserPromptSubmit"), os.getpid())
        self.assertEqual(session_state.tab_states(str(self.repo))[0]["state"], WORKING)
        self.run_claude_hook(self.claude_event("Stop", last_assistant_message="pronto"), os.getpid())
        self.assertEqual(session_state.tab_states(str(self.repo))[0]["state"], DONE)

    def test_claude_hook_with_its_agent_gone_reads_unknown(self):
        self.run_claude_hook(self.claude_event("UserPromptSubmit"), dead_pid())
        self.assertEqual(session_state.tab_states(str(self.repo))[0]["state"], UNKNOWN)

    def test_claude_hook_keeps_the_session_file_with_the_same_vocabulary(self):
        self.run_claude_hook(self.claude_event("Stop", last_assistant_message="pronto"), os.getpid())
        ficha = json.loads((self.state / "sessions" / "11111111-2222-3333-4444-555555555555.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(ficha["state"], DONE)


if __name__ == "__main__":
    unittest.main()
