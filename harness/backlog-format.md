# Formato do `BACKLOG.md`

O `BACKLOG.md` é a lista do trabalho aberto. Não é documento do produto: fica na raiz do checkout, é
escrito em pt-BR, o git o ignora (`/BACKLOG.md`) e ele não tem par de idioma.

## Arquivo

- Só o que está aberto. O que foi entregue e validado sai da lista.
- No topo, depois do título: `Próximo id: N`.
- Ids são inteiros, incrementais e nunca reusados — nem quando um item é removido ou pausado.
- As seções são, nesta ordem: `## Refinadas`, `## A refinar`, `## A validar`.

## Seções

- `## Refinadas` — item despachável: escopo fechado, caminhos, aceite e validação escritos. Refinada é
  despachável, não concluída.
- `## A refinar` — ideia ainda ambígua; não se despacha até ser refinada.
- `## A validar` — entrega esperando a pessoa. Não existe "pronto": a entrega vai para cá e só sai
  quando a pessoa valida.

## Item

Título: `### N. Título curto`, em pt-BR. O item fica em torno de 15 linhas.

Em `Refinadas` e `A refinar`:

- `Categoria:` o assunto da fatia. `Segurança` (freio em código) e `HARNESS.md` (regra escrita) são
  fixas; as demais seguem o assunto.
- `Caminhos:` os arquivos que a fatia toca. O implementador lê só estes.
- `Depende de:` ids dos quais a fatia depende, ou `—`.
- `Aceite:` o que precisa ser verdade quando a fatia terminar.
- `Validação:` como se verifica o aceite — testes, comandos, o que olhar.

Em `A refinar`, o item pode ainda trazer `Contexto (data)` e `Falta decidir:`.

Em `A validar`, os campos mudam para a pessoa conferir:

- `Categoria:` a mesma do item.
- `Conferir:` o que deve estar de pé depois da entrega.
- `Como:` o passo a passo para ver.
- `Decide:` o que fica com a pessoa — escolhas, ajustes que viram item novo.

## Regras

- Fatia de até 65k de contexto; se não cabe numa sessão, divida.
- `(PAUSADA)` no título tira o item do despacho.
- Não refinar sem pedido; não perguntar antes de salvar tarefa nova.
