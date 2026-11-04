# ai-tmux-workspace

Workspace minimalista baseado em tmux, sem pretensão de ser algo novo: só uma abordagem específica (um repo por janela, agente Claude Code ao lado).

**Configuração:** um único `~/.config/at-ai-workspace/settings.json` (`profiles` e `groups`; modelo em `settings.example.json`). O repo guarda só a estrutura. Outro caminho: `AT_WS_CONFIG`.

tmux no estilo do Orca para os repos de `groups` do settings.json. Comando: `workspace` (abre `claude --settings …/settings.claude.json`) ou
`workspace deepseek` (idem com `settings.deepseek.json`); os perfis estão em `profiles` do settings.json. Cada perfil tem o seu
servidor tmux (socket `ws-<perfil>`), separado do seu tmux normal.

    ┌ status: repo • modelo • consumo (5h / 7d, tokens) ───────────────┐
    │ repos       │                        │ ARQUIVOS SESSÕES DIFF ... │
    │ ● grupos    │ claude (5/6)           │ github                    │
    │             │                        │ (aba escolhida)           │
    │             ├────────────────────────┤                           │
    │             │ terminal (1/6)         │                           │
    └─ 2 ─────────┴──────── 7 ─────────────┴──────────── 3 ───────────┘

Proporção 2:7:3. A coluna esquerda é só a lista de repos; branch e commits ficam na aba diff.
Cada repo é uma janela do tmux; clicar/Enter num repo da lista troca para ela.

## Bolinhas
○ parado · 🟢 agente/subagente trabalhando · 🟡 uma ferramenta rodando há mais de 8 s ·
🔴 esperando você (permissão/pergunta). Vêm dos hooks do Claude Code: rode uma vez
`workspace install-hooks` (faz backup do `settings.json`; reinicie as sessões abertas).

## Teclas
- painéis: `j/k`, setas, PgUp/PgDn, `g/G`, mouse; Enter abre; no painel de repos, Enter num grupo recolhe.
- lado direito: a barra do topo troca entre ARQUIVOS (padrão) · SESSÕES salvas (Enter retoma) · DIFF atual (Enter abre o diff) ·
  GITHUB (resumo via `gh`: repo, PR da branch com checks, PRs, issues, CI, release). Clique numa aba, ou foque a barra (`ctrl+b ↑`) e use ←/→ + Enter. Arquivos abrem no nvim.
- `ctrl+b r` recarrega a config e a tira de abas sem sair do tmux.
- abas do Claude (tira de 1 linha no topo do painel do Claude): a primeira é o Claude principal; `+` (ou `ctrl+b t`) abre uma sessão
  temporária no mesmo repo/worktree; clique numa aba (ou `ctrl+b n`/`p`) para trocar. Ao sair do Claude da aba,
  ela some e o foco volta ao principal. Abas temporárias não são restauradas no `restart`.
- `shift+setas` pulam entre painéis.

## Comandos
`workspace [perfil]` · `restart [perfil]` (derruba e sobe de novo; use depois de mudar hooks/config) · `stop [perfil]` · `install` · `install-hooks`.

## Persistência
Sem plugins de tmux. O Claude abre com `claude --continue` (senão, sessão nova) e o resto vem do
`settings.json` e dos arquivos do repo.

## Arquivos
`settings.example.json` (modelo da config) · `tmux.conf` · `workspace` (launcher) · `panel_*.py` (incl. `panel_tabs.py`, a tira de abas) ·
`viewer.py` (desenho sem flicker: só reescreve as linhas que mudaram) · `hook.py` · `statusbar.py`.
