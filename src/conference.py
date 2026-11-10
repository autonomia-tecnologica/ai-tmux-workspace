#!/usr/bin/env python3
"""Conference mode: a shared log between sibling repos and one isolated Claude run per repo.

Everything happens on the options screen (`ctrl+b o`): `converse` shows the log and updates it live
while it is open; the footer is the message box, Enter sends the text to every participant (starting
the conference with the window repo and its siblings when none is active: `siblings` and `parent_folder` in
the config; sharing a `groups` folder alone is not enough). Closing the screen ends the conference
(Ctrl+X explicitly, Esc by leaving the screen), and there is no persistence: the next conference
starts with an empty log. `say` appends Rafael's message and starts, for every participant, a
separate headless Claude (`conference_worker.py`) in that repo: the window's interactive session is
never touched, and each answer goes straight into the log. In that mode the agent changes nothing
except its own repo's `BACKLOG.md`, and only when Rafael asks in the message for something to be
recorded; every other file is read-only. It answers in at most one paragraph. Each participant
receives only Rafael's message, never the previous ones, but may read the sibling repos for the
cross-validation between repos.

Participants answer only about their own repo: a message that is not theirs or is addressed to
another participant gets no log entry (the worker replies with the `NO_ANSWER` sentinel). The prompt
lists the participants and tells each run which one it is. The conference is a quick analysis, not
long work: a tool budget (`MAX_TOOL_STEPS`, `MAX_FILES_READ`, enforced by the workspace hook
`hook.py` through `conference_budget.py`) stops the run from turning into a long analysis, and the
agent then answers with what it already knows and says a deeper analysis needs more time. The only
write the conference accepts is the repo's own `BACKLOG.md`, on Rafael's request: the hook denies
any other edit. The backlog is still what Rafael decides. While a participant works, the screen
header shows what it is doing (`status` in the active state: "analisando…", "lendo docs/x.md",
"buscando conflito").

The module also owns the session requests (`request` commands): any session may ask another repo or
the home to do something, but nothing is read, incorporated or executed before the person approves
it in a tmux popup on the receiving agent. A request declares its type: `conversation` only hands
the text to the receiving agent after approval, `execution` also runs the command after approval.
The popup waits two minutes and silence is a refusal: a refused or expired request leaves no effect.
The receiving side is driven by `hook.py` (it shows the popup on Stop/SessionStart and delivers an
approved conversation on the next prompt).

This module is stdlib-only at import time: it is imported by the worker itself. The heavier modules
(lib, help, viewer, i18n) are imported inside the screen and command functions only.
"""
import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import fcntl
except ImportError:     # no fcntl (Windows): updates are not serialized
    fcntl = None

BASE = Path(__file__).resolve().parent.parent
NO_ANSWER = "<<NO_ANSWER>>"      # worker stdout sentinel: the message is not about that repo
MAX_TOOL_STEPS = 20                 # tool steps one participant may take before it must answer
MAX_FILES_READ = 12                 # distinct files one participant may read in a run
COUNTED_TOOLS = ("Read", "Grep", "Glob")     # read tools the budget counts as a step
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")  # only the repo's BACKLOG.md may be edited


def state_dir():
    """STATE/conference, following WS_STATE (same default as lib.STATE)."""
    return Path(os.environ.get("WS_STATE", Path.home() / ".cache" / "workspace")) / "conference"


def active_path():
    return state_dir() / "active.json"


def log_path():
    return state_dir() / "log.jsonl"


def _lock_path():
    return state_dir() / ".lock"


@contextlib.contextmanager
def _lock():
    """Exclusive lock shared by the commands and the hook (concurrent Stops included)."""
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with open(_lock_path(), "a") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield    # closing the file releases the lock


def _alive(pid):
    """Whether `pid` is still running. Anything we cannot check counts as alive, so a bad read
    never throws away a conference that is still on screen."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def read_active():
    """Active conference state, or {} when there is none (or it cannot be read).

    A conference lives while its screen does: closing the screen process (Esc, the popup or the
    window being closed) ends it, so the state left behind by a dead screen reads as inactive."""
    try:
        data = json.loads(active_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    if "screen" in data and not _alive(data["screen"]):
        return {}
    return data


def _write_active(state):
    active_path().parent.mkdir(parents=True, exist_ok=True)
    tmp = active_path().with_name(f"active.json.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, active_path())
    finally:
        tmp.unlink(missing_ok=True)


def read_log():
    """Log entries in file order; an unreadable log reads as empty."""
    try:
        lines = log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _append(entry):
    log_path().parent.mkdir(parents=True, exist_ok=True)
    with open(log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def first_paragraph(text):
    """Text up to the first blank line, stripped; no artificial length limit."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    return re.split(r"\n[ \t]*\n", text, maxsplit=1)[0].strip()


def _sender(repo):
    return os.path.basename(str(repo).rstrip("/")) or str(repo)


def _short(text, limit=32):
    """One-line status for the header, clipped so a long path does not swallow the title."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _t(key, **kw):
    import i18n
    return i18n.t(key, **kw)


def _default_repos(lib, repo=None):
    """`repo` (default: the current window's repo) plus its siblings, as defined by the config:
    `siblings` written on a `groups` entry and every repo inside `parent_folder`. Sharing a `groups`
    folder is not siblinghood. The repo itself participates, first in the list; [] when it has no
    siblings, because a conference with only the repo it was opened from makes no sense."""
    base = lib.expand_path(repo or lib.current_repo())
    siblings = [p for p in lib.siblings(base) if p != base]
    return [base] + siblings if siblings else []


def start(repos=None, repo=None, screen=None):
    """Starts a conference with the given repos, or with `repo` (default: the current window's repo)
    and its config siblings. `screen` is the pid of the process showing the conference screen: when
    it goes away, the conference ends with it."""
    import lib

    if not lib.is_claude_harness():
        print(_t("conference.opencode", profile=lib.agent_name()), file=sys.stderr)
        return 2
    want = [lib.expand_path(p) for p in (repos or [])]
    if not want:
        want = _default_repos(lib, repo)
        if not want:
            print(_t("conference.no_repos"), file=sys.stderr)
            return 2
    ordered = []
    for path in want:
        if not os.path.isdir(path):
            print(_t("cli.not_exists", path=path), file=sys.stderr)
            return 2
        if path not in ordered:
            ordered.append(path)
    with _lock():
        active = read_active()
        if active and read_log():   # a conference with a live screen is never replaced by accident
            print(_t("conference.already_active"), file=sys.stderr)
            return 2
        # no persistence: a new conference starts empty; the previous log is kept only as a file
        log_path().parent.mkdir(parents=True, exist_ok=True)
        if log_path().exists() and log_path().stat().st_size:
            archive = log_path().with_name(
                f"log-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.jsonl")
            os.replace(log_path(), archive)
        log_path().write_text("", encoding="utf-8")
        _write_active({"repos": ordered, "awaiting": {}, "status": {}, "created": time.time(),
                       "screen": screen or os.getpid()})
    print(_t("conference.started", repos=", ".join(_sender(p) for p in ordered)))
    return 0


def _launch_answer(repo, prompt, message_id, created, others=None):
    """Starts one separate headless Claude for `repo` (never the window's interactive session).

    The prompt goes through a temporary file, not argv, so it never has to fit a command line;
    `others` are the sibling repos the worker may read for the cross-validation. The worker is
    detached so the screen keeps updating while it thinks, and it writes the answer itself;
    nothing here waits for it. An answer that arrives after the screen was closed finds no active
    conference and is discarded (`conference_worker.answer`)."""
    file = None
    try:
        folder = state_dir() / "prompts"
        folder.mkdir(parents=True, exist_ok=True)
        fd, file = tempfile.mkstemp(dir=str(folder), prefix="msg-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        argv = [sys.executable, str(BASE / "src" / "conference_worker.py"),
                repo, file, message_id, str(created)]
        argv += [str(p) for p in (others or []) if str(p) != repo]
        subprocess.Popen(
            argv,
            cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        if file:
            try:
                os.unlink(file)
            except OSError:
                pass
        return False


def say(text, launch=None):
    """Appends Rafael's message to the log and starts one isolated Claude per participant, each with
    the log up to now. The answers arrive in the log as the workers finish."""
    text = str(text or "").strip()
    if not text:
        print(_t("conference.say_empty"), file=sys.stderr)
        return 2
    with _lock():
        active = read_active()
        if not active:
            print(_t("conference.not_active"), file=sys.stderr)
            return 2
        repos = [r for r in active.get("repos") or [] if isinstance(r, str)]
        created = active.get("created") or time.time()
        message_id = f"{time.time_ns()}-{os.getpid()}"      # unique even for two sends in one ms
        _append({"ts": time.time(), "sender": "rafael", "id": message_id, "text": text})
        awaiting = dict(active.get("awaiting") or {})
        status = dict(active.get("status") or {})
        thinking = _t("conference.status.thinking")
        for repo in repos:
            awaiting[repo] = message_id
            status[repo] = thinking       # shown in the header until the worker reports a step
        active["awaiting"] = awaiting
        active["status"] = status
        _write_active(active)

    spawner = launch or _launch_answer
    participants = ", ".join(f"{_sender(p)} ({p})" for p in repos)
    failed = []
    for repo in repos:
        # Only Rafael's message, no history; each run is told who the participants are and which one
        # it is, so it can stay silent when the message is addressed to another participant. The
        # budget is also in the prompt, but the hook is what enforces it.
        prompt = _t("conference.prompt", text=text, no_answer=NO_ANSWER,
                    participants=participants, you=_sender(repo),
                    max_files=MAX_FILES_READ, max_steps=MAX_TOOL_STEPS)
        try:
            ok = spawner(repo, prompt, message_id, created, others=repos)
        except Exception:
            ok = False
        if not ok:
            failed.append(repo)
    if failed:      # a repo whose Claude did not start will not answer: stop waiting on it
        with _lock():
            active = read_active()
            if active:
                awaiting = dict(active.get("awaiting") or {})
                status = dict(active.get("status") or {})
                for repo in failed:
                    awaiting.pop(repo, None)
                    status.pop(repo, None)
                active["awaiting"] = awaiting
                active["status"] = status
                _write_active(active)
        for repo in failed:
            print(_t("conference.send_failed", repo=repo), file=sys.stderr)
    print(_t("conference.said", n=len(repos) - len(failed)))
    return 0 if not failed else 1


def stop():
    """Closes the active conference; the log and the repos' files are left untouched."""
    with _lock():
        if not read_active():
            print(_t("conference.not_active"), file=sys.stderr)
            return 2
        active_path().unlink(missing_ok=True)
    print(_t("conference.stopped", path=str(log_path())))
    return 0


def format_log(entries):
    """Markdown for help.render: one heading per message with sender and time."""
    if not entries:
        return _t("conference.log_empty")
    lines = []
    for entry in entries:
        when = time.strftime("%H:%M", time.localtime(float(entry.get("ts") or 0)))
        sender = entry.get("sender") or "?"
        who = _t("conference.rafael") if sender == "rafael" else sender
        lines += [f"## {when} · {who}", "", str(entry.get("text") or ""), ""]
    return "\n".join(lines).strip()


def _printed(fn, *args, **kwargs):
    """Runs a conference action in-process and returns what it printed (stdout and stderr)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        fn(*args, **kwargs)
    return out.getvalue().strip()


def _screen_frame(state):
    """One frame of the conference screen: (header rows, body rows, number of log entries)."""
    import help
    from viewer import DIM, Row, R, popup_header, term_cols

    active = read_active()
    entries = read_log()
    cols = term_cols()
    if active:
        repos = ", ".join(_sender(r) for r in active.get("repos") or [] if isinstance(r, str))
        title = _t("conference.screen.title", repos=repos)
        awaiting = active.get("awaiting") or {}
        status = active.get("status") or {}
        parts = []
        for repo in awaiting:
            if not isinstance(repo, str):
                continue
            doing = status.get(repo)
            parts.append(f"{_sender(repo)} ({_short(doing)})" if doing else _sender(repo))
        if parts:
            title += _t("conference.screen.waiting", repos=", ".join(parts))
        header = popup_header(title, _t("conference.screen.keys"), cols)
        text = format_log(entries)
    else:
        header = popup_header(_t("conference.screen.title_inactive"),
                              _t("conference.screen.keys_inactive"), cols)
        text = _t("conference.not_active")
    body = [Row()]
    if active:
        body += [Row(f" {DIM}{_t('conference.screen.hint')}{R}"), Row()]
    if state["notice"]:
        body += [Row(f" {DIM}{state['notice']}{R}"), Row()]
    return header, body + help.render(text, cols), len(entries)


def converse(repo=None):
    """Interactive conference screen, everything on screen: the log updates live and the footer is the
    message box. Enter sends the text to every participant, starting the conference with `repo` (the
    window's repo) and its config siblings first when none is active. Closing the screen ends the
    conference: Ctrl+X explicitly, Esc by leaving it. The log is kept. Returns when Esc closes it."""
    from viewer import ORANGE, R, Viewer, close_on_esc

    with _lock():      # an open screen owns the conference: closing it closes the conference too
        active = read_active()
        if active:
            active["screen"] = os.getpid()
            _write_active(active)

    state = {"notice": "", "seen": -1, "text": ""}

    def collect(v):
        header, body, count = _screen_frame(state)
        if count != state["seen"]:      # new message: keep the end of the log in view
            state["seen"] = count
            v.top = 10 ** 6
        return header, body

    def footer(_v):
        return f"{R}{ORANGE}{_t('conference.compose')}{R} {state['text']}█"

    def on_input(v, k):
        if k == "esc" or k.startswith("\x1b"):     # Esc, arrows and PgUp/PgDn stay with the viewer
            return False
        if k == "\x18":                            # Ctrl+X ends the conference
            state["notice"] = _printed(stop)
            v.redraw.set()
            return True
        if k in ("\r", "\n"):
            text = state["text"].strip()
            if text:
                state["text"] = ""
                if not read_active():
                    notice = _printed(start, repo=repo, screen=os.getpid())
                    if not read_active():          # no siblings: keep what was typed
                        state["text"] = text
                        state["notice"] = notice
                        v.redraw.set()
                        return True
                state["notice"] = _printed(say, text)
                v.redraw.set()
            return True
        if k in ("\x7f", "\x08"):                  # Backspace
            state["text"] = state["text"][:-1]
            v.redraw.set()
            return True
        if k.isprintable():
            state["text"] += k
            v.redraw.set()
        return True                                # other control keys are ignored

    Viewer(collect, interval=1.0, on_key=close_on_esc, on_input=on_input,
           footer=footer).run()
    if read_active():      # Esc closes the screen, and the conference goes with it
        _printed(stop)
    return 0


# --- Session requests: one session asks another (or the home), the person approves ------------------
# A request is one JSON file under STATE/requests. It stays `pending` until the person decides in the
# receiving agent's tmux popup (or through the CLI, which is the same decision). The deadline is part
# of the file: a request past it is `expired` and never has an effect, and the popup itself refuses
# when two minutes pass without an answer. Only an approved `execution` runs its command; an approved
# `conversation` is handed to the receiving agent on its next prompt, never before.
REQUEST_TIMEOUT = 120            # seconds the popup waits for the person; silence is a refusal
HOME = "home"                    # receiver name for the session that owns the workspace code
REQUEST_TYPES = {"conversa": "conversation", "conversation": "conversation",
                 "execucao": "execution", "execution": "execution"}
SCRIPT = Path(__file__).resolve()


def requests_dir():
    """STATE/requests, following WS_STATE (same default as lib.STATE)."""
    return Path(os.environ.get("WS_STATE", Path.home() / ".cache" / "workspace")) / "requests"


def _request_path(request_id):
    return requests_dir() / f"{request_id}.json"


@contextlib.contextmanager
def _requests_lock():
    """Exclusive lock for the request files (sender, popup and hook may touch them at once)."""
    folder = requests_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / ".lock", "a") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield    # closing the file releases the lock


def _valid_id(request_id):
    return isinstance(request_id, str) and bool(request_id) and request_id not in (".", "..") \
        and not any(c in request_id for c in "/\\\0")


def _write_request(request):
    path = _request_path(request["id"])
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_request(request_id):
    """One request by id, or {} when it does not exist (or cannot be read)."""
    if not _valid_id(request_id):
        return {}
    try:
        data = json.loads(_request_path(request_id).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _all_requests():
    """Every readable request, oldest first."""
    folder = requests_dir()
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return []
    out = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and _valid_id(data.get("id")):
            out.append(data)
    return sorted(out, key=lambda r: float(r.get("created") or 0))


def _norm(path):
    return os.path.realpath(os.path.expanduser(str(path or "")))


def _is_home(path):
    return _norm(path) == str(BASE)


def _who(sender):
    """Short name shown to the person: `home` for the workspace root, else the repo folder name."""
    return HOME if _is_home(sender) else _sender(sender)


def _same_receiver(target, receiver):
    if target == HOME:
        return _is_home(receiver)
    return bool(target) and _norm(target) == _norm(receiver)


def _expire(request, now):
    """Marks a pending request past its deadline as expired (no effect) and writes it back."""
    if request.get("status") == "pending" and float(request.get("expires") or 0) <= now:
        request.update(status="expired", decided=now)
        _write_request(request)
    return request


def send_request(target, request_type, text, command=None, sender=None, now=None):
    """Queues a request for `target` (a repo path or `home`); returns (returncode, request)."""
    text = str(text or "").strip()
    kind = REQUEST_TYPES.get(str(request_type or "").strip().lower())
    if not text or kind is None:
        return 2, {}
    receiver = HOME if str(target or "").strip().lower() == HOME else _norm(target)
    if receiver != HOME and not os.path.isdir(receiver):
        return 2, {}
    argv = [str(part) for part in (command or [])]
    if kind == "execution" and not argv:      # an execution without a command would do nothing
        return 2, {}
    if kind == "conversation":                # a conversation never carries a command
        argv = []
    now = time.time() if now is None else float(now)
    request = {
        "id": f"{time.time_ns()}-{os.getpid()}", "type": kind,
        "from": _norm(sender or os.getcwd()), "to": receiver, "text": text,
        "command": argv, "created": now, "expires": now + REQUEST_TIMEOUT,
        "status": "pending", "decided": None,
    }
    with _requests_lock():
        _write_request(request)
    return 0, request


def pending_requests(receiver=None, now=None):
    """Pending requests, oldest first: for `receiver` when given, else for every receiver.

    Reading expires what is past the deadline, so a request nobody answered never stays runnable."""
    now = time.time() if now is None else float(now)
    out = []
    with _requests_lock():
        for request in _all_requests():
            request = _expire(request, now)
            if request.get("status") != "pending":
                continue
            if receiver is not None and not _same_receiver(request.get("to"), receiver):
                continue
            out.append(request)
    return out


def _pid(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def claim_request(request_id, pid, now=None):
    """Reserves a pending request for the popup of `pid`; False when another live popup has it."""
    now = time.time() if now is None else float(now)
    with _requests_lock():
        request = read_request(request_id)
        if not request:
            return False
        request = _expire(request, now)
        if request.get("status") != "pending":
            return False
        other = _pid((request.get("asking") or {}).get("pid"))
        if other and other != _pid(pid) and _alive(other):
            return False
        request["asking"] = {"pid": _pid(pid), "since": now}
        _write_request(request)
        return True


def _target_dir(request):
    """Directory the approved execution runs in: the receiving repo, or the workspace root for home."""
    if request.get("to") == HOME:
        return str(BASE)
    return str(request.get("to") or BASE)


def _run_command(command, cwd):
    """Runs an approved command and returns (code, output). Never raises."""
    try:
        proc = subprocess.run([str(part) for part in command], cwd=cwd, capture_output=True,
                              text=True, timeout=300, stdin=subprocess.DEVNULL)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as error:
        return 1, str(error)


def apply_request(request_id, run=None):
    """Runs the command of an approved execution exactly once; a conversation has nothing to run."""
    request = read_request(request_id)
    if not request or request.get("status") != "approved" or request.get("applied"):
        return request
    if request.get("type") != "execution":
        return request
    runner = run or _run_command
    try:
        code, output = runner(request.get("command") or [], _target_dir(request))
    except Exception as error:
        code, output = 1, str(error)
    with _requests_lock():
        request = read_request(request_id)
        if not request or request.get("status") != "approved" or request.get("applied"):
            return request      # another decision applied it first
        request["applied"] = time.time()
        request["result"] = {"code": int(code), "output": str(output or "")[-2000:]}
        _write_request(request)
    return request


def decide_request(request_id, approved, now=None, expired=False, run=None):
    """Records the person's decision. Only an approved execution has an effect.

    `expired` forces the refusal path of the deadline; a request already decided is not touched."""
    now = time.time() if now is None else float(now)
    with _requests_lock():
        request = read_request(request_id)
        if not request:
            return 2, {}
        if request.get("status") != "pending":
            return 2, request
        if expired or now >= float(request.get("expires") or 0):
            request.update(status="expired", decided=now)
            _write_request(request)
            return 2, request
        request.update(status="approved" if approved else "refused", decided=now)
        _write_request(request)
    if request.get("status") == "approved" and request.get("type") == "execution":
        request = apply_request(request_id, run=run)
    return 0, request


def conversation_context(receiver=None, now=None):
    """Approved conversation requests for `receiver`, handed over once (empty when there is none)."""
    receiver = receiver or os.getcwd()
    now = time.time() if now is None else float(now)
    lines = []
    with _requests_lock():
        for request in _all_requests():
            if request.get("type") != "conversation" or request.get("status") != "approved":
                continue
            if request.get("delivered"):
                continue
            if not _same_receiver(request.get("to"), receiver):
                continue
            request["delivered"] = now
            _write_request(request)
            lines.append(_t("request.context", sender=_who(request.get("from")),
                            text=request.get("text") or ""))
    return "\n\n".join(lines)


def render_request(request):
    """Text of the approval popup: who asks, what for, and the command an execution would run."""
    lines = [_t("request.ask.title", sender=_who(request.get("from"))), "",
             _t("request.ask.from", sender=_who(request.get("from")),
                to=_who(request.get("to")), seconds=REQUEST_TIMEOUT),
             _t("request.type." + str(request.get("type") or "conversation")), ""]
    if request.get("type") == "execution":
        lines += [_t("request.ask.command",
                     command=" ".join(shlex.quote(str(p)) for p in request.get("command") or [])), ""]
    lines += [str(request.get("text") or ""), "", _t("request.ask.keys", seconds=REQUEST_TIMEOUT)]
    return "\n".join(lines)


def _read_key(timeout):
    """One key from stdin within `timeout` seconds: "" on end of input, None on timeout."""
    try:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except Exception:
        return ""
    try:
        tty.setcbreak(fd)
        ready, _, _ = select.select([fd], [], [], max(0.0, timeout))
        if not ready:
            return None
        data = os.read(fd, 1)
        return data.decode("utf-8", "replace") if data else ""
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass


def ui(request_id, read_key=None, clock=time.monotonic, out=print, run=None):
    """The popup itself: shows the request and decides it on a/r/Esc, refusing after two minutes."""
    request = read_request(request_id)
    if not request or request.get("status") != "pending":
        out(_t("request.not_pending", id=request_id))
        return 2
    out(render_request(request))
    reader = read_key or _read_key
    deadline = clock() + REQUEST_TIMEOUT
    while True:
        left = deadline - clock()
        if left <= 0:                       # silence is a refusal
            decide_request(request_id, False, expired=True, run=run)
            out(_t("request.ask.expired"))
            return 0
        key = reader(left)
        if key is None:
            continue
        if key in ("a", "A", "\r", "\n"):
            _, request = decide_request(request_id, True, run=run)
            out(_t("request.ask.approved") if request.get("status") == "approved"
                else _t("request.ask.expired"))
            return 0
        if key in ("", "r", "R", "q", "Q", "\x1b"):
            decide_request(request_id, False, run=run)
            out(_t("request.ask.refused"))
            return 0


def _tmux_client():
    """The tmux client showing the receiving pane, "" when there is none (no popup possible)."""
    try:
        import tmux
        pane = os.environ.get("TMUX_PANE")
        if not pane:
            return ""
        session = tmux.display("#{session_name}", target=pane)
        if not session:
            return ""
        clients = tmux.run("list-clients", "-t", session, "-F", "#{client_name}")
        return clients.splitlines()[0].strip() if clients.strip() else ""
    except Exception:
        return ""


def _popup(command, cwd, client):
    """Opens `command` in a tmux popup on `client`, blocking until the person closes it."""
    import tmux
    args = ["display-popup", "-E", "-w", "70%", "-h", "40%"]
    if client:
        args += ["-c", client]
    args += ["-d", cwd, command]
    tmux.run(*args, timeout=None)


def ask(request_id, popup=None, client=None):
    """Opens the approval popup for one pending request (run detached by the receiving hook)."""
    request = read_request(request_id)
    if not request or request.get("status") != "pending":
        return 2
    client = _tmux_client() if client is None else client
    if not client:              # no attached client: no popup, and the request expires on its own
        return 2
    if not claim_request(request_id, os.getpid()):
        return 2
    command = (f"{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT))} "
               f"request ui {shlex.quote(request_id)}")
    opener = popup or _popup
    try:
        opener(command, _target_dir(request), client)
    except Exception:
        pass
    request = read_request(request_id)
    if request.get("status") == "pending":      # popup gone without a decision: silence refuses
        decide_request(request_id, False, expired=True)
    return 0


def _detach(argv):
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def spawn_approval(receiver=None, spawn=None):
    """Receiver side: starts one detached popup for the first pending request of `receiver`.

    Called by the hook, so it never blocks and never fails. No pane (or a conference worker) means
    no popup; the request then expires without effect."""
    if os.environ.get("WS_CONFERENCE_REPO") or os.environ.get("WS_CONFERENCE_BUDGET"):
        return False
    if not os.environ.get("TMUX_PANE"):
        return False
    pending = pending_requests(receiver or os.getcwd())
    if not pending:
        return False
    request = pending[0]
    pid = _pid((request.get("asking") or {}).get("pid"))
    if pid and _alive(pid):
        return False
    spawner = spawn or _detach
    try:
        return bool(spawner([sys.executable, str(SCRIPT), "request", "ask", request["id"]]))
    except Exception:
        return False


def _decision_text(returncode, request, request_id):
    """Prints the outcome of a decision; returns the process exit code."""
    if not request:
        print(_t("request.not_found", id=request_id), file=sys.stderr)
        return 2
    status = request.get("status")
    if status == "approved":
        print(_t("request.approved", id=request_id))
        result = request.get("result") or {}
        if request.get("type") == "execution" and result:
            key = "request.executed" if result.get("code") == 0 else "request.execute_failed"
            print(_t(key, id=request_id, code=result.get("code")))
        return 0
    if status == "refused":
        print(_t("request.refused", id=request_id))
        return 0
    if status == "expired":
        print(_t("request.expired", id=request_id), file=sys.stderr)
        return 1
    print(_t("request.already_decided", id=request_id, status=status), file=sys.stderr)
    return 1


def request_command(args):
    """`conference.py request ...`: the CLI behind `workspace request` and the popup."""
    if not args:
        args = ["inbox"]
    sub, rest = args[0], args[1:]
    if sub == "send":
        if "--" in rest:
            cut = rest.index("--")
            head, command = rest[:cut], rest[cut + 1:]
        else:
            head, command = rest, []
        if len(head) < 3:
            print(_t("request.usage"), file=sys.stderr)
            return 2
        code, request = send_request(head[0], head[1], " ".join(head[2:]), command=command)
        if code:
            print(_t("request.usage"), file=sys.stderr)
            return code
        print(_t("request.sent", id=request["id"], to=_who(request["to"]),
                 seconds=REQUEST_TIMEOUT))
        return 0
    if sub == "inbox":
        receiver = rest[0] if rest else os.getcwd()
        pending = pending_requests(receiver)
        if not pending:
            print(_t("request.inbox_empty"))
            return 0
        for request in pending:
            print(_t("request.inbox_line", id=request["id"],
                     type=_t("request.type." + str(request.get("type"))),
                     sender=_who(request.get("from")), text=request.get("text") or ""))
        return 0
    if sub in ("approve", "refuse"):
        if not rest:
            print(_t("request.usage"), file=sys.stderr)
            return 2
        code, request = decide_request(rest[0], sub == "approve")
        return _decision_text(code, request, rest[0])
    if sub == "ask":
        return ask(rest[0]) if rest else 2
    if sub == "ui":
        return ui(rest[0]) if rest else 2
    print(_t("request.usage"), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(request_command(sys.argv[1:]) if sys.argv[1:2] == ["request"] else 2)
