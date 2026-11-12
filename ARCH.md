# Arquitetura

Camadas, direção das dependências e dono de cada efeito deste repositório. Vence o piso genérico de
[`harness/arch-format.md`](harness/arch-format.md).

[`harness/HARNESS.md`](harness/HARNESS.md) é o texto de regra enxuto e canônico: só tópicos, e é ele
que vale para a LLM. A descrição aberta mora no repo irmão `ambiente-de-trabalho`; a cópia instalada é
somente leitura e `CLAUDE.md`/config do OpenCode só apontam para ela. Precedência: este `ARCH.md` vence
em arquitetura; o `HARNESS.md` vence em segurança e isolamento.

Onde código e este documento discordarem, vale o que está aqui: ou o código se corrige, ou a
divergência entra em `## Dívidas conhecidas`, com o motivo.

## Camadas

| camada | pasta/arquivos | responsabilidade | proibido |
|---|---|---|---|
| Fundação | `src/tmux.py`, `src/i18n.py`, `src/colors.py`, `src/yaml_min.py`, `i18n/` | único dono do comando `tmux`, do texto por locale, das cores ANSI e do leitor YAML mínimo | importar qualquer camada acima; pintar tela; escrever estado |
| Sessão e permissões | `src/session.py`, `src/session_state.py`, `src/permission_state.py`, `src/policy.py` | caixa e papel da sessão, abas persistidas, último modo de permissão, decisão da política | importar `lib`, `viewer`, `panel_*`, `agents` ou `hook`; chamar `tmux` fora de `tmux.py`; decidir permissão fora de `policy.py` |
| Estado e serviços | `src/lib.py`, `src/worktrees.py`, `src/tree_state.py`, `src/backlog.py`, `src/drift.py`, `src/extra_usage.py`, `src/session_risk.py`, `src/notify.py`, `src/opener.py`, `src/clipboard.py`, `src/statusbar.py`, `src/agents/` | config e estado do workspace, obra de worktree, estado de árvore, leitura do backlog, doctor do harness, uso extra, risco, notificação, abrir arquivo, clipboard, texto da barra de status, adaptadores de agente que normalizam o estado para os painéis | importar `viewer`, `panel_*`, `help`, `options` ou `hook`; pintar tela |
| Desenho e telas | `src/viewer.py`, `src/context_menu.py`, `src/panel_diff.py`, `src/panel_folders.py`, `src/panel_github.py`, `src/panel_repos.py`, `src/panel_sessions.py`, `src/panel_tabs.py`, `src/panel_tasks.py`, `src/panel_tree.py`, `src/help.py`, `src/options.py`, `src/session_placeholder.py`, `src/conference.py`, `src/conference_budget.py`, `src/conference_worker.py`, `src/mic.py`, `src/stt.py`, `src/glyphs.py`, `src/icons.py` | pintar linhas, telas e popups; painéis só devolvem `collect(viewer)`; menu de contexto declarado por tipo de item; símbolos e ícones desenhados; conferência e ditado | rodar o comando `tmux` (só `tmux.py`); escrever arquivo de estado; importar `hook`, `installer` ou `bin/workspace` |
| Entrada e adaptadores | `bin/workspace`, `src/lifecycle.py`, `src/installer.py`, `src/config_cmd.py`, `src/hook.py`, `src/statusline.py`, `src/session_menu.py` | launcher e comandos, dono do ciclo de vida dos servidores `ws-*` (perfil ↔ servidor, quais estão vivos; `restart`, `stop` e `reload` agem por servidor), instalação/remoção, `workspace config update` (compara e acrescenta config, harness, settings e hooks), hook do Claude, statusLine, menu de sessão | decidir permissão (só encaminha para `policy.py`); pintar tela por conta própria; ser importado por camada abaixo |
| Regra e motor | `harness/` | texto de regra (`HARNESS.md`, `arch-format.md`, `choke-point.md`, `README.md`, `regras/`) e motor instalado (`harness.py`, `opencode-policy.js`, `policy.yaml`, `settings.json`) | importar `src/` ou `tests/`; virar dependência de runtime do produto |
| Freios | `tests/` | provas executáveis: desenho, idioma, política, config, painéis e este arquivo | ser importado por `src/` ou `bin/` |

## Direção de dependência

`A → B` significa "A pode importar B". A direção permitida, de cima para baixo:

`bin/` e adaptadores → desenho e telas → estado e serviços → sessão e permissões → fundação

Permitido:

- cada camada importa a de baixo; a fundação é a única sem dependência de outra camada de `src/`;
- uma camada importa a própria camada (um painel usa `viewer`, `options` usa `help`);
- import local para quebrar ciclo dentro da mesma camada, com o motivo no código: `lib` importa
  `worktrees` dentro da função porque `worktrees` importa `lib`; `i18n` importa `tmux` só em
  `_apply_tmux`, para instalar os textos no tmux.
- exceção declarada, hoje violada (ver `## Dívidas conhecidas`): `agents` (estado) importa `conference`
  (desenho) no nível do módulo, em `agents/claude.py` e `agents/opencode.py`, para ler as constantes da
  conferência. A regra é import local com motivo. Até a dívida fechar, essa exceção não vale como
  atalho para efeito novo. `conference_worker` (desenho) também importa `agents` no nível do módulo.

Proibido:

- camada de baixo importar camada de cima (fundação → sessão → estado → desenho → entrada), salvo a
  exceção declarada acima;
- `src/` importar `harness/` ou `tests/`, e `bin/` importar `tests/`;
- ciclo entre camadas diferentes; ciclo dentro da mesma camada sem import local e motivo;
- qualquer módulo fora de `src/tmux.py` rodar o comando `tmux`;
- qualquer módulo fora de `src/viewer.py` pintar a tela, salvo as dívidas nomeadas abaixo;
- `bin/workspace` ou adaptador decidir permissão: a decisão é de `src/policy.py`.

## Dono único por efeito

Todo efeito tem um dono; o resto pede a ele. A pergunta é "quem pode produzir este efeito?", não
"este código é igual?". Tudo de desenho de tela mora só na camada de desenho.

| efeito | dono | como pedir |
|---|---|---|
| desenho de tela (linhas, clear, alt-screen, cursor, mouse, tecla crua) | `src/viewer.py` | painel devolve `collect(viewer) -> (header, body)`; exceções em `tests/test_architecture.py` |
| menu de contexto (entradas por tipo de item) | `src/context_menu.py` | painel passa `actions=context_menu.actions` e `on_input`; o `viewer` desenha o popup e a caixa |
| chamada ao `tmux` | `src/tmux.py` | `tmux.run(...)` e as funções nomeadas do módulo; nada de `subprocess` com `tmux` |
| geometria de janela e layout | `layout_apply` em `bin/workspace` | `workspace fit`; item 108 |
| escrita de arquivo | dono do formato: `src/lib.py` (config e estado do workspace), `src/session_state.py` (abas), `src/permission_state.py` (modo), `src/hook.py` (ficha de sessão), `src/installer.py` (manifesto e backup), `src/extra_usage.py` (cache de uso), `src/config_cmd.py` (registro `state/config-offered.json`); o `config.yaml` é gravado só por `lib.write_config_text` (com backup) | chame a função do dono; não abra o mesmo arquivo em outro lugar |
| formatação de caminho | `src/lib.py` (`expand_path`, caminho de config), `src/session.py` (normalização da caixa) e `src/agents/base.py` (`short_path`, caminho curto para texto) | chame o dono do caso; não reimplemente `expanduser`/`relpath` para outro fim |
| persistência | o módulo dono do formato, na linha de escrita | idem |
| i18n | `src/i18n.py` + `i18n/pt-BR.json` | `t("chave", ...)`; texto de usuário não nasce no código |

A barra de status do tmux é texto montado por `src/statusbar.py`; quem pinta a barra é o tmux. Menu de
sessão e popups pedidos ao tmux são texto entregue a ele, não desenho de tela em Python.

## Eventos

Um evento é uma mudança que outra parte precisa notar: evento → emissor → transporte → consumidor → dono.
O sinal só acorda. O estado não viaja no sinal: o consumidor relê o estado do dono ao acordar
(`session_state`, opções `@ws_*`, arquivo de aba). Consumidor não guarda cópia que o dono não conhece.

| evento | emissor | transporte | consumidor | dono |
|---|---|---|---|---|
| terminal redimensionado | tmux, pelo kernel | `SIGWINCH` | `Viewer.run` (`_wake`) | `src/viewer.py` |
| estado de abas mudou | `bin/workspace` (`panel_signal`) | `SIGUSR1` a `panel_tabs.py` | `src/panel_tabs.py` relê `session_state` e redesenha | `src/session_state.py` |
| sessão do Claude mudou | hook do Claude (hooks do `settings.json` gerado) | comando do hook | `src/hook.py` | `src/hook.py` (ficha) e `src/session_state.py` (aba) |
| sessão do OpenCode mudou | `harness/opencode-policy.js` | arquivo `tabs/session-<aba>` | `session_state.session_for` | `src/session_state.py` (dívida: três escritores) |
| pedido de conferência | `src/conference.py` | arquivo em `$WS_STATE/requests` | `conference_worker` e tela da conferência | `src/conference.py` (dívida: estado na camada de desenho) |
| barra de status | `bin/workspace`, pelas opções `@ws_*` | `#(...)` em `config/tmux.conf` | tmux pinta a barra | `src/statusbar.py` (texto) |

Provas: `grep -n SIGWINCH src/viewer.py`; `grep -n USR1 bin/workspace`; `grep -n session- src/hook.py`;
`grep -n session- harness/opencode-policy.js`; `grep -n requests src/conference.py`;
`grep -n statusbar config/tmux.conf`.

## Estado vivo no tmux

O workspace guarda parte do estado nas opções do próprio tmux: `@ws_*` por janela e `@role` por painel.
Só `bin/workspace` escreve nelas. Quem lê usa `tmux show`/`tmux display` ou `pane_with_role`. Estado
do workspace não vai para `set-environment -g`.

| opção | escopo | escritor | leitores | sentido |
|---|---|---|---|---|
| `@ws_repo` | janela | `bin/workspace` (`build_window`) | `src/lib.py` (repo da janela), `bin/workspace`, `config/tmux.conf` | identidade da janela: caminho do repo |
| `@ws_tabs`, `@ws_active`, `@ws_next` | janela | `bin/workspace` (`tab_*`, `session_new`, `tab_exit`) | `bin/workspace` (`tab_render`, `state_sync_window`) | abas vivas, na ordem; aba ativa |
| `@ws_profile`, `@ws_session_profile` | janela | `bin/workspace` | `bin/workspace` (`profile_for_window`), `config/tmux.conf` | perfil da janela e perfil da sessão ativa |
| `@ws_mview`, `@ws_mzoom`, `@ws_side`, `@ws_left` | janela | `bin/workspace` (`mview`, `side`, `left`) | `bin/workspace` (`mview_target`) | vista atual e painéis laterais |
| `@role` | painel | `bin/workspace` (`build_tree`, `tab_*`) | `bin/workspace` (`pane_with_role`) | papel do painel, a chave de `REPO_TREE` |
| `@ws_owner`, `@ws_tab_id`, `@ws_tab_profile`, `@ws_placeholder` | painel | `bin/workspace` (`tab_new`, `session_new`) | `bin/workspace` | dono e identidade da aba de sessão |

Cache reconstruível de `session_state`: `$WS_STATE/tabs/<sha1>.json` é cópia da ordem e da ativa de
`@ws_tabs`/`@ws_active`. `state_sync_window` (`bin/workspace`) grava a partir das janelas vivas, e
`session_state.read` lê arquivo ausente como "sem abas". Pode ser refeito das janelas vivas. O id da
sessão de cada aba (`tabs/session-<aba>`) não é refeito assim: volta pelo hook ou pelo plugin na próxima
sessão.

Prova: `grep -c "@ws_" bin/workspace` (opções escritas e lidas no script).

## UI

Foco, cursor e ativo são três estados separados e não se misturam:

- foco: o painel que recebe a tecla (o pane ativo do tmux);
- cursor: a linha escolhida dentro de um painel (estado do painel, desenhado pelo `viewer`);
- ativo: o item que o workspace usa agora (aba ativa em `@ws_active`, repo da janela, perfil da sessão).

Regra-alvo: o estilo é função pura do estado, `estilo = f(estado)`. Dentro da função não há leitura de
`tmux` nem de arquivo. Quem monta o estado é o painel (`collect(viewer)`); quem pinta é o `viewer`.
A conformidade se verifica painel a painel, na migração para `collect(viewer)`.

Menu de contexto: `src/context_menu.py` é a única camada que declara as entradas por tipo de item
(hoje repo; outro tipo entra ali, não no painel). O painel pede `actions=context_menu.actions` e o
`viewer` desenha o popup, navega e devolve a tecla; um submenu é pedido por `viewer.open_menu(items)`.
O menu de repo tem só duas ações: mover para grupo e marcar como irmão de outro repo. As duas gravam
o `config.yaml` pela `lib` (edição textual mínima, backup e comentários preservados) e valem no
próximo redesenho, sem estado no servidor tmux e sem `restart`.

`REPO_TREE` (em `bin/workspace`) é a tabela única de papéis dos painéis: uma linha por painel, com papel,
pai, divisão, tamanho e comando. `build_tree`, `layout_apply` e `plugin_open` leem essa tabela. Papel
novo de painel entra em `REPO_TREE`, não em código solto. Os papéis `tmp N` e `ttmp N` das abas são a
exceção: nascem em tempo de execução, por `tab_new`.

Na janela da home (o repo principal), a tira do painel esquerdo ganha a aba PASTAS: `src/panel_folders.py`
navega as `free_folders` do `config.yaml` e abre arquivo por `src/opener.py` (`open_file`). Pasta livre é
tipo distinto de repo: não abre janela nem sessão de agente, e só existe como modo do painel esquerdo
(`workspace left folders`), sem papel novo em `REPO_TREE`.

Prova: `grep -n REPO_TREE bin/workspace`.

## Ciclo de vida de sessão

Uma sessão é uma aba com um agente dentro de um repo. Papel e caixa vêm de `src/session.py`: o repo
principal é `home`, os demais são `repo`. `WS_SESSION_ROLE`, `WS_SESSION_ROOT` e `WS_SESSION_PROFILE`
levam a caixa até o hook e o plugin, que aplicam a política.

1. Abertura: `session_new` (`bin/workspace`) cria a aba e a registra em `session_state` (`add`);
   `session_run` roda o processo do agente.
2. Id real: o hook (Claude) ou o plugin (OpenCode) grava o id em `tabs/session-<aba>`, e
   `session_state.session_for` lê o valor.
3. Retomada: `session_restore` reabre as abas na ordem, cada uma pelo id gravado; o adaptador
   declara os argumentos (`resume_args`/`fresh_args`/`continue_args`) e de onde vem o id. Sem id
   gravado, o adaptador decide: Claude recebe um id novo (`--session-id`), OpenCode abre uma sessão
   nova declarada (sem `--continue`).
4. Reinício de contexto: `session_state.reset_session` limpa o arquivo vivo e grava o id novo, ou nenhum.
5. Fim: `session_exit` e `tab_exit` tiram a aba do estado (`remove`) e devolvem o foco ao principal.
6. Reinício do servidor: `restart_one` (`bin/workspace`) salva as abas, derruba o servidor com
   `server_down`, regenera o wiring do perfil (`lib prepare-profile` → `Adapter.prepare`) e sobe de
   novo, então o OpenCode volta com o plugin atual.

## Adaptadores de agente

Toda diferença de agente passa por `src/agents/`: `base.py` (contrato `Adapter`, `Update`, `short_path`
e a escolha do adaptador pelo harness), `claude.py` e `opencode.py`. O contrato de ciclo de vida é
`prepare` (arquivos gerados que o servidor lê), `launch` (linha que sobe o agente), `fresh_args`,
`resume_args` e `continue_args` (argumentos de sessão) e `session_id_source` (de onde vem o id: o
workspace escolhe, ou o hook/plugin grava). Fora de `src/agents/`, o código não ramifica por agente
nesse caminho: `lib.session_command` monta a linha com esses métodos, e `bin/workspace restart`
regenera o wiring por `lib prepare-profile`. A geração de settings do Claude ainda ramifica por
harness fora da pasta (dívida abaixo).

## Config e estado

Cada arquivo tem um dono do formato. Os demais pedem a ele.

| arquivo | dono do formato | escritores |
|---|---|---|
| `~/.config/ai-tmux-workspace/config.yaml` (leitura de `settings.json` antigo) | `src/lib.py` (`config_path`) | a pessoa, editando; `lib` lê e grava (edição textual mínima, com backup) |
| `$WS_STATE/tabs/<sha1>.json` (abas por repo) | `src/session_state.py` | `session_state`; `bin/workspace` pede por `state_sync_window` |
| `$WS_STATE/tabs/session-<aba>` (id vivo da aba) | `src/session_state.py` | `hook.py`, `harness/opencode-policy.js` e `session_state` (dívida) |
| `$WS_STATE/permission.json` (último modo de permissão) | `src/permission_state.py` (`path`, `record_mode`) | `permission_state` |
| `$WS_STATE/policy.jsonl` (decisões da política) | `src/policy.py` | `policy` (anexa) |
| `$WS_STATE/sessions/<id>` (ficha de sessão) | `src/hook.py` | `hook` |
| `$WS_STATE/sessions/<id>.status.json` e `$WS_STATE/limits.json` | `src/statusline.py` | `statusline` |
| `$WS_STATE/settings/<perfil>` (settings gerados por perfil) | `src/lib.py` (`ensure-settings`) | `lib`; o instalador lê o caminho |
| `$WS_STATE/settings/opencode-<perfil>.json` (config gerado do OpenCode) | `src/agents/opencode.py` (`prepare`) | o adaptador, pelo `prepare` do `restart`/`reload` |
| `$WS_STATE/settings/opencode-plugin-<perfil>.js` (cópia do plugin da política) | `src/agents/opencode.py` (`prepare`) | o adaptador, pelo `prepare` do `restart`/`reload` |
| `$WS_STATE/requests/` (pedidos de conferência) | `src/conference.py` | `conference` (dívida: camada de desenho) |
| cache de uso extra | `src/extra_usage.py` | `extra_usage` |

Defaults: `WS_STATE` (`~/.cache/workspace`), o caminho da config e `WS_CONFERENCE` são lidos em vários
módulos, cada um com o próprio default (dívida abaixo). O dono do default deveria ser um só.

## Idioma e contratos

- Código em inglês — identificadores, comentários e docstrings; `tests/test_code_language.py` é o freio.
- Texto ao usuário por i18n, em `i18n/pt-BR.json` e `i18n/HELP.pt-BR.md`; pt-BR é o locale principal.
  Outro locale entra como arquivo novo em `i18n/`, sem texto no código.
- Este `ARCH.md` é escrito em pt-BR, como o `BACKLOG.md`.
- Contratos externos continuam strings e não seguem a regra de identificador: as chaves `tipo` e
  `texto` de `harness/policy.yaml` e os rótulos do `BACKLOG.md`.

## Dívidas conhecidas

| dívida | motivo | direção da correção |
|---|---|---|
| `src/panel_tabs.py` pinta a tira de abas (tela cheia + cbreak) no próprio laço | a tira é o molde dos painéis e nasceu antes de `collect(viewer)` | migrar para `collect(viewer)` e tirar a linha da allowlist de `tests/test_architecture.py` |
| `src/session_placeholder.py` desenha sozinho (clear + `SIGWINCH`) | dívida legítima: precisa sobreviver ao reload sem `lib`/`tmux` | manter até o reload deixar de exigir tela própria; a exceção está documentada no docstring e fixada no teste |
| `src/mic.py` mantém laço de ditado em tela cheia e alt-screen fora do `viewer` | o ditado controla o tempo do laço e o estado do microfone | migrar o laço para o `viewer`, restando ao `mic` só a captura e a transcrição |
| `src/conference.py` lê uma tecla em modo cru no popup de pedido | a tela desenha pelo `viewer`; a leitura de tecla ficou no módulo | usar a leitura de tecla do `viewer`, como os demais popups |
| `bin/workspace` chama `tmux` (209 de 1136 linhas) e é dono de abas, sessão e geometria | o script de entrada virou o motor: abas (`tab_*`, `state_*`), sessão (`session_*`) e geometria (`layout_apply`) | abas e sessão vão para `src/` (sessão e estado), e a chamada a `tmux` vai para `src/tmux.py`; `layout_apply` continua dono da geometria (item 108). Prova: `grep -c tmux bin/workspace` (209) e `grep -n "^tab_\|^session_\|^layout_apply" bin/workspace`. Freio que falta: nenhum; hoje só o grep prova |
| painéis chamam `bin/workspace` por subprocesso (`lib.WS`) | o script é a única porta para ação de aba e sessão, e o painel não tem função de `src/` para chamar | ações viram funções de `src/` que o painel e o script chamam; o painel deixa de usar `lib.WS`. Prova: `grep -n "lib.WS" src/panel_*.py` (repos, sessions, tabs e tasks) |
| `conference*` escreve estado na camada de desenho (pedidos, log, orçamento) | a conferência nasceu com o estado junto da tela | a escrita de estado vai para módulo da camada de estado; `conference*` só desenha e pede. Prova: `grep -n write_text src/conference*.py` e `grep -n "open(" src/conference*.py` |
| `lib` carrega `harness/harness.py` por caminho | a renderização do settings do harness mora no motor instalado, então `src/` depende de `harness/` | `lib` chama o motor instalado (`AT_HARNESS_DIR`) por processo, ou a renderização vai para `src/`; `harness/` fica só com regra e motor. Prova: `grep -n spec_from_file_location src/lib.py` e `grep -n HARNESS_PY src/lib.py` |
| `tabs/session-<aba>` tem três escritores (`session_state`, `hook`, `opencode-policy.js`) | o id vivo da aba nasceu em três lugares, cada um com a própria escrita | um escritor de formato em `session_state`; o plugin OpenCode (JS) precisa de ponte documentada. Prova: `grep -n session- src/hook.py src/session_state.py harness/opencode-policy.js` |
| default de `WS_STATE` e da config repetido em vários módulos | cada módulo reimplementa `Path.home() / ".cache" / "workspace"`, porque a fundação não importa `lib` | um default só, em `src/session.py` (camada de sessão, abaixo de `lib`); os módulos importam dele. Prova: `grep -n WS_STATE src/*.py` (lib, permission_state, hook, session_state, statusline, conference) e `grep -n AT_WS_CONFIG src/lib.py` |
| `agents/claude.py` e `agents/opencode.py` importam `conference` no nível do módulo (estado → desenho) | o adaptador lê as constantes da conferência; import de módulo quebra a direção permitida | import local dentro da função que usa a constante, com motivo (como `lib`), ou levar as constantes para a camada de estado. Prova: `grep -n "^import conference" src/agents/claude.py src/agents/opencode.py` (linhas 13 e 15) |
| `conference_worker` importa `agents` no nível do módulo (desenho → estado) | o worker usa o adaptador para rodar o agente, e o texto anterior dizia que o ciclo era só local | import local dentro da função que chama o adaptador, com motivo no código. Prova: `grep -n "^import agents" src/conference_worker.py` (linha 39) |
| `lib` ramifica por harness fora de `src/agents/` (`is_claude_harness`) | a geração de settings do Claude decide em `lib`, fora do adaptador (sessão e launch já pedem ao adaptador) | a geração de settings vai para o adaptador do Claude, e `lib` pede a ele. Prova: `grep -n is_claude_harness src/lib.py` |

## Manutenção

- Mudou camada, dono ou dívida, este arquivo muda no mesmo commit do código.
- A migração que fecha uma dívida tira a linha daqui e a linha da allowlist do teste no mesmo commit.
- `tests/test_architecture.py` reprova `ARCH.md` ausente, seção mínima faltando e `src/*.py` não
  citado; `src/agents/` entra pela camada, não pelo teste.
- Quando o freio não couber, a dívida é declarada aqui com motivo e vira item de `Segurança`.
