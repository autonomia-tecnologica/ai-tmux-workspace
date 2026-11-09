#!/usr/bin/env bash
# Regenerates docs/demo.gif and docs/image.png from a real workspace run on fictional repos.
#
# Isolation (nothing of the user's is read or written):
#   - repos are created in a temp directory (git init, BACKLOG.md, a few files); no real repo is used;
#   - AT_WS_CONFIG points to a generated copy of demo/settings.json, WS_STATE to the temp directory;
#   - TMUX_TMPDIR gives the workspace its own tmux socket directory. The launcher names its server
#     ws-<profile>, and the profile is "claude" by design, so without this the demo would share the
#     user's server. With it, the server is a different socket file and is killed at the end;
#   - HOME is an empty temp directory, so the status bar finds no real usage logs;
#   - git runs without the global config, and commits use the name "Demo" only.
# The agent in the panes is demo/fake_claude.py, a scripted stand-in: no API, no tokens, no network.
# The waiting time of the fake agent is not recorded: record.py holds each checkpoint for fixed seconds.
#
# Usage: demo/make-demo.sh          (needs tmux, python3 with Pillow, fc-match, git)
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$DEMO_DIR")"
OUT_DIR="$REPO_DIR/docs"

for tool in tmux python3 fc-match git; do
  command -v "$tool" >/dev/null || { echo "make-demo: falta '$tool' no PATH" >&2; exit 1; }
done
python3 -c "import PIL" 2>/dev/null || { echo "make-demo: falta o Pillow (python3 -c 'import PIL')" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ws-demo.XXXXXX")"
REPOS="$WORK/repos"
cleanup() {
  tmux -L ws-claude kill-server 2>/dev/null || true   # same socket name as the launcher, but TMUX_TMPDIR makes it the demo's
  rm -rf "$WORK"
}
trap cleanup EXIT

export HOME="$WORK/home"          # empty: statusbar.py reads usage from $HOME/.claude/projects, and the shell reads $HOME/.zshrc
cat >"$WORK/demo-rc" <<'EOF'
PS1='$ '
EOF
cat >"$WORK/demo-shell" <<EOF
#!/bin/sh
# Shell of the demo panes: bash without the user's rc or profile, and a neutral prompt (no machine or user name).
# tmux run-shell also uses \$SHELL, with -c: those commands go to a plain sh.
if [ "\$1" = "-c" ]; then exec /bin/sh -c "\$2"; fi
exec /bin/bash --rcfile "$WORK/demo-rc" --noprofile -i
EOF
chmod +x "$WORK/demo-shell"
export SHELL="$WORK/demo-shell"
export TMUX_TMPDIR="$WORK/tmux"
export AT_WS_CONFIG="$WORK/settings.json"
export WS_STATE="$WORK/state"
export WS_COLS=160 WS_LINES=46 WS_NO_ATTACH=1
export LC_ALL=C.UTF-8
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=Demo GIT_AUTHOR_EMAIL=demo GIT_COMMITTER_NAME=Demo GIT_COMMITTER_EMAIL=demo
mkdir -p "$HOME" "$TMUX_TMPDIR" "$WS_STATE" "$REPOS" "$OUT_DIR"

# --- fictional repos -------------------------------------------------------------------------
make_repo() { # $1 = name; stdin lists files as "@@ relative/path" lines, each followed by its content
  local dir="$REPOS/$1"
  mkdir -p "$dir"
  git -C "$dir" init -q -b main
  local current="" line
  while IFS= read -r line; do
    if [[ "$line" == "@@ "* ]]; then
      current="$dir/${line#@@ }"; mkdir -p "$(dirname "$current")"; : >"$current"
    else
      printf '%s\n' "$line" >>"$current"
    fi
  done
  git -C "$dir" add -A
  git -C "$dir" -c commit.gpgsign=false commit -q -m "Initial commit"
}

make_repo lojinha-api <<'EOF'
@@ README.md
# lojinha-api
API de exemplo da loja.
@@ src/app.py
def health():
    return {"status": "ok"}
@@ BACKLOG.md
# Backlog

## Refinadas

### 7. Rota de saúde com versão
- Categoria: infra
- Aceite: GET /health devolve status e a versão do build.

## A refinar

### 8. Limite de requisições por cliente
- Categoria: infra

Próximo id: 9
EOF

make_repo lojinha-web <<'EOF'
@@ README.md
# lojinha-web
Interface de exemplo da loja.
@@ src/cart.py
"""Carrinho da loja."""


def total(itens):
    return sum(i["preco"] * i["qtd"] for i in itens)
@@ tests/test_cart.py
from src.cart import total


def test_total_vazio():
    assert total([]) == 0
@@ BACKLOG.md
# Backlog

## Refinadas

### 12. Botão para esvaziar o carrinho
- Categoria: carrinho
- Aceite: clicar no botão zera os itens e o total.

### 13. Frete calculado no carrinho
- Categoria: carrinho
- Depende de: 12
- Aceite: o total mostra o frete pelo CEP informado.

## A refinar

### 14. Cupom de desconto
- Categoria: promoções

Próximo id: 15
EOF

make_repo notas-cli <<'EOF'
@@ README.md
# notas-cli
Ferramenta de exemplo para anotar tarefas no terminal.
@@ BACKLOG.md
# Backlog

## A refinar

### 3. Exportar notas em Markdown
- Categoria: exportação

Próximo id: 4
EOF

sed -e "s|__DEMO_DIR__|$DEMO_DIR|g" -e "s|__REPOS_DIR__|$REPOS|g" "$DEMO_DIR/settings.json" >"$AT_WS_CONFIG"

# --- run the workspace on its own server, then record ------------------------------------------
"$REPO_DIR/bin/workspace"
python3 "$DEMO_DIR/record.py" "$OUT_DIR"
