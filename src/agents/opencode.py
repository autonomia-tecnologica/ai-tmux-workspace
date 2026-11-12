"""OpenCode adapter for the ACP-shaped agent layer.

The adapter knows only the OpenCode side of one headless run: the profile command, the merged
`OPENCODE_CONFIG` and the json event shape. It carries no Claude flag, no Claude settings and no
capability rule. The conference scope is wired as OpenCode permission rules derived from the same
`conference.STRUCTURAL_FILES` (reads) and `conference.EDITABLE_FILES` (edits) the Claude hook
enforces; the central policy engine stays in
`src/policy.py` and `harness/opencode-policy.js`.
"""
import json
import re
import shlex
from pathlib import Path

import conference
from .base import Adapter, Update, _t, clip, short_path


def conference_permissions():
    """OpenCode permission rules: everything denied, then the structural files readable and the
    editable ones writable.

    `README.md` edits ask instead of allowing: a non-interactive run cannot confirm them, so the
    direct write does not happen. The Claude path turns the same intent into a pending diff.
    """
    readme = "README.md"
    rules = [
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": "external_directory", "resource": "*", "effect": "deny"},
    ]
    rules += [{"action": "read", "resource": name, "effect": "allow"}
              for name in conference.STRUCTURAL_FILES]
    for name in conference.EDITABLE_FILES:
        effect = "ask" if name == readme else "allow"
        rules.append({"action": "edit", "resource": name, "effect": effect})
    return rules


def conference_config(profile):
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
    settings["permissions"] = conference_permissions()
    folder = conference._ensure_dir(conference.state_dir() / "settings")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(profile or "opencode")) or "opencode"
    path = folder / f"opencode-conference-{safe}.json"
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    conference._chmod(path)
    return path


class OpenCodeAdapter(Adapter):
    """OpenCode: profile command + `OPENCODE_CONFIG` + json stream."""

    harness = "opencode"
    stream_format = "json"

    def command(self, profile=None):
        """Headless OpenCode run with the profile's config and the conference permissions."""
        profile = self.profile_name(profile)
        command = self.command_line(profile)
        config = conference_config(profile)
        self._settings = str(config)
        return (f"env OPENCODE_CONFIG={shlex.quote(str(config))} {command} "
                f"run --format json --archived")

    def settings_path(self, profile=None):
        if getattr(self, "_settings", ""):
            return self._settings
        return str(conference_config(self.profile_name(profile)))

    def status_label(self, repo, part):
        """Header label for an OpenCode `tool_use` part; "" when the tool is not worth showing."""
        part = part if isinstance(part, dict) else {}
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
        tool = str(part.get("tool") or "").lower()
        if tool == "read":
            return _t("conference.status.reading", path=short_path(repo, tool_input.get("path")))
        if tool == "grep":
            return _t("conference.status.searching", pattern=clip(tool_input.get("pattern")))
        if tool == "glob":
            return _t("conference.status.looking", pattern=clip(tool_input.get("pattern")))
        return ""

    def update(self, event, repo=""):
        """One OpenCode json event: a text part or a tool step."""
        kind = event.get("type")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if kind == "text":
            text = part.get("text")
            return Update(text=text) if isinstance(text, str) else Update()
        if kind == "tool_use":
            return Update(status=self.status_label(repo, part), reset=True)
        return Update()
