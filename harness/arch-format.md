# Formato do `ARCH.md`

O `ARCH.md` é o documento de arquitetura do repo: camadas, direção das dependências e dono de cada
efeito. Este arquivo é o piso; o `ARCH.md` de cada repo preenche o conteúdo.

## Arquivo

- Um `ARCH.md` na raiz do repo, versionado junto com o código (não é gitignored).
- Escrito na língua do repo.
- É ele que declara a língua do código: inglês por padrão; outra língua só como exceção declarada,
  com o motivo.
- Onde código e documentação discordarem, o `ARCH.md` diz o que vale. Ou o código se corrige, ou a
  divergência vira dívida em `## Dívidas conhecidas`, com o motivo.
- Enxuto: camadas, direção, donos, dívidas. Não é documento de produto nem histórico.

## Seções mínimas

Os títulos são exatos e nesta ordem:

- `## Camadas`
- `## Direção de dependência`
- `## Dono único por efeito`
- `## Eventos`
- `## Estado vivo no tmux`
- `## UI`
- `## Ciclo de vida de sessão`
- `## Adaptadores de agente`
- `## Config e estado`
- `## Idioma e contratos`
- `## Dívidas conhecidas`
- `## Manutenção`

Seções que o projeto não tem (por exemplo, `## Adaptadores de agente` num repo sem agentes) entram com
uma linha dizendo que não se aplicam, para o teste ver o título.

Os títulos novos são mínimos: `## Eventos` (evento → emissor → transporte → consumidor → dono; sinal só
acorda, estado vem por releitura), `## Estado vivo no tmux` (opções e quem escreve/lê), `## UI` (foco,
cursor e ativo separados; estilo como função do estado), `## Ciclo de vida de sessão`, `## Adaptadores de
agente` (toda diferença de agente passa pelo adaptador) e `## Config e estado` (arquivo → dono do formato
→ escritores).

## Camadas

Uma tabela por camada, com as colunas:

| camada | pasta/arquivos | responsabilidade | proibido |
|---|---|---|---|

- Toda pasta e todo módulo de código aparecem em uma camada.
- "Responsabilidade" diz o que a camada pode produzir; "proibido" diz o que ela não pode.
- A camada de desenho é a única que pinta tela. Quem precisa de desenho pede a ela.

## Direção de dependência

- A direção permitida, de cima para baixo, e a proibida, de baixo para cima.
- Deixe explícito que camada de baixo não importa camada de cima.
- Ciclo entre camadas diferentes é proibido. Ciclo dentro da mesma camada só com import local e o
  motivo declarado.
- `src/` não importa `tests/` nem a pasta do harness.

## Dono único por efeito

Todo efeito tem um dono no código; o resto pede a ele. A pergunta é "quem pode produzir este efeito?",
não "este código é igual?". Liste pelo menos:

- desenho de tela;
- chamada ao `tmux`;
- escrita de arquivo;
- formatação de caminho;
- persistência;
- i18n.

Para cada efeito: dono e como se pede a ele. Tudo de desenho de tela mora só na camada de desenho.
Antes de criar um segundo lugar que produz o efeito, altere o dono; cópia só como exceção declarada
no próprio arquivo, com o motivo.

## Idioma e contratos

- A língua do código e a língua dos textos ao usuário.
- Texto ao usuário passa por i18n; não nasce no código.
- Contratos externos (chaves de config, rótulos de formato) ficam como strings e são nomeados aqui
  quando fogem da regra.

## Dívidas conhecidas

Uma tabela com dívida, motivo e direção da correção:

| dívida | motivo | direção da correção |
|---|---|---|

- Dívida sem motivo e sem direção não é dívida: é código a corrigir.
- Cada linha aponta o freio que ainda falta ou a allowlist que ela ocupa.
- A lista só encolhe; exceção nova exige linha nova e justificativa.

## Manutenção

- Mudou camada, dono ou dívida, o `ARCH.md` muda no mesmo commit do código.
- A migração que fecha uma dívida tira a linha daqui e a linha da allowlist do teste no mesmo commit.
- Freio em teste quando der: o `ARCH.md` ausente, uma seção mínima faltando ou um módulo não citado
  devem reprovar. Sem freio possível, a pendência fica declarada aqui.
