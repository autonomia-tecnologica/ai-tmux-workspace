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
- [`backlog-format.md`](backlog-format.md) — o formato do `BACKLOG.md`.
- [`choke-point.md`](choke-point.md) — dono único por efeito: quem desenha, chama `tmux`, escreve
  arquivo ou formata caminho, a dívida de desenho e o teste de allowlist que só encolhe.

## Como usar

- Leia só o arquivo que a tarefa pede, e só quando ela pedir. Não leia a pasta inteira.
- Ao criar ou mover um efeito (desenhar, chamar `tmux`, escrever arquivo, formatar caminho), leia
  `choke-point.md` antes da primeira linha.
- O `HARNESS.md` instalado vence este harness em segurança, isolamento e no que ele já declara. O que
  estiver aqui vale como regra deste projeto.
- Harness é texto; freio é código, e mecanismo não mora no harness.
