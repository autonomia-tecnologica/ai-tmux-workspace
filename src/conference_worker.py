#!/usr/bin/env python3
"""One isolated Claude run that answers a conference message for one repo.

Started detached by `conference.say`, never by a person: `conference.py <repo> <prompt file>
<message id> <conference created>`. It runs the configured agent headless (`claude -p`) with the
repo as cwd, so the window's interactive session is neither used nor disturbed, captures the first
paragraph of the answer and appends it to the shared log.

The run speaks only about its own repo, but may read the sibling repos: the conference is a
cross-validation between repos. When the message is not its subject, the model answers with the
`NO_ANSWER` sentinel and nothing is written to the log: not every participant has to say something.
While the run works, its tool steps update the repo's `status` in the active state, which the
conference screen shows in the header ("analisando…", "lendo docs/x.md", "buscando conflito").

The run is read-only except for the repo's own `BACKLOG.md`: Edit is allowed, and the workspace hook
denies any edit whose target is not that file, so code and every other file stay untouched. The
backlog item is written only when Rafael asks for it in the message. Bash/Write/NotebookEdit/Task
are also disallowed, and sessions are not persisted (they must not show up in the session panel). The
prompt carries only Rafael's message, never the previous ones: there is no history to read. The
workspace hook (`hook.py`, registered in the user's Claude Code settings) reads the run's
environment and caps tool steps and distinct files read (`conference_budget.py`); when the budget
runs out the agent is told to answer with what it knows, and `--max-turns` stays above that budget
only as a last-resort exit. Never raises: a failed run still clears the waiting mark and leaves a
visible note in the log.
"""
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import conference  # noqa: E402  (stdlib-only at import time, by design)

TIMEOUT_S = 300
NO_ANSWER = conference.NO_ANSWER      # the prompt's sentinel: the message is not about this repo
MAX_TURNS = conference.MAX_TOOL_STEPS + 4     # last-resort exit; the hook budget comes first

# Everything not listed here needs a permission request, which a headless run denies. Edit is the
# single write allowed: the workspace hook (`conference_budget.py`) denies it unless the target is
# the repo's own BACKLOG.md, and Write and the tools that can change files indirectly are denied
# outright. Deny rules win over any allow rule from the user's settings.
ALLOWED_TOOLS = "Read,Grep,Glob,Edit"
DISALLOWED_TOOLS = "Bash,Task,NotebookEdit,MultiEdit,Write,WebFetch,WebSearch"


def _harness_dir():
    """Installed harness directory: what the harness hook itself reads (same rule as lib)."""
    return Path(os.environ.get(
        "AT_HARNESS_DIR",
        Path.home() / ".local" / "share" / "ai-tmux-workspace" / "harness")).expanduser()


def answer_command(others=None):
    """Command line of the isolated conference agent, with the configured profile and flags.

    A shell string, like the profile itself is written in the config: the profile may carry `env`
    prefixes, `~` paths and quotes, which only a shell expands. `others` are the sibling repos,
    added as extra readable directories: the conference is a cross-validation between repos. The
    installed harness is readable too when it exists: its gate denies a write until one of its
    files is read in the session, and the `BACKLOG.md` write must be able to satisfy it. The
    stream is parsed line by line so the screen can show what the run is doing while it works."""
    import lib

    system = _t("conference.system_prompt", no_answer=NO_ANSWER)
    readable = [str(p) for p in (others or [])]
    harness = _harness_dir()
    if harness.is_dir() and str(harness) not in readable:
        readable.append(str(harness))
    dirs = "".join(f"--add-dir {shlex.quote(p)} " for p in readable)
    return (f"{lib.agent_launch_command()} -p --output-format stream-json --verbose "
            f"--no-session-persistence --permission-mode default --max-turns {MAX_TURNS} {dirs}"
            f"--append-system-prompt {shlex.quote(system)} "
            f"--allowedTools {shlex.quote(ALLOWED_TOOLS)} "
            f"--disallowedTools {shlex.quote(DISALLOWED_TOOLS)}")


def _clip(text, limit=32):
    """One-line label for the header; long paths and patterns keep the message readable."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _short_path(repo, path):
    """Path relative to the repo when possible; the basename for anything outside it."""
    path = str(path or "")
    try:
        rel = os.path.relpath(path, repo) if path else ""
    except (TypeError, ValueError):
        rel = path
    if not rel or rel.startswith(".."):
        rel = os.path.basename(path)
    return _clip(rel or "?")


def _status_label(repo, name, data):
    """Header label for the tool steps worth showing; "" for everything else."""
    data = data if isinstance(data, dict) else {}
    if name == "Read":
        return _t("conference.status.reading", path=_short_path(repo, data.get("file_path")))
    if name == "Grep":
        return _t("conference.status.searching", pattern=_clip(data.get("pattern")))
    if name == "Glob":
        return _t("conference.status.looking", pattern=_clip(data.get("pattern")))
    return ""


def _set_status(repo, message_id, created, text):
    """Publishes the current step for the screen; a stale worker never touches a newer message."""
    if not text:
        return
    with conference._lock():
        active = conference.read_active()
        if not active or str(active.get("created")) != str(created):
            return
        awaiting = active.get("awaiting") or {}
        if awaiting.get(repo) != message_id:
            return
        status = dict(active.get("status") or {})
        if status.get(repo) == text:
            return
        status[repo] = text
        active["status"] = status
        conference._write_active(active)


def _stream_answer(stream, repo, on_status=None):
    """Final text of a stream-json run, reporting its tool steps through `on_status`."""
    final = ""
    for line in stream:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else []
            for block in content or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                label = _status_label(repo, block.get("name"), block.get("input"))
                if label and on_status:
                    on_status(label)
        elif kind == "result" and not event.get("is_error"):
            final = str(event.get("result") or "")
    return final


def _budget_state():
    """Fresh state-file path for one run; the hook creates it on the first counted step."""
    folder = conference.estado_dir() / "budget"
    folder.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(dir=str(folder), prefix="run-", suffix=".json")
    os.close(fd)
    os.unlink(path)
    return path


def _execute(cmd, repo, prompt, on_status=None):
    """(answer, stderr, returncode) of the headless run; the prompt comes from stdin.

    A watchdog kills the whole process group at TIMEOUT_S. stderr goes to a temporary file so a
    talkative child cannot fill the pipe while stdout is being read. The repo and the budget state
    file go in the child's environment, so the PreToolUse hook sees them (the repo check must hold
    even when the budget file cannot be created), and the file is removed afterwards."""
    try:
        budget = _budget_state()
    except OSError:
        budget = None
    env = dict(os.environ, WS_CONFERENCE_REPO=str(repo))
    if budget:
        env.update(WS_CONFERENCE_BUDGET=budget,
                   WS_CONFERENCE_MAX_STEPS=str(conference.MAX_TOOL_STEPS),
                   WS_CONFERENCE_MAX_FILES=str(conference.MAX_FILES_READ))
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as err_file:
            proc = subprocess.Popen(cmd, shell=True, cwd=repo, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=err_file, text=True,
                                    env=env, start_new_session=True)
            timer = threading.Timer(TIMEOUT_S, _kill_group, args=(proc,))
            timer.start()
            try:
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                out = _stream_answer(proc.stdout, repo, on_status)
                code = proc.wait()
                err_file.seek(0)
                err = err_file.read()
            finally:
                timer.cancel()
                if proc.poll() is None:      # an unexpected failure must not leave the child running
                    _kill_group(proc)
    finally:
        if budget:
            for path in (budget, budget + ".lock"):
                try:
                    os.unlink(path)
                except OSError:
                    pass
    return out, err, code


def _kill_group(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _t(key, **kw):
    import i18n
    return i18n.t(key, **kw)


def _save_error(repo, code, detail):
    """Keeps the failure out of the log shown to Rafael but available for diagnosis."""
    try:
        pasta = conference.estado_dir()
        pasta.mkdir(parents=True, exist_ok=True)
        with open(pasta / "errors.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {conference._sender(repo)} "
                    f"(code {code}): {' '.join(str(detail or '').split())[:500]}\n")
    except OSError:
        pass


def answer(repo, prompt, message_id, created, execute=None, others=None):
    """Runs the isolated agent and appends its first paragraph to the log. Returns True when an
    answer was written.

    `others` are the sibling repos the run may read for the cross-validation. A `NO_ANSWER` output
    means the message is not about this repo: nothing is appended.
    An answer is only written while that same conference is still active: a late worker must not
    fall into a conference started after this one. A failed run writes a visible note instead of
    leaving Rafael waiting; the details go to errors.log, never into the log that feeds the next
    prompt."""
    def on_status(text):
        _set_status(repo, message_id, created, text)

    try:
        if execute:      # tests inject the whole run
            out, err, code = execute(answer_command(others), repo, prompt)
        else:
            out, err, code = _execute(answer_command(others), repo, prompt, on_status)
    except Exception as exc:     # timeout, missing executable, ...
        out, err, code = "", str(exc), 1
    silent = code == 0 and NO_ANSWER in (out or "")
    first = "" if silent else conference.first_paragraph(out)
    if not first and not silent:
        _save_error(repo, code, err or out)
    with conference._lock():
        active = conference.read_active()
        if not active or str(active.get("created")) != str(created):
            return False
        text = first or ("" if silent else _t("conference.answer_failed"))
        if text:
            conference._append({"ts": time.time(), "sender": conference._sender(repo),
                                "repo": repo, "reply_to": message_id, "text": text})
        awaiting = dict(active.get("awaiting") or {})
        status = dict(active.get("status") or {})
        if awaiting.get(repo) == message_id:
            awaiting.pop(repo, None)
            status.pop(repo, None)
            active["awaiting"] = awaiting
            active["status"] = status
            conference._write_active(active)
    return bool(first)


def main(argv):
    if len(argv) < 5:
        return 2
    _, repo, prompt_file, message_id, created = argv[:5]
    others = argv[5:]
    try:
        prompt = Path(prompt_file).read_text(encoding="utf-8")
    except OSError:
        return 2
    finally:
        try:
            os.unlink(prompt_file)
        except OSError:
            pass
    answer(repo, prompt, message_id, created, others=others)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
