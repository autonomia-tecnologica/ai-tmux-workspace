# AJUDA · ai-tmux-workspace

Navegar: ↑↓ / j k · PgUp PgDn · g início · G fim · mouse. Fechar: **Esc** ou q.
Abrir de novo: `ctrl+b h`.

## Layout

    ┌ status: repo • modelo • consumo ────────────────────────────┐
    │ REPOS       │ CLAUDE (abas)      │ ARQUIVOS DIFF GITHUB     │
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
| `ctrl+b o` | tela de opções: tudo numa árvore só, já expandida. Prompts salvos (`prompts` no `config.yaml`; Enter ou 1-9 digita no Claude ativo, sem enviar), perguntar ao Haiku (`claude -p`, roda no repo da janela e mostra a resposta) e a conferência com os irmãos ou com repos selecionados do workspace (na de irmãos, o próprio repo participa, primeiro no topo; na de selecionados, escolhe-se antes quais repos do workspace, com o da janela já marcado): cada participante é um Claude isolado (`claude -p`, sem sessão salva; lê o harness e qualquer arquivo do repo e dos irmãos — a conferência é uma validação cruzada — e não altera código: só edita o próprio `BACKLOG.md` quando você pedir na mensagem para salvar algo no backlog), então a sessão principal não é usada; ele fala só do próprio repo e, se a mensagem não for dele ou for dirigida a outro participante, não responde; a análise é rápida e tem orçamento (12 arquivos, 20 passos), e o que exigir análise profunda fica para depois; a tela de conversa mostra o log ao vivo, o passo de cada repo no topo e a caixa **Mensagem:** no rodapé (cresce conforme o texto e mantém o fim visível; digite e Enter envia para todos); Ctrl+X ou Esc encerram a conferência. `options` no `config.yaml` acrescenta mais |
| `ctrl+b r` | recarrega a config sem sair |
| `ctrl+b d` | desanexa (o workspace continua rodando) |

Nos painéis de lista: `j/k` ou setas, PgUp/PgDn, `g/G`, mouse. Enter abre/ativa.

## Barra de status (topo)

`repo • modelo • consumo`

- **repo**: repo da janela atual.
- **modelo**: modelo da última resposta do Claude nesse repo.
- **consumo**: `5h X% · 7d Y%` do plano; com `extra_usage` ligado, mostra também o gasto extra do mês (`$usado/$limite`).

## SESSÕES (aba ao lado de REPOS)

Claudes abertos de todos os repos do workspace, agrupados por repo: título, estado, última fala, mensagens, modelo. Subagentes em execução aparecem como linha `↳` dentro da sessão pai. Enter foca o painel da sessão.

## REPOS

Grupos do `config.yaml`. Enter no grupo recolhe/expande (← → também).

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

Itens abertos do `BACKLOG.md` do repo. Contadores no topo; os itens se agrupam por categoria (`Categoria:` do item): `→` abre o grupo e `←` fecha (começam fechados), e dentro dele a árvore mostra dependências (`←id`).

| Marca | Significado |
|---|---|
| `●` verde | refinada, despachável (não concluída) |
| `○` cinza | a refinar |
| `◆` amarelo | a validar (só você confere) |
| `◌` apagada | pausada (título com `PAUSADA` entre parênteses) |
| `⊘` | bloqueada por dependência ainda aberta |
| `✖` | ciclo de dependência (inválida até desfazer) |
| `⚠` | id de dependência >= próximo id (provável erro de digitação) ou dependência de outra categoria (inválida) |

- `→` expande o grupo ou o item (título completo, dependências, aceite); `←` recolhe. Enter no grupo não faz nada.
- Enter pede confirmação (y/n) e manda a mensagem para o Claude: refinada = implementar, a refinar/pausada = refinar, a validar = "o que preciso validar?". Bloqueada e ciclo não despacham.
- "sem formato" = o `BACKLOG.md` não segue o formato padrão. "nenhum backlog encontrado" = não existe o arquivo.

## CLAUDE e TERMINAL

- Tira de abas no topo de cada um: a primeira é a principal; `+` (ou `ctrl+b t`) abre uma aba temporária no mesmo repo.
- Clique numa aba (ou `ctrl+b n`/`p`) para trocar. Sair do Claude da aba fecha a aba e volta à principal.
- Abas temporárias não são restauradas no `restart`.
- Foco na tira (`ctrl+b ↑`): ←→ move, Enter ativa.

## Lado direito (barra: ARQUIVOS · DIFF · GITHUB)

Clique na aba, ou foque a barra (`ctrl+b ↑`) e use ←→ + Enter.

- **ARQUIVOS**: árvore do repo. Enter abre/fecha pasta ou abre o arquivo no nvim (mídia e documentos abrem no programa padrão do sistema). Espaço (ou clique direito) abre o menu; "Copiar arquivo" copia o arquivo selecionado para o clipboard como arquivo: é só colar (Ctrl+V) em chat, e-mail, upload etc., sem abrir o Finder. Segue o `cd` do terminal ativo. Amarelo = modificado, verde = novo.
- **DIFF**: branch, `+/-` totais e arquivos mudados (`U` novo verde, `M` modificado amarelo, `D` apagado vermelho). Enter abre o diff (ou o arquivo novo) num popup; `q` fecha.
- **GITHUB**: via `gh`: repo, PR da branch com checks, PRs, issues, CI, release. Enter abre no navegador.

## Comandos (shell)

`workspace [perfil]` · `restart [perfil]` · `stop [perfil]` · `install` · `install-hooks` · `doctor`

`restart` só é preciso depois de mudar hooks do Claude ou plugins; o resto o `ctrl+b r` resolve.
