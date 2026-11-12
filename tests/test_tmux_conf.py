"""Regression tests for the hooks that keep the layout fitted to the active client, and for the
single owner of the geometry: `layout_apply` is the only place that resizes, zooms or refreshes.

The phone (SSH) layout is applied by `workspace fit`, which the tmux hooks call. Switching repo
windows resizes the new window but fires no client hook, so without a window/session hook the phone
kept the desktop pane layout squeezed into its width (no zoom) until some client event happened.
These tests read config/tmux.conf and bin/workspace; they need no tmux server.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "config" / "tmux.conf"

# Events that must re-apply the layout: client events (attach/resize/switch/focus) and window events
# (tmux followed the latest client, or the current window changed).
FIT_EVENTS = (
    "client-attached",
    "client-resized",
    "client-active",
    "client-focus-in",
    "window-resized",
    "session-window-changed",
)

HOOK_RE = re.compile(r'^set-hook -g (\S+) (?:\{(.*)\}|"(.*)")\s*$')


def hooks(conf):
    """event -> list of hook commands, in file order (duplicates visible)."""
    found = {}
    for line in conf.splitlines():
        match = HOOK_RE.match(line.strip())
        if match:
            command = match.group(2) if match.group(2) is not None else match.group(3)
            found.setdefault(match.group(1), []).append(command.strip())
    return found


class FitHooksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conf = CONF.read_text(encoding="utf-8")
        cls.hooks = hooks(cls.conf)

    def test_window_size_follows_the_last_used_client(self):
        self.assertIn("set -g window-size latest", self.conf)

    def test_every_relevant_event_runs_one_fit(self):
        for event in FIT_EVENTS:
            with self.subTest(event=event):
                self.assertIn(event, self.hooks, f"{event} must call fit")
                self.assertEqual(len(self.hooks[event]), 1, f"{event} must have one fit hook")

    def test_fit_hooks_run_in_the_background_for_the_profile(self):
        for event in FIT_EVENTS:
            for command in self.hooks[event]:
                with self.subTest(event=event, command=command):
                    self.assertTrue(command.startswith("run-shell -b "), command)
                    self.assertIn('"${WS_DIR}/bin/workspace" fit "#{@ws_profile}"', command)
                    self.assertIn(">/dev/null 2>&1", command)

    def test_fit_hooks_are_identical(self):
        commands = {command for event in FIT_EVENTS for command in self.hooks[event]}
        self.assertEqual(len(commands), 1, commands)


class StatusBarProfileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conf = CONF.read_text(encoding="utf-8")

    def test_statusbar_receives_session_profile_with_fallback_to_window_profile(self):
        self.assertIn(
            '"${WS_PYTHON:-python3}" "${WS_DIR}/src/statusbar.py" "#{@ws_repo}" '
            '"#{?#{@ws_session_profile},#{@ws_session_profile},#{@ws_profile}}"',
            self.conf,
        )


class InterpreterQuotingTest(unittest.TestCase):
    """The tmux server may start without Homebrew on PATH, and WS_DIR may contain a space: every
    interpreter is the one exported by the launcher and every path is passed as one shell word."""

    @classmethod
    def setUpClass(cls):
        cls.conf = CONF.read_text(encoding="utf-8")
        cls.lines = cls.conf.splitlines()

    def commands(self):
        return [line for line in self.lines if not line.lstrip().startswith("#")]

    def test_python_spawns_use_the_launcher_interpreter(self):
        spawns = [line for line in self.commands() if "python3" in line]
        self.assertTrue(spawns)
        for line in spawns:
            with self.subTest(line=line):
                self.assertIn('"${WS_PYTHON:-python3}"', line)
                rest = line.replace('"${WS_PYTHON:-python3}"', "")
                self.assertNotRegex(rest, r"\bpython3\b", "spawn the launcher interpreter, not a bare python3")

    def test_every_ws_dir_is_quoted(self):
        occurrences = list(re.finditer(r"\$\{WS_DIR\}", self.conf))
        self.assertTrue(occurrences)
        for match in occurrences:
            start = match.start()
            with self.subTest(context=self.conf[max(0, start - 40):start + 20]):
                self.assertIn(self.conf[start - 1], "\"'",
                              "quote ${WS_DIR} inside run-shell and #() commands")

    def test_run_shell_commands_quote_the_shell_string(self):
        # run-shell/bind commands are parsed again when the key or hook runs: the shell command is a
        # single quoted tmux argument whose inner quotes reach /bin/sh.
        for line in self.commands():
            if "run-shell" not in line:
                continue
            with self.subTest(line=line):
                self.assertRegex(line, r"run-shell(?: -b)? '")
                self.assertNotRegex(line, r'run-shell(?: -b)? "\$')


class MinimumTmuxVersionTest(unittest.TestCase):
    """The features above the minimum are guarded, not removed: modern tmux keeps them."""

    @classmethod
    def setUpClass(cls):
        cls.conf = CONF.read_text(encoding="utf-8")

    def guarded_block(self, marker):
        lines = self.conf.splitlines()
        for index, line in enumerate(lines):
            if line.strip() != "%if" and not line.strip().startswith("%if "):
                continue
            block = [line]
            for inner in lines[index + 1:]:
                block.append(inner)
                if inner.strip() == "%endif":
                    break
            if any(marker in item for item in block):
                return "\n".join(block)
        self.fail(f"{marker!r} is not inside a %if guard")

    def test_header_declares_the_minimum_versions(self):
        header = self.conf.split("set -g default-terminal", 1)[0]
        self.assertIn("Minimum tmux: 3.2", header)
        self.assertRegex(header, r"popup[^\n]*3\.2")
        self.assertRegex(header, r"pane-border-lines[^\n]*3\.3")
        self.assertRegex(header, r"MouseUp1Status[^\n]*3\.4")

    def test_border_options_are_guarded_and_present(self):
        block = self.guarded_block("set -g pane-border-lines heavy")
        self.assertIn("pane-border-indicators off", block)
        self.assertIn("#{version}", block)

    def test_popup_is_guarded_and_present(self):
        block = self.guarded_block("bind h { display-popup")
        self.assertIn("display-popup", block)
        self.assertIn("#{version}", block)

    def test_status_range_click_is_guarded_and_present(self):
        block = self.guarded_block("bind -n MouseUp1Status")
        self.assertIn("mouse_status_range", block)
        self.assertIn("#{version}", block)


class HarnessCommandTest(unittest.TestCase):
    """The hook command must quote its script path and use the interpreter exported by the launcher."""

    COMMAND = '"$HOME/.local/share/ai-tmux-workspace/security/gate.py"'

    def commands(self, settings):
        return [hook["command"]
                for groups in settings["hooks"].values()
                for group in groups
                for hook in group["hooks"]]

    def test_render_uses_ws_python_and_quotes_it(self):
        harness = ROOT / "security" / "gate.py"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "harness"
            directory.mkdir()
            (directory / "forcado.yaml").write_text("itens: []\n", encoding="utf-8")
            target = Path(tmp) / "settings.json"
            env = dict(os.environ, WS_PYTHON="/opt/My Py/python3")
            proc = subprocess.run(
                [sys.executable, str(harness), "render", "--harness", str(directory),
                 "--out", str(target)],
                env=env, capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for command in self.commands(json.loads(target.read_text(encoding="utf-8"))):
                with self.subTest(command=command):
                    self.assertTrue(command.startswith("'/opt/My Py/python3' "), command)
                    self.assertTrue(command.endswith(" " + self.COMMAND), command)

    def test_static_settings_quotes_the_script_path(self):
        settings = json.loads((ROOT / "security" / "settings.json").read_text(encoding="utf-8"))
        for command in self.commands(settings):
            with self.subTest(command=command):
                self.assertTrue(command.endswith(" " + self.COMMAND), command)


BIN = Path(__file__).resolve().parent.parent / "bin" / "workspace"
FUNC_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{")


def functions(text):
    """name -> body of every top-level bash function (the file closes each with `}` at column 0)."""
    blocks, name, body = {}, None, []
    for line in text.splitlines():
        if name is None:
            match = FUNC_RE.match(line)
            if not match:
                continue
            if line.count("{") <= line.count("}"):   # one-line function
                blocks[match.group(1)] = line
                continue
            name, body = match.group(1), []
            continue
        if line == "}":
            blocks[name] = "\n".join(body)
            name = None
        else:
            body.append(line)
    return blocks


class LayoutOwnerTest(unittest.TestCase):
    """One single owner of the geometry: a session action never resizes tmux."""

    @classmethod
    def setUpClass(cls):
        cls.text = BIN.read_text(encoding="utf-8")
        cls.functions = functions(cls.text)

    def test_layout_apply_replaced_the_old_helpers(self):
        self.assertIn("layout_apply", self.functions)
        self.assertNotIn("mobile_apply", self.functions)
        self.assertNotIn("fit_window", self.functions)

    def test_only_layout_apply_resizes_zooms_or_refreshes(self):
        for name, body in self.functions.items():
            if name == "layout_apply":
                continue
            for command in ("resize-pane", "select-pane -Z", "refresh-client"):
                with self.subTest(function=name, command=command):
                    self.assertNotIn(command, body)

    def test_layout_apply_owns_every_geometry_change(self):
        body = self.functions["layout_apply"]
        for command in ("resize-pane", "select-pane -Z", "refresh-client", "@ws_mzoom"):
            self.assertIn(command, body)

    def test_session_paths_do_not_reapply_the_layout(self):
        for name in ("tab_show", "tab_new", "tab_exit", "tab_step", "session_new", "session_exit",
                     "session_run", "open_sessions", "session_restore", "side", "left"):
            with self.subTest(function=name):
                self.assertNotIn("layout_apply", self.functions[name])

    def test_tab_switch_keeps_the_phone_zoom_without_resizing(self):
        # -Z keeps the window zoomed, so the phone stays inside the geometry applied by layout_apply
        self.assertIn("swap-pane -dZ", self.functions["tab_show"])

    def test_only_the_entries_that_change_the_target_apply_the_layout_again(self):
        for name in ("build_window", "ensure_window", "open_repo", "mview", "fit"):
            with self.subTest(function=name):
                self.assertIn("layout_apply", self.functions[name])

    def test_fit_applies_once_without_repeating_passes(self):
        body = self.functions["fit"]
        self.assertIn("layout_apply", body)
        self.assertNotIn("again", body)
        self.assertNotIn("sleep", body)

    def test_strips_are_signalled_instead_of_respawned(self):
        for name in ("tab_render", "side", "left"):
            with self.subTest(function=name):
                self.assertIn("panel_signal", self.functions[name])
        self.assertNotIn("respawn-pane", self.functions["tab_render"])

    def test_reload_does_not_refresh_the_client(self):
        self.assertNotIn("refresh-client", self.functions["reload"])


class RedrawOnReloadTest(unittest.TestCase):
    """reload redraws the whole screen through layout_apply; fit and ensure_window stay silent."""

    @classmethod
    def setUpClass(cls):
        cls.functions = functions(BIN.read_text(encoding="utf-8"))

    def test_reload_asks_layout_apply_for_a_redraw(self):
        self.assertIn('layout_apply "$W" redraw', self.functions["reload"])

    def test_reload_redraws_after_panels_and_before_doctor(self):
        body = self.functions["reload"]
        panels_at = body.index('panels "$profile"')
        redraw_at = body.index('layout_apply "$W" redraw')
        doctor_at = body.index("doctor --warn")
        self.assertLess(panels_at, redraw_at)
        self.assertLess(redraw_at, doctor_at)

    def test_redraw_is_only_requested_by_reload(self):
        for name in ("fit", "ensure_window"):
            with self.subTest(function=name):
                self.assertNotIn("redraw", self.functions[name])

    def test_layout_apply_refreshes_every_client_only_when_asked(self):
        body = self.functions["layout_apply"]
        self.assertIn('-n "$redraw"', body)
        self.assertIn("list-clients", body)
        self.assertNotIn("refresh-client -S", body)


class BuildWindowTest(unittest.TestCase):
    """A new repo window is mounted off screen and only revealed once it is assembled, so no pane or
    placeholder respawn is seen appearing one at a time."""

    @classmethod
    def setUpClass(cls):
        cls.text = BIN.read_text(encoding="utf-8")
        cls.functions = functions(cls.text)

    def test_build_window_creates_the_window_hidden(self):
        self.assertIn("new-window -d", self.functions["build_window"])

    def test_build_window_selects_only_after_sessions_layout_and_shells(self):
        body = self.functions["build_window"]
        select = body.index("select-window")
        for step in ("open_sessions", "layout_apply", "shells_start"):
            with self.subTest(step=step):
                self.assertLess(body.index(step), select, f"{step} must run before select-window")

    def test_build_window_with_dash_d_stays_hidden(self):
        body = self.functions["build_window"]
        self.assertRegex(body, r'\[ -n "\$d" \] && return')
        self.assertLess(body.index(r'[ -n "$d" ] && return'), body.index("select-window"))


class SessionFastFailTest(unittest.TestCase):
    """A session that dies right after launch (bad settings, missing binary) must not relaunch the
    default profile every few seconds: the last one leaves the placeholder and a status warning."""

    @classmethod
    def setUpClass(cls):
        cls.text = BIN.read_text(encoding="utf-8")
        cls.functions = functions(cls.text)

    def test_session_run_reports_how_long_the_agent_lived(self):
        body = self.functions["session_run"]
        self.assertIn('started="$SECONDS"', body)
        self.assertRegex(body, r'session-exit "\$TMUX_PANE" "\$\(\(SECONDS - started\)\)"')

    def test_unknown_profile_reports_zero_despite_the_sleep(self):
        body = self.functions["session_run"]
        self.assertIn('session-exit "$TMUX_PANE" 0', body)

    def test_dispatch_forwards_the_new_arguments(self):
        self.assertIn('session-exit) session_exit "$2" "${3:-}" ;;', self.text)
        self.assertIn('session-run) session_run "$2" "$3" "$4" "${5:-}" ;;', self.text)

    def test_last_session_guard_runs_before_reopening_the_default(self):
        body = self.functions["session_exit"]
        self.assertIn("SESSION_MIN_SECONDS", body)
        self.assertLess(body.index("SESSION_MIN_SECONDS"), body.index("session_new"))
        self.assertIn("session.failed_fast", body)
        self.assertIn("session_placeholder.py", body)

    def test_guard_only_applies_to_the_main_slot(self):
        body = self.functions["session_exit"]
        self.assertEqual(body.count("SESSION_MIN_SECONDS"), 1, body)
        self.assertEqual(body.count("session.failed_fast"), 1, body)

    def test_threshold_lives_in_a_single_symbol(self):
        self.assertEqual(self.text.count("SESSION_MIN_SECONDS=10"), 1)
        users = [name for name, body in self.functions.items() if "SESSION_MIN_SECONDS" in body]
        self.assertEqual(users, ["session_exit"], users)


class FitLockTest(unittest.TestCase):
    """`fit` takes its lock before the first tmux call: a hook burst must not open one connection to
    the server each (that is how a resize loop piled up processes and exhausted the server's fds)."""

    @classmethod
    def setUpClass(cls):
        cls.body = functions(BIN.read_text(encoding="utf-8"))["fit"]

    def test_takes_the_lock_before_any_tmux_call(self):
        first_tmux = re.search(r"(?m)^\s*tmux\b", self.body)
        self.assertIsNotNone(first_tmux, self.body)
        self.assertIn('mkdir "$lock"', self.body)
        self.assertLess(self.body.index('mkdir "$lock"'), first_tmux.start())

    def test_stale_lock_is_removed_and_the_mkdir_retried_once(self):
        self.assertIn("kill -0", self.body)
        first = self.body.index('mkdir "$lock"')
        second = self.body.index('mkdir "$lock"', first + 1)
        self.assertLess(self.body.index('rm -rf "$lock"', first), second)

    def test_trap_releases_the_lock_before_the_server_is_contacted(self):
        trap = 'trap \'rm -rf "$lock"\' EXIT'
        self.assertIn(trap, self.body)
        self.assertLess(self.body.index(trap), self.body.index("has-session"))


# Fake tmux: logs the subcommand and answers only what `fit` needs, with an empty window list (no
# layout_apply). TMUX_SLEEP widens the window the lock holder keeps, TMUX_NO_SESSION fakes no server.
FAKE_TMUX = """#!/bin/sh
cmd="$1"
[ "$1" = "-L" ] && cmd="$3"
printf '%s\\n' "$cmd" >> "$TMUX_LOG"
case "$cmd" in
  has-session) sleep "${TMUX_SLEEP:-0}"; [ -z "${TMUX_NO_SESSION:-}" ] || exit 1 ;;
  display) printf '/tmp/fake-tmux.sock,1,0\\n' ;;
esac
exit 0
"""


class FitBurstTest(unittest.TestCase):
    """50 concurrent fits reach the server at most once; the lock is always released on exit."""

    PROFILE = "fit-burst"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        bindir = self.root / "bin"
        bindir.mkdir()
        self.log = self.root / "tmux.log"
        fake = bindir / "tmux"
        fake.write_text(FAKE_TMUX, encoding="utf-8")
        fake.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                        TMPDIR=self.tmp.name, TMUX_LOG=str(self.log))
        self.env.pop("TMUX", None)
        self.lock = self.root / f"ws-fit-{self.PROFILE}-{os.getuid()}"

    def test_a_burst_of_concurrent_fits_opens_one_connection(self):
        procs = [subprocess.Popen([str(BIN), "fit", self.PROFILE], env=dict(self.env, TMUX_SLEEP="1.5"),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                 for _ in range(50)]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=30), 0)
        calls = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(calls.count("has-session"), 1, calls)
        self.assertEqual(calls[0], "has-session", calls)
        self.assertFalse(self.lock.exists())

    def test_lock_is_released_when_the_server_does_not_exist(self):
        subprocess.run([str(BIN), "fit", self.PROFILE], env=dict(self.env, TMUX_NO_SESSION="1"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        self.assertEqual(self.log.read_text(encoding="utf-8").splitlines(), ["has-session"])
        self.assertFalse(self.lock.exists())


if __name__ == "__main__":
    unittest.main()
