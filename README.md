# ai-tmux-workspace

Workspace minimalista baseado em tmux, sem pretensão de ser algo novo: só uma abordagem específica (um repo por janela, agente Claude Code ao lado).

A filosofia do ambiente de trabalho mora em `ambiente-de-trabalho` (`docs/pt_BR/filosofia.md`) e vale sobre este repo. O manifesto da Autonomia Tecnológica mora primariamente no site (`site/i18n/pt_BR.json`).

**Configuração:** um único `~/.config/ai-tmux-workspace/settings.json` (`profiles`, `main`, `groups` e `prompts`; modelo em `settings.example.json`). O repo guarda só a estrutura. Outro caminho: `AT_WS_CONFIG`.

tmux para os repos de `groups` do settings.json. Comando: `workspace` (abre `claude --settings …/settings.claude.json`) ou
`workspace deepseek` (idem com `settings.deepseek.json`); os perfis estão em `profiles` do settings.json. Cada perfil tem o seu
servidor tmux (socket `ws-<perfil>`), separado do seu tmux normal.

    ┌ status: repo • modelo • consumo (5h / 7d, tokens) ───────────────┐
    │ REPOS       │ CLAUDE (abas)          │ ARQUIVOS DIFF GITHUB ... │
    │ ● grupos    │                        │ GITHUB                    │
    ├─────────────┤                        │ (aba escolhida)           │
    │ TAREFAS     ├────────────────────────┤                           │
    │ (BACKLOG)   │ TERMINAL (abas)        │                           │
    └─ 2 ─────────┴──────── 7 ─────────────┴──────────── 3 ───────────┘

Proporção 2:7:3. A coluna esquerda tem os repos (metade de cima) e as tarefas do `BACKLOG.md` do repo da janela (metade de baixo); branch e commits ficam na aba diff.
O repo principal (`main` no settings.json) fica fora dos grupos, sempre no topo da lista, em laranja (azul quando é o repo da janela).
Cada repo é uma janela do tmux; clicar/Enter num repo da lista troca para ela.

## Bolinhas
○ parado · 🟢 agente/subagente trabalhando · 🟡 uma ferramenta rodando há mais de 8 s ·
🔴 esperando você (permissão/pergunta). Vêm dos hooks do Claude Code: rode uma vez
`workspace install-hooks` (faz backup do `settings.json`; reinicie as sessões abertas).

## Notificações
Quando uma sessão do Claude passa a 🔴 (precisa de você), o hook dispara uma notificação do sistema:
título = nome do repo, corpo = trecho curto (pergunta, comando pedido ou fim da última resposta). Só na
transição para vermelho, então um pedido de permissão não notifica duas vezes. Desligue com
`"notifications": false` no `settings.json` (padrão: ligado). Plataformas: Linux (`notify-send`), macOS
(`osascript`), Windows nativo e WSL (toast via `powershell`, nada a instalar). Se o comando não existir, ignora.

## Extra usage na barra de status (opt-in, desligado por padrão)
Shows the account's monthly extra usage spend next to the 5h/7d percentages, e.g. `$12.30/$50`
(`$1,234/$5,000` from 100 up; separators follow the system locale; only `$12.30` if there is no monthly limit; omitted if extra usage is off or there is no data).
Enable with `"extra_usage": true` in `settings.json`. When off, nothing is read and no request is made.

When on, at most every 5 minutes `src/extra_usage.py` runs detached and does one GET to
`https://api.anthropic.com/api/oauth/usage` with your Claude Code OAuth access token
(`~/.claude/.credentials.json`, or the macOS keychain item `Claude Code-credentials`). Only the numbers
are cached (`extra_usage.json` next to `limits.json`). Risks: the token is an account key; the endpoint
is unofficial and may change or break without notice. Mitigations: fixed host, GET only, 5s timeout,
no redirects, proxies ignored, TLS verified, token never refreshed, logged, printed or written to disk;
an expired token just fails silently. Amounts come in minor units and are divided by
`10 ** decimal_places` from the response.

## Teclas
- painéis: `j/k`, setas, PgUp/PgDn, `g/G`, mouse; Enter abre; no painel de repos, Enter num grupo recolhe.
- lado direito: a barra do topo troca entre ARQUIVOS (padrão) · DIFF atual (Enter abre o diff) ·
  GITHUB (resumo via `gh`: repo, PR da branch com checks, PRs, issues, CI, release). Clique numa aba, ou foque a barra (`ctrl+b ↑`) e use ←/→ + Enter. Arquivos de código abrem no nvim; mídia e documentos (imagem, vídeo, áudio, PDF, Office...) abrem no programa padrão do sistema.
- `ctrl+b h` abre a ajuda · `ctrl+b o` abre a tela de opções (uma árvore só, já expandida: os prompts salvos em `prompts` no `settings.json`, que são digitados no Claude ativo sem enviar, e perguntar ao Haiku via `claude -p --model haiku` no repo da janela; `options` no `settings.json` acrescenta nós `ask`, `run` e `type`) · `ctrl+b r` recarrega a config e a tira de abas sem sair do tmux · `ctrl+b d` desanexa (o workspace continua rodando).
- barra do painel de repos: REPOS · SESSÕES (Claudes abertos de todos os repos do workspace, agrupados por repo; subagentes em execução viram uma linha `↳` dentro da sessão pai; Enter foca o painel da sessão).
- painel TAREFAS: `→`/`←` expandem/recolhem o item; Enter pede confirmação e manda a mensagem para o Claude (implementar, refinar ou "o que preciso validar?").
- abas do Claude (tira de 1 linha no topo do painel do Claude): a primeira é o Claude principal; `+` (ou `ctrl+b t`) abre uma sessão
  temporária no mesmo repo/worktree; clique numa aba (ou `ctrl+b n`/`p`) para trocar. Ao sair do Claude da aba,
  ela some e o foco volta ao principal. Abas temporárias não são restauradas no `restart`.
- `shift+setas` pulam entre painéis.

## Comandos
`workspace [perfil]` · `reload [perfil]` (recarrega config e painéis sem sair; é o `ctrl+b r`) · `restart [perfil]` (derruba e sobe de novo; só depois de mudar hooks do Claude ou plugins) · `stop [perfil]` · `install` · `install-hooks`.

## Persistência
Sem plugins de tmux. O Claude abre com `claude --continue` (senão, sessão nova) e o resto vem do
`settings.json` e dos arquivos do repo.

## Arquivos
`config/settings.example.json` (modelo da config) · `config/tmux.conf` · `bin/workspace` (launcher) · `src/panel_*.py` (incl. `panel_tabs.py`, a tira de abas, e `panel_tasks.py`) ·
`src/viewer.py` (desenho sem flicker: só reescreve as linhas que mudaram) · `src/hook.py` · `src/statusbar.py` · `src/backlog.py` (parser do `BACKLOG.md`) ·
`src/help.py` (popup da ajuda) · `src/opener.py` · `src/extra_usage.py` · `src/notify.py`.

## Textos (i18n)
Todo texto exibido e todo prompt enviado ao Claude mora em `i18n/` (hoje só `pt-BR`): `i18n/pt-BR.json` (chaves lidas por `src/i18n.py`) e `i18n/HELP.pt-BR.md` (a ajuda do `ctrl+b h`). Idioma novo = arquivos novos nessa pasta.
