# Despacho e worktrees

Detalhe do que o `HARNESS.md` resume. A regra de rotina está lá; aqui fica o modo de execução.

## Modo

- "Faz o 17": uma fatia, fecha e para; é o padrão e o mais barato.
- "Toca tudo": o modo paralelo do `HARNESS.md`; gasta mais token e compra tempo.
- Revisão profunda só quando a pessoa pedir, nunca por fatia.

## Laço

- Gatilho: sem item despachável (`Refinadas` vazia ou tudo bloqueado).
- Volta: refinar o próximo de `A refinar` → despachar → conferir os ids de `Depende de` no arquivo →
  fechar (commit da sessão).
- Para quando `Refinadas` esvaziar, ou quando o próximo depende de decisão grande (aí pergunta).
- `Faz o 17` não entra no laço.
- Paralelo dentro do laço só com `Caminhos` disjuntos e sem `Depende de`.

## Subagente

- Descrição da chamada: `implementador-<id>` (ex.: `implementador-61`), para distinguir os workers.
- Item ambíguo: para e devolve a pergunta.
- Não resolve conflito.

## Worktree

- Worktree novo não traz `.env` nem dependências: o repo tem um passo de preparação.
- Ordem da mescla: uma branch por vez, a mais fundamental primeiro; a `Validação` roda a cada merge.
- A branch também é removida depois de mesclar ou descartar.
