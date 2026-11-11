"""ACP-shaped agent adapter interface, without depending on ACP.

The method names copy the shape of the Agent Client Protocol (`initialize`, `session`, `prompt`,
`permission`, `update`) so every caller sees the same agent surface, but this module does not speak
ACP and does not import an ACP library. When the specification stabilizes (or OpenCode adopts it),
an adapter can be replaced by a real ACP client without changing the conference.

An adapter owns only the harness side of the conversation: building the headless command, reading
its stream and turning one event into an update. It owns no capability rule: `permission` asks the
central engine in `src/policy.py`, the same engine the Claude hook (`src/hook.py`) and the OpenCode
plugin (`harness/opencode-policy.js`) call.
"""
import contextlib
import json
import os
from dataclasses import dataclass, field
from typing import Optional

PROTOCOL = "acp-shaped/1"
HARNESS_CLAUDE = "claude"
HARNESS_OPENCODE = "opencode"

METHODS = ("initialize", "session", "prompt", "permission", "update")


@dataclass(frozen=True)
class Session:
    """One agent session: the window profile, the harness and the command that starts it."""

    profile: str
    harness: str
    cwd: str
    command: str
    settings: str = ""
    env: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Prompt:
    """A prompt ready to run: the command, the text on stdin and the adapter that reads the answer."""

    command: str
    stdin: str
    cwd: str
    adapter: "Adapter"

    def __str__(self):
        return self.command

    def answer(self, stream, on_status=None):
        """Final answer of the run, reporting its tool steps through `on_status`."""
        return self.adapter.read_stream(stream, self.cwd, on_status)


@dataclass(frozen=True)
class Update:
    """One parsed stream event. `None` means that event does not carry that field."""

    status: str = ""
    text: Optional[str] = None
    final: Optional[str] = None
    reset: bool = False


class Adapter:
    """ACP-shaped adapter; subclasses fill the harness-specific command, labels and stream events."""

    harness = ""
    stream_format = ""

    def __init__(self, profile=None):
        self.profile = profile or ""

    def initialize(self):
        """Handshake: the protocol shape, the harness and the stream this adapter reads."""
        return {
            "protocol": PROTOCOL,
            "harness": self.harness,
            "stream": self.stream_format,
            "methods": list(METHODS),
            "permission": "central",
        }

    def command(self, profile=None):
        """Headless command line of `profile`; the context comes from stdin, not from argv."""
        raise NotImplementedError

    def profile_name(self, profile=None):
        """Profile to use: the explicit one, the one this adapter was created for, or one that
        belongs to this adapter's harness. Never falls back to a profile of the other harness."""
        name = profile or self.profile
        if name:
            return name
        try:
            import lib

            configured = lib.agent_name()
            if lib.profile_harness(configured) == self.harness:
                return configured
            for candidate in lib.profile_names():
                if lib.profile_harness(candidate) == self.harness:
                    return candidate
        except Exception:
            pass
        return self.harness

    def command_line(self, profile=None):
        """Profile command, or the bare harness binary when the profile belongs to the other side."""
        name = self.profile_name(profile)
        try:
            import lib

            if lib.profile_harness(name) == self.harness:
                return lib.profile_command(name)
        except Exception:
            pass
        return self.harness

    def session(self, profile=None, cwd=""):
        """Session of `profile` in `cwd` (the conference repo)."""
        profile = self.profile_name(profile)
        command = self.command(profile)
        return Session(profile=profile, harness=self.harness, cwd=str(cwd or ""), command=command,
                       settings=self.settings_path(profile))

    def settings_path(self, profile=None):
        """Generated harness file that carries the conference wiring, or ""."""
        return ""

    def prompt(self, session=None, text=""):
        """Prompt for a session. `prompt("text")`, `prompt(session, "text")` and
        `prompt(session=session, text="text")` are all accepted, so the caller never has to build
        a session just to send one message."""
        if isinstance(session, Session):
            message, chosen = text, session
        elif isinstance(text, Session):
            message, chosen = session, text
        elif session is None:
            message, chosen = text, self.session()
        else:
            message, chosen = session, self.session()
        return Prompt(command=chosen.command, stdin=str(message or ""), cwd=chosen.cwd,
                      adapter=self)

    def permission(self, event, env=None):
        """Central capability decision for one tool event; no rule is implemented here."""
        return central_decision(event, env)

    def update(self, event, repo=""):
        """One stream event as an `Update`; subclasses know their own event shape."""
        raise NotImplementedError

    def read_stream(self, stream, repo="", on_status=None):
        """Final text of the stream, reporting each tool step through `on_status`."""
        return read_stream(stream, repo, on_status, adapter=self)


def _t(key, **kw):
    try:
        import i18n
        return i18n.t(key, **kw)
    except Exception:
        return key


def clip(text, limit=32):
    """One-line label for the header; long paths and patterns keep the message readable."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def short_path(repo, path):
    """Path relative to the repo when possible; the basename for anything outside it."""
    path = str(path or "")
    try:
        rel = os.path.relpath(path, repo) if path else ""
    except (TypeError, ValueError):
        rel = path
    if not rel or rel.startswith(".."):
        rel = os.path.basename(path)
    return clip(rel or "?")


def normalize_event(event):
    """Claude hook or OpenCode event mapped to the one shape `policy.check_event` reads.

    Only field names are normalized. The tool name, the arguments and the declared permission mode
    stay exactly as the harness sent them, so the central engine sees the same action from both
    adapters."""
    event = dict(event or {}) if isinstance(event, dict) else {}
    if not event.get("tool_name"):
        name = event.get("tool") or event.get("toolName") or event.get("name")
        if name:
            event["tool_name"] = name
    if "tool_input" not in event:
        data = (event.get("tool_input") or event.get("toolInput") or event.get("args")
                or event.get("input"))
        if isinstance(data, dict):
            event["tool_input"] = data
    if not event.get("session_id"):
        session = event.get("sessionID") or event.get("sessionId") or event.get("session_id")
        if session:
            event["session_id"] = session
    if not event.get("agent_id"):
        agent = event.get("agentID") or event.get("agentId") or event.get("agent_id")
        if agent:
            event["agent_id"] = agent
    if not event.get("permission_mode"):
        mode = event.get("permissionMode") or event.get("permission_mode") or event.get("mode")
        if isinstance(mode, str) and mode:
            event["permission_mode"] = mode
    return event


@contextlib.contextmanager
def _env_overrides(values):
    if not values:
        yield
        return
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def central_decision(event, env=None):
    """Decision of `src/policy.py` for one event, or a fail-closed denial if the engine fails."""
    try:
        import policy
    except Exception:
        return {"decision": "deny", "capability": None, "reason": "policy.reason.error"}
    try:
        with _env_overrides(env):
            result = policy.check_event(normalize_event(event))
    except Exception:
        return {"decision": "deny", "capability": None, "reason": "policy.reason.error"}
    if not isinstance(result, dict):
        return {"decision": "deny", "capability": None, "reason": "policy.reason.error"}
    return result


def choose_adapter(event):
    """Adapter for a stream event when the caller did not name one (compatibility helper)."""
    kind = event.get("type") if isinstance(event, dict) else None
    if kind in ("text", "tool_use") and isinstance(event.get("part"), dict):
        from .opencode import OpenCodeAdapter
        return OpenCodeAdapter()
    from .claude import ClaudeAdapter
    return ClaudeAdapter()


def read_stream(stream, repo="", on_status=None, adapter=None):
    """Final text of a Claude stream-json or OpenCode json run.

    With `adapter`, every event goes through that adapter. Without it, the event shape chooses the
    adapter, which keeps the old `_stream_answer` callers working while the conference always names
    the window profile."""
    final = ""
    texts = []
    for line in stream:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        chosen = adapter or choose_adapter(event)
        step = chosen.update(event, repo)
        if step.status and on_status:
            on_status(step.status)
        if step.reset:
            texts = []
        if step.text is not None:
            texts.append(step.text)
        if step.final is not None:
            final = step.final
    return final or "\n\n".join(texts)
