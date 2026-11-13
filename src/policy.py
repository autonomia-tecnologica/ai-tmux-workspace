#!/usr/bin/env python3
"""Central capability policy: classification, decisions and audit trail.

One engine, two adapters: the Claude Code `PreToolUse` hook (`src/hook.py`) imports `check_event`
and the OpenCode plugin (`security/opencode-policy.js`) runs `python3 policy.py check`. The installed
engine and policy live in `security/`, next to the harness text in `harness/`. Neither adapter owns a
rule; both get the same `{decision, capability, reason}` from this file.

Input is one JSON object on stdin, either a Claude hook event or an OpenCode event:

    {"tool_name": "Bash", "tool_input": {"command": "git status"},
     "session_id": "...", "agent_id": "..."}          # agent_id marks a subagent

The role comes from `WS_SESSION_ROLE` (`repo`, `home`, `self` or `subagent`) and the box from
`WS_SESSION_ROOT`. `self` is the dev session of the workspace's own code (`workspace dev`): it has
the same table as `repo` (`inherits: repo` in the policy file). When the role is `subagent`, the
parent is `WS_SESSION_PARENT_ROLE` or `WS_PARENT_ROLE`; a subagent inside a repo/home/self session is
also detected by `agent_id`. A missing or unknown role is a deny for this CLI (fail closed); the
adapters deliberately skip the policy when no role was declared, so sessions opened before the
launcher sets the role keep working.

The workspace's own code is a protected path checked before the role table: `WS_DIR` when the
launcher exported it, else this file's code root. Every role may read it; only a session whose
`WS_SESSION_ROOT` is inside that root may write it, whatever its role: the rule is the root, not the
role, so no `self` role is required. The dev session of `workspace dev` qualifies, and so does a
`repo` or `home` session whose root is inside the workspace code (such as the worktrees of this
repo). A session whose root is outside that root is denied, `home` included.

The control plane (`paths.config_dir()`, the folder with `config.yaml`, `state/` and `conference/`)
is the other protected path checked before the role table: every role may read it (`config.read`)
and no agent role writes it (`control_plane.write`, reason `policy.reason.control_plane`). It wins
over the declared session root. The rest of `~/.config` keeps the table (`config.write` for `home`).

The declared session root wins over the configuration roots (`~/.config`, `~/.claude`, the installed
workspace): a worktree may live under one of them and still be writable as its own repo. Secrets win
over everything, including the session root.

`auto` comes from `WS_POLICY_AUTO`/`WS_AUTO` or the event `permission_mode`; it only turns `ask`
into `allow` for path-scoped capabilities inside the box and never turns `deny` into anything.

The engine does not decide what it cannot place, and it does not ask either. A shell program that no
table knows (`dotnet`, `cargo --version`, ...), a known toolchain's form it does not know (`cargo run`,
`npm run`, `make -C dir`), and an ambiguous form (`grep -r`, `find -exec`, `sed`/`awk` scripts,
`python -c`, `sh -c`, `eval`, `$(...)`, backticks, globs, expansions, unclosed quotes, `cd` without a
plain directory, unknown tool names) is answered `allow` with `policy.reason.defer`. The hooks turn
that into no answer, so the session's own mode decides. Such a segment still meets the barriers that
hold whatever its form is (`_ambiguous_findings`): an admin program named anywhere in it, and a secret
or a workspace-code write among its words. Known segments keep their table decision. A `cd` into a
directory, inside or outside the box, reanchors the following segments of the same command; `git -C`
works the same way. Denied for every role, with no list to maintain: secrets and the workspace code (write). `admin`
(`sudo`, `pkexec`, `su`, `doas`, ...) is `privileged` for every role (item 154): the person approves the
whole command, then it runs through pkexec, and no mode turns it into `allow`.

`ask` mode (item 119) is opt-in by the person: `WS_POLICY_ASK`/`WS_POLICY_OPEN` or the marker
`policy.ask`/`policy.open` in `$WS_STATE`. In it an action the engine cannot classify becomes `ask`
(`policy.reason.ask_mode`) instead of a blind `deny`, and `net`/`install` denied by the table become
`ask`. Secrets, `admin`, `repo.outside`, `config.write` and the workspace code stay denied; without
the marker nothing changes.

Every decision is appended to `$WS_STATE/policy.jsonl` (mode 0600) with the role, session,
capability, target, decision and reason. The target is a path or a command summary, never the raw
argument list, so a value passed on the command line is not written to the log.

Stdlib only and no import from the workspace libraries except the YAML reader and the path owner
(`paths.py`, stdlib only): this runs on every tool call.
"""
import fnmatch
import json
import os
import re
import shlex
import sys
import time
import unicodedata
from pathlib import Path

try:
    import yaml_min
except Exception:       # installed without the reader: load_policy fails closed
    yaml_min = None

try:
    import paths
except Exception:       # a hand-copied engine may be missing the path owner: fail closed below
    paths = None


CAPABILITIES = (
    "repo.read", "repo.write", "repo.outside",
    "vcs.read", "vcs.write", "vcs.remote",
    "test.run", "net", "install", "admin",
    "config.read", "config.write", "tmp", "secrets", "session.external",
    "workspace.read", "workspace.write",
)
ROLES = ("repo", "home", "self", "subagent")
# Roles a subagent or a plain session may take a capability table from. `self` is the workspace's
# dev session: it has the table of `repo` (see `inherits` in the policy file).
PARENT_ROLES = ("repo", "home", "self")
DECISIONS = ("allow", "ask", "privileged", "deny")
# Strictness order for combining findings (the lowest rank wins). `privileged` (admin, item 154) sits
# between deny and ask: it is never allow, and `auto` only upgrades `ask`, so it is never upgraded.
_RANK = {"deny": 0, "privileged": 1, "ask": 2, "allow": 3}

# Path-scoped capabilities are the only ones `auto` may upgrade, and only inside the box.
BOX_CAPABILITIES = {"repo.read", "repo.write", "repo.outside", "config.read", "config.write", "tmp"}

# Internal marker for a shell program no table knows (item 135). It is not a capability: it decides
# nothing by itself, so the session's mode decides (`allow` with `policy.reason.defer`, which the
# hooks answer by staying silent). Other findings of the same command still apply.
DEFER = "defer"

# Protected path checked before the role table (item 72): a write into the control plane is denied
# for every role, while reads stay `config.read`. Like DEFER it is not part of CAPABILITIES, so no
# role table can grant it.
CONTROL_PLANE = "control_plane.write"

MAX_TARGET = 300
MAX_SESSION = 100

# Tool names are compared case-insensitively; OpenCode uses lower-case names and Claude Code uses
# CamelCase. The lists below are the whole classification surface: a tool name outside them defers
# to the session's own permission flow, and a malformed event (no tool name) is still denied.
READ_TOOLS = {"read", "glob", "grep", "ls", "list", "notebookread"}
WRITE_TOOLS = {"write", "edit", "multiedit", "patch", "notebookedit"}
BASH_TOOLS = {"bash", "shell", "execute"}
NET_TOOLS = {"webfetch", "websearch", "fetch"}
# A request coming from another session (item 65) is recognized by one of these tool names or by
# an explicit `external_session`/`origin` field. The policy always answers `ask`.
EXTERNAL_TOOLS = {
    "send_message", "session_message", "session_request", "sessionrequest",
    "cross_session", "session.external", "conference",
}
# Tools with no file or network effect of their own. The subagent's own tool calls are still
# classified (agent_id), so allowing `Task` does not allow what the subagent does. `SendMessage`
# and `SubagentHandback` are here because they only talk inside the session's own tree;
# `external_session`/`origin` in the input still sends a `SendMessage` to the external branch.
INERT_TOOLS = {
    "todowrite", "todo", "askuserquestion", "exitplanmode", "task", "skill", "slashcommand",
    "killshell", "bashoutput", "taskoutput", "taskstop", "question", "subagent", "agent",
    "sendmessage", "subagenthandback",
}
# `ToolSearch` only loads tools: it has no effect of its own, and the loaded tool is classified
# one by one when it is actually called (agent_id still marks the subagent). Only a `select:`
# query naming inert tools is allowed; any other form, an empty list or an unknown name fails
# closed. It never resolves a path nor uses the session root.
TOOL_SEARCH_TOOLS = {"toolsearch"}

SECRET_NAMES = {
    ".env", ".netrc", "credentials", ".credentials.json", "auth.json", "id_rsa", "id_ed25519",
    "id_ecdsa",
}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
SECRET_DIRS = {".ssh", ".gnupg"}
# Credential stores that live under the home and are not named like a secret file. They are
# secrets even when the file name is generic (`hosts.yml`, `config.json`, `credentials`).
# The `Library/...` entries are the macOS equivalents (Keychain, Messages, Mail, Safari, cookies
# and the Claude desktop app data); they are listed for every platform because they are only
# reached through the home directory.
SECRET_HOME_DIRS = (
    ".aws", ".docker", ".kube", ".npm", ".gnupg", ".ssh", ".config/gh", ".config/gcloud",
    ".config/hub", ".config/containers",
    "Library/Keychains", "Library/Application Support/Claude", "Library/Cookies",
    "Library/Messages", "Library/Mail", "Library/Safari",
)
SECRET_HOME_FILES = (
    ".git-credentials", ".netrc", ".npmrc", ".pypirc", ".yarnrc.yml",
    ".cargo/credentials.toml", ".config/pip/pip.conf", ".config/git/credentials",
    "Library/Cookies/Cookies.binarycookies",
    "Library/Application Support/Claude/claude_desktop_config.json",
)

# A path with shell expansion or a glob is not a concrete path: the form is ambiguous and defers, and
# `_ambiguous_findings` still checks the secret and workspace parts it names. `~` and `~user` stay
# valid; they are expanded before this check.
_EXPANSION = re.compile(r"[$`*?\[\]{}]")

ADMIN_PROGRAMS = {
    "sudo", "doas", "pkexec", "su", "systemctl", "service", "mount", "umount", "swapon",
    "swapoff", "modprobe", "insmod", "rmmod", "iptables", "ip6tables", "nft", "ufw", "firewall-cmd",
    "useradd", "userdel", "usermod", "groupadd", "groupdel", "passwd", "visudo", "docker",
    "podman", "nerdctl", "kubectl", "helm",
    # macOS admin tools (accounts, launchd, Gatekeeper/SIP, FileVault, network and disks).
    "sysadminctl", "dseditgroup", "dscl", "launchctl", "spctl", "csrutil", "fdesetup",
    "systemsetup", "networksetup", "diskutil", "security",
}
NET_PROGRAMS = {
    "curl", "wget", "http", "https", "ssh", "scp", "sftp", "ftp", "telnet", "nc", "netcat",
    "ncat", "socat", "ping", "ping6", "traceroute", "dig", "nslookup", "host", "gh", "rsync",
}
INSTALL_PROGRAMS = {
    "apt", "apt-get", "aptitude", "pacman", "yay", "paru", "dnf", "yum", "zypper", "brew",
    "pip", "pip3", "gem", "composer", "snap", "flatpak",
}
TEST_PROGRAMS = {"pytest", "tox", "nox", "unittest"}
GODOT_PROGRAMS = {"godot", "godot4"}
# `python` flags that only shape the interpreter before `-m`; every other flag is ambiguous.
PYTHON_SAFE_FLAGS = re.compile(r"^-[IBuEsSO]+$")
READ_PROGRAMS = {
    "cat", "head", "tail", "ls", "stat", "wc", "cut", "sort", "uniq", "diff",
    "file", "tree", "pwd", "date", "which", "whereis", "type", "basename", "dirname", "realpath",
    "readlink", "true", "false", "sleep", "echo", "printf", "test", "[", ":", "id", "whoami",
}
# `sed`/`awk` are deliberately absent: their script language writes files and runs commands.
# `less`/`more` are absent too: their pager escapes (LESSOPEN, `!`) run commands.
TARGET_DIRECTORY_PROGRAMS = {"cp", "mv", "ln", "install"}
FIND_DENIED_OPTIONS = {
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fprint", "-fprint0", "-fprintf", "-fls",
    "-L", "-H", "-follow",
}
WRITE_PROGRAMS = {
    "rm", "rmdir", "mkdir", "touch", "truncate", "cp", "mv", "ln", "install", "tee", "chmod",
    "chown", "chgrp", "setfacl",
}
# Read programs whose option values can be output paths. The value is classified as a write; the
# other read programs keep a summarized audit target so an option value never reaches the log.
WRITE_OPTION_PROGRAMS = {
    "sort": ("-o", "--output"),
    "tree": ("-o", "--output"),
}
# Only cat is safe to log by path: it has no option that takes a value.
PATH_TARGET_READ_PROGRAMS = {"cat"}
GREP_VALUE_OPTIONS = (
    "-A", "-B", "-C", "-m", "--max-count", "--include", "--exclude", "--exclude-dir",
    "--include-dir", "--context", "--after-context", "--before-context", "-g", "--glob",
    "--type", "-t", "--threads", "-j", "--max-depth", "--color", "--colour",
)
# Short options of grep/rg that consume the rest of their group as a value. Everything before
# them in the group is a flag, so `-er` is the regexp "r" while `-re` is recursive.
GREP_SHORT_VALUE = "efmABCDd"
RG_SHORT_VALUE = "efmABCgjTMdE"
GIT_VALUE_OPTIONS = {
    "-m", "--message", "-F", "--file", "-c", "--reedit-message", "-C", "--reuse-message",
    "--author", "--date", "--committer", "--template", "-S", "--gpg-sign", "--cleanup",
    "-t", "--track", "-b", "--orphan", "-B", "--source", "--strategy", "-X",
    "--strategy-option", "--reference", "--depth", "--branch", "--upload-pack",
    "--recurse-submodules", "--grep", "-G", "--since", "--until", "--before", "--after",
    "--format", "--pretty", "-n", "--max-count", "--skip", "--diff-filter", "--stat",
}
GIT_READ = {
    "status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "describe", "shortlog",
    "reflog", "grep", "whatchanged", "name-rev",
}
GIT_WRITE = {
    "add", "commit", "checkout", "switch", "merge", "rebase", "reset", "restore", "rm", "mv",
    "cherry-pick", "revert", "clean", "apply", "am", "stash", "init", "worktree", "submodule",
}
GIT_REMOTE = {"push", "pull", "fetch", "clone", "ls-remote"}
GIT_BRANCH_WRITE_FLAGS = {
    "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy",
    "--set-upstream-to", "-u", "--unset-upstream",
}
GIT_CONFIG_WRITE_FLAGS = {
    "--add", "--unset", "--unset-all", "--replace-all", "--rename-section", "--remove-section",
}

# Redirect tokens produced by shlex with punctuation_chars=True.
REDIRECT_OUT = {">", ">>", ">|", "&>", "&>>"}
REDIRECT_IN = {"<", "<<", "<<<", "0<"}
SHELL_NOISE = {"|", "||", "&&", ";", "&", "(", ")", "1", "2", "0"}
DEV_NULL = {"/dev/null", "/dev/stdout", "/dev/stderr"}


class PolicyError(Exception):
    """The policy file is missing or invalid; `reason` is an i18n key."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Finding:
    """One classified effect: a capability, a log-safe target and, when path-scoped, the path."""

    __slots__ = ("capability", "target", "path")

    def __init__(self, capability, target, path=None):
        self.capability = capability
        self.target = target
        self.path = path


class _Ambiguous(Exception):
    """A path argument was present but could not be resolved. The caller treats the segment as ambiguous."""


# --- policy file -------------------------------------------------------------------------------

_CACHE = {}


def policy_path():
    """First existing policy file: WS_POLICY, next to this module, WS_DIR, repo security, install."""
    override = os.environ.get("WS_POLICY")
    if override:
        return Path(override).expanduser()
    here = Path(__file__).resolve().parent
    candidates = [here / "policy.yaml"]
    ws_dir = os.environ.get("WS_DIR")
    if ws_dir:
        candidates.append(Path(ws_dir).expanduser() / "security" / "policy.yaml")
    candidates.append(here.parent / "security" / "policy.yaml")
    candidates.append(Path.home() / ".local" / "share" / "ai-tmux-workspace" / "security" / "policy.yaml")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[-1]


def load_policy(path=None):
    """Parsed and validated policy. Raises PolicyError for a missing or invalid file."""
    target = Path(path) if path is not None else policy_path()
    try:
        stat = target.stat()
        key = (str(target), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (str(target), None, None)
    if key in _CACHE:
        return _CACHE[key]
    if yaml_min is None:
        raise PolicyError("policy.reason.invalid")
    try:
        document = yaml_min.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PolicyError("policy.reason.invalid")
    if not isinstance(document, dict):
        raise PolicyError("policy.reason.invalid")
    _validate(document)
    _CACHE[key] = document
    return document


def _validate(document):
    if document.get("default") != "deny":
        raise PolicyError("policy.reason.invalid")
    roles = document.get("roles")
    if not isinstance(roles, dict):
        raise PolicyError("policy.reason.invalid")
    for role in ("repo", "home"):
        entries = roles.get(role)
        if not isinstance(entries, dict) or set(entries) != set(CAPABILITIES):
            raise PolicyError("policy.reason.invalid")
        for value in entries.values():
            if value not in DECISIONS:
                raise PolicyError("policy.reason.invalid")
    own = roles.get("self")
    if not isinstance(own, dict) or own != {"inherits": "repo"}:
        raise PolicyError("policy.reason.invalid")
    subagent = roles.get("subagent")
    if not isinstance(subagent, dict) or subagent.get("inherits") != "parent":
        raise PolicyError("policy.reason.invalid")
    for name, value in subagent.items():
        if name == "inherits":
            continue
        if name not in CAPABILITIES or value not in DECISIONS:
            raise PolicyError("policy.reason.invalid")


def _role_table(document, role):
    """Capability table of a role; a role with `inherits` (self) takes the table it names."""
    entries = document["roles"][role]
    if "inherits" in entries:
        return dict(document["roles"][entries["inherits"]])
    return dict(entries)


def _effective_capabilities(document, parent, subagent):
    """Parent's table for a subagent, with explicit subagent entries allowed only to tighten."""
    capabilities = _role_table(document, parent)
    if not subagent:
        return capabilities
    for name, value in (document["roles"].get("subagent") or {}).items():
        if name == "inherits":
            continue
        if _RANK[value] <= _RANK[capabilities.get(name, "deny")]:
            capabilities[name] = value
    return capabilities


# --- path helpers ------------------------------------------------------------------------------

def _is_darwin():
    """Whether the host is macOS. Patched in tests so the darwin cases run on Linux."""
    return sys.platform == "darwin"


def _norm(value):
    """Comparison form of a path: NFC-normalized and case-folded on macOS only.

    APFS (macOS) is case- and normalization-insensitive, so `~/.SSH` names `~/.ssh` there and an
    NFD spelling names the same file as its NFC form. Linux is case- and normalization-sensitive:
    folding there would let a differently-spelled path match a box it is not inside. The path that
    is stored, reported and logged is never changed.
    """
    text = str(value)
    return unicodedata.normalize("NFC", text).casefold() if _is_darwin() else text


def _inside(path, root):
    if not path or not root:
        return False
    try:
        left = _norm(os.path.normpath(os.path.abspath(str(path))))
        right = _norm(os.path.normpath(os.path.abspath(str(root))))
        return os.path.commonpath([left, right]) == right
    except (OSError, ValueError):
        return False


def _config_roots():
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        return ()
    return (home / ".config", home / ".claude", home / ".local" / "share" / "ai-tmux-workspace")


def control_plane_root():
    """The workspace control plane (`paths.config_dir`): config, state and conference.

    Public because the protected-path rule is the policy's, not the adapters'. It reads the
    environment on every call (`AT_WS_CONFIG` may point the tests at a temporary config folder).
    """
    if paths is not None:
        return paths.config_dir()
    override = os.environ.get("AT_WS_CONFIG")
    if override:
        return Path(override).expanduser().parent
    return Path.home() / ".config" / "ai-tmux-workspace"


def workspace_root():
    """The workspace's own code: `WS_DIR` when exported, else this file's code root.

    Public because `session.py` (the `self` role) and `lib.py` (the dev window name) use the same root.

    A checkout keeps this module under `src/` and an installed engine under `security/`; in both
    the code root is the parent directory, which is the clone (dev or installed) being protected.
    """
    value = os.environ.get("WS_DIR")
    if isinstance(value, str) and value.strip():
        try:
            return os.path.realpath(os.path.normpath(
                os.path.abspath(os.path.expanduser(value.strip()))))
        except (OSError, ValueError):
            return None
    try:
        return os.path.realpath(str(Path(__file__).resolve().parent.parent))
    except (OSError, RuntimeError):
        return None


TMP_ROOTS = ("/tmp",)
# macOS per-session temp directory: `tempfile.gettempdir()` returns something under
# `/var/folders`, and `/var` is a symlink to `/private/var`, so both spellings are in the box.
MACOS_TMP_ROOTS = ("/private/var/folders", "/var/folders")


def _tmp_roots():
    """The only global areas of the box: /tmp and, on macOS, the fixed temp root.

    On macOS the temp directory is a per-session directory under `/var/folders` (real path
    `/private/var/folders`). Both are fixed strings: TMPDIR is deliberately ignored, so a session
    cannot enlarge the box by pointing TMPDIR elsewhere.
    """
    roots = list(TMP_ROOTS)
    if _is_darwin():
        roots.extend(MACOS_TMP_ROOTS)
    for candidate in tuple(roots):
        try:
            real = os.path.realpath(candidate)
        except OSError:
            real = candidate
        if real not in roots:
            roots.append(real)
    return tuple(roots)


def _is_secret(path):
    try:
        path = os.path.expanduser(str(path))
    except (TypeError, ValueError):
        return True
    name = _norm(os.path.basename(path))
    if name.endswith(".example"):        # F05 exception: model files stay readable
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES):
        return True
    parts = Path(path).parts
    if any(_norm(part) in SECRET_DIRS for part in parts):
        return True
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        return False
    relative_path = _norm(os.path.normpath(path).lstrip(os.sep))
    for relative in SECRET_HOME_DIRS + SECRET_HOME_FILES:
        secret = _norm(os.path.normpath(relative))
        if _inside(path, home / relative):
            return True
        # Also catches relative paths and `rev:path` arguments from git, where the candidate is
        # not anchored at the home (e.g. `.config/gh/hosts.yml` or `HEAD:.docker/config.json`).
        if (relative_path == secret or relative_path.startswith(secret + os.sep)
                or relative_path.endswith(os.sep + secret)):
            return True
    return False


def _resolve(value, root):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if _EXPANSION.search(value):
        return None                     # `$`, backtick, glob or brace expansion: not a path
    try:
        expanded = os.path.expanduser(value)
        if expanded.startswith("~"):
            return None                 # unknown ~user: ambiguous
        if not os.path.isabs(expanded):
            if not root:
                return None
            expanded = os.path.join(root, expanded)
        return os.path.realpath(os.path.normpath(os.path.abspath(expanded)))
    except (OSError, ValueError):
        return None


def _path_capability(path, root, write):
    if _is_secret(path):
        return "secrets"
    # The control plane is a hard barrier checked before the workspace code and before the declared
    # session root. Reads stay `config.read`; only writes get the protected capability.
    if _inside(path, control_plane_root()):
        return CONTROL_PLANE if write else "config.read"
    workspace = workspace_root()
    if workspace and _inside(path, workspace):
        return "workspace.write" if write else "workspace.read"
    # The declared session root wins over the configuration roots: a worktree may live under
    # ~/.local/share/ai-tmux-workspace (a config root) and still be its own writable repo. Secrets
    # were checked first, so they stay denied inside the box.
    if root and _inside(path, root):
        return "repo.write" if write else "repo.read"
    for config_root in _config_roots():
        if _inside(path, config_root):
            return "config.write" if write else "config.read"
    if any(_inside(path, temporary) for temporary in _tmp_roots()):
        return "tmp"
    return "repo.outside"


def _inside_box(path, root):
    if not path or not root:
        return False
    return _inside(path, root) or any(_inside(path, temporary) for temporary in _tmp_roots())


def _protected_decision(capability, root, declared):
    """Decision for a protected workspace path, before the role table.

    Reading the workspace code is always allowed (the agent may inspect the code it runs). Writing
    it is allowed only when a declared session box (`WS_SESSION_ROOT`) is inside the protected
    root, whatever the role: the dev session of `workspace dev`, and a repo or home session whose
    root is the workspace code (such as this repo's worktrees), write it. A session rooted outside
    that code, sibling repos and home included, is denied. No `self` role is required. Without a
    declared box the allow has no anchor, so writes fail closed.
    """
    if capability == "workspace.read":
        return "allow", "policy.reason.workspace_read"
    workspace = workspace_root()
    if declared and workspace and root and _inside(root, workspace):
        return "allow", "policy.reason.workspace_self"
    return "deny", "policy.reason.workspace_write"


# --- classification ----------------------------------------------------------------------------

def _canonical(tool):
    return tool.strip().lower().replace("__", "_") if isinstance(tool, str) else ""


def _input_path(tool_input):
    for key in ("file_path", "filePath", "notebook_path", "notebookPath", "path", "filename"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _tool_search_finding(tool_input):
    """Inert Finding for a `select:` query that names only inert tools, else None.

    The accepted form is `select:<name>[,<name>...]`; names are canonicalized, so CamelCase and
    lower-case spellings match, and spaces around them are ignored. An empty selection, another
    query form, a non-object input or a name outside `INERT_TOOLS` returns None (fail closed).
    """
    query = tool_input.get("query") if isinstance(tool_input, dict) else None
    if not isinstance(query, str):
        return None
    query = query.strip()
    prefix = "select:"
    if not query.startswith(prefix):
        return None
    names = [name.strip() for name in query[len(prefix):].split(",")]
    if any(not name or _canonical(name) not in INERT_TOOLS for name in names):
        return None
    return [Finding(None, "toolsearch", None)]


def classify(event, root):
    """List of Findings for the event, or None when the action cannot be classified."""
    if not isinstance(event, dict):
        return None
    tool = event.get("tool_name")
    if not isinstance(tool, str) or not tool.strip():
        return None
    name = _canonical(tool)
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    if tool_input.get("external_session") is True or tool_input.get("origin") == "external_session":
        return [Finding("session.external", "external_session", None)]
    if name in TOOL_SEARCH_TOOLS:
        return _tool_search_finding(tool_input)
    if name in INERT_TOOLS:
        return [Finding(None, name, None)]
    if name in EXTERNAL_TOOLS:
        return [Finding("session.external", name, None)]
    if name in BASH_TOOLS:
        command = tool_input.get("command") or tool_input.get("cmd")
        return _classify_command(command, root)
    if name in NET_TOOLS:
        return [Finding("net", name, None)]
    if name in READ_TOOLS:
        value = _input_path(tool_input)
        if value is None:
            if not root:
                return None
            return [Finding("repo.read", root, root)]
        path = _resolve(value, root)
        if path is None:
            return None
        return [Finding(_path_capability(path, root, False), path, path)]
    if name in WRITE_TOOLS:
        value = _input_path(tool_input)
        if value is None:
            return None
        path = _resolve(value, root)
        if path is None:
            return None
        return [Finding(_path_capability(path, root, True), path, path)]
    # Unknown tool name (an MCP tool, a tool this table does not list): not a command the engine
    # can place, so it defers to the session's own permission flow instead of being denied (item 1xx).
    return [Finding(DEFER, name, None)]


# --- command splitting and classification ------------------------------------------------------

def _split_segments(command):
    """Shell segments split on ; && || | and newline, respecting single/double quotes."""
    segments, current, quote, i = [], [], None, 0
    while i < len(command):
        char = command[i]
        if quote:
            current.append(char)
            if quote == '"' and char == "\\" and i + 1 < len(command):
                current.append(command[i + 1])
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in "\"'":
            quote = char
            current.append(char)
        elif command.startswith("&&", i) or command.startswith("||", i):
            segments.append("".join(current))
            current = []
            i += 1
        elif char in "|;\n":
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
        i += 1
    if quote:
        raise ValueError("unterminated quote")
    segments.append("".join(current))
    return [segment.strip() for segment in segments if segment.strip()]


def _tokens(segment):
    lexer = shlex.shlex(segment, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _extract_redirects(tokens):
    """(findings, cleaned tokens) for output/input redirections and shell operators."""
    findings, clean, i = [], [], 0
    while i < len(tokens):
        token = tokens[i]
        # `n>&m` / `n>&-` duplicates or closes a descriptor; any other `>&` target is a file.
        if token.isdigit() and i + 1 < len(tokens) and tokens[i + 1] == ">&":
            after = tokens[i + 2] if i + 2 < len(tokens) else None
            if after is not None and (after.isdigit() or after == "-"):
                i += 3
                continue
            i += 1                       # fd prefix; `>&` is handled next as a file redirect
            continue
        if token.isdigit() and i + 1 < len(tokens) and (
                tokens[i + 1] in REDIRECT_OUT or tokens[i + 1] in REDIRECT_IN):
            i += 1                       # fd prefix; the operator is handled next
            continue
        if token == ">&":
            target = tokens[i + 1] if i + 1 < len(tokens) else None
            if target is None:
                return None, None
            if target != "-" and not target.isdigit() and target not in DEV_NULL:
                findings.append((True, target))
            i += 2
            continue
        if token.startswith(">&"):
            target = token[2:]
            if target and target != "-" and not target.isdigit() and target not in DEV_NULL:
                findings.append((True, target))
            i += 1
            continue
        if token in REDIRECT_OUT or token in REDIRECT_IN:
            target = tokens[i + 1] if i + 1 < len(tokens) else None
            if target is None:
                return None, None
            if target not in DEV_NULL:
                findings.append((token in REDIRECT_OUT, target))
            i += 2
            continue
        if token in SHELL_NOISE:
            i += 1
            continue
        if token.startswith(">") and len(token) > 1:
            if token[1:] not in DEV_NULL:
                findings.append((True, token[1:]))
            i += 1
            continue
        if token.startswith("<") and len(token) > 1:
            if token[1:] not in DEV_NULL:
                findings.append((False, token[1:]))
            i += 1
            continue
        clean.append(token)
        i += 1
    return findings, clean


def _path_args(arguments, skip_first=False):
    """Non-flag arguments taken as paths, with the first skipped when it is a script/pattern."""
    values = [argument for argument in arguments if not argument.startswith("-")]
    if skip_first and values:
        values = values[1:]
    return values


def _classify_paths(arguments, root, write, skip_first=False, target=None, anchor=None):
    """Findings for the positional arguments. `target` keeps option values out of the audit log.

    `anchor` is the directory relative paths are resolved against (a `cd` in the same command);
    the capability is still decided against the session box `root`. No path argument means None
    (the caller may use its fallback). A path argument that cannot be resolved raises
    `_Ambiguous`, which the caller turns into an ambiguous segment: `*`/`$HOME` are not paths.
    """
    paths = _path_args(arguments, skip_first)
    if not paths:
        return None
    base = anchor or root
    findings = []
    for value in paths:
        path = _resolve(value, base)
        if path is None:
            raise _Ambiguous(value)
        findings.append(Finding(_path_capability(path, root, write),
                                target if target is not None else path, path))
    return findings


def _write_option_paths(program, arguments, root, anchor=None):
    """Paths written through an option of an otherwise read program (`sort -o`, `tree -o`)."""
    options = WRITE_OPTION_PROGRAMS.get(program)
    if not options:
        return None
    base = anchor or root
    findings, index = [], 0
    while index < len(arguments):
        argument = arguments[index]
        value = None
        if argument in options:
            if index + 1 >= len(arguments):
                raise _Ambiguous(argument)
            value = arguments[index + 1]
            index += 2
        else:
            for option in options:
                if option.startswith("--") and argument.startswith(option + "="):
                    value = argument.split("=", 1)[1]
                    break
                if not option.startswith("--") and argument.startswith(option) and \
                        len(argument) > len(option):
                    value = argument[len(option):]
                    break
            index += 1
        if value is not None:
            path = _resolve(value, base)
            if path is None:
                raise _Ambiguous(value)
            findings.append(Finding(_path_capability(path, root, True), path, path))
    return findings or None


def _target_directory_paths(program, arguments, root, anchor=None):
    """Write findings for cp/mv/ln/install -t/--target-directory, including glued values."""
    if program not in TARGET_DIRECTORY_PROGRAMS:
        return None
    base = anchor or root
    findings, index = [], 0
    while index < len(arguments):
        argument = arguments[index]
        value = None
        consumed = 1
        if argument in ("-t", "--target-directory"):
            if index + 1 >= len(arguments):
                raise _Ambiguous(argument)
            value = arguments[index + 1]
            consumed = 2
        elif argument.startswith("--target-directory="):
            value = argument.split("=", 1)[1]
        elif argument.startswith("-") and not argument.startswith("--") and len(argument) > 1:
            group = argument[1:]
            for offset, char in enumerate(group):
                if char == "t":
                    rest = group[offset + 1:]
                    if rest:
                        value = rest
                    else:
                        if index + 1 >= len(arguments):
                            raise _Ambiguous(argument)
                        value = arguments[index + 1]
                        consumed = 2
                    break
                if char in "Smog":
                    break          # the rest of the group is this option's value
        index += consumed
        if value is not None:
            path = _resolve(value, base)
            if path is None:
                raise _Ambiguous(value)
            findings.append(Finding(_path_capability(path, root, True), path, path))
    return findings or None


def _denied_read_options(program, arguments):
    """Fail closed on read-program options that write, execute or read an arbitrary file."""
    if program == "sort":
        exact = {"-T", "--temporary-directory", "--compress-program", "--random-source",
                 "--files0-from"}
        prefixes = ("--temporary-directory=", "--compress-program=", "--random-source=",
                    "--files0-from=")
        for argument in arguments:
            if argument in exact or argument.startswith(prefixes):
                return True
            if argument.startswith("-") and not argument.startswith("--") and len(argument) > 1:
                for char in argument[1:]:
                    if char == "T":
                        return True
                    if char in "ktSo":
                        break            # the rest of the group is a value
        return False
    if program == "file":
        exact = {"-C", "--compile", "-f", "--files-from", "-m", "--magic-file"}
        prefixes = ("--compile=", "--files-from=", "--magic-file=")
        for argument in arguments:
            if argument in exact or argument.startswith(prefixes):
                return True
            if argument.startswith("-") and not argument.startswith("--") and len(argument) > 1:
                for char in argument[1:]:
                    if char in "Cfm":
                        return True
                    if char in "eFP":
                        break            # the rest of the group is a value
        return False
    if program == "diff":
        exact = {"--from-file", "--to-file", "--exclude-from"}
        prefixes = ("--from-file=", "--to-file=", "--exclude-from=")
        return any(argument in exact or argument.startswith(prefixes) for argument in arguments)
    if program == "wc":
        return any(argument == "--files0-from" or argument.startswith("--files0-from=")
                   for argument in arguments)
    if program == "tree":
        return any(argument == "--fromfile" or argument.startswith("--fromfile=")
                   for argument in arguments)
    return False


def _grep_or_rg_denied(program, arguments):
    """Recursive search (grep) or hidden/unrestricted search (rg) can cross into secret files."""
    for argument in arguments:
        if argument in ("--exclude-from", "--include-from", "--ignore-file"):
            return True
        if argument.startswith(("--exclude-from=", "--include-from=", "--ignore-file=")):
            return True
    if program == "grep":
        for index, argument in enumerate(arguments):
            if argument in ("--recursive", "--dereference-recursive"):
                return True
            if argument in ("-d", "--directories"):
                if index + 1 < len(arguments) and arguments[index + 1] == "recurse":
                    return True
            if argument.startswith("--directories="):
                if argument.split("=", 1)[1] == "recurse":
                    return True
            if argument.startswith("-") and not argument.startswith("--") and len(argument) > 1:
                for char in argument[1:]:
                    if char in "rR":
                        return True
                    if char in GREP_SHORT_VALUE:
                        break
                if argument[2:] == "recurse":
                    return True
        return False
    for argument in arguments:
        if argument in ("--hidden", "--unrestricted", "--follow"):
            return True
        if argument.startswith("--no-ignore"):
            return True
        if argument.startswith("-") and not argument.startswith("--") and len(argument) > 1:
            for char in argument[1:]:
                if char in "uL":
                    return True
                if char in RG_SHORT_VALUE:
                    break
    return False


def _grep_paths(arguments):
    """Files of grep/rg: the pattern (or -e value) is skipped, every file after it is kept."""
    files, index, has_pattern = [], 0, False
    while index < len(arguments):
        argument = arguments[index]
        if argument in ("-e", "--regexp"):
            has_pattern = True
            index += 2
            continue
        if argument in ("-f", "--file"):
            has_pattern = True
            if index + 1 < len(arguments):
                files.append(arguments[index + 1])
            index += 2
            continue
        if argument.startswith("--regexp="):
            has_pattern = True
            index += 1
            continue
        if argument.startswith("--file="):
            has_pattern = True
            files.append(argument.split("=", 1)[1])
            index += 1
            continue
        if argument.startswith("-e") and len(argument) > 2:
            has_pattern = True
            index += 1
            continue
        if argument.startswith("-f") and len(argument) > 2:
            has_pattern = True
            files.append(argument[2:])
            index += 1
            continue
        if argument in GREP_VALUE_OPTIONS:
            index += 2
            continue
        if argument.startswith("-") and argument != "-":
            index += 1
            continue
        if not has_pattern:
            has_pattern = True          # the first positional is the pattern
            index += 1
            continue
        files.append(argument)
        index += 1
    return files


def _git_path_arguments(arguments):
    """Non-option git arguments, skipping the values of options such as -m/--message."""
    values, skip = [], False
    for argument in arguments:
        if skip:
            skip = False
            continue
        if argument in GIT_VALUE_OPTIONS:
            skip = True
            continue
        if argument.startswith("-") and argument != "-":
            # Only a short group whose last letter is m/F consumes the next argument. `-am msg`
            # does; `-mmsg` and `-Ffile` carry the value inside the group, so `.env` stays a path.
            short = argument[1:]
            if not argument.startswith("--") and len(short) > 1 and short[-1] in ("m", "F"):
                skip = True
            continue
        values.append(argument)
    return values


def _git_secret_argument(arguments):
    """Whether a path-like git argument is a secret, including `rev:path` and `rev:./path`."""
    for argument in _git_path_arguments(arguments):
        candidates = [argument]
        if ":" in argument:
            candidates.append(argument.split(":", 1)[1])
        for candidate in candidates:
            if candidate and _is_secret(candidate):
                return True
    return False


def _git_expanded_argument(arguments):
    """Git pathspecs accept their own globs, so an unexpanded shell glob is ambiguous."""
    return any(_EXPANSION.search(argument) for argument in _git_path_arguments(arguments))


def _classify_git(arguments, root, anchor=None):
    if not arguments:
        return None
    # `-C <dir>` reanchors the repository, inside the box or outside it: the subcommand is classified
    # normally, so a worktree under `.claude/worktrees/` is vcs.read/vcs.write like any other repo.
    base = anchor or root
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token in ("--no-pager", "-P", "--paginate"):
            index += 1
            continue
        if token == "-C":
            if index + 1 >= len(arguments):
                return None                     # -C without a value: ambiguous
            resolved = _resolve(arguments[index + 1], base)
            if resolved is None:
                return None                     # expansion or glob: ambiguous
            if _is_secret(resolved):
                return [Finding("secrets", "git -C", None)]
            base = resolved
            index += 2
            continue
        if token.startswith("-C"):
            return None                         # `-C<dir>` glued: no unambiguous parse
        if token in ("-c", "--git-dir", "--work-tree"):
            return None                     # changes the repository/config: ambiguous
        break
    if index >= len(arguments):
        return None
    subcommand = arguments[index]
    rest = arguments[index + 1:]
    if _git_expanded_argument(rest):
        return None                     # shell/pathspec glob: ambiguous, fail closed
    if _git_secret_argument(rest):
        return [Finding("secrets", "git " + subcommand, None)]
    if subcommand in ("branch", "tag"):
        values = [arg for arg in rest if not arg.startswith("-")]
        writes = any(arg in GIT_BRANCH_WRITE_FLAGS for arg in rest) or bool(values)
        return [Finding("vcs.write" if writes else "vcs.read", "git " + subcommand, None)]
    if subcommand == "stash":
        read = rest[:1] == ["list"] or rest[:1] == ["show"]
        return [Finding("vcs.read" if read else "vcs.write", "git stash", None)]
    if subcommand == "worktree":
        return [Finding("vcs.read" if rest[:1] == ["list"] else "vcs.write",
                        "git worktree", None)]
    if subcommand == "submodule":
        first = rest[0] if rest else ""
        if first in ("status", "summary"):
            return [Finding("vcs.read", "git submodule " + first, None)]
        if first in ("update", "sync", "foreach"):
            return [Finding("vcs.remote" if first == "update" else "vcs.write",
                            "git submodule " + first, None)]
        return [Finding("vcs.write", "git submodule", None)]
    if subcommand == "remote":
        first = rest[0] if rest else ""
        if first in ("add", "remove", "rm", "rename", "set-url"):
            return [Finding("vcs.write", "git remote " + first, None)]
        if first in ("update", "prune"):
            return [Finding("vcs.remote", "git remote " + first, None)]
        return [Finding("vcs.read", "git remote", None)]
    if subcommand == "config":
        writes = any(arg in GIT_CONFIG_WRITE_FLAGS for arg in rest)
        values = [arg for arg in rest if not arg.startswith("-")]
        if not writes and len(values) < 2:
            return [Finding("config.read", "git config", None)]
        return [Finding("config.write", "git config", None)]
    if subcommand in GIT_READ:
        return [Finding("vcs.read", "git " + subcommand, None)]
    if subcommand in GIT_REMOTE:
        return [Finding("vcs.remote", "git " + subcommand, None)]
    if subcommand in GIT_WRITE:
        return [Finding("vcs.write", "git " + subcommand, None)]
    return None


def _classify_python(arguments):
    """Classify `python -m <module>` after the harmless interpreter flags that precede `-m`.

    `-I`, `-B`, `-u`, `-E`, `-s`, `-S`, `-O`, `-OO`, `-X <value>` and `-W <value>` (separate or
    glued) only shape the interpreter, so the module decides the capability. `-c`, stdin, a
    positional script or an unknown flag is arbitrary code: an ambiguous form, so it defers.
    """
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token in ("-X", "-W"):
            if index + 1 >= len(arguments):
                return None
            index += 2
            continue
        if token.startswith(("-X", "-W")) and len(token) > 2:
            index += 1
            continue
        if PYTHON_SAFE_FLAGS.match(token):
            index += 1
            continue
        break
    if index >= len(arguments) or arguments[index] != "-m" or index + 1 >= len(arguments):
        return None
    module = arguments[index + 1]
    rest = arguments[index + 2:]
    if module in ("unittest", "pytest", "tox", "nox"):
        return [Finding("test.run", "python3 -m " + module, None)]
    if module == "pip":
        action = rest[0] if rest else ""
        if action in ("install", "uninstall", "download"):
            return [Finding("install", "python3 -m pip " + action, None)]
        if action in ("list", "show", "freeze", "check"):
            return [Finding("test.run", "python3 -m pip " + action, None)]
    if module == "venv":
        return [Finding("install", "python3 -m venv", None)]    # same target rule as pip: none
    return None                         # arbitrary module, script or -c: ambiguous, defers


def _classify_godot(arguments, root, anchor=None):
    """`godot`/`godot4` run tests only with `--headless`, and every path stays inside the box.

    Paths are the values of `--path`, `--main-pack`, `--script`/`-s` (separate or glued) and every
    positional argument. `--export*` writes an artifact with its own flags, so it is ambiguous.
    """
    if "--headless" not in arguments:
        return None
    base = anchor or root
    paths, index = [], 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--headless":
            index += 1
            continue
        if argument.startswith("--export"):
            return None
        value, consumed = None, 1
        if argument in ("--path", "--main-pack", "--script", "-s"):
            if index + 1 >= len(arguments):
                return None
            value, consumed = arguments[index + 1], 2
            if value.startswith("-"):
                return None                 # missing value, another flag follows
        elif argument.startswith(("--path=", "--main-pack=", "--script=")):
            value = argument.split("=", 1)[1]
        elif argument.startswith("-s") and len(argument) > 2:
            value = argument[2:]
        elif argument.startswith("--") and "=" in argument:
            value = argument.split("=", 1)[1]
        elif not argument.startswith("-"):
            value = argument
        index += consumed
        if value is not None:
            path = _resolve(value, base)
            if path is None:
                return None                 # expansion or glob: ambiguous
            # outside the box is the table's decision (`repo.outside`), not an ambiguity
            paths.append(Finding(_path_capability(path, root, False), path, path))
    return [Finding("test.run", "godot --headless", None)] + paths


def _classify_pip(arguments):
    if not arguments:
        return None
    action = arguments[0]
    if action in ("install", "uninstall", "download"):
        return [Finding("install", "pip " + action, None)]
    if action in ("list", "show", "freeze", "check"):
        return [Finding("test.run", "pip " + action, None)]
    return None


def _classify_node(arguments, program):
    if not arguments:
        return None
    action = arguments[0]
    if action in ("install", "i", "add", "ci"):
        return [Finding("install", program + " " + action, None)]
    if action in ("test", "run-test", "ls", "list", "outdated"):
        return [Finding("test.run", program + " " + action, None)]
    return None


def _classify_rust(arguments):
    if not arguments:
        return None
    action = arguments[0]
    if action in ("test", "bench"):
        return [Finding("test.run", "cargo " + action, None)]
    if action in ("build", "check", "clean", "doc"):
        return [Finding("repo.write", "cargo " + action, None)]
    if action == "install":
        return [Finding("install", "cargo install", None)]
    if action in ("fetch", "update", "publish", "login"):
        return [Finding("net", "cargo " + action, None)]
    return None


def _classify_go(arguments):
    if not arguments:
        return None
    action = arguments[0]
    if action in ("test", "vet"):
        return [Finding("test.run", "go " + action, None)]
    if action in ("build", "clean", "fmt", "generate"):
        return [Finding("repo.write", "go " + action, None)]
    if action in ("install", "get", "mod"):
        return [Finding("install", "go " + action, None)]
    return None                             # `go run` and other forms: see _toolchain_findings


def _classify_make(arguments):
    if not arguments:
        return None
    # `-C dir`, `-Cdir` and `--directory` change the directory the make runs in: not a known form.
    if any(argument.startswith("-C") or argument.startswith("--directory")
           for argument in arguments):
        return None
    targets = [argument for argument in arguments if not argument.startswith("-")]
    if targets and targets[0] in ("test", "check", "tests"):
        return [Finding("test.run", "make " + targets[0], None)]
    return None


def _classify_tokens_impl(tokens, root, anchor=None):
    """Findings for one shell segment, or None when the segment is unknown/ambiguous.

    `anchor` is the directory a `cd` left the shell in; relative paths resolve against it while
    capabilities keep using the session box `root`.
    """
    anchor = anchor or root
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return [Finding("repo.read", "env", None)]
    if tokens[0] == "env":
        tokens = tokens[1:]
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            return [Finding("repo.read", "env", None)]
    program = os.path.basename(tokens[0])
    arguments = tokens[1:]

    if program in ADMIN_PROGRAMS:
        return [Finding("admin", program, None)]
    if program in NET_PROGRAMS:
        return [Finding("net", program, None)]
    if program in INSTALL_PROGRAMS:
        if program in ("pip", "pip3"):
            return _classify_pip(arguments)
        return [Finding("install", program, None)]
    if program in TEST_PROGRAMS:
        return [Finding("test.run", program, None)]
    if program == "git":
        return _classify_git(arguments, root, anchor)
    if program.startswith("python"):
        return _classify_python(arguments)
    if program in GODOT_PROGRAMS:
        return _classify_godot(arguments, root, anchor)
    if program in ("npm", "yarn", "pnpm", "bun"):
        return _toolchain_findings(_classify_node(arguments, program), program, arguments,
                                   root, anchor)
    if program == "cargo":
        return _toolchain_findings(_classify_rust(arguments), program, arguments, root, anchor)
    if program == "go":
        return _toolchain_findings(_classify_go(arguments), program, arguments, root, anchor)
    if program == "make":
        # The directory of `-C dir` / `-Cdir` is a path too: it meets the path barrier.
        attached = [argument[2:] for argument in arguments
                    if argument.startswith("-C") and len(argument) > 2]
        return _toolchain_findings(_classify_make(arguments), program, arguments, root, anchor,
                                   attached)
    if program in ("sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh"):
        return None
    if program in ("eval", "exec", "source", "."):
        return None
    if program in ("grep", "rg"):
        if _grep_or_rg_denied(program, arguments):
            return None
        return _classify_paths(_grep_paths(arguments), root, False, anchor=anchor) or (
            [Finding("repo.read", root, root)] if root else None)
    if program == "find":
        if any(argument in FIND_DENIED_OPTIONS for argument in arguments):
            return None
        paths = _path_args(arguments)
        if not paths:
            return [Finding("repo.read", root, root)] if root else None
        return _classify_paths([paths[0]], root, False, anchor=anchor)
    if program == "dd":
        findings = []
        for argument in arguments:
            if argument.startswith("of="):
                path = _resolve(argument[3:], anchor)
                if path is None:
                    return None
                findings.append(Finding(_path_capability(path, root, True), path, path))
            elif argument.startswith("if="):
                path = _resolve(argument[3:], anchor)
                if path is None:
                    return None
                findings.append(Finding(_path_capability(path, root, False), path, path))
        return findings or None
    if program in ("echo", "printf"):
        return [Finding("repo.read", program, None)]
    if program in WRITE_PROGRAMS:
        findings = _classify_paths(arguments, root, True, anchor=anchor)
        targets = _target_directory_paths(program, arguments, root, anchor)
        if findings is None and targets is None:
            return None
        return (findings or []) + (targets or [])
    if program in READ_PROGRAMS:
        if _denied_read_options(program, arguments):
            return None
        target = None if program in PATH_TARGET_READ_PROGRAMS else program
        findings = _classify_paths(arguments, root, False, target=target, anchor=anchor)
        writes = _write_option_paths(program, arguments, root, anchor)
        if findings is None and writes is None:
            return [Finding("repo.read", root, root)] if root else None
        return (findings or []) + (writes or [])
    # A program no table knows: not ambiguous (item 135). Sub-classifiers that
    # return None (arbitrary code such as `python -c`, `sh -c`, `eval`) still fail closed above.
    return _unknown_program_findings(program, arguments, root, anchor)


def _toolchain_findings(known, program, arguments, root, anchor, attached=()):
    """A toolchain's known subcommands keep their capability; any other form defers (item 135).

    `known` is what the toolchain's sub-classifier returned; None means a form it does not know
    (`cargo run`, `npm run`, `make -C dir`, ...), which is not a deny: it is classified like an
    unknown program, with the path barriers on its arguments and `DEFER` for the session mode.
    """
    if known is not None:
        return known
    return _unknown_program_findings(program, arguments, root, anchor, attached)


def _unknown_program_findings(program, arguments, root, anchor, attached=()):
    """Findings for a program no table knows (item 135): no command list, but no path escape either.

    Its path-like values still meet the path barriers: positional words, the value of
    `--opt=value` and the `attached` values the caller extracts (make's `-Cdir`). A value that
    cannot be resolved (glob, expansion) raises `_Ambiguous` and the segment is ambiguous, which
    defers with the barriers of `_ambiguous_findings`. The `DEFER` finding is always added, so the
    session mode (or the `ask` mode marker) decides the rest. An admin program in command position
    in the call is still `admin`. Other attached short-option values (`-o/path`) are not checked.
    """
    # An admin program in command position in the call (`xargs sudo rm`) is still admin.
    admin = _admin_in_text(" ".join([program] + list(arguments)))
    if admin:
        return [Finding("admin", admin, None), Finding(DEFER, program, None)]
    values = list(attached)
    for argument in arguments:
        if argument.startswith("-"):
            if "=" not in argument:
                continue
            argument = argument.split("=", 1)[1]
        # A script passed as one word (`'s/x/y/w /etc/foo'`) hides paths inside it: ambiguous.
        if re.search(r"\s", argument) and ("/" in argument or "~" in argument):
            raise _Ambiguous(argument)
        values.append(argument)
    findings = _classify_paths(values, root, True, anchor=anchor) or []
    return findings + [Finding(DEFER, program, None)]


def _classify_tokens(tokens, root, anchor=None):
    """Findings for one shell segment; None when an unresolvable path makes the segment ambiguous."""
    try:
        return _classify_tokens_impl(tokens, root, anchor)
    except _Ambiguous:
        return None


_CD_NOT = object()


def _cd_anchor(tokens, root, anchor):
    """`(findings, new_anchor)` after a `cd`, `_CD_NOT` for a non-cd segment, None for an ambiguous cd.

    Only `cd <dir>`, with an optional `-L`/`-P`, reanchors the following segments of the same
    command, inside the box or outside it: the capability of each following segment is still the
    table's, so the `cd` adds no finding of its own (a secret directory is the one exception). `cd`
    without a directory, `cd -`, extra arguments, any other flag and an expansion or a glob are
    ambiguous (None). The new anchor is local to this command.
    """
    stripped = tokens
    while stripped and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped[0]):
        stripped = stripped[1:]
    if not stripped or os.path.basename(stripped[0]) != "cd":
        return _CD_NOT
    arguments = stripped[1:]
    while arguments and arguments[0] in ("-L", "-P"):
        arguments = arguments[1:]
    if len(arguments) != 1 or arguments[0].startswith("-"):
        return None                         # `cd`, `cd -`, `cd --`, extra arguments, other flags
    path = _resolve(arguments[0], anchor)
    if path is None:
        return None                         # expansion or glob
    if _is_secret(path):
        return [Finding("secrets", path, path)], path
    return [], path


PROGRAM_DIRECTORIES = {
    "", "/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin", "/usr/local/sbin",
    # Apple Silicon Homebrew (Intel uses /usr/local, already listed).
    "/opt/homebrew/bin", "/opt/homebrew/sbin",
}
# Words that run the next word as a command: `xargs sudo`, `find -exec sudo`, `env sudo`, `time sudo`.
COMMAND_WRAPPERS = {
    "xargs", "eval", "exec", "env", "nohup", "nice", "timeout", "time", "then", "do", "else", "if",
    "while", "-exec", "-execdir", "-ok", "-okdir",
}
_PUNCTUATION = set(";&|()<>`{}$")
_QUOTES = set("'\"")
# A quoted script is a command only after one of these: `sh -c "sudo id"`, `find -exec 'sudo' ;`.
QUOTE_COMMAND_PRECEDERS = {
    "-c", "-exec", "-execdir", "-ok", "-okdir", "eval", "exec", "xargs", "env",
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh",
}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _is_admin_word(word):
    """Whether a word names an admin program: the bare name or one in a program directory.

    `/etc/passwd` is a path, not `passwd`, so it does not match.
    """
    return os.path.basename(word) in ADMIN_PROGRAMS and os.path.dirname(word) in PROGRAM_DIRECTORIES


def _admin_in_text(text):
    """The admin program in command position anywhere in a text, or None.

    Command position is the start of a segment or a substitution (after `;&|()`, a backtick or `$`),
    a quoted script after `-c`/`-exec`/a shell name, a word after a command wrapper (`xargs`,
    `find -exec`, `env`), or after leading `NAME=value` assignments. A bare admin name used as an
    argument (`grep -r passwd src`, `grep -r "mount" docs`) is not one.
    """
    expect, previous = True, ""
    for token in re.findall(r"[^\s;&|()<>`'\"{}$]+|[;&|()<>`'\"{}$]", text):
        if token in _QUOTES:
            if previous in QUOTE_COMMAND_PRECEDERS:
                expect = True
            continue
        if token in _PUNCTUATION:
            expect = True
            continue
        if expect and _is_admin_word(token):
            return os.path.basename(token)
        if token in COMMAND_WRAPPERS or (expect and _ASSIGNMENT.match(token)):
            expect = True
        else:
            expect = False
        previous = token
    return None


def _secret_glob(pattern):
    """Whether a glob word can name a secret (`*.env`, `.env*`, `*.pem`). A bare wildcard cannot.

    The pattern must hold a letter, so `*` alone is not taken for the secret names it could match.
    """
    if not re.search(r"[A-Za-z]", pattern):
        return False
    if any(pattern.endswith(suffix) for suffix in SECRET_SUFFIXES):
        return True
    return any(fnmatch.fnmatch(name, pattern) for name in SECRET_NAMES | SECRET_DIRS)


def _ambiguous_findings(text, root, anchor):
    """Findings for a command the engine cannot place: `DEFER`, plus the barriers that hold anyway.

    An unknown form (`grep -r`, `find -exec`, `sh -c`, `$(...)`, a glob, an unclosed quote) is not
    denied and not asked: `DEFER` leaves the decision to the session's mode. What is still checked
    is what the text names: an admin program anywhere in it (`echo $(sudo id)`), a secret among its
    words (`cat .env*`, `sed -n p ~/.ssh/id_rsa`) and a write into the workspace code. `$HOME` is
    expanded first, and a glob counts through its literal prefix. A bare name (`credentials`,
    `id_rsa`) counts only when it names an existing file, so `grep -r credentials .` is not a secret;
    a word with a `/`, a leading `.` or `~`, or a glob is explicit. Other paths are not decided here.
    """
    findings = [Finding(DEFER, "shell", None)]
    admin = _admin_in_text(text)
    if admin:
        findings.append(Finding("admin", admin, None))
    home = os.path.expanduser("~")
    if not home.startswith("~"):
        text = text.replace("${HOME}", home).replace("$HOME", home)
    for word in re.split(r"[\s;&|()<>`'\"{}$=,]+", text):
        if not word or word.startswith("-"):
            continue
        glob = re.search(r"[*?\[]", word)
        if glob and _secret_glob(os.path.basename(word)):
            findings.append(Finding("secrets", word, None))
            continue
        base = word[:glob.start()] if glob else word
        base = base.rstrip("/") or base[:1]
        # A glob with no literal prefix (`*`, `*.py`) is in the directory the shell is in.
        path = _resolve(base, anchor) if base else (anchor if glob else None)
        if path is None:
            continue
        explicit = bool(glob) or "/" in word or word.startswith((".", "~")) or os.path.exists(path)
        if explicit and _is_secret(path):
            findings.append(Finding("secrets", path, path))
        else:
            capability = _path_capability(path, root, True)
            if capability in ("workspace.write", CONTROL_PLANE):
                findings.append(Finding(capability, path, path))
    return findings


def _segment_findings(segment, root, anchor):
    """`(findings, anchor)` for one shell segment: its redirections and program, and the directory the
    shell is in afterwards. An ambiguous segment defers with `_ambiguous_findings`; an ambiguous `cd`
    leaves the anchor at the box root, since the real directory is unknown."""
    try:
        tokens = _tokens(segment)
    except ValueError:
        return _ambiguous_findings(segment, root, anchor), anchor
    redirects, clean = _extract_redirects(tokens)
    if redirects is None:
        return _ambiguous_findings(segment, root, anchor), anchor
    findings = []
    for write, value in redirects:
        path = _resolve(value, anchor)
        if path is None:
            return _ambiguous_findings(segment, root, anchor), anchor
        findings.append(Finding(_path_capability(path, root, write), path, path))
    reanchored = _cd_anchor(clean, root, anchor)
    if reanchored is None:
        return _ambiguous_findings(segment, root, anchor), root
    if reanchored is _CD_NOT:
        part = _classify_tokens(clean, root, anchor)
        if part is None:
            part = _ambiguous_findings(segment, root, anchor)
        return findings + part, anchor
    part, new_anchor = reanchored
    return findings + part, new_anchor


def _classify_command(command, root):
    """Findings for a shell command. Nothing here is a deny by itself: an unknown or ambiguous
    segment defers (`_ambiguous_findings`) and every known segment keeps its own table entry."""
    if not isinstance(command, str) or not command.strip():
        return [Finding(DEFER, "shell", None)]
    if "`" in command or "$(" in command:
        return _ambiguous_findings(command, root, root)     # command substitution
    try:
        segments = _split_segments(command)
    except ValueError:                      # unclosed quote
        return _ambiguous_findings(command, root, root)
    findings = []
    anchor = root
    for segment in segments:
        part, anchor = _segment_findings(segment, root, anchor)
        findings.extend(part)
    return findings or [Finding(None, "shell", None)]


# --- decisions, auto and log -------------------------------------------------------------------

def _root_from_env(event):
    value = os.environ.get("WS_SESSION_ROOT")
    declared = bool(value)
    if not value and isinstance(event, dict):
        value = event.get("cwd")
    if not value:
        return None, False
    try:
        return os.path.realpath(os.path.normpath(os.path.abspath(os.path.expanduser(str(value))))), declared
    except (OSError, ValueError):
        return None, declared


def _auto_enabled(event):
    setting = os.environ.get("WS_POLICY_AUTO") or os.environ.get("WS_AUTO")
    if isinstance(setting, str) and setting.strip().lower() in ("1", "true", "yes", "auto"):
        return True
    if isinstance(event, dict):
        if event.get("auto") is True:
            return True
        mode = event.get("permission_mode") or event.get("permissionMode") or event.get("mode")
        if isinstance(mode, str) and mode.strip().lower() == "auto":
            return True
    return False


def _state_root():
    if paths is not None:
        return paths.state_dir()
    base = os.environ.get("WS_STATE")
    if base:
        return Path(base).expanduser()
    override = os.environ.get("AT_WS_CONFIG")
    folder = (Path(override).expanduser().parent if override
              else Path.home() / ".config" / "ai-tmux-workspace")
    return folder / "state"


def _log_path():
    return _state_root() / "policy.jsonl"


def _ask_mode():
    """Whether the person chose to decide unknown actions themselves (opt-in, item 119).

    On when `WS_POLICY_ASK`/`WS_POLICY_OPEN` is 1/true/yes or when the person dropped the marker
    `policy.ask`/`policy.open` in the state directory. Without either the engine keeps failing
    closed, so deleting the marker closes the mode again.
    """
    setting = os.environ.get("WS_POLICY_ASK") or os.environ.get("WS_POLICY_OPEN")
    if isinstance(setting, str) and setting.strip().lower() in ("1", "true", "yes"):
        return True
    try:
        root = _state_root()
        return any((root / marker).is_file() for marker in ("policy.ask", "policy.open"))
    except (OSError, RuntimeError):
        return False


def _one_line(value, limit):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def record(entry):
    """Append one JSONL line, mode 0600, never raising."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            try:
                os.fchmod(descriptor, 0o600)
            except (AttributeError, OSError):
                os.chmod(path, 0o600)
            os.write(descriptor, line)
        finally:
            os.close(descriptor)
    except Exception:
        pass


def _finish(decision, capability, reason, event, role=None, target=None):
    _role = role if role is not None else os.environ.get("WS_SESSION_ROLE")
    session = None
    if isinstance(event, dict):
        session = event.get("session_id")
    if not isinstance(session, str) or not session:
        session = os.environ.get("WS_SESSION_ID")
    record({
        "ts": time.time(),
        "role": _one_line(_role, MAX_SESSION) if _role else None,
        "session": _one_line(session, MAX_SESSION) if session else None,
        "capability": capability,
        "target": _one_line(target, MAX_TARGET) if target else None,
        "decision": decision,
        "reason": reason,
    })
    return {"decision": decision, "capability": capability, "reason": reason}


def check_event(event):
    """The `{decision, capability, reason}` for one normalized event. Never raises."""
    if not isinstance(event, dict):
        return _finish("deny", None, "policy.reason.unknown", event)
    declared = os.environ.get("WS_SESSION_ROLE")
    role = declared.strip().lower() if isinstance(declared, str) and declared.strip() else ""
    if not role:
        return _finish("deny", None, "policy.reason.no_role", event)
    root, root_declared = _root_from_env(event)

    subagent = bool(event.get("agent_id")) or event.get("subagent") is True or role == "subagent"
    if role == "subagent":
        parent = (os.environ.get("WS_SESSION_PARENT_ROLE") or os.environ.get("WS_PARENT_ROLE")
                  or event.get("parent_role"))
        parent = parent.strip().lower() if isinstance(parent, str) else ""
    else:
        parent = role
    if subagent and parent not in PARENT_ROLES:
        return _finish("deny", None, "policy.reason.no_parent", event, role="subagent")
    if not subagent and parent not in PARENT_ROLES:
        return _finish("deny", None, "policy.reason.unknown_role", event, role=role)

    try:
        document = load_policy()
    except PolicyError as error:
        return _finish("deny", None, error.reason, event, role="subagent" if subagent else parent)

    try:
        findings = classify(event, root)
    except Exception:
        findings = None
    failed_closed = "subagent" if subagent else parent
    ask_mode = _ask_mode()
    if findings is None:
        # A malformed event (no tool name, not an object) cannot be placed at all: it stays a deny.
        # Opt-in item 119: with the `ask` marker it is the person's call instead. The target is the
        # tool name, never the raw command.
        if ask_mode:
            tool = event.get("tool_name")
            return _finish("ask", None, "policy.reason.ask_mode", event,
                           role=failed_closed, target=tool if isinstance(tool, str) else None)
        return _finish("deny", None, "policy.reason.unknown", event, role=failed_closed)

    auto = _auto_enabled(event) and root_declared
    capabilities = _effective_capabilities(document, parent, subagent)
    best = None
    for finding in findings:
        if finding.capability is None:
            decision, reason = "allow", "policy.reason.inert"
        elif finding.capability == DEFER:
            # Unknown program: the workspace does not decide; the session mode does (item 135).
            # `ask` mode (opt-in marker) keeps asking the person about the unknown.
            decision, reason = (("ask", "policy.reason.ask_mode") if ask_mode
                                else ("allow", "policy.reason.defer"))
        elif finding.capability == CONTROL_PLANE:
            # Protected-path list before the role capabilities (item 72): no role writes the
            # control plane, and neither `auto` nor `ask` mode turns this into anything else.
            decision, reason = "deny", "policy.reason.control_plane"
        elif finding.capability in ("workspace.read", "workspace.write"):
            decision, reason = _protected_decision(finding.capability, root, root_declared)
        else:
            decision = capabilities.get(finding.capability, "deny")
            reason = "policy.reason." + decision
            if decision == "deny" and ask_mode and finding.capability in ("net", "install"):
                decision, reason = "ask", "policy.reason.ask_mode"
            if (decision == "ask" and auto and finding.capability in BOX_CAPABILITIES
                    and _inside_box(finding.path, root)):
                decision, reason = "allow", "policy.reason.auto"
        if best is None or _RANK[decision] < _RANK[best[0]]:
            best = (decision, finding, reason)

    decision, finding, reason = best
    if finding.capability == DEFER:
        # The log names the tool, never the program's arguments, like the ask-mode path did.
        tool = event.get("tool_name")
        return _finish(decision, None, reason, event, role="subagent" if subagent else parent,
                       target=tool if isinstance(tool, str) else None)
    return _finish(decision, finding.capability, reason, event,
                   role="subagent" if subagent else parent, target=finding.target)


# --- CLI ---------------------------------------------------------------------------------------

USAGE = "usage: policy.py check < event.json"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if not argv or argv[0] not in ("check", "privileged"):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        event = json.load(sys.stdin)
    except (OSError, ValueError):
        event = None
    result = check_event(event)
    if argv[0] == "privileged" and result.get("decision") == "privileged":
        # OpenCode adapter (item 154): the person approves, then pkexec runs the command here, in the
        # same call. The Claude hook calls `privileged.run_approved` itself and never reaches this.
        import privileged     # lazy: tmux is only needed on the approval path
        result = privileged.run_approved(event)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
