#!/usr/bin/env bash
# Killswitch: undoes install.sh from its backup manifest, restoring the previous state.
# Refuses to run as root/under sudo. With no manifest there is nothing to remove.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  printf 'ai-tmux-workspace: python3 is required to run remove.sh.\n' >&2
  exit 1
fi

exec python3 "$ROOT/src/installer.py" --remove "$@"
