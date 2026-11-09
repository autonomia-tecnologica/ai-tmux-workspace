#!/usr/bin/env bash
# Installs the workspace: checks python3 and hands over to the step-by-step Python assistant.
# The assistant installs the CLI, the autocomplete, the initial harness and the Claude Code hooks,
# and only shows the OS dependency commands (it never runs sudo/pkexec). Undo with ./remove.sh.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  printf 'ai-tmux-workspace: python3 is required to run the installer.\n' >&2
  exit 1
fi

exec python3 "$ROOT/src/installer.py" "$@"
