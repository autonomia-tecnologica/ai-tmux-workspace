---
name: config-backlog
description: Applies the general BACKLOG.md rule to the repos configured in ai-tmux-workspace (config.yaml groups) so the tasks panel can show their tasks. Audits each repo, then fixes .gitignore, migrates legacy TODO/ROADMAP files and normalizes the format. Use when the user runs /config-backlog or asks to standardize, migrate or check the backlog of the workspace repos.
---

# /config-backlog

Brings every repo listed in `~/.config/ai-tmux-workspace/config.yaml` (`groups`) to the general
backlog rule, so `src/panel_tasks.py` (via `src/backlog.py`) can read it. The rule is defined in
`~/.claude/CLAUDE.md`, section Backlog. Read it first; it is the source of truth. This skill only
applies it. Arguments (optional): repo names or paths to restrict the run; none = all repos.

## What "conforming" means (per repo)

1. `BACKLOG.md` at the repo root, pt-BR, only open items. No `TODO.md`, `docs/BACKLOG.md`, etc.
2. `/BACKLOG.md` in the repo's `.gitignore` (root-anchored) and the file not tracked by git. This
   applies to every repo, even one without a backlog.
3. Format: `Próximo id: N` at the top; the two sections `## Refinadas`, `## A refinar` in that order
   (a legacy `## A validar` is tolerated: its items are closed and not listed; do not add it); items `### N. Title` starting with the mandatory `- Categoria: X` field, then the four fields
   (`Caminhos`, `Depende de`, `Aceite`, `Validação`; `Arquivos` is a legacy alias for `Caminhos`); items are grouped by category and
   categories never depend on each other (a `Depende de` id in another category is invalid); unique, incremental integer ids; no dependency cycles; item ~15 lines.
4. A repo without a `BACKLOG.md` has no tasks: never create an empty one.

## Steps

1. **Audit** (read-only): `python3 -I ~/Codes/ai-tmux-workspace/.claude/skills/config-backlog/audit.py [repo ...]`.
   It prints one JSON line per repo: `ok`, `ignored_as_root_rule`, `tracked`, `legacy_files`,
   `panel_parses`, `problems`, `cycles`, `typo_deps`, `cross_category`, `gone`, `legacy_validate`. Summarize it as a table (repo → what is wrong).
   List the `gone` ids per repo: they are delivered by absence, so the person confirms none is a lost or mistyped item.
2. **Plan**: list the exact changes per repo and ask for confirmation before touching any repo. Group
   the cheap mechanical ones (gitignore, empty sections) separately from the ones that need judgment
   (legacy migration, missing fields).
3. **Apply**, per repo, only after confirmation:
   - **Gitignore**: append `/BACKLOG.md` to `.gitignore` (create the file if absent). If a non-anchored
     `BACKLOG.md` line exists, replace it with `/BACKLOG.md`. If the file is tracked, `git rm --cached BACKLOG.md`.
   - **Format**: add the missing `## A refinar` (or other) heading, set `Próximo id` above the highest id
     ever used, fix titles to `### N. Title`. Do not renumber existing items. Never reuse an id.
   - **Legacy files** (`TODO.md`, `ROADMAP.md`, ...): convert each open entry into an item in
     `## A refinar` with a new id from `Próximo id`. Fill only the fields the source actually supports;
     write `Aceite`/`Validação` as "a definir" when unknown rather than inventing them. Done entries are dropped.
     Show the result and ask before deleting the legacy file (removal is the user's call).
   - **Missing `Categoria`**: propose a category per item (short name, reuse the repo's existing ones), ask, then insert `- Categoria: X` right under the title.
   - **Missing fields / cycles / typo deps / cross-category deps**: report them, propose the fix, ask. Do not guess.
   - An item too big for one slice (65k context) is split into new-numbered items; the original leaves.
4. **Commit**: the only versioned change is `.gitignore` (and the untracking). One commit per repo with
   that message ending in `AI Generated` (English message, no tool/model/co-author mention). `BACKLOG.md`
   is never staged. Never push; each repo's remote is the user's call.
5. **Re-audit** the touched repos and report: repos fixed, repos still pending and why, anything skipped.
   Remember the panel refreshes on its own; `Ctrl+b r` reloads if needed.

## Limits

- Do not edit anything outside the listed repos' `BACKLOG.md`/`.gitignore`/legacy backlog files.
- Do not change a repo with uncommitted work in `.gitignore` without saying so: check `git status` first and
  commit only `.gitignore`.
- Keep the audit script and the parser (`src/backlog.py`) in sync if the format rule changes.
