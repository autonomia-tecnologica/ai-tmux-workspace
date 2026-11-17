# Segurança

Este documento explica o que o ai-tmux-workspace protege, o que ele não protege hoje e o que é só atrito. O resumo está no [`README.md`](README.md), em "Segurança: resumo".

## Papéis

- **O Claude executa.** Lê arquivos, escreve, roda comandos e usa ferramentas com as permissões da sua conta de usuário.
- **O ai-tmux-workspace organiza.** Abre um repo por janela do tmux, monta os painéis, as sessões e a config em `~/.config/ai-tmux-workspace` (com `state/` e `conference/`), e registra os hooks do Claude Code. Nada impede o agente de escrever nesse plano de controle: não há política de capacidades. O código instalado e o `HARNESS.md` ficam em `~/.local/share/ai-tmux-workspace` (`app/`, `harness/`, `security/`). Ele não é um sandbox.
- **O `ambiente-de-trabalho` define a regra.** É o repositório que guarda a descrição das regras (`HARNESS.md`) e os bloqueios forçados (`forcado.yaml`). O workspace copia essa regra para o harness instalado.

## Harness: forçado e recomendado

- **Forçado (hooks).** Bloqueio em código. Hoje o gate (`security/gate.py`) injeta o `HARNESS.md` no início da sessão e nega `Write`/`Edit`/`NotebookEdit` até o harness ser lido; o `deny` da settings gerada vem do `forcado.yaml`; o hook do workspace (`src/hook.py`) aplica o orçamento da conferência. A política central de capacidades foi removida: o que essas três peças não cobrem não tem freio. Este é o único tipo de freio que conta como barreira.
- **Recomendado (texto).** O `HARNESS.md` que a LLM lê em cada sessão. É orientação. A LLM pode ignorá-la, seja por erro, por instrução de outra fonte ou por contexto mal interpretado. Não trate uma regra escrita como proteção.
- **Lacuna aberta.** Sem política, a sessão não tem caixa: o agente lê e escreve o que a sua conta permitir, e o papel (`WS_SESSION_ROLE`) só identifica a sessão. O gate falha aberto: erro no hook ou `HARNESS.md` ausente não bloqueia.

## Não instale como LLM

Quem instala é você. O `install.sh` mostra o que vai fazer e pede confirmação a cada passo (CLI, autocomplete, config, harness, hooks). Uma LLM que roda o instalador e responde "s" pelo usuário anula essa confirmação, e a instalação deixa de ser uma decisão sua. Não peça para um agente instalar o workspace.

## Killswitch: `remove.sh`

`remove.sh` desfaz o que o `install.sh` mudou, usando o manifesto e os backups que o instalador grava:

- Mostra a lista do que vai restaurar ou apagar e pede confirmação. Nada fora da lista é tocado.
- Só restaura caminhos dentro da sua home e da config. Entrada fora disso é pulada e o manifesto é mantido para você ver.
- Recusa rodar como root ou sob `sudo`/`pkexec`.
- Se alguma entrada falhar, o manifesto fica para nova tentativa.
- A pasta do clone (`~/.local/share/ai-tmux-workspace/app`) não é apagada: fica para reinstalar. Um clone do harness com alterações locais também é mantido.

Limite: `settings.json` é restaurado como arquivo inteiro a partir do backup (`settings.json.bak-workspace`). Alterações que você fez em `~/.claude/settings.json` depois da instalação se perdem nessa restauração.

## O workspace nunca roda privilegiado

- O instalador recusa rodar como root (uid 0) e também quando detecta `sudo`, `pkexec` ou `doas` pelas variáveis de ambiente que eles definem.
- O instalador não instala pacotes e não executa `sudo`/`pkexec`. Quando falta uma dependência, mostra o comando para você rodar.
- Há um caminho para comandos privilegiados (`workspace privileged`): o comando completo é exibido, você confirma que leu, e ele roda numa tela separada do tmux, que você vê e fecha. Fora de um cliente tmux anexado, nada roda.
- Não há mais aprovação de comando administrativo pedido à IA por janela do desktop: esse fluxo foi removido. `sudo` e afins seguem o `deny` e as permissões do Claude Code.

Isso impede que o workspace se eleve. Não impede que o agente, rodando com a sua conta, faça o que essa conta permite.

## Garantia e atrito

**Garantia (o que o isolamento deveria dar):**

- Um usuário de sistema operacional dedicado para o agente, sem acesso aos seus arquivos pessoais.
- Arquivos de outro dono, fora do alcance do agente.

**Hoje não há essa garantia.** O agente roda com a sua conta, e isso está marcado como não implementado (`R-ISO-01`). Usuário dedicado e arquivos de outro dono são o objetivo do item 35 do backlog, que está pausado e é outra categoria de trabalho.

**Atrito (reduz erro, não é barreira):**

- `deny` de `Bash`. A lista atual bloqueia `sudo` e escrita em `.env`, `*.pem` e `*.key`. Um comando diferente faz o mesmo efeito sem passar por ela.
- Gate de leitura do harness. Obriga a ler o `HARNESS.md` antes de escrever, mas não restringe o quê nem onde. Um `python` ou `node` liberado pelas permissões do Claude Code pode ler e escrever o que quiser.
- Sem caixa por repo. A restrição de escrita ao próprio repo e a leitura só entre repos do mesmo grupo foram removidas junto com a política; `groups` só organiza a lista e a conferência.

## Sua configuração é respeitada

- `workspace install-hooks` acrescenta os hooks e o `statusLine` do workspace ao seu `~/.claude/settings.json`. Hooks de terceiros não são alterados.
- Se já existir um `statusLine` de outra origem, ele não é trocado. O workspace avisa.
- Se o `settings.json` não for um JSON válido, nada é gravado.
- Antes de alterar, o arquivo é copiado para `settings.json.bak-workspace`.
- Com `manage: false` no `config.yaml`, nenhum hook é registrado e nenhuma settings é gerada.
- A settings gerada do agente é uma camada a mais via `--settings`. Não substitui hooks nem o `deny` da base, e o `settings.json` da pessoa continua valendo.
- A migração da config (`ctrl+b o`) não altera o `settings.json`.

## Riscos aceitos

- **Sem política de capacidades.** Rede, instalação, contêiner, cluster, comandos administrativos e escrita fora do repo não passam por motor nenhum: valem só as permissões do Claude Code por perfil (`acceptEdits` por padrão, nunca `bypassPermissions` por padrão) e o `deny` do `forcado.yaml`. O risco aceito é uma sessão alcançar qualquer coisa que a sua conta alcance.
- **Segredos.** Nada impede a leitura de arquivos de segredo pelo agente; a regra é só texto no `HARNESS.md`. A chave do provedor fica no cofre (`apiKeyHelper`) e nunca na settings gerada.

## Falha aberta conhecida: a LLM gerencia a config

Nada impede a LLM de editar `~/.config/ai-tmux-workspace/config.yaml`, inclusive a lista de repos e `groups`. A lista de repos não é uma barreira contra ela; trate a config como alterável pela LLM. Não há modelagem de ameaças no repo (`docs/threat-model.*` foi removida com a política).
