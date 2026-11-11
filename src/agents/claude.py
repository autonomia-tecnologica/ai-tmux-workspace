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
from .base import Adapter, Update, _t, clip, short_path

ALLOWED_TOOLS = "Read,Edit"
DISALLOWED_TOOLS = "Bash,Task,NotebookEdit,MultiEdit,Write,WebFetch,WebSearch,Grep,Glob"

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
    on the repo's own `README.md`/`BACKLOG.md`, and turns a README edit into a pending diff.
    """
    import lib

    base_path = lib.ensure_profile_settings(profile)
    source = Path(base_path) if base_path else settings_path_from_command(command)
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

    def command(self, profile=None):
        """Headless Claude run with the profile's settings, the conference hook and only two tools."""
        profile = self.profile_name(profile)
        command = self.command_line(profile)
        settings = conference_settings(profile, command)
        self._settings = str(settings)
        command = replace_settings(command, settings)
        launch = command if profile == "claude" else f"env ENABLE_TOOL_SEARCH=false {command}"
        system = _t("conference.system_prompt", no_answer=conference.NO_ANSWER,
                    max_files=conference.MAX_FILES_READ, max_steps=conference.MAX_TOOL_STEPS)
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
