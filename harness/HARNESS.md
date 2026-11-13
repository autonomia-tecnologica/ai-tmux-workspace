# HARNESS.md

Regras operacionais para a LLM, só com tópicos: o texto é enxuto de propósito para gastar menos token
em toda sessão. Descrição aberta em `ambiente-de-trabalho`; bloqueios em `forcado.yaml`. Termo ou
motivo: `harness/regras/glossary.md`, quando não estiver claro; os demais arquivos de `regras/` ficam
no harness do workspace.

## Contexto

- Contexto separado por repositório, sessão e ferramenta.
- Este arquivo vence harness de home ou repo; `CLAUDE.md` e config do OpenCode só apontam para cá.
- A cópia instalada é somente leitura.

## Filosofia

- O desenvolvedor está abaixo do ambiente de trabalho: quem não pede para alterá-lo, respeita-o.
- A autonomia é com risco assumido: leia o código; se não for ler, saiba os riscos que está assumindo.
- A LLM muda rápido: o ambiente limita o espalhamento com choke points (dono único por efeito) e
  `ARCH.md`.
- Falha é aceita; a resposta é corrigir, não tirar a liberdade da pessoa.
- Dois vocabulários: harness de inferência é texto, que a LLM pode ignorar; harness determinístico é
  hook, política, allowlist, `forcado.yaml`, sandbox, e é o que força.
- O glossário canônico mora em `ambiente-de-trabalho`; o glossário daqui é harness adicional, lido
  só sob demanda. Harness pessoal de repo é permitido, nos limites deste arquivo.
- SAST/DAST/SCA não bastam para código gerado por IA quando ninguém lê o PR: o ambiente exige harness
  determinístico, choke points e revisão.

## Harness

- Harness de inferência é texto; harness determinístico é código (allowlist, hook, política, sandbox,
  usuário de SO).
- Guardar só regra estável; não guardar log, medição, diário nem estado da máquina.
- Mecanismo não mora no harness de inferência; `forcado.yaml` declara o bloqueio.
- Regra geral sobe para cá; repo que contrariar diz o motivo.
- `CLAUDE.md` (global e de repo) aponta só para este arquivo, sem regra própria.
- Mudança de harness só fecha quando a instância usa: a sessão entrega à pessoa um comando de uma linha
  que leva o harness do repo à cópia instalada (`workspace config update --apply` hoje; `workspace
  update` quando o item 139 entregar). A sessão nunca escreve nessa cópia.

## Ambiente

- Um repo por janela; pasta livre não recebe agente.
- Sessão de repo escreve só no próprio repo e em `/tmp`.
- Home lê e edita qualquer repo do ambiente; não edita o código do workspace.
- Código do workspace é protegido: só a sessão com root nele escreve.
- Comando interativo ou privilegiado: tela separada da sessão, nunca no painel do agente; privilégio pede ao ambiente (`pkexec`), nunca `sudo`/`pkexec` direto. Fora do tmux, entregar um comando de uma linha pronto para colar; o código de saída volta para a sessão.
- Não matar sessões ou janelas que não criou.

## Modelo

- CLI ativo: Claude Code hoje; OpenCode também usa este `HARNESS.md`.
- Provedor: Anthropic ou DeepSeek.
- Nomes curtos: Sonnet, Opus, Haiku (Anthropic); Flash, Pro (DeepSeek).
- DeepSeek: Sonnet→flash, Opus→v4-pro, Haiku→flash; Flash→flash, Pro→v4-pro.
- Opus e Pro só por pedido explícito do Rafael.

## Conversa

- Autonomia por padrão: a sessão decide e segue.
- Pergunta à pessoa só no caso grande: o que toca `ARCH.md` (regra dura de desenho) ou quebra o
  harness, ou irreversível/externa (push, apagar dado). Fora disso, a sessão segue sozinha.
- Pergunta grande vai como enquete: opções objetivas, recomendada primeiro, uma dúvida por enquete.
- Dúvida menor: escolhe o padrão recomendado, registra `Decidido (pela sessão, por padrão)` no item ou
  no commit e segue; a pessoa veta no PR.
- Comando para a pessoa: um comando, numa linha, pronto para colar. O que só ela edita (home,
  `config.yaml`, repo de que a sessão não é dona) não vira item: a sessão entrega esse comando ou
  patch e segue.
- Decidir pelo conjunto, não por "não foi esta sessão".

## Código

- Código em inglês; texto ao usuário por i18n (pt-BR principal).
- Repo sem `ARCH.md` na raiz está fora da regra; com ele, vale `harness/arch-format.md`.
- Mínimo: camadas separadas e dono único por efeito; desenho de tela só na camada de desenho.
- Nada fixo da demanda no código: entra por argumento ou `config.json`.
- Um script por tarefa, não por caso; estender o que já existe.
- Script reutilizável fica na pasta de scripts do repo, não em temporário.
- Pasta de scripts tem README; atualizar ao criar, renomear ou remover.

## Backlog

- `BACKLOG.md` na raiz, pt-BR, gitignored; só o que está aberto.
- Topo `Próximo id: N`; ids inteiros, incrementais, nunca reusados.
- Seções: `## Refinadas`, `## A refinar`.
- Entrega concluída sai do `BACKLOG.md` na hora de fechar; fica no commit/PR. Validação humana é
  opcional e acontece por PR, nunca por seção do backlog.
- Item: `Categoria`, `Caminhos`, `Depende de`, `Aceite`, `Validação`.
- Categorias fixas: `Segurança` (freio em código), `HARNESS.md` (regra escrita) e `Arquitetura`
  (camadas, dono único, eventos, UI, ciclo de vida e dívida de desenho; freio em `tests/test_architecture.py`).
- Status explícito do item: `ready`, `blocked` ou `done`; só `ready` despacha.
- `blocked: decisão X` quando a decisão é grande; a sessão pergunta e, respondida, volta para `ready`.
- Refinada = despachável, não concluída; item que depende de decisão grande fica em `A refinar`, com a
  pergunta.
- Regra dura de desenho mora em `ARCH.md`; `BACKLOG.md` é controle do agente. Decisão que não conflita
  com `ARCH.md` nem com o harness, a sessão toma sozinha.
- Tarefa que a pessoa pede entra já refinada (`Caminhos`, `Depende de`, `Aceite`, `Validação`); o leve
  se decide sozinho.
- Refinar só com pedido da pessoa ("não refinar sem pedido"); o pedido vale para tudo de `A refinar`:
  refina tudo na mesma rodada, decidindo pelo padrão. `A refinar` termina vazia, salvo item com decisão
  grande (aí pergunta).
- Refinamento com decisão grande: uma enquete, recomendada primeiro: salvar já refinada (e perguntar uma
  por enquete) ou salvar sem refinar em `A refinar`. "Salva sem refinar" vale a qualquer momento, sem
  enquete.
- A pergunta não interrompe o processo em curso: o item é salvo na hora com o que se sabe; o
  refinamento pesado vem depois.
- Decisão da pessoa vai para o `Decidido` do item na hora em que ela responde, antes da próxima pergunta; nunca em lote no fim.
- Item com `Decidido` não é perguntado de novo; pergunta só o que está em `Falta decidir`.
- Trabalho entregue por subagente vai para o `Estado` do item (branch, validação, o que falta) antes de encerrar a sessão.
- Fatia de até 65k de contexto; item em torno de 15 linhas.
- Mecânica (ciclo, `Depende de`, bloqueio, fio da meada): `harness/regras/backlog.md`; leia antes de criar, refinar ou despachar item.

## Agentes

- Refinar: modelo forte, em conversa com a pessoa.
- Todo pedido que altera código ou config (`src/`, `harness/`, `security/`, `bin/`, `tests/`, `config/`) vira
  tarefa de subagente, uma tarefa por subagente; a sessão principal não implementa, só refina,
  despacha, confere e fecha. Backlog, handoff, commit e a conferência entre repos ficam na sessão
  principal.
- Implementar: subagente lê só o item e os `Caminhos`, roda a `Validação`, não commita; fecha com
  `SubagentHandback` — arquivos alterados, validação rodada e resultado; entrega sem handback não conta.
- Fechar e commitar: sessão que despachou.
- Sem item despachável, a sessão principal refina o próximo e despacha: laço refinar → despachar →
  conferir → fechar, até esvaziar `Refinadas`. `Faz o 17` não entra no laço.
- Paralelo só com `Caminhos` disjuntos e sem `Depende de`; worktree e branch por fatia; um integrador.
- Worktree não é sandbox; só remover o que o próprio agente criou.
- Registrar tokens e retrabalho no commit quando disponível.
- Modo e worktree: `harness/regras/dispatch.md`; leia antes de despachar subagente, abrir worktree ou paralelizar fatias.

## Permissões e isolamento

- Política única para Claude e OpenCode; negado por padrão; falha fechada.
- Permissão por forma de uso, nunca por comando solto.
- Caixa: repo no próprio repo e `/tmp`; home nos repos do ambiente; config da máquina read-only.
- Sessão principal em `acceptEdits`; `auto` só dentro da caixa; sem caixa, não.
- `bypassPermissions` só por escolha da pessoa; `deny` vale em todo modo.
- Comunicação entre sessões só com autorização de quem recebe; repo não edita repo irmão: pede por `workspace request send` e quem edita é a sessão do irmão; execução externa do pedido exige aprovação da pessoa.
- Toda ação deixa rastro; pendência de freio em código vai para `Segurança`.
- Caso novo: `harness/regras/permissions.md`; leia antes de liberar comando, mexer na allowlist ou decidir ação fora da caixa.

## Commits

- Última linha da mensagem: `AI Generated`.
- Scan de segredos antes de todo commit; achado diz onde, nunca o valor.
- Custo no corpo do commit, quando disponível.

## Segredos

- Segredo em claro fora da pasta de trabalho: cifrado no repo ou em config do usuário.
- Nunca no chat, em argv, em variável de ambiente ou em arquivo claro.
- Chave nunca nas settings geradas; a settings só aponta para o cofre.
- Varredura e canal: `harness/regras/secrets.md`; leia antes de commit, de criar chave ou de decidir onde um segredo mora.

## Operação

- Ver se a pasta existe antes de criar.
- Conferir espaço antes de download; dado pesado no disco de dados.
- Serviço de usuário; job longo com `systemd-run --user -p MemoryMax=...`.
- Infra versionada por stack; `.env` em `~/.config`; imagem com tag exata de versão.

## Harness adicional

- O projeto pode trazer um harness próprio, numa pasta separada da Pasta de Trabalho — a Pasta de
  Trabalho é da pessoa, e o projeto não a edita. O índice é o `README.md` dessa pasta (por convenção,
  `harness/` na raiz do projeto).
- Leia só o arquivo que a tarefa pede, e só quando ela pedir. O que estiver no harness do projeto vale
  como regra daquele projeto.
- Este `HARNESS.md` vence em segurança, isolamento e no que ele já declara.
