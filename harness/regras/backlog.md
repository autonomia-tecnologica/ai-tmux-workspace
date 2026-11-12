# Backlog: mecânica

Detalhe do que o `HARNESS.md` resume em uma linha. O formato e as decisões de rotina estão lá; aqui
fica a mecânica que precisa de contexto.

## Formato

- Título do item: `### N. Título curto`, em pt-BR.
- Em `A refinar`, o item pode trazer `Contexto (data)` e `Falta decidir:`.

## Ciclo

- `A refinar` → `Refinadas` → despacho → `Estado` → conferência e fecho.
- Refinar fecha o escopo; despachar implementa. São passos separados.

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
