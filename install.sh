#!/bin/sh
# Installs the workspace: checks the basics and hands over to the step-by-step Python assistant.
# The assistant installs the CLI, the autocomplete, the initial harness and the Claude Code hooks,
# and only shows the OS dependency commands (it never runs sudo/pkexec). Undo with ./remove.sh.
#
# From a checkout: ./install.sh
# From the web:
#   curl -fsSL https://raw.githubusercontent.com/autonomia-tecnologica/ai-tmux-workspace/main/install.sh | sh
# Piped, it clones the code into ~/.local/share/ai-tmux-workspace/app first.
set -eu

REPO_URL="${AT_WS_REPO_URL:-https://github.com/autonomia-tecnologica/ai-tmux-workspace.git}"
INSTALL_DIR="${AT_WS_INSTALL_DIR:-$HOME/.local/share/ai-tmux-workspace/app}"

# The installer needs Python >= 3.9 (it uses str.removeprefix). On a fresh macOS
# /usr/bin/python3 is the Xcode Command Line Tools shim: `command -v` succeeds, but running it
# either pops the CLT installer or fails with `xcrun: error: invalid active developer path`.
platform() {
  uname -s 2>/dev/null || printf 'unknown'
}

python_hint() {
  if [ "$(platform)" = "Darwin" ]; then
    printf '  Install the Apple Command Line Tools: xcode-select --install\n' >&2
    printf '  Or install Python with Homebrew:      brew install python\n' >&2
  fi
}

if ! command -v python3 >/dev/null 2>&1; then
  printf 'ai-tmux-workspace: python3 >= 3.9 is required to run the installer.\n' >&2
  python_hint
  exit 1
fi

PYTHON_PATH="$(command -v python3)"
# Without the Command Line Tools, macOS leaves only the shim at /usr/bin/python3; running it would
# pop the CLT installer. Detect that before running anything.
if [ "$(platform)" = "Darwin" ] && [ "$PYTHON_PATH" = "/usr/bin/python3" ] \
   && ! xcode-select -p >/dev/null 2>&1; then
  printf 'ai-tmux-workspace: python3 >= 3.9 is required; the macOS Command Line Tools are not installed.\n' >&2
  python_hint
  exit 1
fi

# Run the interpreter: the shim can exist and still fail, so the check is the version probe itself,
# not `command -v`. `set -e` does not abort inside the `if` condition.
PYTHON_VERSION="$("$PYTHON_PATH" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
if ! "$PYTHON_PATH" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  printf 'ai-tmux-workspace: python3 >= 3.9 is required (found: %s).\n' "${PYTHON_VERSION:-unknown}" >&2
  python_hint
  exit 1
fi

# $0 is the script path when run from a file and the shell name when piped.
ROOT=""
if [ -f "$0" ]; then
  ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
fi

if [ -z "$ROOT" ] || [ ! -f "$ROOT/src/installer.py" ] || [ ! -f "$ROOT/bin/workspace" ]; then
  if ! command -v git >/dev/null 2>&1; then
    printf 'ai-tmux-workspace: git is required to install from the web.\n' >&2
    exit 1
  fi
  if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull --ff-only
  elif [ -e "$INSTALL_DIR" ]; then
    printf 'ai-tmux-workspace: %s exists and is not a git checkout; remove it or set AT_WS_INSTALL_DIR.\n' "$INSTALL_DIR" >&2
    exit 1
  else
    mkdir -p "$(dirname -- "$INSTALL_DIR")"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
  ROOT="$INSTALL_DIR"
fi

# Under `curl ... | sh` stdin is the script itself, already consumed: the assistant asks questions, so it
# reads the terminal instead when there is one. AT_WS_NO_TTY=1 keeps the inherited stdin (tests, automation).
if [ ! -t 0 ] && [ -z "${AT_WS_NO_TTY:-}" ] && (exec </dev/tty) 2>/dev/null; then
  exec </dev/tty
fi

exec "$PYTHON_PATH" "$ROOT/src/installer.py" "$@"
