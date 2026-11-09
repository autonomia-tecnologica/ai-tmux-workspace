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

if ! command -v python3 >/dev/null 2>&1; then
  printf 'ai-tmux-workspace: python3 is required to run the installer.\n' >&2
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

exec python3 "$ROOT/src/installer.py" "$@"
