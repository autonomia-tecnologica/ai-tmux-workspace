#!/usr/bin/env python3
"""One isolated agent run that answers a conference message for one repo.

Started detached by `conference.say`, never by a person: `conference.py <repo> <prompt file>
<message id> <conference created>`. It runs the window profile headless (`claude -p` or
`opencode run`) with the repo as cwd, so the window's interactive session is neither used nor
disturbed, captures the first paragraph of the answer and appends it to the persistent log.

The run speaks only about its own repo and only from its own `README.md` and `BACKLOG.md`, and reads
them only when Rafael asks. The prompt carries the whole conference log (capped by
`conference.MAX_PROMPT_CHARS`) as untrusted data, never as instructions. The run has no siblings,
no installed harness, no `Grep`/`Glob`, no `Bash`, no subagent and no extra turns: `Read` and
`Edit` are the only tools, and a worker hook denies any path other than the two permitted files.
The file budget is two distinct files and a few tool steps; when it runs out the hook tells the
agent to answer with what it already knows.

`BACKLOG.md` is edited directly, only when Rafael asks. A `README.md` edit becomes a proposal: the
hook captures the diff and denies the direct write, and the conference screen shows the diff until
Rafael confirms it (`conference.confirm_pending`). A secret in either permitted file blocks the run
before the prompt reaches the provider; the finding names the file and the pattern, never the value.

Never raises: a failed run still clears the waiting mark and leaves a visible note in the log.
"""
import json
import os
import re
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
MAX_TURNS = 0                          # kept for callers that imported the old constant

# The conference has two tools and two files. Everything else needs a permission request, which a
# headless run denies; the worker hook enforces the path and the README proposal even when a tool
# is listed here by mistake. Deny rules win over any allow rule from the user's settings.
ALLOWED_TOOLS = "Read,Edit"
DISALLOWED_TOOLS = ("Bash,Task,NotebookEdit,MultiEdit,Write,WebFetch,WebSearch,Grep,Glob")


def _active_profile():
    try:
        profile = conference.read_active().get("profile")
    except Exception:
        return ""
    return profile if isinstance(profile, str) else ""


def _settings_path_from_command(command):
    """Path of an explicit `--settings` in a profile command, or None."""
    match = re.search(r"--settings(?:=|\s+)(?:(['\"])(.*?)\1|(\S+))", command or "")
    if not match:
        return None
    return Path(match.group(2) or match.group(3) or "").expanduser()


def _conference_settings(profile, command):
    """Generated settings of the run: the profile/harness settings plus the conference hook.

    The hook is what makes the two-file scope a mechanism: it denies every tool except `Read`/`Edit`
    on the repo's own `README.md`/`BACKLOG.md`, and turns a README edit into a pending diff.
    """
    import lib

    base_path = lib.ensure_profile_settings(profile)
    source = Path(base_path) if base_path else _settings_path_from_command(command)
    if source and source.is_file():
        settings = json.loads(source.read_text(encoding="utf-8"))
    else:
        settings = lib.harness_base_settings()
    if not isinstance(settings, dict):
        settings = {}
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = settings["hooks"] = {}
    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        pre = hooks["PreToolUse"] = []
    command_hook = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} hook"
    pre.append({"hooks": [{"type": "command", "command": command_hook}]})
    folder = conference._ensure_dir(conference.state_dir() / "settings")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(profile or "claude")) or "claude"
    path = folder / f"conference-{safe}.json"
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    conference._chmod(path)
    return path


def _replace_settings(command, path):
    """Point the command at the merged settings, replacing an explicit `--settings` when present."""
    quoted = shlex.quote(str(path))
    if re.search(r"--settings(?:=|\s+)(?:(['\"]).*?\1|\S+)", command or ""):
        return re.sub(r"--settings(?:=|\s+)(?:(['\"]).*?\1|\S+)",
                      f"--settings {quoted}", command, count=1)
    return f"{command} --settings {quoted}"


def _claude_command(profile):
    """Headless Claude run with the profile's settings, the conference hook and only two tools."""
    import lib

    command = lib.profile_command(profile)
    command = _replace_settings(command, _conference_settings(profile, command))
    launch = command if profile == "claude" else f"env ENABLE_TOOL_SEARCH=false {command}"
    system = _t("conference.system_prompt", no_answer=NO_ANSWER,
                max_files=conference.MAX_FILES_READ, max_steps=conference.MAX_TOOL_STEPS)
    return (f"{launch} -p --output-format stream-json --verbose --no-session-persistence "
            f"--permission-mode default "
            f"--append-system-prompt {shlex.quote(system)} "
            f"--allowedTools {shlex.quote(ALLOWED_TOOLS)} "
            f"--disallowedTools {shlex.quote(DISALLOWED_TOOLS)}")


def _opencode_permissions():
    """OpenCode V2 permission rules: everything denied, then the two files allowed.

    `README.md` edits ask instead of allowing: a non-interactive run cannot confirm them, so the
    direct write does not happen. The Claude path turns the same intent into a pending diff.
    """
    return [
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": "external_directory", "resource": "*", "effect": "deny"},
        {"action": "read", "resource": "README.md", "effect": "allow"},
        {"action": "read", "resource": "BACKLOG.md", "effect": "allow"},
        {"action": "edit", "resource": "README.md", "effect": "ask"},
        {"action": "edit", "resource": "BACKLOG.md", "effect": "allow"},
    ]


def _opencode_config(profile):
    """Merged OpenCode config for the run: the profile's config plus the conference permissions."""
    import lib

    settings = {}
    source = lib.profile_config(profile)
    if source:
        try:
            settings = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
    if not isinstance(settings, dict):
        settings = {}
    settings["permissions"] = _opencode_permissions()
    folder = conference._ensure_dir(conference.state_dir() / "settings")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(profile or "opencode")) or "opencode"
    path = folder / f"opencode-conference-{safe}.json"
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    conference._chmod(path)
    return path


def _opencode_command(profile):
    """Headless OpenCode run with the profile's config and the conference permissions."""
    import lib

    command = lib.profile_command(profile)
    config = _opencode_config(profile)
    return (f"env OPENCODE_CONFIG={shlex.quote(str(config))} {command} "
            f"run --format json --archived")


def answer_command(others=None, profile=None):
    """Command line of the isolated conference agent, with the window profile and its flags.

    `others` is accepted for compatibility with the old cross-repo conference and deliberately
    ignored: siblings are out of scope. A Claude profile gets a shell string, like the profile is
    written in the config (it may carry `env` prefixes, `~` paths and quotes). An OpenCode profile
    gets its own command; no Claude flag leaks into it."""
    import lib

    profile = profile or _active_profile() or lib.agent_name()
    if lib.profile_harness(profile) == lib.HARNESS_OPENCODE:
        return _opencode_command(profile)
    return _claude_command(profile)


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


def _opencode_status(repo, part):
    """Header label for an OpenCode `tool_use` part; "" when the tool is not worth showing."""
    part = part if isinstance(part, dict) else {}
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
    tool = str(part.get("tool") or "").lower()
    if tool == "read":
        return _t("conference.status.reading", path=_short_path(repo, tool_input.get("path")))
    if tool == "grep":
        return _t("conference.status.searching", pattern=_clip(tool_input.get("pattern")))
    if tool == "glob":
        return _t("conference.status.looking", pattern=_clip(tool_input.get("pattern")))
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
    """Final text of a Claude stream-json or OpenCode json run, reporting its tool steps."""
    final = ""
    texts = []
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
                texts = []          # text before a tool call is not the final answer
        elif kind == "result" and not event.get("is_error"):
            final = str(event.get("result") or "")
        elif kind == "text":
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
        elif kind == "tool_use":
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            label = _opencode_status(repo, part)
            if label and on_status:
                on_status(label)
            texts = []
    return final or "\n\n".join(texts)


def _budget_state():
    """Fresh state-file path for one run; the hook creates it on the first counted step."""
    folder = conference._ensure_dir(conference.state_dir() / "budget")
    fd, path = tempfile.mkstemp(dir=str(folder), prefix="run-", suffix=".json")
    os.close(fd)
    os.unlink(path)
    return path


def _execute(cmd, repo, prompt, on_status=None, message_id="", profile=""):
    """(answer, stderr, returncode) of the headless run; the prompt comes from stdin.

    A watchdog kills the whole process group at TIMEOUT_S. stderr goes to a temporary file so a
    talkative child cannot fill the pipe while stdout is being read. The repo, the message id and
    the budget state file go in the child's environment, so the worker hook sees them; the file is
    removed afterwards. `WS_CONFERENCE_REPO` is deliberately not set: the old budget hook would
    restrict writes to BACKLOG.md before the README proposal could be captured."""
    try:
        budget = _budget_state()
    except OSError:
        budget = None
    env = dict(os.environ, WS_CONFERENCE_ROOT=str(repo), WS_CONFERENCE_MESSAGE=str(message_id),
               WS_CONFERENCE_PROFILE=str(profile or ""))
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
        folder = conference._ensure_dir(conference.state_dir())
        with open(folder / "errors.log", "a", encoding="utf-8") as f:
            conference._chmod(folder / "errors.log")
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {conference._sender(repo)} "
                    f"(code {code}): {' '.join(str(detail or '').split())[:500]}\n")
    except OSError:
        pass


def _tool_path(repo, tool_input):
    """Absolute resolved path of a tool call, or None when it cannot be resolved."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    value = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(repo) / path
    try:
        return path.resolve()
    except OSError:
        return None


def _budget_reason(data):
    """Step/file budget denial for one permitted call, or None.

    `conference_worker._execute` names the per-run state file in `WS_CONFERENCE_BUDGET`; the
    `conference_budget` hook owns the counting. A missing state file or a broken one never blocks
    the run (fail open), like the rest of the budget."""
    if not os.environ.get("WS_CONFERENCE_BUDGET"):
        return None
    try:
        import conference_budget
        return conference_budget.check(data)
    except Exception:
        return None


def check_tool(data, repo=None):
    """Denial reason for one conference tool call, or None when the call is allowed.

    Read/Edit only, on the repo's own README.md/BACKLOG.md. A README Edit is captured as a pending
    diff and denied; BACKLOG.md is edited directly. Every other tool and path is denied."""
    if not isinstance(data, dict):
        return _t("conference.tool_denied")
    tool = str(data.get("tool_name") or "").strip().lower()
    root = repo or os.environ.get("WS_CONFERENCE_ROOT") or ""
    if not root:
        return _t("conference.tool_denied")
    try:
        base = Path(root).expanduser().resolve()
        allowed = {(base / name).resolve() for name in conference.PERMITTED_FILES}
    except OSError:
        return _t("conference.tool_denied")
    path = _tool_path(base, data.get("tool_input"))
    if tool == "read":
        if path not in allowed:
            return _t("conference.read_denied")
        return _budget_reason(data)
    if tool == "edit":
        if path not in allowed:
            return _t("conference.write_denied")
        budget = _budget_reason(data)
        if budget:
            return budget
        if path.name == "README.md":
            tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
            old = tool_input.get("old_string")
            new = tool_input.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str):
                return _t("conference.write_denied")
            try:
                before = path.read_text(encoding="utf-8")
            except OSError:
                return _t("conference.write_denied")
            if old not in before:
                return _t("conference.readme_stale")
            after = before.replace(old, new, -1 if tool_input.get("replace_all") else 1)
            conference.add_pending(base, path, before, after,
                                   os.environ.get("WS_CONFERENCE_MESSAGE") or "")
            return _t("conference.readme_pending")
        return None
    if tool in ("write", "multiedit", "notebookedit", "patch"):
        return _t("conference.write_denied")
    return _t("conference.tool_denied")


def hook_main():
    """PreToolUse hook entry point: reads one event, emits a denial when the call is out of scope."""
    try:
        data = json.load(sys.stdin)
        reason = check_tool(data)
    except Exception:
        reason = _t("conference.tool_denied")
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}, ensure_ascii=False))


def _blocked(repo, message_id, created, found):
    """Visible note for a secret finding, without the value; clears the waiting mark."""
    file, pattern = found
    with conference._lock():
        active = conference.read_active()
        if not active or str(active.get("created")) != str(created):
            return
        conference._append({"ts": time.time(), "sender": conference.SYSTEM_SENDER, "repo": repo,
                            "text": _t("conference.secret_blocked", repo=conference._sender(repo),
                                       file=file, pattern=pattern)})
        awaiting = dict(active.get("awaiting") or {})
        status = dict(active.get("status") or {})
        workers = dict(active.get("workers") or {})
        if awaiting.get(repo) == message_id:
            awaiting.pop(repo, None)
            status.pop(repo, None)
            workers.pop(repo, None)
            active["awaiting"] = awaiting
            active["status"] = status
            active["workers"] = workers
            conference._write_active(active)


def answer(repo, prompt, message_id, created, execute=None, others=None, profile=None):
    """Runs the isolated agent and appends its first paragraph to the log. Returns True when an
    answer was written.

    A `NO_ANSWER` output means the message is not about this repo: nothing is appended. An answer
    is only written while that same conference generation is still active: a late worker must not
    fall into a conference started after this one or into a `/clear`. A failed run writes a visible
    note instead of leaving Rafael waiting; the details go to errors.log, never into the log that
    feeds the next prompt."""
    def on_status(text):
        _set_status(repo, message_id, created, text)

    found = conference.scan_secrets(repo)
    if found:
        _blocked(repo, message_id, created, found)
        return False
    try:
        command = answer_command(others, profile=profile)
        if execute:      # tests inject the whole run
            out, err, code = execute(command, repo, prompt)
        else:
            out, err, code = _execute(command, repo, prompt, on_status,
                                      message_id=message_id, profile=profile or "")
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
        workers = dict(active.get("workers") or {})
        if awaiting.get(repo) == message_id:
            awaiting.pop(repo, None)
            status.pop(repo, None)
            workers.pop(repo, None)
            active["awaiting"] = awaiting
            active["status"] = status
            active["workers"] = workers
            conference._write_active(active)
    return bool(first)


def main(argv):
    if len(argv) > 1 and argv[1] == "hook":
        hook_main()
        return 0
    if len(argv) < 5:
        return 2
    _, repo, prompt_file, message_id, created = argv[:5]
    try:
        prompt = Path(prompt_file).read_text(encoding="utf-8")
    except OSError:
        return 2
    finally:
        try:
            os.unlink(prompt_file)
        except OSError:
            pass
    answer(repo, prompt, message_id, created)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
