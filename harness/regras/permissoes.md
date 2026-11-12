# Permissões: detalhe

Detalhe do que o `HARNESS.md` resume. A decisão de rotina está lá; aqui fica como classificar o caso
novo.

## Formas de uso

- As formas: ler repo, editar repo, ler git, escrever git, rodar teste, rodar build, rede, instalar,
  administrar, configuração e `/tmp`.
- Cada forma tem a própria allowlist; `git` e `python` sozinhos não são permissão.
- Só entra na regra o que dá para centralizar; configuração que só existe numa ferramenta não vira
  regra do ambiente.

## Caso desconhecido

- Comando desconhecido ou ambíguo cai em `ask`.
- Sem pessoa para responder, cai em `deny`.
- `auto` não amplia capacidade.
- Falha do motor com papel declarado: `deny`, não `ask`.

## Rastro

- Toda decisão registra comando, forma de uso, decisão, sessão e pasta.
