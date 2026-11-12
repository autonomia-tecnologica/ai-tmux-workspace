# Formato do `BACKLOG.md`

O `BACKLOG.md` é a lista do trabalho aberto. Não é documento do produto: fica na raiz do checkout, é
escrito em pt-BR, o git o ignora (`/BACKLOG.md`) e ele não tem par de idioma.

## Arquivo

- Só o que está aberto. A entrega concluída sai da lista na hora de fechar; ela fica no commit/PR.
- No topo, depois do título: `Próximo id: N`.
- Ids são inteiros, incrementais e nunca reusados — nem quando um item é removido ou pausado.
- Id menor que `Próximo id` e ausente da lista está entregue. Por isso o id nunca é reusado.
- As seções são, nesta ordem: `## Refinadas`, `## A refinar`.

## Seções

- `## Refinadas` — item despachável: escopo fechado, caminhos, aceite e validação escritos. Refinada é
  despachável, não concluída.
- `## A refinar` — ideia ainda ambígua; não se despacha até ser refinada.

## Item

Título: `### N. Título curto`, em pt-BR. O item fica em torno de 15 linhas.

Em `Refinadas` e `A refinar`:

- `Categoria:` o assunto da fatia. `Segurança` (freio em código), `HARNESS.md` (regra escrita) e
  `Arquitetura` (camadas, dono único por efeito, eventos, UI, ciclo de vida e dívida de desenho; freio
  no `tests/test_architecture.py` e no `ARCH.md`) são fixas; as demais seguem o assunto.
- `Caminhos:` os arquivos que a fatia toca. O implementador lê só estes.
- `Depende de:` ids dos quais a fatia depende, ou `—`.
- `Aceite:` o que precisa ser verdade quando a fatia terminar.
- `Validação:` como se verifica o aceite — testes, comandos, o que olhar.
- `Estado:` entrega de subagente registrada pela sessão: branch, validação e o que falta. Vazio até
  haver entrega.

Em `A refinar`, o item pode ainda trazer `Contexto (data)` e `Falta decidir:`.

## Regras

- Fatia de até 65k de contexto; se não cabe numa sessão, divida.
- `(PAUSADA)` no título tira o item do despacho.
- Tarefa que a pessoa pede entra já refinada: `Caminhos`, `Depende de`, `Aceite` e `Validação` escritos;
  o que for leve a sessão decide sozinha.
- Refinamento que exige decisão pesada: perguntar uma vez, em enquete, entre salvar já refinada (e então
  fazer as perguntas, uma por vez) ou salvar sem refinar em `A refinar`. "Salva sem refinar" a qualquer
  momento vale sem enquete.
- A pergunta nunca interrompe o processo em curso: o item é salvo na hora com o que se sabe, e o
  refinamento pesado vem depois.
- A decisão da pessoa entra no `Decidido` na hora em que ela responde (ver `HARNESS.md`).
