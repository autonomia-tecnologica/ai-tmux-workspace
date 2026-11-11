# Dono único por efeito (choke point)

Regra deste projeto para quem cria ou move código. Vale junto com o `HARNESS.md` instalado, que
vence em segurança, isolamento e no que ele já declara.

## Regra

- Todo efeito (desenhar na tela, chamar `tmux`, escrever arquivo, formatar caminho) tem um dono no
  código; o resto pede a ele. A pergunta não é "este código é igual?", é "quem pode produzir este
  efeito?".
- Antes de implementar, rode o grep do efeito e diga qual é o dono. Se já existe, altere o dono, não
  crie um segundo. O grep vai colado na entrega e no retrabalho.
- Se importar o dono custa caro, o dono está no lugar errado: conserte o dono, não copie. Cópia só
  vale como exceção declarada no próprio arquivo, com o motivo.
- Quem coleta não pinta: o painel devolve dados (`collect(viewer)`) e o dono desenha.

## Por que não é DRY

DRY fala de forma do código ("não copie linhas"). A LLM sempre consegue argumentar que o caso dela é
"parecido, não igual": é uma regra não falsificável. "Quem pode produzir este efeito?" se confere por
grep e vira freio em código.

## Desenho no terminal

O dono é `src/viewer.py`. A tabela efeito→dono está no docstring dele. Os painéis devolvem
`collect(viewer) -> (header, body)` e não pintam.

Dívida atual, nomeada em `tests/test_architecture.py` com o motivo de cada exceção:

| módulo | motivo |
|---|---|
| `src/viewer.py` | dono: único que pinta linhas, usa alt-screen e redesenha em `SIGWINCH` |
| `src/panel_tabs.py` | dívida: pinta a tira de abas (tela cheia + cbreak) no próprio laço |
| `src/session_placeholder.py` | dívida legítima: desenha sozinho para sobreviver ao reload sem `lib`/`tmux` |
| `src/mic.py` | dívida: laço de ditado em tela cheia e alt-screen fora do `viewer` |
| `src/conference.py` | dívida: lê uma tecla em modo cru no popup de pedido; a tela em si desenha pelo `viewer` |

`sys.stdout.write` fica fora da guarda: `src/policy.py` (JSON do hook) e `src/session_risk.py`
(prompt) escrevem em stdout sem desenhar tela. `import termios`/`tty` dentro de função é leitura de
uma tecla, não dono do terminal; a guarda olha import no nível do módulo.

## O freio

`tests/test_architecture.py` varre `src/*.py` e reprova tela cheia (`\x1b[2J`), cursor home
(`\x1b[H`), alt-screen (`?1049h`), import de `termios`/`tty` no nível do módulo e handler de
`SIGWINCH` que desenha, fora da allowlist. A allowlist é a dívida de cima; `EXPECTED_ALLOWED` fixa o
conjunto, então exceção nova exige edição explícita e a lista só encolhe. Cada migração de painel
para `collect(viewer)` tira uma linha da allowlist.

Para o resto dos efeitos, o mesmo princípio: antes de criar um segundo lugar que chama `tmux` para
geometria, por exemplo, o dono é `layout_apply` em `bin/workspace` (item 108).
