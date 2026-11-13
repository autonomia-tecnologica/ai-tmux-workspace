# Backlog: mecânica

Detalhe do que o `HARNESS.md` resume em uma linha. O formato e as decisões de rotina estão lá; aqui
fica a mecânica que precisa de contexto.

## Formato

- Título do item: `### N. Título curto`, em pt-BR.
- Em `A refinar`, o item pode trazer `Contexto (data)` e `Falta decidir:`.

## Ciclo

- `A refinar` → `Refinadas` → despacho → `Estado` → conferência e fecho.
- Refinar fecha o escopo; despachar implementa. São passos separados.

## Decisão grande ou pequena

- Grande (pergunta à pessoa, em enquete): o que toca `ARCH.md` (regra dura de desenho, que mora lá) ou
  quebra o harness; irreversível ou externo: push, apagar dado, ação em repo alheio.
- `BACKLOG.md` é controle do agente: não é lugar de regra dura. Decisão que não conflita com `ARCH.md`
  nem com o harness é da sessão.
- Pequena: a sessão escolhe o padrão recomendado e registra no item ou no commit:
  `Decidido (pela sessão, por padrão): <decisão>, <motivo curto>`. A pessoa veta no PR.

## Status

- Campo `Status` do item: `ready`, `blocked` ou `done`. Só `ready` despacha.
- `blocked: decisão X` quando depende de decisão grande; a sessão pergunta em enquete. Respondida a
  decisão, o item vai para `ready` (o `Decidido` registra a resposta).
- `done` é transitório: o item sai do `BACKLOG.md` na hora de fechar.

## Refinamento em lote

- Pedido de refinar vale para tudo de `A refinar`, numa rodada só.
- Cada item sai com `Caminhos`, `Depende de`, `Aceite` e `Validação`, decidindo pelo padrão.
- Só o item com decisão grande fica em `A refinar`, com `Falta decidir:` e uma enquete por decisão.

## Só a pessoa edita

- Home, `config.yaml` e repo que a sessão não é dona não viram item de backlog.
- A sessão entrega um comando ou patch pronto para colar, numa linha quando der, e segue o que não
  depende dele.

## Depende de

- Lista de ids separados por vírgula e espaço, ou `—`.
- Bloqueado: algum id listado ainda existe no arquivo. Id ausente conta como cumprido.
- Id maior ou igual ao `Próximo id` é provável erro de digitação.
- Ciclo, inclusive autodependência, é erro de formato: os itens do ciclo ficam bloqueados até alguém
  desfazer.

## Bloqueio

- Item bloqueado não é despachado, mesmo em `Refinadas`.
- Quem despacha confere os ids no arquivo, não só o `Depende de` do item.
- Não se edita o item bloqueado quando a dependência fecha: o id entregue sai do arquivo e ele volta
  a ser despachável.

## Fio da meada

- O item é a memória da fatia: leia antes de perguntar, para não repetir o que já andou.
- O que ficou em aberto fica no item, para a próxima sessão continuar do ponto; o que fechou sai.
- Regra que virou regra mora no harness, não no item.

## Estado

- Vazio até haver entrega.
