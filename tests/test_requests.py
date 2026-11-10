"""Tests for session requests (item 65): nothing between sessions is read, incorporated or executed
before the person approves it on the receiving agent, and a refusal or an expiry leaves no effect.

Every test points HOME, WS_STATE and AT_WS_CONFIG at a temporary folder, so the real cache and config
are never touched. The popup, its clock and the key reader are injected: no tmux and no subprocess
running the approved command in the tests that care about it.
"""
import contextlib
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
import hook  # noqa: E402
import i18n  # noqa: E402


@contextlib.contextmanager
def without_env(*names):
    """Removes the variables for the block and restores whatever was there before."""
    saved = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class FakeClock:
    """Clock handed to the popup: the values run in order and the last one repeats."""

    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class RequestFixture(unittest.TestCase):
    """Temporary state plus a sender repo and two receiver repos (A and B)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.a = self.tmp / "repo-a"
        self.b = self.tmp / "repo-b"
        self.sender = self.tmp / "repo-sender"
        for folder in (self.a, self.b, self.sender):
            folder.mkdir()
        cfg = self.tmp / "config.yaml"
        cfg.write_text("", encoding="utf-8")
        env = mock.patch.dict(os.environ, {
            "HOME": str(self.tmp / "home"),
            "WS_STATE": str(self.tmp / "state"),
            "AT_WS_CONFIG": str(cfg),
        })
        env.start()
        self.addCleanup(env.stop)

    def path(self, folder):
        return os.path.realpath(str(folder))

    def send(self, target=None, kind="execucao", text="faz isso", command=None, now=None):
        if command is None and kind in ("execucao", "execution"):
            command = [sys.executable, "-c", "print('ran')"]
        code, request = conference.send_request(
            target or self.path(self.b), kind, text, command=command,
            sender=self.path(self.sender), now=now)
        self.assertEqual(code, 0)
        return request

    def marker_command(self, marker):
        return [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]


class SendAndDecideTest(RequestFixture):
    """A request is queued; only an approved execution has an effect, exactly once."""

    def test_an_execution_request_is_only_queued_and_does_not_run(self):
        marker = self.tmp / "ran"
        request = self.send(command=self.marker_command(marker))

        self.assertEqual(request["status"], "pending")
        self.assertIsNone(request["decided"])
        self.assertFalse(request.get("applied"))
        self.assertFalse(marker.exists())
        self.assertEqual([r["id"] for r in conference.pending_requests(self.path(self.b))],
                         [request["id"]])

    def test_approve_runs_the_execution_once_and_records_the_result(self):
        request = self.send()
        calls = []

        def run(command, cwd):
            calls.append((command, cwd))
            return 0, "saida"

        code, decided = conference.decide_request(request["id"], True, run=run)

        self.assertEqual(code, 0)
        self.assertEqual(decided["status"], "approved")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], request["command"])
        self.assertEqual(calls[0][1], self.path(self.b))
        stored = conference.read_request(request["id"])
        self.assertTrue(stored.get("applied"))
        self.assertEqual(stored["result"], {"code": 0, "output": "saida"})

        conference.apply_request(request["id"], run=run)            # already applied: does not run
        code, again = conference.decide_request(request["id"], True, run=run)
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 1)

    def test_the_cli_approve_runs_the_execution(self):
        request = self.send()
        out = io.StringIO()
        with mock.patch.object(conference, "_run_command", return_value=(0, "ok")) as run:
            with contextlib.redirect_stdout(out):
                code = conference.request_command(["approve", request["id"]])

        self.assertEqual(code, 0)
        run.assert_called_once()
        stored = conference.read_request(request["id"])
        self.assertTrue(stored.get("applied"))
        self.assertEqual(stored["result"], {"code": 0, "output": "ok"})
        self.assertIn(i18n.t("request.executed", id=request["id"], code=0), out.getvalue())

    def test_refuse_does_not_run_and_leaves_no_effect(self):
        marker = self.tmp / "ran"
        request = self.send(command=self.marker_command(marker))
        calls = []
        code, decided = conference.decide_request(
            request["id"], False, run=lambda command, cwd: calls.append((command, cwd)))

        self.assertEqual(code, 0)
        self.assertEqual(decided["status"], "refused")
        self.assertEqual(calls, [])
        self.assertFalse(marker.exists())
        stored = conference.read_request(request["id"])
        self.assertFalse(stored.get("applied"))
        self.assertNotIn("result", stored)

    def test_an_execution_past_the_deadline_expires_and_is_not_run(self):
        request = self.send(now=1000.0)
        self.assertEqual(conference.REQUEST_TIMEOUT, 120)
        self.assertEqual(request["expires"], 1000.0 + conference.REQUEST_TIMEOUT)
        calls = []
        late = request["expires"] + 1

        code, decided = conference.decide_request(
            request["id"], True, now=late, run=lambda command, cwd: calls.append(command))

        self.assertEqual(code, 2)
        self.assertEqual(decided["status"], "expired")
        self.assertEqual(calls, [])
        stored = conference.read_request(request["id"])
        self.assertEqual(stored["status"], "expired")
        self.assertFalse(stored.get("applied"))
        self.assertNotIn("result", stored)

    def test_reading_pending_requests_expires_what_is_past_the_deadline(self):
        request = self.send(now=1000.0)

        self.assertEqual(conference.pending_requests(self.path(self.b), now=1121.0), [])
        self.assertEqual(conference.read_request(request["id"])["status"], "expired")

    def test_a_pending_execution_is_not_applied_without_a_decision(self):
        request = self.send()
        calls = []

        conference.apply_request(request["id"], run=lambda command, cwd: calls.append(command))

        self.assertEqual(calls, [])
        self.assertFalse(conference.read_request(request["id"]).get("applied"))


class ConversationTest(RequestFixture):
    """An approved conversation is handed to the receiving agent once; nothing else is ever delivered."""

    def test_an_approved_conversation_is_delivered_once(self):
        request = self.send(kind="conversa", text="mensagem para o repo")
        self.assertEqual(conference.conversation_context(self.path(self.b)), "")   # still pending

        self.assertEqual(conference.decide_request(request["id"], True)[0], 0)
        first = conference.conversation_context(self.path(self.b))
        self.assertIn("mensagem para o repo", first)
        self.assertIn(conference._who(self.path(self.sender)), first)
        self.assertEqual(conference.conversation_context(self.path(self.b)), "")
        self.assertTrue(conference.read_request(request["id"]).get("delivered"))

    def test_pending_refused_and_expired_conversations_are_never_delivered(self):
        self.send(kind="conversa", text="pendente")
        refused = self.send(kind="conversa", text="recusada")
        expired = self.send(kind="conversa", text="expirada", now=1000.0)
        conference.decide_request(refused["id"], False)
        conference.decide_request(expired["id"], True, now=1121.0)

        text = conference.conversation_context(self.path(self.b))

        self.assertEqual(text, "")
        for word in ("pendente", "recusada", "expirada"):
            self.assertNotIn(word, text)

    def test_an_approved_conversation_never_runs_a_command(self):
        request = self.send(kind="conversa", text="só texto",
                            command=[sys.executable, "-c", "print('nunca')"])
        calls = []

        code, decided = conference.decide_request(
            request["id"], True, run=lambda command, cwd: calls.append(command))

        self.assertEqual(code, 0)
        self.assertEqual(decided["command"], [])
        self.assertEqual(calls, [])
        self.assertNotIn("result", conference.read_request(request["id"]))


class ReceiverScopeTest(RequestFixture):
    """A request only shows up for its receiver: the home or the exact repo, never another repo."""

    def test_a_request_is_visible_only_to_its_receiver(self):
        to_b = self.send(target=self.path(self.b), text="para b")
        to_home = self.send(target=conference.HOME, text="para casa")

        self.assertEqual(conference.pending_requests(self.path(self.a)), [])
        self.assertEqual([r["id"] for r in conference.pending_requests(self.path(self.b))],
                         [to_b["id"]])
        self.assertEqual([r["id"] for r in conference.pending_requests(str(conference.BASE))],
                         [to_home["id"]])
        self.assertEqual(to_home["to"], conference.HOME)
        self.assertEqual(conference._target_dir(to_home), str(conference.BASE))

    def test_spawn_approval_uses_only_the_receivers_request(self):
        to_b = self.send(target=self.path(self.b))
        self.send(target=conference.HOME)
        calls = []

        def spawn(argv):
            calls.append(argv)
            return True

        with without_env("WS_CONFERENCE_REPO", "WS_CONFERENCE_BUDGET"), \
                mock.patch.dict(os.environ, {"TMUX_PANE": "%1"}):
            self.assertFalse(conference.spawn_approval(self.path(self.a), spawn=spawn))
            self.assertEqual(calls, [])
            self.assertTrue(conference.spawn_approval(self.path(self.b), spawn=spawn))

        self.assertEqual(len(calls), 1)
        self.assertIn("ask", calls[0])
        self.assertIn(to_b["id"], calls[0])


class PopupTest(RequestFixture):
    """The popup itself: `a` approves, `r`/Esc refuse, silence expires and refuses."""

    def test_the_popup_approves_on_a(self):
        request = self.send(kind="conversa", text="aprovar")
        out = []
        code = conference.ui(request["id"], read_key=lambda timeout: "a", out=out.append)

        self.assertEqual(code, 0)
        self.assertEqual(conference.read_request(request["id"])["status"], "approved")
        self.assertTrue(any(i18n.t("request.ask.approved") == line for line in out))

    def test_the_popup_refuses_on_r_and_esc(self):
        for key in ("r", "\x1b"):
            with self.subTest(key=key):
                request = self.send(kind="conversa", text="recusar")
                out = []
                code = conference.ui(request["id"], read_key=lambda timeout, k=key: k,
                                     out=out.append)
                self.assertEqual(code, 0)
                self.assertEqual(conference.read_request(request["id"])["status"], "refused")
                self.assertTrue(any(i18n.t("request.ask.refused") == line for line in out))

    def test_the_popup_expires_and_refuses_after_the_deadline(self):
        request = self.send(kind="conversa", text="sem resposta")
        out = []
        clock = FakeClock(0.0, conference.REQUEST_TIMEOUT + 1)
        code = conference.ui(request["id"], read_key=lambda timeout: self.fail("read a key"),
                             clock=clock, out=out.append)

        self.assertEqual(code, 0)
        self.assertEqual(conference.read_request(request["id"])["status"], "expired")
        self.assertTrue(any(i18n.t("request.ask.expired") == line for line in out))

    def test_claim_request_keeps_one_live_popup(self):
        request = self.send(kind="conversa")
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (other.kill(), other.wait()))

        self.assertTrue(conference.claim_request(request["id"], other.pid))
        self.assertFalse(conference.claim_request(request["id"], os.getpid()))
        self.assertTrue(conference.claim_request(request["id"], other.pid))
        self.assertEqual(conference.read_request(request["id"])["status"], "pending")

    def test_a_decided_request_cannot_be_claimed(self):
        request = self.send(kind="conversa")
        conference.decide_request(request["id"], False)
        self.assertFalse(conference.claim_request(request["id"], os.getpid()))

    def test_ask_opening_the_popup_on_a_repo_and_closing_it_refuses(self):
        request = self.send(kind="conversa")
        calls = []

        def popup(command, cwd, client):
            calls.append((command, cwd, client))

        self.assertEqual(conference.ask(request["id"], popup=popup, client="client-1"), 0)

        self.assertEqual(len(calls), 1)
        self.assertIn("request ui", calls[0][0])
        self.assertIn(request["id"], calls[0][0])
        self.assertEqual(calls[0][1], self.path(self.b))
        self.assertEqual(calls[0][2], "client-1")
        self.assertEqual(conference.read_request(request["id"])["status"], "expired")

    def test_ask_without_a_client_opens_nothing_and_leaves_the_request_pending(self):
        request = self.send(kind="conversa")
        calls = []
        self.assertEqual(conference.ask(request["id"], popup=lambda *a: calls.append(a),
                                        client=""), 2)
        self.assertEqual(calls, [])
        self.assertEqual(conference.read_request(request["id"])["status"], "pending")


class HookTest(RequestFixture):
    """The receiving hook: delivers only approved conversations and stays out of conference runs."""

    def test_request_context_prints_only_the_approved_conversation(self):
        self.send(kind="conversa", text="pendente")
        approved = self.send(kind="conversa", text="aprovada")
        refused = self.send(kind="conversa", text="recusada")
        conference.decide_request(approved["id"], True)
        conference.decide_request(refused["id"], False)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hook._request_context({"cwd": self.path(self.b)})

        payload = json.loads(out.getvalue())
        text = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("aprovada", text)
        self.assertNotIn("pendente", text)
        self.assertNotIn("recusada", text)

        again = io.StringIO()
        with contextlib.redirect_stdout(again):
            hook._request_context({"cwd": self.path(self.b)})
        self.assertEqual(again.getvalue(), "")            # handed over once

    def test_request_context_is_silent_without_an_approved_conversation(self):
        self.send(kind="conversa", text="pendente")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hook._request_context({"cwd": self.path(self.b)})
        self.assertEqual(out.getvalue(), "")

    def test_request_popup_stays_out_of_conference_runs(self):
        for name in ("WS_CONFERENCE_REPO", "WS_CONFERENCE_BUDGET"):
            with self.subTest(name=name):
                with mock.patch.dict(os.environ, {name: "x"}), \
                        mock.patch.object(conference, "spawn_approval") as spawn:
                    hook._request_popup({"cwd": self.path(self.b)})
                spawn.assert_not_called()

    def test_request_popup_spawns_the_approval_on_the_normal_path(self):
        with without_env("WS_CONFERENCE_REPO", "WS_CONFERENCE_BUDGET"), \
                mock.patch.object(conference, "spawn_approval", return_value=True) as spawn:
            hook._request_popup({"cwd": self.path(self.b)})
        spawn.assert_called_once_with(self.path(self.b))


class CliTest(RequestFixture):
    """`workspace request send` refuses an execution with no command after `--`."""

    def test_send_without_a_command_is_refused_with_usage(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = conference.request_command(["send", self.path(self.b), "execucao", "faz"])

        self.assertEqual(code, 2)
        self.assertIn(i18n.t("request.usage"), err.getvalue())
        self.assertEqual(conference.pending_requests(self.path(self.b)), [])

    def test_send_with_an_empty_command_is_refused_with_usage(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = conference.request_command(["send", self.path(self.b), "execucao", "faz", "--"])

        self.assertEqual(code, 2)
        self.assertIn(i18n.t("request.usage"), err.getvalue())
        self.assertEqual(conference.pending_requests(self.path(self.b)), [])

    def test_cli_send_queues_the_execution_without_running_it(self):
        marker = self.tmp / "ran"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = conference.request_command(
                ["send", self.path(self.b), "execucao", "cria a pasta", "--",
                 *self.marker_command(marker)])

        self.assertEqual(code, 0)
        self.assertFalse(marker.exists())
        pending = conference.pending_requests(self.path(self.b))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["type"], "execution")
        self.assertEqual(pending[0]["command"], self.marker_command(marker))
        self.assertIn(i18n.t("request.sent", id=pending[0]["id"], to="repo-b",
                             seconds=conference.REQUEST_TIMEOUT), out.getvalue())


if __name__ == "__main__":
    unittest.main()
