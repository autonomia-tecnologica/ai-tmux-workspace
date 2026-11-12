# HARNESS.md

Regras operacionais para a LLM, só com tópicos: o texto é enxuto de propósito para gastar menos token
em toda sessão. Descrição aberta em `ambiente-de-trabalho`; bloqueios em `forcado.yaml`.

## Contexto

- Contexto separado por repositório, sessão e ferramenta.
- Este arquivo vence harness de home ou repo; `CLAUDE.md` e config do OpenCode só apontam para cá.
- A cópia instalada é somente leitura.

## Harness

- Harness é texto; freio real é código (allowlist, hook, política, sandbox, usuário de SO).
- Guardar só regra estável; não guardar log, medição, diário nem estado da máquina.
- Mecanismo não mora no harness; `forcado.yaml` declara o bloqueio.
- Regra geral sobe para cá; repo que contrariar diz o motivo.

## Ambiente

- Um repo por janela; pasta livre não recebe agente.
- Sessão de repo escreve só no próprio repo e em `/tmp`.
- Home lê e edita qualquer repo do ambiente; não edita o código do workspace.
- Código do workspace é protegido: só a sessão com root nele escreve.
- Comando privilegiado: pedir ao ambiente (tela separada com `pkexec`); nunca `sudo`/`pkexec` direto.
- Não matar sessões ou janelas que não criou.

## Conversa

- Um item por vez.
- Dúvida vai como enquete: opções objetivas, recomendada primeiro, uma dúvida por pergunta.
- Comando para a pessoa: um comando, numa linha, pronto para colar.
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
- Categorias fixas: `Segurança` (freio em código) e `HARNESS.md` (regra escrita).
- Refinada = despachável, não concluída; item ambíguo fica em `A refinar`.
- Não refinar sem pedido; não perguntar antes de salvar tarefa nova.
- Fatia de até 65k de contexto; item em torno de 15 linhas; `(PAUSADA)` tira do despacho.

## Agentes

- Refinar: modelo forte, em conversa com a pessoa.
- Todo pedido que altera código ou config (`src/`, `harness/`, `bin/`, `tests/`, `config/`) vira
  tarefa de subagente, uma tarefa por subagente; a sessão principal não implementa, só refina,
  despacha, confere e fecha. Backlog, handoff, commit e a conferência entre repos ficam na sessão
  principal.
- Implementar: subagente lê só o item e os `Caminhos`, roda a `Validação`, não commita; fecha com
  `SubagentHandback` — arquivos alterados, validação rodada e resultado; entrega sem handback não conta.
- Fechar e commitar: sessão que despachou.
- Paralelo só com `Caminhos` disjuntos e sem `Depende de`; worktree e branch por fatia; um integrador.
- Worktree não é sandbox; só remover o que o próprio agente criou.
- Registrar tokens e retrabalho no commit quando disponível.

## Permissões e isolamento

- Política única para Claude e OpenCode; negado por padrão; falha fechada.
- Permissão por forma de uso, nunca por comando solto.
- Caixa: repo no próprio repo e `/tmp`; home nos repos do ambiente; config da máquina read-only.
- Sessão principal em `acceptEdits`; `auto` só dentro da caixa; sem caixa, não.
- `bypassPermissions` só por escolha da pessoa; `deny` vale em todo modo.
- Comunicação entre sessões só com autorização de quem recebe; repo não edita repo irmão.
- Toda ação deixa rastro; pendência de freio em código vai para `Segurança`.

## Commits

- Última linha da mensagem: `AI Generated`.
- Scan de segredos antes de todo commit; achado diz onde, nunca o valor.
- Custo no corpo do commit, quando disponível.

## Segredos

- Segredo em claro fora da pasta de trabalho: cifrado no repo ou em config do usuário.
- Nunca no chat, em argv, em variável de ambiente ou em arquivo claro.
- Chave nunca nas settings geradas; a settings só aponta para o cofre.

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
- Este `HARNESS.md` vence em segurança, isolamento e no que ele já declara. Harness é texto; freio é
  código, e mecanismo não mora no harness.
