"""Doctor for the installed harness: compares it with source.json, the canonical source and the code.

`workspace doctor` calls this module; `reload` calls it in warning mode (`--warn`), which
never fails the reload. Exit code: 0 when the installed harness is consistent, 1 when the
installed copy, the canonical source or a path cited in the HARNESS.md drifted.

The roots used to resolve paths cited in the HARNESS.md are the canonical source (when it
still exists), the installed harness and the running code (WS_DIR, falling back to this
checkout). The full installed-code layout (backlog item 75) is out of scope here.

Stdlib only. All end-user text comes from i18n/pt-BR.json (`doctor.*` keys).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

from i18n import t

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("HOME") or Path.home())
DEFAULT_HARNESS_DIR = HOME / ".local" / "share" / "ai-tmux-workspace" / "harness"
SOURCE_FILE = "source.json"

# Only code/config paths are checked: the HARNESS.md quotes prose, commands and anti-examples
# (e.g. `docs/BACKLOG.md`) that must not turn into a false drift.
CODE_EXTENSIONS = {".py", ".js", ".yaml", ".yml", ".json", ".sh", ".conf", ".toml", ".ini",
                   ".cfg", ".bash", ".zsh"}
CODE_DIRS = {"src", "bin", "harness", "config", "i18n", "tests"}


def harness_dir():
    """The installed harness directory: AT_HARNESS_DIR overrides it, as in lib.py."""
    value = os.environ.get("AT_HARNESS_DIR")
    return Path(value).expanduser() if value else DEFAULT_HARNESS_DIR


def code_root():
    """The code that is running: WS_DIR when set, otherwise this checkout."""
    value = os.environ.get("WS_DIR")
    return Path(value).expanduser() if value else ROOT


def sha256_file(path):
    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit(path):
    """HEAD of the git worktree containing path, or None when it is not a git repository."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        result = subprocess.run([git, "-C", str(path), "rev-parse", "HEAD"],
                                capture_output=True, text=True)
    except OSError:
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def read_record(installed):
    """The source.json next to the installed HARNESS.md, or an error string."""
    path = Path(installed) / SOURCE_FILE
    if not path.is_file():
        return None, t("doctor.source_json_missing", path=str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, t("doctor.source_json_invalid", path=str(path), error=str(error))
    if not isinstance(data, dict) or not isinstance(data.get("source"), str):
        return None, t("doctor.source_json_invalid", path=str(path), error="missing source")
    return data, None


def cited_paths(text):
    """Relative code/config paths quoted in inline code spans, in order, without duplicates."""
    found = []
    for token in re.findall(r"`([^`\n]+)`", text):
        candidate = token.strip()
        if not candidate or " " in candidate or "\t" in candidate:
            continue
        if candidate.startswith(("/", "~")) or "://" in candidate:
            continue
        if any(char in candidate for char in "<>*${}\\"):
            continue
        parts = PurePosixPath(candidate).parts
        if "/" not in candidate or not parts or ".." in parts or candidate.endswith("/"):
            continue
        if PurePosixPath(candidate).suffix in CODE_EXTENSIONS or parts[0] in CODE_DIRS:
            if candidate not in found:
                found.append(candidate)
    return found


def path_roots(installed, source):
    """Existing roots where a path cited in the HARNESS.md may live."""
    roots = [Path(installed), code_root()]
    if source is not None and Path(source).is_dir():
        roots.insert(0, Path(source))
    unique, seen = [], set()
    for root in roots:
        try:
            key = os.path.realpath(str(root))
        except OSError:
            key = str(root)
        if key not in seen and Path(root).is_dir():
            seen.add(key)
            unique.append(Path(root))
    return unique


def check(installed=None):
    """List of (level, message) from comparing the installed harness with source.json.

    level is "fail" (the doctor rejects the state), "drift" (the installed state changed) or
    "note" (informational, does not change the exit code).
    """
    installed = Path(installed) if installed is not None else harness_dir()
    issues = []
    record, error = read_record(installed)
    if error:
        return [("fail", error)]
    installed_harness = installed / "HARNESS.md"
    current_installed = sha256_file(installed_harness)
    expected_installed = record.get("installed_sha256")
    if current_installed is None:
        issues.append(("fail", t("doctor.installed_missing", path=str(installed_harness))))
    elif expected_installed != current_installed:
        issues.append(("drift", t("doctor.installed_edited")))

    source = Path(record["source"]).expanduser()
    if source.is_dir():
        source_harness = source / "HARNESS.md"
        if sha256_file(source_harness) != record.get("source_sha256"):
            issues.append(("drift", t("doctor.canonical_changed")))
        expected_commit = record.get("commit")
        current_commit = git_commit(source)
        if expected_commit != current_commit:
            issues.append(("drift", t("doctor.commit_changed",
                                      expected=expected_commit or "?", current=current_commit or "?")))
    else:
        issues.append(("note", t("doctor.source_missing", path=str(source))))

    text = installed_harness.read_text(encoding="utf-8", errors="replace") if installed_harness.is_file() else ""
    roots = path_roots(installed, source if source.is_dir() else None)
    for relative in cited_paths(text):
        if not any((root / relative).exists() for root in roots):
            issues.append(("fail", t("doctor.path_missing", path=relative)))
    return issues


def doctor(warn=False):
    """Print the report; return 0 when consistent (or in warning mode) and 1 otherwise."""
    issues = check()
    for level, message in issues:
        if level == "note":
            if not warn:
                print(t("doctor.note", message=message))
        elif level == "fail":
            print(t("doctor.fail", message=message))
        else:
            print(t("doctor.warn", message=message))
    if warn:
        return 0
    if any(level != "note" for level, _message in issues):
        return 1
    print(t("doctor.ok", path=str(harness_dir())))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    unknown = [argument for argument in argv if argument not in ("--warn", "-w")]
    if unknown:
        print(t("doctor.usage"), file=sys.stderr)
        return 2
    return doctor(warn=bool([argument for argument in argv if argument in ("--warn", "-w")]))


if __name__ == "__main__":
    sys.exit(main())
