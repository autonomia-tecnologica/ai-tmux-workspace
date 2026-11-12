#!/usr/bin/env bash
# Killswitch: undoes install.sh from its backup manifest, restoring the previous state, then removes
# the clone that `install.sh` made (~/.local/share/ai-tmux-workspace/app). Refuses to run as root/under
# sudo. With no manifest there is nothing to remove. A dev checkout is never removed: only INSTALL_DIR,
# and only when it is a real clone of this repo (not a symlink, origin = AT_WS_REPO_URL).
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="${AT_WS_REPO_URL:-https://github.com/autonomia-tecnologica/ai-tmux-workspace.git}"
INSTALL_DIR="${AT_WS_INSTALL_DIR:-$HOME/.local/share/ai-tmux-workspace/app}"

if ! command -v python3 >/dev/null 2>&1; then
  printf 'ai-tmux-workspace: python3 is required to run remove.sh.\n' >&2
  exit 1
fi

python3 "$ROOT/src/installer.py" --remove "$@"

if [ -L "$INSTALL_DIR" ]; then
  printf 'ai-tmux-workspace: %s is a symlink; not removed.\n' "$INSTALL_DIR" >&2
elif [ -d "$INSTALL_DIR/.git" ] && [ -f "$INSTALL_DIR/src/installer.py" ] \
    && [ "$(git -C "$INSTALL_DIR" config --get remote.origin.url 2>/dev/null || true)" = "$REPO_URL" ]; then
  rm -rf -- "$INSTALL_DIR"
  printf 'removed: %s\n' "$INSTALL_DIR"
  # Parents that the install's mkdir -p created (ai-tmux-workspace/, share/, .local/) go too, but only
  # while they are empty: the walk stops at the first directory with anything else in it, or at HOME.
  parent="$(dirname -- "$INSTALL_DIR")"
  while [ "$parent" != "$HOME" ] && [ "$parent" != "/" ] && rmdir -- "$parent" 2>/dev/null; do
    parent="$(dirname -- "$parent")"
  done
elif [ -e "$INSTALL_DIR" ]; then
  printf 'ai-tmux-workspace: %s is not a clone of %s; not removed.\n' "$INSTALL_DIR" "$REPO_URL" >&2
fi
