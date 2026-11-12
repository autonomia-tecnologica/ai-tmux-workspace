"""Common base for the workspace panels: repo list, agent state, window repo."""
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from i18n import t
import paths
import permission_state
import policy
import session
import session_state
import tmux
import yaml_min

BASE = Path(__file__).resolve().parent.parent   # repo root
WS = os.path.join(os.environ.get("WS_DIR", str(BASE)), "bin", "workspace")   # the `workspace` script
HOME = Path.home()
STATE = paths.state_dir()
SESSIONS = STATE / "sessions"
WAIT_S = 8                 # a tool open for longer than this = "waiting for things to happen"
WAIT_CAP_S = 30 * 60       # no event for this long: the wait on a tool turns idle
NO_EVENT_S = 10 * 60       # working with no open tool and no event for this long = idle
IDLE_AFTER_S = 6 * 3600    # no event for this long = idle (a Stop red also ages out)
ORPHAN_AGE_S = 24 * 3600   # a dead session file older than this is deleted
PRIORITY = {"idle": 0, "working": 1, "waiting": 2, "question": 3}


def expand_path(p):
    return os.path.realpath(os.path.expanduser(p))


class ConfigError(ValueError):
    """The config file exists but cannot be read, so nothing may be written over it."""


class SettingsError(ValueError):
    """The workspace-generated settings for a profile cannot be built safely (literal key, bad shape, I/O)."""


class RepoDisabledError(ValueError):
    """The repo is switched off ("repos_disabled"): no agent process is built or started for it."""


def config_path():
    """Single config.yaml; lives outside the repo (the repo holds only the structure). AT_WS_CONFIG overrides the path."""
    return paths.config_path()


def legacy_path():
    """Old settings.json (JSON), read while config.yaml does not exist. With AT_WS_CONFIG, it is the same path with .json."""
    return paths.legacy_config_path()


CONFIG_ERROR = None   # (path, line or None) of the last failed read; None when it read fine


def _read_file(path):
    """dict from a config file: JSON when the path ends with .json, else YAML."""
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix == ".json" else (yaml_min.loads(text) or {})
    if not isinstance(data, dict):
        raise ValueError("config root is not a mapping")
    return data


def read_config():
    """Config as a dict: config_path() when it exists; else the old settings.json; else {}. A read error gives {} and marks CONFIG_ERROR."""
    global CONFIG_ERROR
    CONFIG_ERROR = None
    try:
        if config_path().exists():
            return _read_file(config_path())
        if legacy_path().exists():
            return _read_file(legacy_path())
    except (OSError, ValueError) as e:
        CONFIG_ERROR = (str(config_path()), getattr(e, "line", None))
    return {}


def needs_migration():
    """True when only the old settings.json exists; migration is offered, never done on its own."""
    return config_path().suffix != ".json" and not config_path().exists() and legacy_path().exists()


def migrate_config():
    """Write config_path() as YAML from the old settings.json, after reading the YAML back and checking it is equal.
    Does not change or delete the JSON. Returns the written path; raises OSError/ValueError on failure."""
    data = _read_file(legacy_path())
    text = yaml_min.dumps(data)
    if yaml_min.loads(text) != data:
        raise ValueError("YAML does not read back to the same config")
    destination = config_path()
    if destination.exists():
        raise FileExistsError(str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(destination)
    return destination


def manage_enabled():
    """False only when config.yaml sets `manage: false` (item 93).

    The workspace then leaves the agent options alone: no generated settings, no permission mode
    and no tool-search flag on the launch line. A missing key keeps the historical behavior
    (managed), so configs written before `manage` existed do not change."""
    return read_config().get("manage", True) is not False


def agent_name():
    """Chosen agent (config.yaml "agent"); falls back to "claude" when unset or not in "profiles"."""
    profiles = read_config().get("profiles", {})
    name = read_config().get("agent", "claude")
    return name if name in profiles or name == "claude" else "claude"


def default_profile():
    """Default profile name: the configured agent."""
    return agent_name()


def profile_names():
    """Profile names from config.yaml["profiles"], in file order, with `claude` always present.

    A config without a `profiles` map (or with a malformed one) lists only `claude`. The order is the
    config order (panels and the settings refresh rely on it), so it is never sorted."""
    profiles = read_config().get("profiles")
    if not isinstance(profiles, dict):
        return ["claude"]
    names = list(profiles.keys())
    if "claude" not in names:
        names.append("claude")
    return names


def _profile_value(name):
    """Raw value of a profile in config.yaml: a command string (legacy) or a mapping."""
    profiles = read_config().get("profiles", {})
    return profiles.get(name) if isinstance(profiles, dict) else None


HARNESSES = ("claude", "opencode")
HARNESS_CLAUDE = "claude"
HARNESS_OPENCODE = "opencode"


def profile_harness(name=None):
    """Harness of a profile: "claude" (default) or "opencode".

    A string profile is always the legacy Claude command line. A mapping declares the harness with
    the `harness` key; unknown values fall back to "claude" (the command line still decides what
    actually runs)."""
    value = _profile_value(name or agent_name())
    if isinstance(value, dict):
        harness = value.get("harness")
        if isinstance(harness, str) and harness.strip() in HARNESSES:
            return harness.strip()
    return HARNESS_CLAUDE


def is_claude_harness(name=None):
    """True when the profile runs Claude Code (the surface that owns settings/hooks/transcripts)."""
    return profile_harness(name) == HARNESS_CLAUDE


def profile_command(name=None):
    """Command line of a profile. A mapping uses its `command`; a string is the command.

    The default follows the harness: `claude` for the Claude harness, `opencode` for the OpenCode
    one. A string profile keeps the historical `claude` default."""
    name = name or agent_name()
    default = "opencode" if profile_harness(name) == HARNESS_OPENCODE else "claude"
    value = _profile_value(name)
    if isinstance(value, dict):
        command = value.get("command")
        return command if isinstance(command, str) and command.strip() else default
    return value if isinstance(value, str) and value.strip() else default


def profile_config(name=None):
    """OpenCode config file declared by the profile (`config`), expanded; "" when not set.

    The workspace does not read or generate this file: it only exports its path as
    `OPENCODE_CONFIG`, the OpenCode switch for a config outside the global one."""
    value = _profile_value(name or agent_name())
    if isinstance(value, dict):
        config = value.get("config")
        if isinstance(config, str) and config.strip():
            return os.path.realpath(os.path.expanduser(config.strip()))
    return ""


def profile_settings(name=None):
    """Extra settings of a profile (the `settings` map of a mapping profile).

    None means a legacy string profile: its command owns `--settings`, so the workspace does not generate
    one. {} means a profile with no extra settings (the harness base is still generated)."""
    value = _profile_value(name or agent_name())
    if not isinstance(value, dict):
        return None
    settings = value.get("settings")
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise SettingsError(t("settings.err.profile", profile=str(name or agent_name())))
    return settings


def agent_command(profile=None):
    """Command line of a profile: config.yaml["profiles"][profile] (string or mapping), else `claude`."""
    return profile_command(profile or agent_name())


def profile_adapter(name=None):
    """ACP-shaped adapter of a profile (`src/agents`), chosen by the profile's harness.

    The window profile is the only selector: a Claude profile gets the Claude adapter and an
    OpenCode profile gets the OpenCode adapter, so no caller has to know either harness. Imported
    lazily to keep `lib` free of an import cycle with the adapters."""
    import agents

    return agents.adapter_for(name or agent_name())


def agent_adapter(profile=None):
    """Alias of `profile_adapter`, for callers that name the argument `profile`."""
    return profile_adapter(profile)


def adapter_for(profile=None):
    """Alias of `profile_adapter`, matching the `src/agents` entry point name."""
    return profile_adapter(profile)


def agent_identity(profile=None):
    """Recorded identity of a profile for `--continue` and permission-mode decisions.

    A mapping profile can use the same `claude` command as another profile, so the profile name is part
    of the identity; otherwise switching between two mapping profiles would look like the same
    environment and reapply the previous profile's mode/session. String profiles keep the old identity
    (their command line), which is what the recorded state already holds."""
    name = profile or agent_name()
    command = profile_command(name)
    return f"{command} [{name}]" if isinstance(_profile_value(name), dict) else command


# --- workspace-generated settings -------------------------------------------------------------
# The workspace launches Claude Code with one generated settings file per profile: the gate hooks and
# deny rules from forcado.yaml (or the committed security/settings.json until item 33 installs the
# harness), plus the profile's own `settings` map (URL, models, flags, apiKeyHelper). Access keys never
# enter this file: an env key with a literal value is refused and the credential must come from
# apiKeyHelper, which Claude Code runs at launch (F08 in the harness secrets rules).

GATE_PY = BASE / "security" / "gate.py"
SECURITY_SETTINGS = BASE / "security" / "settings.json"

_SETTINGS_FLAG = re.compile(r"(?:^|\s)--settings(?:=|\s|$)")
_SECRET_ENV = re.compile(
    r"(?i)(?:^|_)(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|SESSION_TOKEN|REFRESH_TOKEN|TOKEN"
    r"|SECRET(?:_ACCESS)?_KEY|CLIENT_SECRET|SECRET|PASSWORD)(?:$|_)")
_KEY_LITERAL = re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{10,}|sk-or-v1-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{24,})")


def settings_dir():
    """Directory of the generated per-profile settings. Read at call time, like the other state under
    WS_STATE, so a caller that changes it (tests, a different state dir) gets its own files."""
    return paths.settings_dir()


def _has_settings_flag(command):
    """True when the command already passes `--settings` (a legacy profile that owns its file)."""
    return bool(_SETTINGS_FLAG.search(command))


def security_dir():
    """Installed mechanism directory: AT_SECURITY_DIR, the harness sibling, or the default."""
    return paths.security_dir()


def _security_module(path=None):
    """Load security/gate.py without importing from src/ (the file is installed on its own)."""
    script = Path(path) if path is not None else GATE_PY
    spec = importlib.util.spec_from_file_location("ws_gate_render", script)
    if spec is None or spec.loader is None:
        raise SettingsError(t("settings.err.base", path=str(script), error="module not found"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gate_hook_command(installed):
    """Script of the gate hook: the installed one when it exists, else this repo's copy. The installed
    harness is absent until item 33; pointing at a missing script would make Claude Code block every
    prompt (a non-zero hook aborts UserPromptSubmit), so the repo copy is the explicit fallback."""
    script = security_dir() / "gate.py"
    # The hook runs later, through the shell: use the launcher's interpreter when the agent has it
    # (macOS may not have Homebrew's python3 in a GUI-launched PATH) and fall back to `python3`.
    return f'"${{WS_PYTHON:-python3}}" "{script if script.is_file() else GATE_PY}"'


def _set_hook_command(settings, command):
    """Every gate hook uses the same command; rewrite it to the script that actually exists."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            entries = group.get("hooks")
            for hook in entries if isinstance(entries, list) else []:
                if isinstance(hook, dict) and hook.get("type") == "command":
                    hook["command"] = command
    return settings


def security_base_settings():
    """Hooks + deny of the deterministic gate. The installed harness is used when it has a forcado.yaml;
    until item 33 installs it, the committed security/settings.json is the explicit fallback."""
    installed = paths.harness_dir()
    command = _gate_hook_command(installed)
    if (installed / "forcado.yaml").is_file():
        try:
            script = security_dir() / "gate.py"
            module = _security_module(script if script.is_file() else GATE_PY)
            return _set_hook_command(module.build_settings(module.load_rules(installed)), command)
        except SettingsError:
            raise
        except Exception as error:
            raise SettingsError(t("settings.err.render_harness", path=str(installed), error=str(error)))
    try:
        base = json.loads(SECURITY_SETTINGS.read_text(encoding="utf-8"))
        if not isinstance(base, dict):
            raise ValueError("settings root is not a mapping")
        return _set_hook_command(base, command)
    except (OSError, ValueError) as error:
        raise SettingsError(t("settings.err.base", path=str(SECURITY_SETTINGS), error=str(error)))


def _merge_settings(base, profile):
    """Base first, profile on top. Harness hooks always win and `permissions.deny` is a union, so the
    forced part survives; every other profile key (env, model, apiKeyHelper, flags) is applied."""
    merged = copy.deepcopy(base)
    for key, value in profile.items():
        if key == "hooks":
            continue
        if key == "permissions" and isinstance(value, dict):
            permissions = merged.get("permissions")
            if not isinstance(permissions, dict):
                permissions = merged["permissions"] = {}
            for pkey, pvalue in value.items():
                if pkey == "deny":
                    if isinstance(pvalue, list):
                        deny = permissions.get("deny") if isinstance(permissions.get("deny"), list) else []
                        permissions["deny"] = deny + [item for item in pvalue if item not in deny]
                    continue
                if isinstance(pvalue, dict) and isinstance(permissions.get(pkey), dict):
                    permissions[pkey] = _merge_settings(permissions[pkey], pvalue)
                else:
                    permissions[pkey] = copy.deepcopy(pvalue)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_settings(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _check_secrets(value, path="settings"):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "env" and isinstance(item, dict):
                for name, val in item.items():
                    if isinstance(val, str) and val and _SECRET_ENV.search(str(name)):
                        raise SettingsError(t("settings.err.secret", name=f"{path}.env.{name}"))
            _check_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_secrets(item, f"{path}[{index}]")
    elif isinstance(value, str) and _KEY_LITERAL.search(value):
        raise SettingsError(t("settings.err.secret_value", path=path))


def validate_settings(settings, profile=""):
    """Refuse a generated settings that carries a literal access key (F08)."""
    _check_secrets(settings, f"settings[{profile}]" if profile else "settings")


def _safe_profile_name(name):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(name or "claude"))
    return "claude" if safe in ("", ".", "..") else safe


def expected_profile_settings(name=None):
    """The settings dict a profile must carry, or None when it has no generated settings (see
    ensure_profile_settings). Writes nothing; `workspace config update` compares it with the file."""
    name = name or agent_name()
    if not manage_enabled():
        return None
    if not is_claude_harness(name):
        return None
    if _has_settings_flag(profile_command(name)):
        return None
    settings = _merge_settings(security_base_settings(), profile_settings(name) or {})
    validate_settings(settings, name)
    return settings


def profile_settings_path(name=None):
    """Where the generated settings of a profile live (it may not exist yet)."""
    return settings_dir() / f"{_safe_profile_name(name or agent_name())}.json"


def write_config_text(text):
    """Replace config_path() with `text`, keeping a copy of the current file as
    `config.yaml.bak-<stamp>` (never overwritten). Returns the backup path, or None when there was no
    file. The write goes through a temporary file and a replace, so a failure leaves the old file."""
    path = config_path()
    backup = None
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        counter = 1
        while backup.exists():
            counter += 1
            backup = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
        backup.write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return backup


def ensure_profile_settings(name=None):
    """Write the profile's generated settings and return its path. None means the profile has no
    workspace-generated settings: `manage: false` (the workspace leaves the agent options alone), a
    legacy string profile that already owns `--settings`, or an OpenCode profile (its config comes
    from `OPENCODE_CONFIG`). Raises SettingsError when the settings cannot be generated safely."""
    name = name or agent_name()
    settings = expected_profile_settings(name)
    if settings is None:
        return None
    directory = settings_dir()
    path = profile_settings_path(name)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(settings, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                             encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as error:
        raise SettingsError(t("settings.err.write", path=str(path), error=str(error)))
    return path


# Usual per-user binary folders that a tmux server started without the interactive shell may lack:
# the rc that appends `~/.opencode/bin` never runs, so `env opencode` exits with "No such file or
# directory". They are added to the launch line's PATH when they exist and are missing from it.
# The system folders are the Homebrew locations a GUI-launched agent on macOS does not inherit;
# they are only added there (on Linux they are already in PATH, and adding them would surprise a
# launch that builds a minimal PATH on purpose).
HOMEBREW_BIN_DIRS = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin")
AGENT_BIN_DIRS = ("~/.opencode/bin", "~/.local/bin", "~/bin", "~/.bun/bin") + HOMEBREW_BIN_DIRS


def agent_path(path=None):
    """PATH (the current one by default) plus the existing AGENT_BIN_DIRS missing from it.

    Entries are compared after `normpath`, so a folder already present is never added twice. The
    Homebrew folders are macOS-only. The user and the cwd stay untouched: only the launch line's
    environment changes."""
    entries = (os.environ.get("PATH", "") if path is None else path).split(os.pathsep)
    seen = {os.path.normpath(entry) for entry in entries if entry}
    darwin = _is_darwin()
    for folder in AGENT_BIN_DIRS:
        if folder in HOMEBREW_BIN_DIRS and not darwin:
            continue
        full = os.path.expanduser(folder)
        key = os.path.normpath(full)
        if key in seen or not os.path.isdir(full):
            continue
        entries.append(full)
        seen.add(key)
    return os.pathsep.join(entries)


def agent_launch_command(mode="", repo=None, profile=None, tab=None):
    """Command line that actually launches one profile's agent.

    `profile` names the profile; without it the configured agent is used. The harness line is the
    adapter's (`profile_adapter(...).launch`): Claude adds the generated settings, the tool-search
    flag and `--permission-mode`; OpenCode exports `OPENCODE_CONFIG` (the generated config with the
    workspace plugin) and adds the user binary folders to `PATH`. With `manage: false` the Claude
    profile runs exactly as it declares. Nothing here branches by agent.

    With `repo`, the line is prefixed with the session box (`WS_SESSION_ROLE`/`WS_SESSION_ROOT`/
    `WS_SESSION_PROFILE` from `src/session.py`), so the hook and the OpenCode plugin enforce the same
    role table. A `tab` also adds `WS_SESSION_TAB`/`WS_STATE`, the per-tab session recording used by
    `session_command`. Without a repo (the conference worker) no box is declared on purpose. `auto` is
    never fixed here: the policy reads it from the event's `permission_mode`."""
    if repo and not repo_enabled(repo):
        raise RepoDisabledError(t("repo.disabled", repo=os.path.basename(expand_path(repo))))
    name = profile or agent_name()
    cmd = profile_command(name)
    if _KEY_LITERAL.search(cmd):
        # a key in the command would travel in argv; F08 forbids it
        raise SettingsError(t("settings.err.secret_value", path=f"profiles[{name}].command"))
    launch = profile_adapter(name).launch(name, mode)
    box = session.prefix(repo, _main_path(), name, tab) if repo else ""
    return f"{box} {launch}" if box else launch


def _main_path():
    """Path of the config's main repo, or None (the main repo is the `home` session)."""
    main = read_main()
    return main[1] if main else None


# Per-repo agent identity of the last opening: {repo path: {"command": ...}}. Only the OpenCode
# harness records here (its identity has no Claude permission state); the Claude harness records its
# identity in permission.json, per profile (item 97).
AGENT_STATE = STATE / "agent.json"


def _read_agent_state():
    try:
        data = json.loads(AGENT_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_agent_state(state):
    try:
        AGENT_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = AGENT_STATE.with_name(AGENT_STATE.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(AGENT_STATE)
    except OSError:
        pass


def project_dir(repo):
    """Claude Code project folder of a cwd: under ~/.claude/projects, every non-alphanumeric as `-`."""
    return HOME / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", repo)


def _newest_jsonl(folder):
    try:
        files = [f for f in folder.glob("*.jsonl") if f.is_file()]
        return max(files, key=lambda f: f.stat().st_mtime) if files else None
    except OSError:
        return None


def previous_transcript(repo):
    """Newest transcript of the repo: ~/.claude/projects/<project>/*.jsonl, else the `transcript_path`
    recorded in the session cache. Returns a Path or None."""
    repo = expand_path(repo)
    found = _newest_jsonl(project_dir(repo))
    if found:
        return found
    best = None
    try:
        files = list(SESSIONS.glob("*.json"))
    except OSError:
        return None
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cwd = d.get("cwd") if isinstance(d, dict) else None
        if not isinstance(cwd, str) or not cwd or expand_path(cwd) != repo:
            continue
        path = d.get("transcript_path")
        if not isinstance(path, str) or not path.strip():
            continue
        path = Path(os.path.expanduser(path))
        if not path.is_file():
            continue
        ts = d.get("ts") if isinstance(d.get("ts"), (int, float)) else 0
        if best is None or ts >= best[0]:
            best = (ts, path)
    return best[1] if best else None


def _state_call(repo, tab, session=None):
    """Shell call that resets a tab's session id through `session_state.py` (used in fallbacks)."""
    arguments = [sys.executable, str(Path(session_state.__file__).resolve()), "reset-session",
                 str(repo), str(tab)]
    if session is not None:
        arguments.append(str(session))
    return " ".join(shlex.quote(argument) for argument in arguments)


def _session_args(launch, args):
    """Launch line plus session arguments; an empty list leaves the line untouched."""
    tokens = [str(arg) for arg in args or []]
    if not tokens:
        return launch
    return " ".join([launch, *(shlex.quote(token) for token in tokens)])


def _fresh_id(adapter):
    """Session id the workspace must choose before launch, or "" when the agent reports it."""
    from agents import SESSION_ID_WORKSPACE

    return session_state.new_id() if adapter.session_id_source == SESSION_ID_WORKSPACE else ""


def session_command(profile, repo, tab=""):
    """Session command line of one profile for a repo, built from the adapter's lifecycle contract.

    Without `tab` (the legacy two-argument call), the line is `launch + continue_args || launch`: it
    resumes the last session of the repo and falls back to a new one. With `tab`, the tab's own
    session id is used instead, so two tabs never resume each other's session:

    - the adapter says where the id comes from (`session_id_source`). When the workspace chooses it
      (Claude), a new tab gets `fresh_args(<id>)` and a recorded id gets `resume_args(<id>)`; an id
      that no longer exists falls back to a fresh session with a new id.
    - when the agent reports it (OpenCode), a new tab starts a fresh session with no session
      argument and never adopts the repo's last session in silence; the plugin records the real id
      for the next restore.

    The permission mode is read and recorded only for an adapter that declares
    `records_permission_mode`, per repo+profile, so two profiles can run in the same repo without
    overwriting each other's mode. A SettingsError from `agent_launch_command` propagates: nothing is
    recorded before the line is built."""
    repo = expand_path(repo)
    if not repo_enabled(repo):   # before anything is recorded for the repo
        raise RepoDisabledError(t("repo.disabled", repo=os.path.basename(repo)))
    adapter = profile_adapter(profile)
    if adapter.records_permission_mode:
        identity = agent_identity(profile)
        mode = permission_state.mode_for(repo, identity, True, profile)
        launch = agent_launch_command(mode, repo, profile, tab or None)
        permission_state.record_launch(repo, identity, mode, profile)
    else:
        launch = agent_launch_command("", repo, profile, tab or None)
        state = _read_agent_state()
        state[repo] = {"command": agent_identity(profile)}
        _write_agent_state(state)
    if not tab:
        continued = _session_args(launch, adapter.continue_args())
        return f"{continued} || {launch}" if continued != launch else launch
    session = session_state.session_for(repo, tab)
    if session:
        session_state.add(repo, tab, profile, session)
        resumed = _session_args(launch, adapter.resume_args(session))
        fresh = _fresh_id(adapter)
        reset = _state_call(repo, tab, fresh or None)
        return f"{resumed} || {{ {reset} && {_session_args(launch, adapter.fresh_args(fresh))}; }}"
    fresh = _fresh_id(adapter)
    session_state.reset_session(repo, tab, fresh)
    session_state.add(repo, tab, profile, fresh or None)
    return _session_args(launch, adapter.fresh_args(fresh))


def opening_command(repo, profile=None):
    """Command line that opens the repo's agent pane: the session command of the default profile.

    Every session (Claude or OpenCode) now tries `--continue` and falls back to a fresh session
    (item 97); the old migration prompt is gone."""
    return session_command(profile or agent_name(), repo)


def set_agent(name):
    """Persists the chosen agent in the config (other keys untouched). Refuses to overwrite a config that
    failed to read (ConfigError). With only the old settings.json, the whole config is written as YAML."""
    cfg = read_config()
    if CONFIG_ERROR:
        raise ConfigError(str(config_path()))
    cfg["agent"] = name
    path = config_path()
    text = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n" if path.suffix == ".json" else yaml_min.dumps(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _entries(paths):
    """Paths of a group list: each entry is a string, or {"path": ..., "siblings": [...]}. Bad entries are skipped."""
    res = []
    for c in paths if isinstance(paths, list) else []:
        if isinstance(c, dict):
            c = c.get("path")
        if isinstance(c, str) and c:
            res.append(expand_path(c))
    return res


def repos_folder():
    """Repos folder (config "repos_folder"), expanded, or None. Every git repo directly inside it is a repo."""
    value = read_config().get("repos_folder")
    if not isinstance(value, str) or not value.strip():
        return None
    return expand_path(value.strip())


def folder_repos(folder=None):
    """[(name, path), ...] of the git repos directly inside the repos folder, sorted by name. [] without a folder."""
    folder = folder or repos_folder()
    if not folder or not os.path.isdir(folder):
        return []
    found = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if name.startswith(".") or not os.path.isdir(path) or not os.path.exists(os.path.join(path, ".git")):
            continue
        found.append((name, path))
    return found


def free_folders():
    """[(label, path), ...] of the free folders ("free_folders" in config.yaml), in config order.

    A free folder is a plain folder (Documents, Pictures, Videos...) shown as a navigation tab of
    the home window: it is not a repo, and gets no window and no agent session. Each entry is a path
    string, or a map with `path` and an optional `label`; paths accept ~. Entries with no usable path
    are skipped, and a repeated path keeps the first label. A missing folder is still listed (the
    panel shows it as not found), so a typo in the config is visible.
    """
    raw = read_config().get("free_folders")
    found, seen = [], set()
    for entry in raw if isinstance(raw, list) else []:
        label = None
        if isinstance(entry, dict):
            label, entry = entry.get("label"), entry.get("path")
        if not isinstance(entry, str) or not entry.strip():
            continue
        path = expand_path(entry.strip())
        if path in seen:
            continue
        seen.add(path)
        name = str(label).strip() if isinstance(label, str) and label.strip() else ""
        found.append((name or os.path.basename(path.rstrip("/")) or path, path))
    return found


def disabled_repos():
    """Names of the folder repos switched off (config "repos_disabled"). Every other repo is on."""
    value = read_config().get("repos_disabled")
    if not isinstance(value, list):
        return set()
    return {name for name in value if isinstance(name, str) and name}


def repo_enabled(path):
    """False only for a repo directly inside the repos folder that is listed in "repos_disabled".

    Paths outside the folder (the main repo, worktrees, `workspace open <path>`) are not governed by
    the switch and stay on. Without a folder every repo is on."""
    folder = repos_folder()
    if not folder:
        return True
    target = expand_path(path)
    if os.path.dirname(target) != folder:
        return True
    return os.path.basename(target) not in disabled_repos()


def set_repo_enabled(name, enabled):
    """Switches one folder repo on or off in config.yaml ("repos_disabled"); other keys are kept.

    The previous file is copied to `<config>.bak` before the write, and the write is atomic. Raises
    ConfigError when the config cannot be read, ValueError when `name` is not a repo of the folder."""
    cfg = read_config()
    if CONFIG_ERROR:
        raise ConfigError(t("repo.config_error", path=str(config_path())))
    if name not in {n for n, _ in folder_repos()}:
        raise ValueError(t("repo.not_in_folder", repo=name))
    off = disabled_repos()
    if enabled:
        off.discard(name)
    else:
        off.add(name)
    if off:
        cfg["repos_disabled"] = sorted(off)
    else:
        cfg.pop("repos_disabled", None)
    path = config_path()
    if path.exists():
        path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
    text = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n" if path.suffix == ".json" else yaml_min.dumps(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---- minimal textual edits of config.yaml -------------------------------------------------------
# The context menu writes a group or a sibling relation without rewriting the file: only the lines
# of the affected block are inserted or removed, so comments and the rest of the person's config
# survive. The writes go through write_config_text (backup + atomic replace).
# -------------------------------------------------------------------------------------------------

GROUP_KEY = "groups"
SIBLINGS_KEY = "siblings"


def _content_line(line):
    text = line.strip()
    return bool(text) and not text.startswith("#")


def _indent_of(line):
    return len(line) - len(line.lstrip(" "))


def _line_data(line):
    """(key, value) of a mapping line, or None. A block key with children reads as value None."""
    text = line.strip()
    if not text or text.startswith("#") or text.startswith("-"):
        return None
    try:
        data = yaml_min.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict) and len(data) == 1:
        return next(iter(data.items()))
    return None


def _section(lines, name):
    """{"begin", "end", "last"} of a top-level key block in `lines`, or None.

    `end` is the next top-level key; `last` is the last content line of the block, so an insertion
    there leaves trailing comments with the next key."""
    start = None
    for i, line in enumerate(lines):
        if _indent_of(line) == 0 and _content_line(line):
            pair = _line_data(line)
            if pair and pair[0] == name:
                start = i
                break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _content_line(lines[j]) and _indent_of(lines[j]) == 0:
            end = j
            break
    last = start
    for j in range(start, end):
        if _content_line(lines[j]):
            last = j
    return {"begin": start, "end": end, "last": last}


def _children(lines, section):
    """([{"key", "value", "begin", "end", "indent"}], child_indent) of the mappings in `section`."""
    first = next((j for j in range(section["begin"] + 1, section["end"])
                  if _content_line(lines[j])), None)
    if first is None:
        return [], section["begin"] + 2
    child_indent = _indent_of(lines[first])
    if child_indent == 0:
        return [], 2
    children = []
    j = first
    while j < section["end"]:
        if _content_line(lines[j]) and _indent_of(lines[j]) == child_indent:
            pair = _line_data(lines[j])
            if pair is None:
                j += 1
                continue
            end = j + 1
            while end < section["end"] and not (_content_line(lines[end])
                                                and _indent_of(lines[end]) <= child_indent):
                end += 1
            children.append({"key": pair[0], "value": pair[1], "begin": j, "end": end,
                             "indent": child_indent})
            j = end
        else:
            j += 1
    return children, child_indent


def _list_entries(lines, group):
    """([{"begin", "end", "indent"}], list_indent) of the block sequence under `group`."""
    first = next((j for j in range(group["begin"] + 1, group["end"])
                  if _content_line(lines[j])), None)
    if first is None:
        return [], group["indent"] + 2
    list_indent = _indent_of(lines[first])
    if not lines[first].strip().startswith("-"):
        return [], list_indent
    entries = []
    j = first
    while j < group["end"]:
        if (_content_line(lines[j]) and _indent_of(lines[j]) == list_indent
                and lines[j].strip().startswith("-")):
            end = j + 1
            while end < group["end"] and not (_content_line(lines[end])
                                              and _indent_of(lines[end]) <= list_indent):
                end += 1
            entries.append({"begin": j, "end": end, "indent": list_indent})
            j = end
        else:
            j += 1
    return entries, list_indent


def _entry_item(lines, entry):
    """The sequence item (string or mapping) of one list entry, or None when it cannot be read."""
    chunk = [line[entry["indent"]:] for line in lines[entry["begin"]:entry["end"]]
             if _content_line(line)]
    if not chunk:
        return None
    try:
        data = yaml_min.loads("\n".join(chunk))
    except ValueError:
        return None
    return data[0] if isinstance(data, list) and len(data) == 1 else None


def _item_path(item):
    value = item.get("path") if isinstance(item, dict) else item
    return expand_path(value) if isinstance(value, str) and value else None


def _config_path_value(path):
    """Path as written in config.yaml: `~`-relative inside the home, absolute otherwise."""
    path = expand_path(path)
    home = str(HOME)
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _entry_lines(item, indent):
    """Canonical YAML lines of a sequence item at `indent` (a string or {path, siblings})."""
    block = yaml_min.dumps({"e": [item]}).splitlines()[1:]
    return [" " * indent + line[2:] for line in block]


def _key_text(key):
    first = yaml_min.dumps({key: []}).splitlines()[0]
    return first[:-len(": []")]


def _trailing_comment(line):
    """The `# ...` comment of a line when it is outside quotes, else ""."""
    quote = None
    i = 0
    while i < len(line):
        char = line[i]
        if quote == '"':
            if char == "\\":
                i += 2
                continue
            if char == '"':
                quote = None
        elif quote == "'":
            if char == "'":
                quote = None
        elif char in "\"'" and (i == 0 or line[i - 1] in " [,:-"):
            quote = char
        elif char == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[i:]
        i += 1
    return ""


def _groups_model(lines):
    section = _section(lines, GROUP_KEY)
    if section is None:
        return None, []
    groups, _indent = _children(lines, section)
    return section, groups


def _normalize_inline_groups(lines, target):
    """Rewrites a `Group: [repo]` line that holds `target` as a block list, keeping its comment."""
    _section_value, groups = _groups_model(lines)
    for group in reversed(groups):
        if not isinstance(group["value"], list):
            continue
        if not any(_item_path(item) == target for item in group["value"]):
            continue
        comment = _trailing_comment(lines[group["begin"]])
        header = " " * group["indent"] + _key_text(group["key"]) + ":"
        if comment:
            header += "  " + comment
        block = [header]
        for item in group["value"]:
            block += _entry_lines(item, group["indent"] + 2)
        lines[group["begin"]:group["begin"] + 1] = block
    return lines


def _find_group_entries(lines, target):
    """[(group, entry, item)] for every group entry pointing at `target`."""
    _section_value, groups = _groups_model(lines)
    found = []
    for group in groups:
        if isinstance(group["value"], list):
            for item in group["value"]:
                if _item_path(item) == target:
                    found.append((group, None, item))
            continue
        entries, _list_indent = _list_entries(lines, group)
        for entry in entries:
            item = _entry_item(lines, entry)
            if _item_path(item) == target:
                found.append((group, entry, item))
    return found


def _merged_entry(matches, target):
    """One item for the moved repo: keeps the first mapping and the union of the declared siblings."""
    if not matches:
        return _config_path_value(target)
    items = [item for _group, _entry, item in matches]
    mappings = [item for item in items if isinstance(item, dict)]
    if not mappings:
        return items[0]
    merged = dict(mappings[0])
    merged["path"] = mappings[0].get("path") or items[0]
    siblings = []
    for mapping in mappings:
        for sibling in mapping.get("siblings") if isinstance(mapping.get("siblings"), list) else []:
            if isinstance(sibling, str) and sibling and sibling not in siblings:
                siblings.append(sibling)
    if siblings:
        merged["siblings"] = siblings
    else:
        merged.pop("siblings", None)
    return merged


def _append_to_group(lines, name, item, comment=""):
    section, groups = _groups_model(lines)
    if section is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{GROUP_KEY}:")
        section = _section(lines, GROUP_KEY)
        groups = []
    else:
        pair = _line_data(lines[section["begin"]])
        if pair and pair[1] not in (None, {}, []):
            raise ConfigError(t("repos.err.write", path=str(config_path())))
        if pair and pair[1] is not None:      # "groups: {}": make it a block
            comment = _trailing_comment(lines[section["begin"]])
            header = " " * _indent_of(lines[section["begin"]]) + _key_text(GROUP_KEY) + ":"
            if comment:
                header += "  " + comment
            lines[section["begin"]] = header
            section = _section(lines, GROUP_KEY)
            groups = []
    target = next((group for group in groups if group["key"] == name), None)
    if target is None:
        indent = groups[0]["indent"] if groups else 2
        block = yaml_min.dumps({name: [item]}).splitlines()
        if comment and len(block) > 1:
            block[1] += "  " + comment
        at = section["last"] + 1
        lines[at:at] = [" " * indent + line for line in block]
        return lines
    if isinstance(target["value"], list):
        comment = _trailing_comment(lines[target["begin"]])
        header = " " * target["indent"] + _key_text(target["key"]) + ":"
        if comment:
            header += "  " + comment
        block = [header]
        for existing in target["value"]:
            block += _entry_lines(existing, target["indent"] + 2)
        new_lines = _entry_lines(item, target["indent"] + 2)
        if comment:
            new_lines[0] += "  " + comment
        lines[target["begin"]:target["begin"] + 1] = block + new_lines
        return lines
    entries, list_indent = _list_entries(lines, target)
    if entries:
        at = max(j for j in range(target["begin"], target["end"]) if _content_line(lines[j])) + 1
    else:
        at = target["begin"] + 1
        list_indent = target["indent"] + 2
    new_lines = _entry_lines(item, list_indent)
    if comment:
        new_lines[0] += "  " + comment
    lines[at:at] = new_lines
    return lines


def _text_lines(text):
    return text.splitlines()


def _text_join(lines, original):
    joined = "\n".join(lines)
    return joined + "\n" if original.endswith("\n") else joined


def _check_repo_path(path):
    """A repo the menu may write: a folder repo, or the main repo (which stays outside the groups)."""
    main = read_main()
    if main and path == main[1]:
        return
    folder = repos_folder()
    if folder and path not in {p for _, p in folder_repos(folder)}:
        raise ValueError(t("repo.not_in_folder", repo=os.path.basename(path)))


def _check_group_written(data, group, target):
    groups = data.get(GROUP_KEY)
    items = groups.get(group) if isinstance(groups, dict) else None
    if not isinstance(items, list) or not any(_item_path(item) == target for item in items):
        raise ConfigError(t("repos.err.write", path=str(config_path())))


def _write_config_data(cfg):
    text = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    write_config_text(text)


def _edit_config_text(edit, check):
    """Applies `edit(text) -> text` to config.yaml, validates it, then writes with a backup."""
    path = config_path()
    read_config()      # refreshes CONFIG_ERROR
    if CONFIG_ERROR:
        raise ConfigError(t("repo.config_error", path=str(path)))
    if not path.exists():
        raise ConfigError(t("repo.config_error", path=str(path)))
    text = path.read_text(encoding="utf-8")
    new_text = edit(text)
    if new_text == text:          # nothing changed: no backup, no write
        return
    try:
        data = yaml_min.loads(new_text)
    except ValueError as error:
        raise ConfigError(t("repos.err.write", path=str(path))) from error
    if not isinstance(data, dict):
        raise ConfigError(t("repos.err.write", path=str(path)))
    check(data)
    write_config_text(new_text)


def _move_in_data(cfg, target, group):
    groups = cfg.get(GROUP_KEY)
    if not isinstance(groups, dict):
        groups = {}
        cfg[GROUP_KEY] = groups
    found = []
    for name, items in list(groups.items()):
        if not isinstance(items, list):
            continue
        keep = []
        for item in items:
            if _item_path(item) == target:
                found.append(item)
            else:
                keep.append(item)
        groups[name] = keep
    groups.setdefault(group, []).append(_merged_entry(found, target))
    return cfg


def _sibling_in_data(cfg, target, other):
    siblings = cfg.get(SIBLINGS_KEY)
    if not isinstance(siblings, dict):
        siblings = {}
        cfg[SIBLINGS_KEY] = siblings
    key = next((name for name in siblings if expand_path(name) == target), _config_path_value(target))
    declared = siblings.get(key)
    if not isinstance(declared, list):
        declared = []
        siblings[key] = declared
    if not any(isinstance(item, str) and expand_path(item) == other for item in declared):
        declared.append(_config_path_value(other))
    return cfg


def move_repo_to_group(path, group):
    """Moves one repo to `group` in config.yaml, creating the group when needed.

    Other keys, comments and the rest of the file are preserved, and the previous file is kept as a
    backup. Raises ConfigError when the config cannot be read or written safely, and ValueError when
    the repo is not a folder repo or the group name is empty."""
    group = (group or "").strip()
    if not group or "\n" in group or "\r" in group:
        raise ValueError(t("repos.err.group_name", group=group))
    target = expand_path(path)
    _check_repo_path(target)

    if config_path().suffix == ".json":
        cfg = read_config()
        if CONFIG_ERROR:
            raise ConfigError(t("repo.config_error", path=str(config_path())))
        _move_in_data(cfg, target, group)
        _write_config_data(cfg)
        return

    def edit(text):
        lines = _normalize_inline_groups(_text_lines(text), target)
        matches = _find_group_entries(lines, target)
        if len(matches) == 1 and matches[0][0]["key"] == group:
            return text
        item = _merged_entry(matches, target)
        comments = []
        remove = set()
        for _group, entry, _item in matches:
            comment = _trailing_comment(lines[entry["begin"]])
            if comment and comment not in comments:
                comments.append(comment)
            for j in range(entry["begin"], entry["end"]):
                if _content_line(lines[j]) and _indent_of(lines[j]) >= entry["indent"]:
                    remove.add(j)
        lines = [line for j, line in enumerate(lines) if j not in remove]
        lines = _append_to_group(lines, group, item, " ".join(comments))
        return _text_join(lines, text)

    _edit_config_text(edit, lambda data: _check_group_written(data, group, target))


def mark_repo_sibling(path, other):
    """Declares `other` as a sibling of `path` in config.yaml; the relation reads in both directions.

    The relation goes to the root "siblings" map, so it also works for a repo with no "groups" entry
    (the implicit "Sem categoria"). Comments and the rest of the file are preserved, with a backup."""
    target, sibling = expand_path(path), expand_path(other)
    if target == sibling:
        raise ValueError(t("repos.err.same_repo"))
    _check_repo_path(target)
    _check_repo_path(sibling)
    cfg = read_config()
    if CONFIG_ERROR:
        raise ConfigError(t("repo.config_error", path=str(config_path())))
    if sibling in _sibling_pairs(cfg).get(target, set()):
        return                        # already a sibling, declared on either side

    if config_path().suffix == ".json":
        _sibling_in_data(cfg, target, sibling)
        _write_config_data(cfg)
        return

    def edit(text):
        return _text_join(_add_sibling(_text_lines(text), target, sibling), text)

    def check(data):
        if sibling not in _sibling_pairs(data).get(target, set()):
            raise ConfigError(t("repos.err.write", path=str(config_path())))

    _edit_config_text(edit, check)


def _flow_add(line, value):
    start, end = line.find("["), line.rfind("]")
    if start < 0 or end < start:
        raise ConfigError(t("repos.err.write", path=str(config_path())))
    inside = line[start + 1:end].strip()
    merged = f"{inside}, {value}" if inside else value
    return line[:start + 1] + merged + line[end:]


def _add_sibling(lines, target, other):
    section = _section(lines, SIBLINGS_KEY)
    if section is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{SIBLINGS_KEY}:")
        section = _section(lines, SIBLINGS_KEY)
        children, indent = [], 2
    else:
        pair = _line_data(lines[section["begin"]])
        if pair and pair[1] not in (None, {}, []):
            raise ConfigError(t("repos.err.write", path=str(config_path())))
        if pair and pair[1] is not None:      # "siblings: {}" / "siblings: []": make it a block
            comment = _trailing_comment(lines[section["begin"]])
            header = " " * _indent_of(lines[section["begin"]]) + _key_text(SIBLINGS_KEY) + ":"
            if comment:
                header += "  " + comment
            lines[section["begin"]] = header
            section = _section(lines, SIBLINGS_KEY)
        children, indent = _children(lines, section)
    key, value = _config_path_value(target), _config_path_value(other)
    for child in children:
        if expand_path(child["key"]) != target:
            continue
        declared = child["value"]
        if isinstance(declared, list):
            if any(isinstance(item, str) and expand_path(item) == other for item in declared):
                return lines
            lines[child["begin"]] = _flow_add(
                lines[child["begin"]], yaml_min.dumps({"e": value}).split("e: ", 1)[1])
            return lines
        if declared is None:
            entries, list_indent = _list_entries(lines, child)
            for entry in entries:
                if _item_path(_entry_item(lines, entry)) == other:
                    return lines
            if entries:
                at = max(j for j in range(child["begin"], child["end"])
                         if _content_line(lines[j])) + 1
            else:
                at = child["begin"] + 1
                list_indent = child["indent"] + 2
            lines[at:at] = [" " * list_indent + "- " + value]
            return lines
        raise ConfigError(t("repos.err.write", path=str(config_path())))
    block = yaml_min.dumps({key: [value]}).splitlines()
    at = section["last"] + 1
    lines[at:at] = [" " * indent + line for line in block]
    return lines


def group_names():
    """Group names declared in config.yaml, in file order. The implicit group of the folder is not here."""
    groups = read_config().get("groups")
    return [name for name in groups if isinstance(name, str) and name] if isinstance(groups, dict) else []


def _folder_repos_groups(folder):
    """Groups of the repos folder: the declared "groups" first, then the implicit uncategorized group.

    A "groups" entry only counts when it points to a repo directly inside the folder; the folder is the
    only source of repos. A repo in no declared group is shown under the implicit "Sem categoria"."""
    repos = folder_repos(folder)
    if not repos:
        return []
    by_path = {path: name for name, path in repos}
    declared = read_config().get("groups")
    groups = []
    used = set()
    for group_name, paths in declared.items() if isinstance(declared, dict) else []:
        if not isinstance(group_name, str) or not group_name:
            continue
        members = [(by_path[path], path) for path in _entries(paths) if path in by_path]
        if members:
            groups.append((group_name, members))
            used |= {path for _, path in members}
    rest = [(name, path) for name, path in repos if path not in used]
    if rest:
        groups.append((t("repos.group.uncategorized"), rest))
    return groups


def read_repos():
    """[(group, [(name, path), ...]), ...]. With "repos_folder" the folder is the only source: "groups" classify
    its git repos and a repo in no group goes to the implicit "Sem categoria" (repos outside the folder are not
    supported). Without it: "groups" (category; each entry is a path or {path, siblings})."""
    folder = repos_folder()
    if folder:
        return _folder_repos_groups(folder)
    groups = []
    for group_name, paths in read_config().get("groups", {}).items():
        repos = [(os.path.basename(p) or p, p) for p in _entries(paths)]
        groups.append((group_name, repos))
    return groups


def read_relations():
    """[(name, [path, ...]), ...] from "relations": grouping by relation, independent of "groups". Opens no windows."""
    relations = read_config().get("relations", {})
    if not isinstance(relations, dict):
        return []
    return [(name, _entries(paths)) for name, paths in relations.items()]


def _sibling_pairs(cfg):
    """{path: {siblings}}: "siblings" holds in both directions, on a "groups" entry or at the root.

    The root map (repo path -> siblings) is written by the repos context menu for repos that have no
    "groups" entry, including the ones in the implicit "Sem categoria"."""
    pairs = {}
    for paths in cfg.get("groups", {}).values():
        for entry in paths if isinstance(paths, list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
                continue
            repo = expand_path(entry["path"])
            declared = entry.get("siblings")
            for path in declared if isinstance(declared, list) else []:
                if isinstance(path, str) and path and expand_path(path) != repo:
                    other = expand_path(path)
                    pairs.setdefault(repo, set()).add(other)
                    pairs.setdefault(other, set()).add(repo)
    root = cfg.get("siblings")
    for key, declared in root.items() if isinstance(root, dict) else []:
        if not isinstance(key, str) or not key:
            continue
        repo = expand_path(key)
        for path in declared if isinstance(declared, list) else []:
            if isinstance(path, str) and path and expand_path(path) != repo:
                other = expand_path(path)
                pairs.setdefault(repo, set()).add(other)
                pairs.setdefault(other, set()).add(repo)
    return pairs


def _inside(path, folder):
    return path == folder or path.startswith(folder + "/")


def siblings(path):
    """Siblings of a repo, without itself, sorted. Two sources:
    (a) "parent_folder": when the repo is inside it, every listed repo (main and groups) also inside it;
    (b) "siblings" written on a "groups" entry, which holds in both directions (even outside the parent folder)."""
    cfg = read_config()
    target = expand_path(path)
    found = set(_sibling_pairs(cfg).get(target, set()))
    folder = cfg.get("parent_folder")
    if isinstance(folder, str) and folder:
        folder = expand_path(folder)
        if _inside(target, folder):
            listed = _entries([cfg.get("main")]) + [p for _, rs in read_repos() for _, p in rs]
            found |= {p for p in listed if p != target and _inside(p, folder)}
    return sorted(found)


def read_main():
    """(name, path) of the main repo from config.yaml ("main"), or None. It stays outside the groups."""
    c = read_config().get("main")
    if not isinstance(c, str) or not c:
        return None
    p = expand_path(c)
    return (os.path.basename(p) or p, p)


def current_repo():
    """Repo of the window where this panel runs (the @ws_repo option of the tmux window)."""
    pane = os.environ.get("TMUX_PANE")
    if pane:
        try:
            repo = tmux.display("#{@ws_repo}", target=pane)
            if repo:
                return repo
        except Exception:
            pass
    return os.getcwd()


def _color(d, now):
    state, ts = d.get("state", "idle"), d.get("ts", 0)
    # error: true keeps the red state until the next event; other red states age out like the rest.
    if now - ts > IDLE_AFTER_S and not (state == "question" and d.get("error") is True):
        return "idle"
    if state == "working":
        f = d.get("tool")
        if f and f not in ("Task", "Agent"):
            if now - ts > WAIT_CAP_S:  # no event for 30 min: the tool is not coming back
                return "idle"
            if now - d.get("tool_ts", ts) > WAIT_S:
                return "waiting"
        if not f and now - ts > NO_EVENT_S:     # no event for 10 min: the session died without notice
            return "idle"
    return state


def session_color(d, now):
    """Displayed state of one session file. `agents` is informational only: the hook keeps the state
    at "working" while background tasks run, so nothing here masks a red state."""
    return _color(d, now)


def repo_of(cwd, paths):
    """Longest path in `paths` that contains `cwd`, or None.

    The comparison uses `session_state.path_key`, so on macOS a repo written with another case or
    Unicode normalization still matches; the entry returned is always the original one."""
    key = session_state.path_key(cwd)
    best = None
    for p in paths:
        parent = session_state.path_key(p)
        if (key == parent or key.startswith(parent + "/")) and (best is None or len(p) > len(best)):
            best = p
    return best


def _pid_of(d):
    try:
        return max(0, int(d.get("claude_pid") or 0))
    except (TypeError, ValueError):
        return 0


def _is_darwin():
    """True on macOS. Kept as a function so tests can patch it and run the Darwin paths on Linux."""
    return sys.platform == "darwin"


def _ps_field(pid, field):
    """One `ps` column for `pid` on macOS, or None when `ps` fails (missing or unreadable process)."""
    try:
        proc = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)],
                              capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _darwin_pid_alive(pid):
    """`ps`-based liveness on macOS: a zombie is dead, and a reused pid is not claude."""
    state = _ps_field(pid, "stat")
    if not state or state.startswith("Z"):
        return False
    command = _ps_field(pid, "command")
    if command is None:
        return False
    return "claude" in command.lower()


def _pid_alive(pid):
    """True when `pid` is a live claude process; False when it died, became a zombie or was reused by
    another program; None when it cannot be told. Linux reads /proc; macOS uses `ps`."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:     # it exists, but belongs to another user
        pass
    except OSError:
        return None
    if _is_darwin():
        return _darwin_pid_alive(pid)
    if not os.path.isdir("/proc/self"):
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
        if state == "Z":
            return False
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
            name = f.read()
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except (OSError, IndexError):
        return False
    return "claude" in f"{name} {argv}".lower()


def _other_server(d, current_socket):
    """True when the session was recorded on a tmux server other than the one in use."""
    sock = d.get("tmux_socket")
    return bool(sock and current_socket and os.path.realpath(sock) != os.path.realpath(current_socket))


def _is_alive(d, live, current_socket):
    """True = alive, False = dead (may be deleted), None = cannot be told.

    With claude_pid > 0 the test is the process. Without it (old file), the test is the pane on the
    current server: a pane back at the shell is already out of `live`.
    """
    pid = _pid_of(d)
    if pid > 0:
        return _pid_alive(pid)
    if not d.get("pane"):
        return False
    if _other_server(d, current_socket) or live is None:
        return None
    return d["pane"] in live


def _live_sessions(now):
    """[(sid, state_json, color)] of live sessions on the current tmux server, newest first.

    Reads only <sid>.json (skips *.status.json and *.lock). Deletes a dead session file older than
    ORPHAN_AGE_S; never deletes a live session or one that cannot be tested. An unlink failure is ignored.
    """
    try:
        files = [f for f in SESSIONS.glob("*.json") if "." not in f.stem]
    except OSError:
        return []
    live = tmux.live_panes()
    current_socket = tmux.server_socket()
    res = []
    for f in files:
        try:
            d = json.loads(f.read_text())
            if not isinstance(d, dict):
                d = {}
        except Exception:       # unreadable: no pane or pid, so orphaned by the file age
            d = {}
        try:
            ref = d["ts"] if isinstance(d.get("ts"), (int, float)) else f.stat().st_mtime
        except OSError:
            ref = now
        alive = _is_alive(d, live, current_socket)
        if alive is False and now - ref > ORPHAN_AGE_S:
            try:
                f.unlink()
            except OSError:
                pass
            continue
        # Not shown: no pane, dead, or from another server (a pane id may repeat across servers).
        if alive is False or not d.get("pane") or _other_server(d, current_socket):
            continue
        res.append((f.stem, d, session_color(d, now)))
    res.sort(key=lambda x: x[1].get("ts", 0), reverse=True)
    return res


def session_for_pane(pane_id, now=None):
    """(sid, state_json, color) of the live session of the pane on the current server (the newest, if
    there is more than one), or None. For the statusbar and whoever needs one pane's session."""
    if not pane_id:
        return None
    for entry in _live_sessions(now or time.time()):
        if entry[1].get("pane") == pane_id:
            return entry
    return None


def live_sessions(paths, now=None):
    """{repo_path: [(sid, state_json, color)]} of live Claude sessions inside `paths`, newest first.

    See _live_sessions for what counts as live. Sessions without a cwd are skipped.
    """
    now = now or time.time()
    res = {}
    for sid, d, color in _live_sessions(now):
        cwd = d.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        repo = repo_of(expand_path(cwd), paths)
        if repo:
            res.setdefault(repo, []).append((sid, d, color))
    return res


def states_by_repo(repos, now=None):
    """{repo_path: 'idle'|'working'|'waiting'|'question'} — the worst session of the repo wins."""
    paths = [p for _, rs in repos for _, p in rs]
    res = {}
    for repo, sessions in live_sessions(paths, now).items():
        for _, _, c in sessions:
            if PRIORITY[c] >= PRIORITY[res.get(repo, "idle")]:
                res[repo] = c
    return res


def git_full(repo, *args, timeout=15, input=None):
    """(rc, stdout, stderr) of `git -C repo *args`; exceptions become rc 1 with the message in stderr."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C.UTF-8")
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                           timeout=timeout, env=env, input=input)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        # stdout keeps the old git() contract (the message); stderr makes it available to git_full().
        return 1, str(e), str(e)


def git(repo, *args, timeout=15, input=None):
    rc, out, _ = git_full(repo, *args, timeout=timeout, input=input)
    return rc, out


def git_status(repo, untracked=True):
    """`git status` as {path: xy}, or None when it is not a repo. Uses -z: unquoted paths,
    and a rename/copy carries the old path in an extra field, which is skipped."""
    rc, out = git(repo, "status", "--porcelain", "-z", "-uall" if untracked else "-uno")
    if rc != 0:
        return None
    res = {}
    fields = out.split("\0")
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if len(field) < 4:
            continue
        xy = field[:2]
        res[field[3:]] = xy
        if xy[0] in "RC":
            i += 1
    return res


def git_branch(repo):
    """Current branch; on a detached HEAD, the short hash."""
    rc, out = git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    if rc == 0 and out.strip():
        return out.strip()
    rc, out = git(repo, "rev-parse", "--short", "HEAD")
    return out.strip() if rc == 0 and out.strip() else "?"


_GH_CACHE = {}        # repo -> (ts, state)
_FETCH_TS = {}        # repo -> last fetch ts, only when it cannot be marked on disk
GH_TTL = 10           # seconds between local re-reads
FETCH_TTL = 300       # seconds between background fetches (only this tells whether it is behind)
_FETCH_DIR = STATE / "fetch"   # one marker per repo: shared across windows (processes)


def _fetch_bg(repo):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        subprocess.run(["git", "-C", repo, "fetch", "--quiet"], timeout=30, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _fetch_head(repo):
    """Repo FETCH_HEAD. In a worktree (or submodule) '.git' is a file with 'gitdir: <path>'."""
    g = os.path.join(repo, ".git")
    if os.path.isfile(g):
        try:
            with open(g, encoding="utf-8") as fh:
                line = fh.read().strip()
        except OSError:
            return None
        if not line.startswith("gitdir:"):
            return None
        d = line[len("gitdir:"):].strip()
        g = d if os.path.isabs(d) else os.path.join(repo, d)
    return os.path.join(g, "FETCH_HEAD")


def _mtime(p):
    try:
        return os.path.getmtime(p) if p else 0
    except OSError:
        return 0


def _fetch_due(repo, now):
    """True when no window fetched this repo in the last FETCH_TTL seconds; None when it cannot be marked.

    Last fetch = the newest between git FETCH_HEAD and a marker in STATE/fetch (the marker covers a
    fetch that failed, which does not rewrite FETCH_HEAD). The marker stays locked while it checks and
    writes, so two windows at the same instant do not trigger two fetches.
    """
    mark = _FETCH_DIR / hashlib.sha1(repo.encode("utf-8")).hexdigest()
    try:
        _FETCH_DIR.mkdir(parents=True, exist_ok=True)
        with open(mark, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                marked = float(f.read() or 0)
            except ValueError:
                marked = 0
            last = max(marked, _mtime(_fetch_head(repo)))
            if now - last < FETCH_TTL:
                return False
            f.seek(0)
            f.truncate()
            f.write(repr(now))
            f.flush()
            return True
    except OSError:
        return None


def git_state(repo):
    """(state, dirty): state = None without git · 'local' git created but not pushed · 'ok' pushed and up to date ·
    'diverged' ahead/behind. dirty = there is uncommitted work (modified or new, outside the stash)."""
    now = time.time()
    c = _GH_CACHE.get(repo)
    if c and now - c[0] < GH_TTL:
        return c[1]
    if not os.path.exists(os.path.join(repo, ".git")):
        state = None
    else:
        due = _fetch_due(repo, now)
        if due is None:    # cannot mark on disk: fall back to this window's timer
            due = now - _FETCH_TS.get(repo, 0) > FETCH_TTL
            if due:
                _FETCH_TS[repo] = now
        if due:
            import threading
            threading.Thread(target=_fetch_bg, args=(repo,), daemon=True).start()
        rc, out = git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}", timeout=5)
        if rc != 0:
            # no upstream: it is only "local" when there is no remote at all; with a remote (public or
            # private) the GitHub icon stays normal even without a tracking branch
            rc2, remotes = git(repo, "remote", timeout=5)
            state = "ok" if rc2 == 0 and remotes.strip() else "local"
        else:
            ahead, behind = (int(x) for x in out.split())
            state = "diverged" if ahead or behind else "ok"
    dirty = False
    if state is not None:
        rc, out = git(repo, "status", "--porcelain", timeout=5)
        dirty = rc == 0 and bool(out.strip())
    _GH_CACHE[repo] = (now, (state, dirty))
    return state, dirty


# --- Plugins -----------------------------------------------------------------------------------
# Listed in config.yaml ("plugins": {profile: [{"id", "path"}]}). Nothing is discovered: a folder
# is loaded only when its entry is there, and only from the fixed path written in the entry.
# The folder holds plugin.json: id, label (key of the plugin's own i18n/pt-BR.json), layout (same
# line format as REPO_TREE in bin/workspace; the root pane is the window itself and has role claude),
# needs (list of strings, validated but not acted on yet).
PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
PLUGIN_ROLE = re.compile(r"[a-z][a-z0-9-]*")
PLUGIN_SPLIT = re.compile(r"[vh][bf]*")
PLUGIN_SIZE = re.compile(r"(\d+%?|TERM|LEFT|RIGHT)?")


class PluginError(Exception):
    """Message already translated (i18n), ready to show the person."""


def listed_plugins(profile):
    """Raw entries of config.yaml["plugins"][profile]: only what is listed there is ever loaded."""
    return read_config().get("plugins", {}).get(profile, [])


def _layout_plugin(id_, text):
    """Checks the layout lines and returns them in the REPO_TREE format (role|parent|split|size|cmd)."""
    lines, roles = [], {"claude"}
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue

        def fail(key):
            return PluginError(t("plugin.err.layout", id=id_, line=n, detail=t(key)))

        fields = s.split("|", 4)
        if len(fields) != 5:
            raise fail("plugin.err.layout_fields")
        role, parent, split, size, command = fields
        if role == "rtabs":
            raise fail("plugin.err.layout_reserved")
        if not PLUGIN_ROLE.fullmatch(role) or role in roles:
            raise fail("plugin.err.layout_role")
        if parent not in roles:
            raise fail("plugin.err.layout_parent")
        if not PLUGIN_SPLIT.fullmatch(split):
            raise fail("plugin.err.layout_split")
        if not PLUGIN_SIZE.fullmatch(size):
            raise fail("plugin.err.layout_size")
        if command != "shell":
            raise fail("plugin.err.layout_cmd")
        roles.add(role)
        lines.append(f"{role}|{parent}|{split}|{size}|{command}")
    if not lines:
        raise PluginError(t("plugin.err.field", id=id_, field="layout"))
    return "\n".join(lines)


def load_plugin(entry):
    """(id, folder, translated label, layout) of one config.yaml entry; raises PluginError if invalid."""
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) \
            or not isinstance(entry.get("path"), str):
        raise PluginError(t("plugin.err.entry", entry=json.dumps(entry, ensure_ascii=False)))
    id_ = entry["id"]
    if not PLUGIN_ID.fullmatch(id_):
        raise PluginError(t("plugin.err.id", id=id_))
    folder = expand_path(entry["path"])
    file = Path(folder) / "plugin.json"
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PluginError(t("plugin.err.not_found", id=id_, path=folder))
    except (OSError, ValueError):
        raise PluginError(t("plugin.err.json", id=id_, path=str(file)))
    if not isinstance(data, dict):
        raise PluginError(t("plugin.err.json", id=id_, path=str(file)))
    if data.get("id") != id_:
        raise PluginError(t("plugin.err.id_mismatch", id=id_, found=str(data.get("id"))))
    label = data.get("label")
    if not isinstance(label, str) or not label:
        raise PluginError(t("plugin.err.field", id=id_, field="label"))
    layout = data.get("layout")
    if not isinstance(layout, str):
        raise PluginError(t("plugin.err.field", id=id_, field="layout"))
    needs = data.get("needs", [])
    if not isinstance(needs, list) or not all(isinstance(x, str) for x in needs):
        raise PluginError(t("plugin.err.field", id=id_, field="needs"))
    try:
        texts = json.loads((Path(folder) / "i18n" / "pt-BR.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        texts = {}
    if label not in texts:
        raise PluginError(t("plugin.err.label", id=id_, key=label))
    return id_, folder, texts[label], _layout_plugin(id_, layout)


if __name__ == "__main__":
    import sys
    command = sys.argv[1]
    profile = sys.argv[2] if len(sys.argv) > 2 else ""
    if command == "profile-cmd":        # bash: the command line of the argv profile (mode per repo when given)
        repo = expand_path(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else ""
        try:
            name = profile or agent_name()
            if repo and profile_adapter(name).records_permission_mode:
                cmd = agent_identity(name)
                mode = permission_state.mode_for(repo, cmd, True, name)
                launch = agent_launch_command(mode, repo, name)
                permission_state.record_launch(repo, cmd, mode, name)
                print(launch)
            else:
                print(agent_launch_command(repo=repo, profile=name))
        except (SettingsError, RepoDisabledError) as error:
            print(error, file=sys.stderr)
            sys.exit(2)
    elif command == "session-command" and len(sys.argv) > 3:   # bash: session line of a profile for a repo
        try:
            print(session_command(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else ""))
        except (SettingsError, RepoDisabledError) as error:
            print(error, file=sys.stderr)
            sys.exit(2)
    elif command == "profile-list":     # bash: every profile name, one per line, in config order
        print("\n".join(profile_names()))
    elif command == "default-profile":  # bash: the configured agent name
        print(agent_name())
    elif command == "ensure-settings":   # bash: generate a profile's Claude settings without launching
        try:
            path = ensure_profile_settings(profile or agent_name())
        except SettingsError as error:
            print(error, file=sys.stderr)
            sys.exit(2)
        if path:
            print(path)
    elif command == "prepare-profile":   # bash restart/reload: regenerate the harness wiring of a profile
        name = profile or agent_name()
        try:
            path = profile_adapter(name).prepare(name)
        except SettingsError as error:
            print(error, file=sys.stderr)
            sys.exit(2)
        if path:
            print(path)
    elif command == "profile-names":     # bash reload: every profile whose generated settings must be refreshed
        print("\n".join(profile_names()))
    elif command == "agent-harness":     # bash: "claude" or "opencode", the harness of the chosen agent
        print(profile_harness(profile or agent_name()))
    elif command == "opening-command" and len(sys.argv) > 2:   # bash: command line that opens a repo
        try:
            print(opening_command(sys.argv[2]))
        except (SettingsError, RepoDisabledError) as error:
            print(error, file=sys.stderr)
            sys.exit(2)
    elif command == "repo-enabled" and len(sys.argv) > 2:   # bash: exit 1 with the message when the repo is off
        if not repo_enabled(sys.argv[2]):
            print(t("repo.disabled", repo=os.path.basename(expand_path(sys.argv[2]))), file=sys.stderr)
            sys.exit(1)
    elif command == "repo":   # `workspace repo list | on <name> | off <name>` (repos folder switch)
        action = sys.argv[2] if len(sys.argv) > 2 else ""
        if not repos_folder():
            print(t("repo.no_folder"), file=sys.stderr)
            sys.exit(2)
        if action == "list":
            off = disabled_repos()
            for name, _ in folder_repos():
                print(f"{name}\t{t('repo.state.off' if name in off else 'repo.state.on')}")
        elif action in ("on", "off") and len(sys.argv) > 3:
            try:
                set_repo_enabled(sys.argv[3], action == "on")
            except ValueError as error:      # ConfigError and not-in-folder are both ValueError
                print(error, file=sys.stderr)
                sys.exit(2)
            print(t("repo.set", repo=sys.argv[3], state=t("repo.state." + action)))
        else:
            print(t("repo.usage"), file=sys.stderr)
            sys.exit(2)
    elif command == "initial-repo":    # bash: path of the first repo to open on a fresh start (main, else first listed)
        print((read_main() or read_repos()[0][1][0])[1])
    elif command == "window-names":   # bash: names a window of profile PROFILE may have (for prune); empty = keep all
        from worktrees import all_window_names   # lazy: worktrees imports lib
        names = [read_main()[0]] if read_main() else []
        names += [n for g, r in read_repos() for n, p in r]
        names += all_window_names()              # worktree windows: window_name is the path's basename
        code = policy.workspace_root()           # `workspace dev` window: not in config, kept by prune
        if code:
            names.append(os.path.basename(code))
        names += ["plugin:" + e["id"] for e in listed_plugins(profile)
                  if isinstance(e, dict) and isinstance(e.get("id"), str)]
        print("\n".join(names))
    elif command == "plugins":           # bash reads: "@@id<TAB>folder<TAB>label", then the layout lines
        for entry in listed_plugins(profile):
            try:
                id_, folder, label, tree = load_plugin(entry)
            except PluginError as e:
                print(e, file=sys.stderr)
                continue
            print(f"@@{id_}\t{folder}\t{label}")
            print(tree)
    elif command == "plugin-ids":      # listed ids, valid or not (a broken plugin keeps its window)
        for entry in listed_plugins(profile):
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                print(entry["id"])
