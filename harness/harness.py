#!/usr/bin/env python3
"""Mechanism of the forced harness: the Claude Code hook and the settings renderer.

Hook mode (no arguments) reads the hook JSON from stdin and never breaks the session: any error,
invalid input or missing session id exits 0 with no decision (fail open). Events handled:

- SessionStart: injects the installed harness text (HARNESS.md; the old CLAUDE.md + regras/*.md
  layout is still read when HARNESS.md is absent).
- UserPromptSubmit: injects a short reminder derived from item F07 of forcado.yaml.
- PostToolUse (Read): records that a file of the harness was read in this session.
- PreToolUse (Write, Edit, NotebookEdit): denies the write until the harness was read.

`render` writes settings.json from the forcado.yaml of the harness it points at. It registers the
hooks and translates every item with `tipo: deny` into a permissions.deny entry. The hook command
uses the interpreter exported by the launcher (`WS_PYTHON`) when present, with a plain `python3`
fallback, and quotes the script path.

The harness directory comes from AT_HARNESS_DIR or --harness (render); the default is derived from
Path.home(). The session state comes from AT_HARNESS_STATE, defaulting under WS_STATE (or
~/.cache/workspace). Stdlib only, and no import from src/: this file is installed on its own.
"""
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

DEFAULT_HARNESS_DIR = Path(".local", "share", "ai-tmux-workspace", "harness")
GATE_ITEM_ID = "F07"
DEFAULT_GATE_TOOLS = ("Write", "Edit", "NotebookEdit")
HOOK_SCRIPT = '"$HOME/.local/share/ai-tmux-workspace/harness/harness.py"'

USAGE = """usage: harness.py [--help]
       harness.py render [--harness DIR] [--out FILE]

Without arguments it runs as a Claude Code hook reading JSON from stdin.
`render` writes the settings.json derived from the harness forcado.yaml."""


# --- minimal YAML reader (stdlib only) ---------------------------------------------------------
# Supports the subset used by forcado.yaml: block mappings and sequences, plain and quoted
# scalars, one-line flow sequences of scalars, and # comments.

class YamlError(ValueError):
    pass


_INT = re.compile(r"[-+]?\d+")
_FLOAT = re.compile(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?")
_QUOTED_KEY = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\']|\'\')*\'')
_RESERVED = "&*!|>%@`"
_NULLS = ("", "~", "null", "Null", "NULL")
_BOOLS = {"true": True, "True": True, "TRUE": True, "false": False, "False": False, "FALSE": False}


def _strip_comment(text):
    quote, i = None, 0
    while i < len(text):
        c = text[i]
        if quote == '"':
            if c == "\\":
                i += 2
                continue
            if c == '"':
                quote = None
        elif quote == "'":
            if c == "'":
                quote = None
        elif c in "\"'" and (i == 0 or text[i - 1] in " [,:-"):
            quote = c
        elif c == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
        i += 1
    return text.rstrip()


def _content_lines(text):
    lines = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line.strip():
            continue
        lead = line[:len(line) - len(line.lstrip())]
        if "\t" in lead:
            raise YamlError(f"line {number}: tab used for indentation")
        if line.strip() in ("---", "...") or line.lstrip().startswith("%"):
            raise YamlError(f"line {number}: documents and directives are not supported")
        lines.append([number, len(line) - len(line.lstrip(" ")), line.strip()])
    return lines


def _split_key(text):
    match = _QUOTED_KEY.match(text)
    if match:
        end = match.end()
        if text[end:end + 1] != ":" or text[end + 1:end + 2] not in ("", " "):
            return None
        return _scalar(match.group()), text[end + 1:].strip()
    match = re.search(r":(\s|$)", text)
    if not match or text.startswith(("[", "{", "- ")) or text == "-":
        return None
    key = text[:match.start()].strip()
    if not key:
        return None
    return key, text[match.end():].strip()


def _flow_parts(text):
    parts, current, quote, i = [], "", None, 0
    while i < len(text):
        c = text[i]
        if quote == '"':
            current += c
            if c == "\\" and i + 1 < len(text):
                current += text[i + 1]
                i += 2
                continue
            if c == '"':
                quote = None
        elif quote == "'":
            current += c
            if c == "'":
                quote = None
        elif c == ",":
            parts.append(current.strip())
            current = ""
        else:
            if c in "\"'" and not current.strip():
                quote = c
            current += c
        i += 1
    parts.append(current.strip())
    return parts


def _value(text):
    if text.startswith("["):
        if not text.endswith("]"):
            raise YamlError("flow sequence must fit on one line")
        inside = text[1:-1].strip()
        return [] if not inside else [_scalar(part) for part in _flow_parts(inside)]
    if text == "{}":
        return {}
    if text.startswith("{"):
        raise YamlError("flow mappings are not supported")
    return _scalar(text)


def _scalar(text):
    if text.startswith('"'):
        try:
            value, end = json.JSONDecoder().raw_decode(text)
        except ValueError:
            raise YamlError("invalid double-quoted string")
        if text[end:].strip():
            raise YamlError("text after a quoted string")
        return value
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'"):
            raise YamlError("unterminated single-quoted string")
        body = text[1:-1]
        if "'" in body.replace("''", ""):
            raise YamlError("invalid single-quoted string")
        return body.replace("''", "'")
    if text[:1] in _RESERVED:
        raise YamlError(f"unsupported syntax: {text[0]}")
    if text in _NULLS:
        return None
    if text in _BOOLS:
        return _BOOLS[text]
    if _INT.fullmatch(text):
        return int(text)
    if _FLOAT.fullmatch(text):
        return float(text)
    return text


class _Reader:
    def __init__(self, lines):
        self.lines = lines
        self.i = 0

    def current(self):
        return self.lines[self.i] if self.i < len(self.lines) else None

    def block(self, indent):
        _, _, text = self.lines[self.i]
        if text == "-" or text.startswith("- "):
            return self.sequence(indent)
        return self.mapping(indent)

    def child(self, indent):
        current = self.current()
        if current and current[1] > indent:
            return self.block(current[1])
        return None

    def sequence(self, indent):
        items = []
        while (current := self.current()) and current[1] == indent and (
                current[2] == "-" or current[2].startswith("- ")):
            number, _, text = current
            rest = text[1:].lstrip()
            if not rest:
                self.i += 1
                items.append(self.child(indent))
            elif _split_key(rest) is not None:
                column = indent + (len(text) - len(rest))
                self.lines[self.i] = [number, column, rest]
                items.append(self.block(column))
            else:
                self.i += 1
                items.append(_value(rest))
        if (current := self.current()) and current[1] > indent:
            raise YamlError(f"line {current[0]}: bad indentation")
        return items

    def mapping(self, indent):
        result = {}
        while (current := self.current()) and current[1] == indent:
            number, _, text = current
            if text == "-" or text.startswith("- "):
                break
            pair = _split_key(text)
            if pair is None:
                raise YamlError(f"line {number}: expected 'key: value'")
            key, rest = pair
            if key in result:
                raise YamlError(f"line {number}: duplicate key {key!r}")
            self.i += 1
            if rest:
                result[key] = _value(rest)
            else:
                following = self.current()
                if following and following[1] == indent and (
                        following[2] == "-" or following[2].startswith("- ")):
                    result[key] = self.sequence(indent)
                else:
                    result[key] = self.child(indent)
        if (current := self.current()) and current[1] > indent:
            raise YamlError(f"line {current[0]}: bad indentation")
        return result


def yaml_loads(text):
    lines = _content_lines(text)
    if not lines:
        return None
    if len(lines) == 1 and lines[0][1] == 0 and lines[0][2] in ("{}", "[]"):
        return _value(lines[0][2])
    if lines[0][1] != 0:
        raise YamlError(f"line {lines[0][0]}: bad indentation")
    reader = _Reader(lines)
    result = reader.block(0)
    if reader.i != len(lines):
        raise YamlError(f"line {lines[reader.i][0]}: bad indentation")
    return result


# --- paths and harness data --------------------------------------------------------------------

def harness_dir(override=None):
    """Installed harness directory: argument, AT_HARNESS_DIR, or the default under Path.home()."""
    if override:
        return Path(override).expanduser()
    from_env = os.environ.get("AT_HARNESS_DIR")
    if from_env:
        return Path(from_env).expanduser()
    return Path.home() / DEFAULT_HARNESS_DIR


def state_dir():
    """Directory of the per-session read markers."""
    if os.environ.get("AT_HARNESS_STATE"):
        return Path(os.environ["AT_HARNESS_STATE"]).expanduser()
    base = os.environ.get("WS_STATE")
    return (Path(base).expanduser() if base else Path.home() / ".cache" / "workspace") / "harness"


def load_rules(directory):
    """Parsed forcado.yaml of the given harness; empty mapping when it is not there."""
    document = yaml_loads((Path(directory) / "forcado.yaml").read_text(encoding="utf-8"))
    return document if isinstance(document, dict) else {}


def find_item(document, item_id):
    for item in document.get("itens") or []:
        if isinstance(item, dict) and str(item.get("id")) == item_id:
            return item
    return None


def gate_item(directory):
    """F07 when it is an active hook/reminder; None when the harness forbids no read gate."""
    item = find_item(load_rules(directory), GATE_ITEM_ID)
    if not isinstance(item, dict) or item.get("tipo") not in ("lembrete", "hook"):
        return None
    return item


def gate_tools(item):
    tools = [part.strip() for part in str(item.get("alvo") or "").split("|") if part.strip()]
    return tools or list(DEFAULT_GATE_TOOLS)


def _one_line(value, limit=300):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def session_context(directory):
    directory = Path(directory)
    files = []
    entry = directory / "HARNESS.md"
    if entry.is_file():
        files.append(entry)
    else:
        # Legacy layout: the entry point plus the per-category files.
        entry = directory / "CLAUDE.md"
        if entry.is_file():
            files.append(entry)
        rules = directory / "regras"
        if rules.is_dir():
            files.extend(sorted(path for path in rules.glob("*.md") if path.is_file()))
    sections = []
    for path in files:
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        sections.append(f"## {path.relative_to(directory).as_posix()}\n\n{body}")
    return "\n\n".join(sections)


# --- session state -----------------------------------------------------------------------------

def _valid_session_id(value):
    return (isinstance(value, str) and bool(value) and value not in (".", "..")
            and not any(char in value for char in "/\\\0"))


def _state_path(session_id):
    return state_dir() / "sessions" / f"{session_id}.json"


def _read_state(session_id):
    try:
        data = json.loads(_state_path(session_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def harness_was_read(session_id):
    return bool(_read_state(session_id).get("read"))


def mark_harness_read(session_id, relative_path):
    """Atomically marks the session as having read a harness file."""
    path = _state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_state(session_id)
    files = data.get("files") if isinstance(data.get("files"), list) else []
    if relative_path not in files:
        files.append(relative_path)
    data.update(read=True, files=files, ts=time.time())
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def path_inside_harness(file_path, directory):
    try:
        root = os.path.realpath(str(directory))
        target = os.path.realpath(str(file_path))
        return os.path.commonpath([root, target]) == root
    except (ValueError, OSError):
        return False


# --- hook mode ---------------------------------------------------------------------------------

def _emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")


def _prompt_context(item, read):
    rule = _one_line(item.get("regra"))
    text = _one_line(item.get("texto"))
    gated = _one_line(", ".join(gate_tools(item)))
    parts = ["[harness]"]
    if rule:
        parts.append(rule.rstrip(".") + ".")
    if text:
        parts.append(f"Read {text} before writing.")
    parts.append("Harness read in this session." if read else
                 f"Harness not read in this session yet; {gated} blocked until then.")
    return " ".join(parts)


def _deny_reason(item, tool):
    rule = _one_line(item.get("regra"))
    text = _one_line(item.get("texto"))
    reason = "[harness]"
    if rule:
        reason += f" {rule.rstrip('.')}."
    target = f" Read {text} first." if text else ""
    return f"{reason}{target} {tool} is blocked until the harness is read in this session."


def handle_hook(data, directory):
    if not isinstance(data, dict):
        return
    event = data.get("hook_event_name")
    if event == "SessionStart":
        context = session_context(directory)
        if context:
            _emit({"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": context}})
        return
    if event == "UserPromptSubmit":
        item = gate_item(directory)
        if item is None:
            return
        session_id = data.get("session_id")
        read = _valid_session_id(session_id) and harness_was_read(session_id)
        _emit({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": _prompt_context(item, read)}})
        return
    if event == "PostToolUse":
        if data.get("tool_name") != "Read":
            return
        session_id = data.get("session_id")
        if not _valid_session_id(session_id):
            return
        file_path = (data.get("tool_input") or {}).get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return
        directory = Path(directory)
        if path_inside_harness(file_path, directory):
            try:
                relative = Path(os.path.realpath(file_path)).relative_to(os.path.realpath(directory))
            except (ValueError, OSError):
                relative = Path(os.path.basename(file_path))
            mark_harness_read(session_id, relative.as_posix())
        return
    if event == "PreToolUse":
        tool = data.get("tool_name")
        if not isinstance(tool, str):
            return
        session_id = data.get("session_id")
        if not _valid_session_id(session_id):
            return
        if data.get("agent_id"):     # subagent: reliable marker, exempt from the gate
            return
        item = gate_item(directory)
        if item is None or tool not in gate_tools(item):
            return
        if harness_was_read(session_id):
            return
        _emit({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(item, tool),
        }})


def run_hook():
    try:
        data = json.load(sys.stdin)
        handle_hook(data, harness_dir())
    except Exception:
        return      # fail open: the hook never breaks the session


# --- render mode -------------------------------------------------------------------------------

def _option(argv, name):
    for index, argument in enumerate(argv):
        if argument == name and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith(name + "="):
            return argument.split("=", 1)[1]
    return None


def _bash_glob(pattern):
    glob = pattern.strip()
    return glob[:-2] + ":*" if glob.endswith(" *") else glob


def _tool_glob(tool, pattern):
    if tool == "Bash":
        return f"Bash({_bash_glob(pattern)})"
    glob = pattern.strip()
    if not glob.startswith(("./", "/", "~")):
        glob = "./" + glob
    return f"{tool}({glob})"


def deny_entries(document):
    """permissions.deny entries of every item with `tipo: deny`, in declaration order."""
    entries, seen = [], set()
    for item in document.get("itens") or []:
        if not isinstance(item, dict) or item.get("tipo") != "deny":
            continue
        tools = [part.strip() for part in str(item.get("alvo") or "").split("|") if part.strip()]
        patterns = item.get("padrao")
        patterns = patterns if isinstance(patterns, list) else [patterns]
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            for tool in tools:
                # Claude Code's Edit deny already covers Write; a Write(...) deny entry is never
                # matched and only shows a warning, so it is emitted as Edit(...).
                entry = _tool_glob("Edit" if tool == "Write" else tool, pattern)
                if entry not in seen:
                    seen.add(entry)
                    entries.append(entry)
    return entries


def hook_command():
    """Hook command line: the launcher's interpreter (WS_PYTHON) with the script path quoted.

    `workspace` exports WS_PYTHON before the agent starts because a GUI-launched shell may not have
    Homebrew on PATH. `shlex.quote` leaves a plain name untouched and quotes a path with spaces, so a
    standalone render (WS_PYTHON unset) still produces the historical `python3 "$HOME/..."` command.
    """
    python = os.environ.get("WS_PYTHON")
    if python:
        return f"{shlex.quote(python)} {HOOK_SCRIPT}"
    # A standalone render (no launcher) still has to work on a Mac: the hook is a shell command,
    # so resolve the launcher's interpreter at run time and fall back to `python3`.
    return f'"${{WS_PYTHON:-python3}}" {HOOK_SCRIPT}'


def build_settings(document):
    hook = {"type": "command", "command": hook_command()}
    gate = find_item(document, GATE_ITEM_ID)
    gate_matcher = "|".join(gate_tools(gate)) if isinstance(gate, dict) else "|".join(DEFAULT_GATE_TOOLS)
    return {
        "permissions": {"deny": deny_entries(document)},
        "hooks": {
            "SessionStart": [{"hooks": [dict(hook)]}],
            "UserPromptSubmit": [{"hooks": [dict(hook)]}],
            "PostToolUse": [{"matcher": "Read", "hooks": [dict(hook)]}],
            "PreToolUse": [{"matcher": gate_matcher, "hooks": [dict(hook)]}],
        },
    }


def render(argv):
    try:
        directory = harness_dir(_option(argv, "--harness"))
        output = _option(argv, "--out")
        target = (Path(output).expanduser() if output
                  else Path(__file__).resolve().parent / "settings.json")
        settings = build_settings(load_rules(directory))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(settings, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                          encoding="utf-8")
        print(target)
        return 0
    except Exception as error:
        print(f"render failed: {error}", file=sys.stderr)
        return 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if argv and argv[0] == "render":
        return render(argv[1:])
    run_hook()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
