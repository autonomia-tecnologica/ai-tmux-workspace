# Arquitetura

Camadas, direção das dependências e dono de cada efeito deste repositório. Vence o piso genérico de
[`harness/arch-format.md`](harness/arch-format.md); o `HARNESS.md` instalado vence em segurança e
isolamento.

Onde código e este documento discordarem, vale o que está aqui: ou o código se corrige, ou a
divergência entra em `## Dívidas conhecidas`, com o motivo.

## Camadas

| camada | pasta/arquivos | responsabilidade | proibido |
|---|---|---|---|
| Fundação | `src/tmux.py`, `src/i18n.py`, `src/colors.py`, `src/yaml_min.py`, `i18n/` | único dono do comando `tmux`, do texto por locale, das cores ANSI e do leitor YAML mínimo | importar qualquer camada acima; pintar tela; escrever estado |
| Sessão e permissões | `src/session.py`, `src/session_state.py`, `src/permission_state.py`, `src/policy.py` | caixa e papel da sessão, abas persistidas, último modo de permissão, decisão da política | importar `lib`, `viewer`, `panel_*`, `agents` ou `hook`; chamar `tmux` fora de `tmux.py`; decidir permissão fora de `policy.py` |
| Estado e serviços | `src/lib.py`, `src/worktrees.py`, `src/tree_state.py`, `src/backlog.py`, `src/drift.py`, `src/extra_usage.py`, `src/session_risk.py`, `src/notify.py`, `src/opener.py`, `src/clipboard.py`, `src/statusbar.py`, `src/agents/` | config e estado do workspace, obra de worktree, estado de árvore, leitura do backlog, doctor do harness, uso extra, risco, notificação, abrir arquivo, clipboard, texto da barra de status, adaptadores de agente que normalizam o estado para os painéis | importar `viewer`, `panel_*`, `help`, `options` ou `hook`; pintar tela |
| Desenho e telas | `src/viewer.py`, `src/panel_diff.py`, `src/panel_github.py`, `src/panel_repos.py`, `src/panel_sessions.py`, `src/panel_tabs.py`, `src/panel_tasks.py`, `src/panel_tree.py`, `src/help.py`, `src/options.py`, `src/session_placeholder.py`, `src/conference.py`, `src/conference_budget.py`, `src/conference_worker.py`, `src/mic.py`, `src/stt.py`, `src/glyphs.py`, `src/icons.py` | pintar linhas, telas e popups; painéis só devolvem `collect(viewer)`; símbolos e ícones desenhados; conferência e ditado | rodar o comando `tmux` (só `tmux.py`); escrever arquivo de estado; importar `hook`, `installer` ou `bin/workspace` |
| Entrada e adaptadores | `bin/workspace`, `src/installer.py`, `src/config_cmd.py`, `src/hook.py`, `src/statusline.py`, `src/session_menu.py` | launcher e comandos, instalação/remoção, `workspace config update` (compara e acrescenta config, harness, settings e hooks), hook do Claude, statusLine, menu de sessão | decidir permissão (só encaminha para `policy.py`); pintar tela por conta própria; ser importado por camada abaixo |
| Regra e motor | `harness/` | texto de regra (`HARNESS.md`, `arch-format.md`, `choke-point.md`, `README.md`) e motor instalado (`harness.py`, `opencode-policy.js`, `policy.yaml`, `settings.json`) | importar `src/` ou `tests/`; virar dependência de runtime do produto |
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
- exceção declarada de ciclo entre camadas: `agents` (estado) importa `conference` (desenho) para ler
  as constantes da conferência, e `conference` usa `lib` e `viewer`; o ciclo `lib`/`agents` ↔
  `conference` fica confinado aos imports locais de `lib.profile_adapter` e `conference_worker` e
  não vale como atalho para efeito novo.

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
| chamada ao `tmux` | `src/tmux.py` | `tmux.run(...)` e as funções nomeadas do módulo; nada de `subprocess` com `tmux` |
| geometria de janela e layout | `layout_apply` em `bin/workspace` | `workspace fit`; item 108 |
| escrita de arquivo | dono do formato: `src/lib.py` (config e estado do workspace), `src/session_state.py` (abas), `src/permission_state.py` (modo), `src/hook.py` (ficha de sessão), `src/installer.py` (manifesto e backup), `src/extra_usage.py` (cache de uso), `src/config_cmd.py` (registro `state/config-offered.json`); o `config.yaml` é gravado só por `lib.write_config_text` (com backup) | chame a função do dono; não abra o mesmo arquivo em outro lugar |
| formatação de caminho | `src/lib.py` (`expand_path`, caminho de config), `src/session.py` (normalização da caixa) e `src/agents/base.py` (`short_path`, caminho curto para texto) | chame o dono do caso; não reimplemente `expanduser`/`relpath` para outro fim |
| persistência | o módulo dono do formato, na linha de escrita | idem |
| i18n | `src/i18n.py` + `i18n/pt-BR.json` | `t("chave", ...)`; texto de usuário não nasce no código |

A barra de status do tmux é texto montado por `src/statusbar.py`; quem pinta a barra é o tmux. Menu de
sessão e popups pedidos ao tmux são texto entregue a ele, não desenho de tela em Python.

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

## Manutenção

- Mudou camada, dono ou dívida, este arquivo muda no mesmo commit do código.
- A migração que fecha uma dívida tira a linha daqui e a linha da allowlist do teste no mesmo commit.
- `tests/test_architecture.py` reprova `ARCH.md` ausente, seção mínima faltando e `src/*.py` não
  citado; `src/agents/` entra pela camada, não pelo teste.
- Quando o freio não couber, a dívida é declarada aqui com motivo e vira item de `Segurança`.
