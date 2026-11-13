"""Claude Code adapter for the ACP-shaped agent layer.

The adapter knows only the Claude side of one headless run: the profile command plus `--settings`,
the conference hook that enforces the two-file scope and the stream-json event shape. It carries no
capability rule of its own: the hook (`conference_worker.py hook`) and `src/policy.py` decide.
"""
import json
import re
import shlex
import sys
from pathlib import Path

import conference
from session_state import BLOCKED, DONE, IDLE, WORKING
from .base import SESSION_ID_WORKSPACE, Adapter, Update, _t, clip, short_path

ALLOWED_TOOLS = "Read,Edit"
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
ASK_TOOLS = ("AskUserQuestion", "ExitPlanMode")       # the agent waits on the person
PROMPT_NOTIFICATIONS = ("permission_prompt", "elicitation_dialog")
DISALLOWED_TOOLS = "Bash,Task,NotebookEdit,MultiEdit,Write,WebFetch,WebSearch,Grep,Glob"
SETTINGS_FLAG = re.compile(r"(?:^|\s)--settings(?:=|\s|$)")

WORKER = Path(conference.__file__).resolve().parent / "conference_worker.py"


def settings_path_from_command(command):
    """Path of an explicit `--settings` in a profile command, or None."""
    match = re.search(r"--settings(?:=|\s+)(?:(['\"])(.*?)\1|(\S+))", command or "")
    if not match:
        return None
    return Path(match.group(2) or match.group(3) or "").expanduser()


def _hook_command():
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(WORKER))} hook"


def conference_settings(profile, command):
    """Generated settings of the run: the profile/harness settings plus the conference hook.

    The hook is what makes the two-file scope a mechanism: it denies every tool except `Read`/`Edit`
    on the repo's own structural files (reads) and `EDITABLE_FILES` (edits), and turns a README edit
    into a pending diff.
    """
    import lib

    base_path = lib.ensure_profile_settings(profile)
    source = Path(base_path) if base_path else settings_path_from_command(command)
    if source and source.is_file():
        settings = json.loads(source.read_text(encoding="utf-8"))
    else:
        settings = lib.security_base_settings()
    if not isinstance(settings, dict):
        settings = {}
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = settings["hooks"] = {}
    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        pre = hooks["PreToolUse"] = []
    pre.append({"hooks": [{"type": "command", "command": _hook_command()}]})
    folder = conference._ensure_dir(conference.state_dir() / "settings")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(profile or "claude")) or "claude"
    path = folder / f"conference-{safe}.json"
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    conference._chmod(path)
    return path


def replace_settings(command, path):
    """Point the command at the merged settings, replacing an explicit `--settings` when present."""
    quoted = shlex.quote(str(path))
    if re.search(r"--settings(?:=|\s+)(?:(['\"]).*?\1|\S+)", command or ""):
        return re.sub(r"--settings(?:=|\s+)(?:(['\"]).*?\1|\S+)",
                      f"--settings {quoted}", command, count=1)
    return f"{command} --settings {quoted}"


class ClaudeAdapter(Adapter):
    """Claude Code: profile command + conference settings + stream-json."""

    harness = "claude"
    stream_format = "stream-json"
    session_id_source = SESSION_ID_WORKSPACE
    records_permission_mode = True

    def prepare(self, profile=None):
        """Generated settings of the profile (harness hooks, deny rules, profile settings).

        "" when the profile has no generated settings: `manage: false`, a legacy command that already
        owns `--settings`, or an OpenCode profile (`ensure_profile_settings` returns None)."""
        import lib

        path = lib.ensure_profile_settings(self.profile_name(profile))
        self._settings = str(path) if path else ""
        return self._settings

    def launch(self, profile=None, mode=""):
        """Interactive Claude command: profile command, generated settings when managed, tool search
        off for profiles other than `claude`, and the recorded permission mode."""
        import lib

        profile = self.profile_name(profile)
        command = self.command_line(profile)
        if not lib.manage_enabled():
            return command
        if not SETTINGS_FLAG.search(command):
            settings = self.prepare(profile)
            if settings:
                command = f"{command} --settings {shlex.quote(settings)}"
        launch = command if profile == "claude" else f"env ENABLE_TOOL_SEARCH=false {command}"
        if mode:
            launch = f"{launch} --permission-mode {shlex.quote(mode)}"
        return launch

    def fresh_args(self, session=""):
        """A new session with the id the workspace chose: Claude needs it on the launch line."""
        return ["--session-id", str(session)] if session else []

    def resume_args(self, session):
        """Resume exactly the recorded session id."""
        return ["--resume", str(session)]

    def continue_args(self):
        """Continue the last session of the repo, the legacy two-argument opening."""
        return ["--continue"]

    def command(self, profile=None):
        """Headless Claude run with the profile's settings, the conference hook and only two tools."""
        profile = self.profile_name(profile)
        command = self.command_line(profile)
        settings = conference_settings(profile, command)
        self._settings = str(settings)
        command = replace_settings(command, settings)
        launch = command if profile == "claude" else f"env ENABLE_TOOL_SEARCH=false {command}"
        system = _t("conference.system_prompt", no_answer=conference.NO_ANSWER,
                    max_files=conference.MAX_FILES_READ, max_steps=conference.MAX_TOOL_STEPS,
                    files=conference.STRUCTURAL_FILES_TEXT)
        return (f"{launch} -p --output-format stream-json --verbose --no-session-persistence "
                f"--permission-mode default "
                f"--append-system-prompt {shlex.quote(system)} "
                f"--allowedTools {shlex.quote(ALLOWED_TOOLS)} "
                f"--disallowedTools {shlex.quote(DISALLOWED_TOOLS)}")

    def settings_path(self, profile=None):
        if getattr(self, "_settings", ""):
            return self._settings
        profile = self.profile_name(profile)
        return str(conference_settings(profile, self.command_line(profile)))

    def status_label(self, repo, name, data):
        """Header label for the tool steps worth showing; "" for everything else."""
        data = data if isinstance(data, dict) else {}
        if name == "Read":
            return _t("conference.status.reading", path=short_path(repo, data.get("file_path")))
        if name == "Grep":
            return _t("conference.status.searching", pattern=clip(data.get("pattern")))
        if name == "Glob":
            return _t("conference.status.looking", pattern=clip(data.get("pattern")))
        return ""

    def update(self, event, repo=""):
        """One Claude stream-json event: a tool step, an error or the final result."""
        kind = event.get("type")
        if kind == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else []
            label = ""
            tool_use = False
            for block in content or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_use = True
                candidate = self.status_label(repo, block.get("name"), block.get("input"))
                if candidate:
                    label = candidate
            return Update(status=label, reset=tool_use)
        if kind == "result" and not event.get("is_error"):
            return Update(final=str(event.get("result") or ""))
        return Update()

    def state_of(self, event, current=""):
        """Agent state declared by one Claude Code hook event (the `hook_event_name` payload).

        Blocked: the agent asks for an approval or an answer (`PermissionRequest`, a permission or
        elicitation prompt, `AskUserQuestion`, `ExitPlanMode`). Done: the turn ended with no
        background work left (`Stop`). Working: a prompt, a tool step or a denied permission. Idle:
        a fresh session, or the session ended. A subagent's tool step keeps a `blocked` state, as the
        session still waits on the person. Events that carry no state change return None."""
        if not isinstance(event, dict):
            return None
        name = event.get("hook_event_name")
        tool = event.get("tool_name")
        if name == "SessionStart":
            return None if event.get("source") == "compact" else IDLE
        if name == "SessionEnd":
            return IDLE
        if name == "UserPromptSubmit":
            return WORKING
        if name in ("Stop", "StopFailure"):
            return WORKING if has_background_work(event) else DONE
        if name == "PermissionDenied":
            return WORKING
        if name == "Elicitation":
            return BLOCKED
        if name == "PreToolUse" and tool in ASK_TOOLS:
            return BLOCKED
        if name in TOOL_EVENTS and event.get("agent_id"):
            return BLOCKED if current == BLOCKED else WORKING
        if name in TOOL_EVENTS:
            return WORKING
        if name == "PermissionRequest":
            return BLOCKED
        if name == "Notification" and event.get("notification_type") in PROMPT_NOTIFICATIONS:
            return BLOCKED
        return None


def has_background_work(event):
    """True while background tasks or session crons keep the session working after its turn ended."""
    return bool(event.get("background_tasks")) or bool(event.get("session_crons"))
