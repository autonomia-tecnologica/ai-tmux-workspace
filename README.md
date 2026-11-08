# ai-tmux-workspace

> **Sem garantia (no warranty).** Este software é fornecido "como está", sem garantia de qualquer tipo, expressa ou implícita — incluindo comercialização, adequação a um propósito específico e não violação. Os autores não respondem por qualquer dano, perda de dados ou prejuízo decorrente do uso ou da impossibilidade de usar este software. A licença completa está em [`LICENSE`](LICENSE).
>
> O workspace executa um agente de IA com acesso ao seu sistema de arquivos, terminal e credenciais. Use por sua conta e risco: revise as permissões, mantenha backups e leia o código antes de confiar.

![Demo: repos, agente e tarefas](docs/demo.gif)

Workspace minimalista baseado em tmux, sem pretensão de ser algo novo: só uma abordagem específica (um repo por janela, agente Claude Code ao lado).

A filosofia do ambiente de trabalho mora em `ambiente-de-trabalho` (`docs/pt_BR/filosofia.md`) e vale sobre este repo. O manifesto da Autonomia Tecnológica mora primariamente no site (`site/i18n/pt_BR.json`).

**Configuração:** um único `~/.config/ai-tmux-workspace/config.yaml` (`profiles`, `main`, `groups` e `prompts`; modelo em `config/config.example.yaml`). O repo guarda só a estrutura. Outro caminho: `AT_WS_CONFIG`. Quem ainda tem o `settings.json` antigo pode migrar pela tela de opções (`ctrl+b o`).

tmux para os repos de `groups` do config.yaml. Comando: `workspace`. O agente que roda nos painéis do Claude é escolhido em `agent` do config.yaml (comandos em `profiles`,
ex.: `claude` ou `claude --settings ~/.claude/settings.deepseek.json`); troca pelo menu de opções (Ctrl+b o) e reinicia o
workspace inteiro. Cada repo lembra a linha de comando do agente com que foi aberto por último: a mesma linha retoma a
sessão com `--continue`; uma linha diferente (ou a primeira abertura depois desta mudança) abre sessão nova. Na primeira
vez que o ambiente muda, o primeiro repo aberto com transcrição anterior recebe um prompt para relê-la e não perder o
contexto; os demais abrem a sessão nova sem repetir o aviso (vale para qualquer perfil ou config editada à mão). O
servidor tmux (socket `ws-claude`) é separado do seu tmux normal.

    ┌ status: repo • modelo • consumo (5h / 7d, tokens) ───────────────┐
    │ REPOS       │ CLAUDE (abas)          │ ARQUIVOS DIFF GITHUB ... │
    │ ● grupos    │                        │ GITHUB                    │
    ├─────────────┤                        │ (aba escolhida)           │
    │ TAREFAS     ├────────────────────────┤                           │
    │ (BACKLOG)   │ TERMINAL (abas)        │                           │
    └─ 2 ─────────┴──────── 7 ─────────────┴──────────── 3 ───────────┘

Proporção 2:7:3. A coluna esquerda tem os repos (metade de cima) e as tarefas do `BACKLOG.md` do repo da janela (metade de baixo); branch e commits ficam na aba diff.
O repo principal (`main` no config.yaml) fica fora dos grupos, sempre no topo da lista, em laranja (azul quando é o repo da janela).
Cada repo é uma janela do tmux; clicar/Enter num repo da lista troca para ela.

## Compatibilidade com modelos não-Claude

Perfis que não são o `claude` (DeepSeek, OpenRouter, Qwen, Strata...) sobem com o *tool search* do Claude Code desligado (`ENABLE_TOOL_SEARCH=false`). O motivo é de compatibilidade: o Claude Code grava o estado do tool search no transcript como blocos `tool_addition`/`tool_definition`, e os endpoints compatíveis com a Anthropic desses provedores recusam esse variant (422 em `messages[1].content`), o que quebra o `--continue`. Desligado por padrão, a sessão nova já nasce compatível e a troca de perfil deixa de produzir esse erro. O perfil `claude` não muda.

O modo de permissão segue a mesma regra de troca: o `claude` abre em `acceptEdits` e um perfil não-Claude abre em `bypassPermissions` — sem o classifier, `acceptEdits` pararia cada comando de Bash num prompt. O último modo escolhido é salvo por repo (enquanto a linha de comando do perfil for a mesma) e reaplicado nas aberturas seguintes daquele repo; a troca de perfil não reaplica o modo do perfil anterior, e `auto` nunca é aplicado a um perfil não-Claude. Os settings de cada perfil não-Claude também declaram `permissions.defaultMode: bypassPermissions`, para sessões abertas fora do workspace.

Sessões antigas abertas no perfil `claude` ainda não são retomadas no perfil novo: elas abrem uma sessão nova com o prompt de migração (uma vez só). Retomar o transcript antigo de verdade é item de backlog, não um passo escondido no launcher. A medida vale enquanto a ferramenta de harness não for trocada; quando o at-harness entrar, a decisão é revista. Ela não é uma barreira de segurança.

## Platforms (Windows)
Decision: Windows is supported only through WSL2, plus the system toast notification (see Notificações). There is no tmux clone and no custom multiplexer for native Windows.

Why:
- tmux does not run on native Windows, and the whole workspace is built on it.
- The launcher (`bin/workspace`) is a bash script.
- `src/viewer.py` and `src/panel_tabs.py` use the POSIX terminal APIs `termios`, `tty`, `select` and `SIGWINCH`.
- `src/statusbar.py` uses `fcntl.flock`, which is also POSIX-only.

Linux and macOS are the supported platforms; on Windows, run the workspace inside a WSL2 distribution.

## Bolinhas
○ parado · 🟢 agente/subagente trabalhando · 🟡 uma ferramenta rodando há mais de 8 s ·
🔴 esperando você (permissão/pergunta). Vêm dos hooks do Claude Code: rode uma vez
`workspace install-hooks` (faz backup do `~/.claude/settings.json`; reinicie as sessões abertas).

## Notificações
Quando uma sessão do Claude passa a 🔴 (precisa de você), o hook dispara uma notificação do sistema:
título = nome do repo, corpo = trecho curto (pergunta, comando pedido ou fim da última resposta). Só na
transição para vermelho, então um pedido de permissão não notifica duas vezes. Desligue com
`notifications: false` no `config.yaml` (padrão: ligado). Plataformas: Linux (`notify-send`), macOS
(`osascript`), WSL2 no Windows (toast via `powershell`, nada a instalar). Se o comando não existir, ignora.

## Extra usage na barra de status (opt-in, desligado por padrão)
Shows the account's monthly extra usage spend next to the 5h/7d percentages, e.g. `$12.30/$50`
(`$1,234/$5,000` from 100 up; separators follow the system locale; only `$12.30` if there is no monthly limit; omitted if extra usage is off or there is no data).
Enable with `extra_usage: true` in `config.yaml`. When off, nothing is read and no request is made.

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
- `ctrl+b h` abre a ajuda · `ctrl+b o` abre a tela de opções (uma árvore só, já expandida: os prompts salvos em `prompts` no `config.yaml`, que são digitados no Claude ativo sem enviar, e perguntar ao Haiku via `claude -p --model haiku` no repo da janela; a conferência com os irmãos ou com repos selecionados do workspace, numa tela de conversa com o log ao vivo e caixa de mensagem no rodapé (digite e Enter envia, Ctrl+X encerra); `options` no `config.yaml` acrescenta nós `ask`, `run` e `type`) · `ctrl+b r` recarrega a config e a tira de abas sem sair do tmux · `ctrl+b d` desanexa (o workspace continua rodando).
- barra do painel de repos: REPOS · SESSÕES (Claudes abertos de todos os repos do workspace, agrupados por repo; subagentes em execução viram uma linha `↳` dentro da sessão pai; Enter foca o painel da sessão).
- painel TAREFAS: `→`/`←` expandem/recolhem o item; Enter pede confirmação e manda a mensagem para o Claude (implementar, refinar ou "o que preciso validar?").
- abas do Claude (tira de 1 linha no topo do painel do Claude): a primeira é o Claude principal; `+` (ou `ctrl+b t`) abre uma sessão
  temporária no mesmo repo/worktree; clique numa aba (ou `ctrl+b n`/`p`) para trocar. Ao sair do Claude da aba,
  ela some e o foco volta ao principal. Abas temporárias não são restauradas no `restart`.
- `shift+setas` pulam entre painéis.

## Conferência: irmãos ou repos selecionados
Em `ctrl+b o` → **Conferência**, as folhas **Repos irmãos** e **Repos selecionados** criam a conferência e
abrem a tela de conversa. **Repos irmãos** usa o repo da janela e os irmãos dele: o próprio repo da janela
participa, e aparece primeiro no topo. Irmão é o que o `config.yaml` define: `siblings` declarado numa entrada
de `groups` ou outro repo listado dentro do `parent_folder`; estar no mesmo `groups` não basta. **Repos
selecionados** abre a lista de repos do workspace antes: o repo da janela já vem marcado, Espaço marca ou
desmarca, `a` marca todos, `n` limpa e Enter abre com os marcados (Esc cancela). O log aparece na hora e se
atualiza sozinho quando chegam respostas; o topo mostra o que cada participante está fazendo enquanto trabalha
(analisando, lendo um arquivo, buscando).
Na tela: a caixa **Mensagem:** no rodapé é o campo de texto; digite e Enter envia a mesma mensagem para todos.
Fechar a tela encerra a conferência: Ctrl+X encerra explicitamente e Esc fecha e encerra. Não há persistência: a
próxima conferência começa com o log vazio, e a anterior não volta na tela (o log antigo fica só como arquivo).
Fechar o popup ou a janela também encerra, sem deixar conferência pendente: conferência existe só enquanto a tela
existe. Cada participante é um **Claude isolado** (`claude -p`, um processo por repo, sem sessão salva): a sessão
principal da janela não é usada nem alterada, por isso a conferência não depende de os painéis estarem livres. Cada
um fala **só do próprio repo**, mas pode ler os irmãos — a conferência é uma **validação cruzada entre repos**, e
ler os irmãos é recomendado — e, antes de responder, lê o harness (o `CLAUDE.md`/`.claude/CLAUDE.md` e o que ele
mandar ler). Se a mensagem não for do repo, ele **não responde**: nem linha no log, nem backlog. A conferência é
análise rápida, não trabalho longo: a resposta vem em um parágrafo curto; se exigir análise mais profunda, a
análise vira item no `BACKLOG.md` daquele repo, nunca mudança de código na hora. Nesse modo só o `BACKLOG.md` do
próprio repo pode ser alterado; editar outro arquivo é negado, e Bash, subagentes e ferramentas de rede ficam
desligados. As respostas aparecem no log conforme cada Claude termina (o topo mostra quem ainda está aguardando e
o passo atual). Não há comando de conferência na CLI: a CLI do `workspace` é para instalação e ações
administrativas, nunca para o uso do tmux.

## Comandos
`workspace [perfil]` · `reload [perfil]` (recarrega config e painéis sem sair; é o `ctrl+b r`) · `restart [perfil]` (derruba e sobe de novo; só depois de mudar hooks do Claude ou plugins) · `stop [perfil]` · `install` · `install-hooks`.

## Persistência
Sem plugins de tmux. O Claude abre com `claude --continue` (senão, sessão nova); quando a linha de comando do
agente mudou desde a última abertura daquele repo, ou ainda não há registro, abre sessão nova (o prompt de
migração aparece uma vez só por mudança; ver acima). O resto vem do `config.yaml` e dos arquivos do repo.

## Arquivos
`config/config.example.yaml` (modelo da config) · `config/tmux.conf` · `bin/workspace` (launcher) · `src/panel_*.py` (incl. `panel_tabs.py`, a tira de abas, e `panel_tasks.py`) ·
`src/viewer.py` (desenho sem flicker: só reescreve as linhas que mudaram) · `src/hook.py` · `src/statusbar.py` · `src/backlog.py` (parser do `BACKLOG.md`) ·
`src/help.py` (popup da ajuda) · `src/conference.py` (discussão entre o repo da janela e os irmãos) · `src/conference_worker.py` (o Claude isolado que responde por um repo) · `src/opener.py` · `src/extra_usage.py` · `src/notify.py`.

## Segurança: modelagem de ameaças (pendente)

A modelagem de ameaças deste ambiente será feita em breve e está pendente; até lá, tudo aqui está sujeito a falhas. Acredita-se que o ambiente já seja melhor que usar o Claude diretamente, mas isso não é garantia.

Falha aberta conhecida: hoje a própria LLM gerencia quais repositórios entram na config (lista de repos em `config.yaml`). Isso significa que ela poderia adicionar qualquer pasta do ambiente ao workspace. A lista de repos não é uma barreira contra a LLM.

## Textos (i18n)
Todo texto exibido e todo prompt enviado ao Claude mora em `i18n/` (hoje só `pt-BR`): `i18n/pt-BR.json` (chaves lidas por `src/i18n.py`) e `i18n/HELP.pt-BR.md` (a ajuda do `ctrl+b h`). Idioma novo = arquivos novos nessa pasta.
