# Segurança

Este documento explica o que o ai-tmux-workspace protege, o que ele não protege hoje e o que é só atrito. O resumo está no [`README.md`](README.md), em "Segurança: resumo". A modelagem completa de ameaças está em [`docs/threat-model.md`](docs/threat-model.md) (leitura humana) e [`docs/threat-model.json`](docs/threat-model.json) (fonte da verdade). Se os dois divergirem, o JSON vale.

## Papéis

- **O Claude executa.** Lê arquivos, escreve, roda comandos e usa ferramentas com as permissões da sua conta de usuário.
- **O ai-tmux-workspace organiza.** Abre um repo por janela do tmux, monta os painéis, as sessões e a config em `~/.config/ai-tmux-workspace` (com `state/` e `conference/`), e registra os hooks do Claude Code. Nenhum papel de agente escreve nesse plano de controle: a política nega a escrita antes da tabela de papéis. O código instalado e o `HARNESS.md` ficam em `~/.local/share/ai-tmux-workspace` (`app/`, `harness/`, `security/`). Ele não é um sandbox.
- **O `ambiente-de-trabalho` define a regra.** É o repositório que guarda a descrição das regras (`HARNESS.md`) e os bloqueios forçados (`forcado.yaml`). O workspace copia essa regra para o harness instalado.

## Harness: forçado e recomendado

- **Forçado (hooks).** Bloqueio em código. O hook do Claude Code chama a política central a cada ferramenta, e a decisão (permitir, pedir confirmação ou negar) não depende do que a LLM decide fazer. Negado por padrão: o que o motor não reconhece é negado. Este é o único tipo de freio que conta como barreira.
- **Recomendado (texto).** O `HARNESS.md` que a LLM lê em cada sessão. É orientação. A LLM pode ignorá-la, seja por erro, por instrução de outra fonte ou por contexto mal interpretado. Não trate uma regra escrita como proteção.
- **Lacuna aberta.** O hook só vale para uma sessão que declare o papel dela (`WS_SESSION_ROLE`). O requisito de que toda sessão tenha papel (`R-POL-02`) ainda está marcado como não implementado no `docs/threat-model.json`. Até fechar, não assuma que toda sessão está coberta.

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
- Comando administrativo pedido à IA: uma janela gráfica do desktop mostra o comando inteiro e só o Sim roda, com `pkexec` (o polkit pede a senha). Sem desktop, nada roda. O risco aceito é você aprovar um comando que fura a caixa.

Isso impede que o workspace se eleve. Não impede que o agente, rodando com a sua conta, faça o que essa conta permite.

## Garantia e atrito

**Garantia (o que o isolamento deveria dar):**

- Um usuário de sistema operacional dedicado para o agente, sem acesso aos seus arquivos pessoais.
- Arquivos de outro dono, fora do alcance do agente.

**Hoje não há essa garantia.** O agente roda com a sua conta, e isso está marcado como não implementado (`R-ISO-01`). Usuário dedicado e arquivos de outro dono são o objetivo do item 35 do backlog, que está pausado e é outra categoria de trabalho.

**Atrito (reduz erro, não é barreira):**

- `deny` de `Bash`. A lista atual bloqueia `sudo` e escrita em `.env`, `*.pem` e `*.key`. Um comando diferente faz o mesmo efeito sem passar por ela.
- Hooks de política sem o usuário dedicado (item 35). O hook enxerga o comando que recebe, mas não o que um interpretador permitido faz depois. Um `python` ou `node` liberado pode ler e escrever o que quiser.
- Caixa do repo. É uma regra de política, não um sandbox: escrever só no próprio repo e em `/tmp` depende de o hook estar ativo. Repos do mesmo grupo no `config.yaml` se leem, mas não se escrevem (item 161).

## Sua configuração é respeitada

- `workspace install-hooks` acrescenta os hooks e o `statusLine` do workspace ao seu `~/.claude/settings.json`. Hooks de terceiros não são alterados.
- Se já existir um `statusLine` de outra origem, ele não é trocado. O workspace avisa.
- Se o `settings.json` não for um JSON válido, nada é gravado.
- Antes de alterar, o arquivo é copiado para `settings.json.bak-workspace`.
- Com `manage: false` no `config.yaml`, nenhum hook é registrado e nenhuma settings é gerada.
- A settings gerada do agente é uma camada a mais via `--settings`. Não substitui hooks nem o `deny` da base, e o `settings.json` da pessoa continua valendo.
- A migração da config (`ctrl+b o`) não altera o `settings.json`.

## Riscos aceitos

- **Só o que exige senha ou root é comando administrativo.** `admin` (pkexec com aprovação) ficou com `sudo`, `doas`, `pkexec`, `su`, `mount`, `iptables`, `useradd` e afins. Estes saíram e rodam com a sua conta, sem pkexec: `systemctl`, `service`, as ferramentas do macOS (`launchctl`, `security`, `diskutil`, ...) e `docker`, `podman`, `nerdctl`, `kubectl`, `helm`. Com `sudo` na frente (`sudo systemctl ...`) o comando continua `admin`.
- **Rede e instalação na sessão da casa.** `net` e `install` são `allow` no papel `home`, como no papel `repo`. O risco aceito: a sessão da casa acessa a rede e instala software sem perguntar. Ela enxerga arquivos de vários projetos, então um envio para fora ou uma instalação passa sem confirmação. `vcs.remote` (push, pull, fetch) e `session.external` continuam `ask`; `secrets` e `admin` (privileged) não mudam.
- **Contêiner e cluster.** O risco aceito: um contêiner com uma pasta do host montada (ou o socket do daemon) pode escrever fora da caixa do repo, porque o motor não enxerga o que roda dentro dele. A pessoa decidiu aceitar esse risco.

## Falha aberta conhecida: a LLM gerencia a lista de repos

A sessão do repo principal (`main` no `config.yaml`) abre com o papel `home`, que pode escrever na config (`config.write: allow` em `security/policy.yaml`). Por isso a LLM pode incluir qualquer pasta do ambiente na lista de repos. A lista de repos não é uma barreira contra a LLM. Isso corresponde a `R-SCOPE-04` e `R-SCOPE-02` no threat model, ambos não implementados.

A modelagem de ameaças ainda não está fechada: há decisões em aberto (`needs_decision`) e requisitos sem implementação. O ponto de partida é `docs/threat-model.md`, com o JSON como fonte da verdade. Até essa modelagem fechar, trate a config como alterável pela LLM.
