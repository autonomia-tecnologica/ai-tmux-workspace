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
- Ambiguidade leve: escolhe o padrão recomendado, segue e registra no handback
  `Decidido (pela sessão, por padrão): <decisão>, <motivo curto>`.
- Devolve pergunta (para, não implementa) só se a decisão toca `ARCH.md` (regra dura de desenho), quebra o
  harness, ou é irreversível ou externa (push, apagar dado, ação em repo alheio).
- Pergunta devolvida: a sessão faz a enquete; o item fica como estava, sem `Decidido`.
- Não resolve conflito.

## Worktree

- Worktree novo não traz `.env` nem dependências: o repo tem um passo de preparação.
- Ordem da mescla: uma branch de fatia por vez, a mais fundamental primeiro; a `Validação` roda a cada merge.
- Integrador (a sessão que despachou): integra direto na `main`, sem branch de integração intermediária. Merge fast-forward; se a `main` andou, rebaseia a fatia antes.
- Ao fechar: remove os worktrees e as branches que a sessão criou, descartadas ou mescladas; não sobra branch nem worktree dela, só a `main`. Não toca em worktree ou branch de outra sessão.
