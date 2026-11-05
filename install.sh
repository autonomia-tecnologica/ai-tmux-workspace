#!/usr/bin/env bash
# Installs the `workspace` command: symlinks bin/workspace into ~/.local/bin (override with PREFIX_BIN),
# checks the dependencies and seeds the config from the example when it does not exist yet.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
TARGET_DIR="${PREFIX_BIN:-$HOME/.local/bin}"
CONFIG="${AT_WS_CONFIG:-$HOME/.config/ai-tmux-workspace/settings.json}"

missing=()
for dep in tmux python3 git; do command -v "$dep" >/dev/null || missing+=("$dep"); done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "error: missing required tools: ${missing[*]}" >&2
  exit 1
fi
for dep in claude gh nvim; do
  command -v "$dep" >/dev/null || echo "warn: optional tool not found: $dep"
done

mkdir -p "$TARGET_DIR"
ln -sf "$ROOT/bin/workspace" "$TARGET_DIR/workspace"
echo "ok: $TARGET_DIR/workspace -> $ROOT/bin/workspace"

case ":$PATH:" in
  *":$TARGET_DIR:"*) ;;
  *) echo "warn: $TARGET_DIR is not in your PATH; add it to your shell profile" ;;
esac

if [ ! -f "$CONFIG" ]; then
  mkdir -p "$(dirname "$CONFIG")"
  cp "$ROOT/config/settings.example.json" "$CONFIG"
  echo "ok: created $CONFIG from the example; edit it to list your repos"
fi

echo "next: workspace install-hooks   (optional, lights the status dots)"
echo "      workspace                 (opens the workspace)"
