# Glossário e racional

Termos usados no `HARNESS.md` e o motivo das regras. Consulte o verbete quando o termo ou o motivo
não estiver claro; não leia a pasta.

## Racional

- Contextos separados por repositório, sessão e ferramenta: fronteira clara reduz custo (menos token e
  retrabalho), mantém regra e permissão no lugar certo e preserva o foco.
- O freio é código porque a LLM pode ignorar texto, intencional ou não; a garantia vem do mecanismo
  determinístico, não da recomendação.
- O `HARNESS.md` é enxuto porque entra em toda sessão e custa token; o detalhe fica em `regras/` e só
  é lido quando o gatilho pede.

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
- **Harness** — a camada de texto que orienta o agente; não é garantia.
- **Freio** — o mecanismo em código que faz a regra valer.
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
