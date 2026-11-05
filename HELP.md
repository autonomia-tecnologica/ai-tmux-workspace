# AJUDA · ai-tmux-workspace

Navegar: ↑↓ / j k · PgUp PgDn · g início · G fim · mouse. Fechar: **Esc** ou q.
Abrir de novo: `ctrl+b h`.

## Layout

    ┌ status: repo • modelo • consumo ────────────────────────────┐
    │ REPOS       │ CLAUDE (abas)      │ ARQUIVOS SESSÕES DIFF    │
    │             │                    │ GITHUB (aba escolhida)   │
    ├─────────────┤                    │                          │
    │ TAREFAS     ├────────────────────┤                          │
    │             │ TERMINAL (abas)    │                          │
    └─────────────┴────────────────────┴──────────────────────────┘

- Um repo por janela do tmux. Clicar/Enter num repo da lista troca para a janela dele.
- Foco: o painel ativo tem fundo azul; laranja enquanto `ctrl+b` ou o modo NAVEGAR estiver ativo.

## Teclas

| Tecla | O que faz |
|---|---|
| `ctrl+b h` | esta ajuda |
| `ctrl+b` + setas | move o foco e entra no modo NAVEGAR (setas seguem movendo; Enter/Esc sai) |
| `shift+setas` | pula entre painéis, sem prefixo |
| `ctrl+b t` | nova aba temporária (Claude ou terminal, conforme o painel em foco) |
| `ctrl+b n` / `p` | próxima / anterior aba do painel em foco |
| `ctrl+b z` | zoom do painel em foco |
| `ctrl+b r` | recarrega a config sem sair |
| `ctrl+b d` | desanexa (o workspace continua rodando) |

Nos painéis de lista: `j/k` ou setas, PgUp/PgDn, `g/G`, mouse. Enter abre/ativa.

## Barra de status (topo)

`repo • modelo • consumo`

- **repo**: repo da janela atual.
- **modelo**: modelo da última resposta do Claude nesse repo.
- **consumo**: `5h X% · 7d Y%` do plano; com `extra_usage` ligado, mostra também o gasto extra do mês (`usado/limite`).

## REPOS

Grupos do `settings.json`. Enter no grupo recolhe/expande (← → também).

| Marca | Significado |
|---|---|
| `○` cinza | parado |
| `●` verde | agente (ou subagente) trabalhando |
| `●` amarelo | uma ferramenta rodando há mais de 8 s |
| `●` vermelho | esperando você (permissão ou pergunta) |

Grupo recolhido mostra a pior bolinha de dentro. Nome em azul = repo da janela atual.

Ao lado do nome, dois ícones:

- **branch**: esmaecido = sem git · normal = limpo · amarelo = trabalho não commitado.
- **GitHub**: esmaecido = não está no remoto · normal = em dia · amarelo = ahead/behind do upstream.

## TAREFAS

Itens abertos do `BACKLOG.md` do repo. Contadores no topo; árvore mostra dependências (`←id`).

| Marca | Significado |
|---|---|
| `●` verde | refinada, pronta para despachar |
| `○` cinza | a refinar |
| `◆` amarelo | a validar (só você confere) |
| `◌` apagada | pausada (título com `PAUSADA` entre parênteses) |
| `⊘` | bloqueada por dependência ainda aberta |
| `✖` | ciclo de dependência (inválida até desfazer) |
| `⚠` | id de dependência >= próximo id (provável erro de digitação) |

- `→` expande o item (título completo, dependências, aceite); `←` recolhe.
- Enter pede confirmação (y/n) e manda a mensagem para o Claude: pronta = implementar, a refinar/pausada = refinar, a validar = "o que preciso validar?". Bloqueada e ciclo não despacham.
- "sem formato" = o `BACKLOG.md` não segue o formato padrão. "nenhum backlog encontrado" = não existe o arquivo.

## CLAUDE e TERMINAL

- Tira de abas no topo de cada um: a primeira é a principal; `+` (ou `ctrl+b t`) abre uma aba temporária no mesmo repo.
- Clique numa aba (ou `ctrl+b n`/`p`) para trocar. Sair do Claude da aba fecha a aba e volta à principal.
- Abas temporárias não são restauradas no `restart`.
- Foco na tira (`ctrl+b ↑`): ←→ move, Enter ativa.

## Lado direito (barra: ARQUIVOS · SESSÕES · DIFF · GITHUB)

Clique na aba, ou foque a barra (`ctrl+b ↑`) e use ←→ + Enter.

- **ARQUIVOS**: árvore do repo. Enter abre/fecha pasta ou abre o arquivo no nvim. Segue o `cd` do terminal ativo. Amarelo = modificado, verde = novo.
- **SESSÕES**: sessões salvas do Claude neste repo (título, última fala, mensagens, quando, modelo). Enter retoma.
- **DIFF**: branch, `+/-` totais e arquivos mudados (`U` novo verde, `M` modificado amarelo, `D` apagado vermelho). Enter abre o diff (ou o arquivo novo) num popup; `q` fecha.
- **GITHUB**: via `gh`: repo, PR da branch com checks, PRs, issues, CI, release. Enter abre no navegador.

## Comandos (shell)

`workspace [perfil]` · `restart [perfil]` · `stop [perfil]` · `install` · `install-hooks`

`restart` só é preciso depois de mudar hooks do Claude ou plugins; o resto o `ctrl+b r` resolve.
