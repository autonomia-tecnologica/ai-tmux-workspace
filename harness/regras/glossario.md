# Glossário e racional

Termos usados no `HARNESS.md` e o motivo das regras. Consulte o verbete quando o termo ou o motivo
não estiver claro; não leia a pasta.

> O glossário canônico do ambiente mora no repo irmão `ambiente-de-trabalho`. Esta cópia é o harness
> adicional deste repo: vale como regra daqui, é lida só quando a tarefa pede e não se sobrepõe ao
> `HARNESS.md` em segurança, isolamento e no que ele já declara.

## Racional

- Contextos separados por repositório, sessão e ferramenta: fronteira clara reduz custo (menos token e
  retrabalho), mantém regra e permissão no lugar certo e preserva o foco.
- O freio é código porque a LLM pode ignorar texto, intencional ou não; a garantia vem do mecanismo
  determinístico, não da recomendação.
- O `HARNESS.md` é enxuto porque entra em toda sessão e custa token; o detalhe fica em `regras/` e só
  é lido quando o gatilho pede.
- O desenvolvedor está abaixo do ambiente de trabalho: o ambiente é a base estável que a pessoa usa;
  quem não pede para alterá-lo o respeita. A autonomia é com risco assumido: ler o código é a
  contrapartida; quem não lê assume o risco de propósito.
- A LLM muda coisas rápido; sem limite, uma mudança local se espalha. Choke point (dono único por
  efeito) mantém o efeito no lugar, e o `ARCH.md` dá o mapa das camadas para revisar.
- Falha é aceita: o ambiente não tira a liberdade da pessoa para evitar erro; o erro se corrige com
  rastro, revisão e uma regra melhor.
- Dois vocabulários, um ambiente: o harness de inferência é texto e pode ser ignorado; o harness
  determinístico é código e força. Tratar recomendação como garantia é o erro que o glossário separa.
- SAST/DAST/SCA não bastam para código gerado por IA quando ninguém lê o PR: a ferramenta acha padrão
  conhecido, não confirma o que a mudança faz. No lugar, o ambiente exige harness determinístico,
  choke points e revisão.

## Termos

- **Ambiente de trabalho** — o conjunto que a pessoa usa: repositórios, pastas livres, a home, o
  workspace, o `HARNESS.md` e as features de segurança.
- **Workspace** — a instância do `ai-tmux-workspace` que organiza o ambiente: janelas, painel,
  sessões e o código instalado.
- **Repositório** — pasta de código versionada, aberta numa janela e numa sessão.
- **Repositório irmão** — repo declarado como relacionado (`siblings`); a relação é declarada, nunca
  inferida.
- **Pasta livre** — pasta que não é repositório; entra na navegação, mas não vira repo.
- **Pasta de Trabalho** — a pasta principal da pessoa.
- **Sessão** — instância de agente com papel e raiz, relativa a um repositório ou à home.
- **Home** — a sessão principal, cliente do workspace.
- **Caixa** — área em que a sessão pode agir, imposta pelo SO ou, nesta fase, pela política de
  harness.
- **Harness (de inferência)** — a camada de texto que orienta o agente; não é garantia. A LLM pode
  ignorar, intencional ou não.
- **Harness determinístico** — o freio em código: hook, política, allowlist, `forcado.yaml`, sandbox
  (e usuário de SO quando houver). Faz a regra valer; o adversário é a própria LLM.
- **Freio** — o harness determinístico; o mecanismo em código que faz a regra valer.
- **Choke point** — o dono único por efeito: o único ponto que desenha, chama `tmux`, escreve arquivo
  ou formata caminho; o resto pede a ele. Limita o espalhamento da mudança. Detalhe em
  `choke-point.md`.
- **Harness adicional** — o harness próprio do repo (por convenção, `harness/`), inclusive o harness
  pessoal de repo. É permitido; vale como regra daquele repo, lido só sob demanda, sem se sobrepor ao
  `HARNESS.md` em segurança, isolamento e no que ele já declara.
- **Política** — o motor central de capacidades, com um adaptador para o Claude e outro para o
  OpenCode.
- **Forma de uso / allowlist** — a unidade de permissão; a lista de cada forma está em
  `permissoes.md`.
- **Rastro** — o registro do que foi feito.
- **Conferência** — conversa entre o repo da janela e os irmãos, cada um respondendo pelo próprio
  repo.
- **Fatia** — uma entrega de backlog.
- **Refinada** — item com escopo, caminhos, aceite e validação fechados.
- **Fio da meada** — o que já foi decidido e o que falta, guardados no item.
- **Dono único** — quem pode produzir um efeito no código; o resto pede a ele.
- **Backlog** — o arquivo de tarefas abertas de cada repo.
