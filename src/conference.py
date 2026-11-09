#!/usr/bin/env python3
"""Conference mode: a shared log between sibling repos and one isolated Claude run per repo.

Everything happens on the options screen (`ctrl+b o`): `converse` shows the log and updates it live
while it is open; the footer is the message box, Enter sends the text to every participant (starting
the conference with the window repo and its siblings when none is active: `siblings` and `parent_folder` in
the config; sharing a `groups` folder alone is not enough). Closing the screen ends the conference
(Ctrl+X explicitly, Esc by leaving the screen), and there is no persistence: the next conference
starts with an empty log. `say` appends Rafael's message and starts, for every participant, a
separate headless Claude (`conference_worker.py`) in that repo: the window's interactive session is
never touched, and each answer goes straight into the log. In that mode the agent is read-only, and
it answers in at most one paragraph. Each participant receives only Rafael's message, never the
previous ones, but may read the sibling repos for the cross-validation between repos.

Participants answer only about their own repo: a message that is not theirs or is addressed to
another participant gets no log entry (the worker replies with the `NO_ANSWER` sentinel). The prompt
lists the participants and tells each run which one it is. The conference is a quick analysis, not
long work: a tool budget (`MAX_TOOL_STEPS`, `MAX_FILES_READ`, enforced by the workspace hook
`hook.py` through `conference_budget.py`) stops the run from turning into a long analysis, and the
agent then answers with what it already knows and says a deeper analysis needs more time. The
backlog is never written here: it is only what Rafael decides. While a participant works, the
screen header shows what it is doing (`status` in the active state: "analisando…", "lendo
docs/x.md", "buscando conflito").

This module is stdlib-only at import time: it is imported by the worker itself. The heavier modules
(lib, help, viewer, i18n) are imported inside the screen and command functions only.
"""
import contextlib
import io
import json
import os
import re
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
NO_ANSWER = "<<SEM_RESPOSTA>>"      # worker stdout sentinel: the message is not about that repo
MAX_TOOL_STEPS = 20                 # tool steps one participant may take before it must answer
MAX_FILES_READ = 12                 # distinct files one participant may read in a run
COUNTED_TOOLS = ("Read", "Grep", "Glob")     # the tools the budget counts as a step


def estado_dir():
    """ESTADO/conference, following WS_ESTADO (same default as lib.ESTADO)."""
    return Path(os.environ.get("WS_ESTADO", Path.home() / ".cache" / "workspace")) / "conference"


def active_path():
    return estado_dir() / "active.json"


def log_path():
    return estado_dir() / "log.jsonl"


def _lock_path():
    return estado_dir() / ".lock"


@contextlib.contextmanager
def _lock():
    """Exclusive lock shared by the commands and the hook (concurrent Stops included)."""
    pasta = estado_dir()
    pasta.mkdir(parents=True, exist_ok=True)
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
    base = lib.expandir(repo or lib.repo_atual())
    siblings = [p for p in lib.siblings(base) if p != base]
    return [base] + siblings if siblings else []


def start(repos=None, repo=None, screen=None):
    """Starts a conference with the given repos, or with `repo` (default: the current window's repo)
    and its config siblings. `screen` is the pid of the process showing the conference screen: when
    it goes away, the conference ends with it."""
    import lib

    want = [lib.expandir(p) for p in (repos or [])]
    if not want:
        want = _default_repos(lib, repo)
        if not want:
            print(_t("conference.no_repos"), file=sys.stderr)
            return 2
    ordered = []
    for caminho in want:
        if not os.path.isdir(caminho):
            print(_t("cli.not_exists", path=caminho), file=sys.stderr)
            return 2
        if caminho not in ordered:
            ordered.append(caminho)
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
    arquivo = None
    try:
        pasta = estado_dir() / "prompts"
        pasta.mkdir(parents=True, exist_ok=True)
        fd, arquivo = tempfile.mkstemp(dir=str(pasta), prefix="msg-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        argv = [sys.executable, str(BASE / "src" / "conference_worker.py"),
                repo, arquivo, message_id, str(created)]
        argv += [str(p) for p in (others or []) if str(p) != repo]
        subprocess.Popen(
            argv,
            cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        if arquivo:
            try:
                os.unlink(arquivo)
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
    from viewer import DIM, Linha, R, popup_header, term_cols

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
    body = [Linha()]
    if active:
        body += [Linha(f" {DIM}{_t('conference.screen.hint')}{R}"), Linha()]
    if state["notice"]:
        body += [Linha(f" {DIM}{state['notice']}{R}"), Linha()]
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
            v.recoletar.set()
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
                        v.recoletar.set()
                        return True
                state["notice"] = _printed(say, text)
                v.recoletar.set()
            return True
        if k in ("\x7f", "\x08"):                  # Backspace
            state["text"] = state["text"][:-1]
            v.recoletar.set()
            return True
        if k.isprintable():
            state["text"] += k
            v.recoletar.set()
        return True                                # other control keys are ignored

    Viewer(collect, intervalo=1.0, ao_tecla=close_on_esc, entrada=on_input,
           rodape=footer).rodar()
    if read_active():      # Esc closes the screen, and the conference goes with it
        _printed(stop)
    return 0
