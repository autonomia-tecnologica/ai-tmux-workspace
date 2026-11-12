# Harness adicional do projeto

Este é o harness do projeto: os documentos de trabalho que valem para quem mantém este repositório.
Ele não é o `HARNESS.md` do workspace — o arquivo instalado em
`~/.local/share/ai-tmux-workspace/harness/HARNESS.md`, que vence em segurança, isolamento e no que ele
já declara.

**Não confunda com a `Harness/` da Pasta de Trabalho.** Aquela pasta é da pessoa: é a face da função
`harness` do AT, com os documentos dela, e o projeto não a lê nem a edita. A pasta do harness adicional
é outra coisa, e é versionada junto com o projeto.

## Índice

- [`arch-format.md`](arch-format.md) — o piso do `ARCH.md`: camadas, direção de dependência, dono
  único por efeito, dívidas e manutenção.
- [`choke-point.md`](choke-point.md) — dono único por efeito: quem desenha, chama `tmux`, escreve
  arquivo ou formata caminho, a dívida de desenho e o teste de allowlist que só encolhe.
- [`regras/`](regras/) — detalhe do que o `HARNESS.md` resume em uma linha.
  - [`regras/glossario.md`](regras/glossario.md) — termos do ambiente e o motivo das regras; leia
    quando um termo ou o motivo não estiver claro.
  - [`regras/backlog.md`](regras/backlog.md) — ciclo, `Depende de`, bloqueio e fio da meada; leia
    antes de criar, refinar ou despachar item.
  - [`regras/permissoes.md`](regras/permissoes.md) — formas de uso, caso desconhecido e rastro; leia
    antes de liberar comando, mexer na allowlist ou decidir ação fora da caixa.
  - [`regras/despacho.md`](regras/despacho.md) — modo de despacho e worktree; leia antes de despachar
    subagente, abrir worktree ou paralelizar fatias.
  - [`regras/segredos.md`](regras/segredos.md) — varredura, onde um segredo mora e canal seguro; leia
    antes de commit, de criar chave ou de decidir onde um segredo mora.

## Como usar

- Leia só o arquivo que a tarefa pede, e só quando ela pedir. Não leia a pasta inteira.
- O `HARNESS.md` diz qual arquivo de `regras/` ler e quando; o índice acima repete o gatilho.
- Ao criar ou mover um efeito (desenhar, chamar `tmux`, escrever arquivo, formatar caminho), leia
  `choke-point.md` antes da primeira linha.
- O `HARNESS.md` instalado vence este harness em segurança, isolamento e no que ele já declara. O que
  estiver aqui vale como regra deste projeto.
