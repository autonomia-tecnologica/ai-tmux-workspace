# AJUDA · ai-tmux-workspace

Navegar: ↑↓ / j k · PgUp PgDn · g início · G fim · mouse. Fechar: **Esc** ou q.
Abrir de novo: `ctrl+b h`.

## Layout

    ┌ status: repo • modelo • consumo ────────────────────────────┐
    │ REPOS       │ SESSÕES (abas)     │ ARQUIVOS DIFF GITHUB     │
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
| `ctrl+b t` | menu de perfis (sessão) / nova aba de terminal, conforme o painel em foco |
| `ctrl+b n` / `p` | próxima / anterior aba do painel em foco |
| `ctrl+b z` | zoom do painel em foco |
| `ctrl+b o` | tela de opções: tudo numa árvore só, já expandida. Prompts salvos (`prompts` no `config.yaml`; Enter ou 1-9 digita na sessão ativa, sem enviar), perguntar ao Haiku (`claude -p`, roda no repo da janela e mostra a resposta) e a conferência com os irmãos ou com repos selecionados do workspace (na de irmãos, o próprio repo participa, primeiro no topo; na de selecionados, escolhe-se antes quais repos do workspace, com o da janela já marcado): cada participante é um Claude isolado (`claude -p`, sem sessão salva; lê o harness e qualquer arquivo do repo e dos irmãos — a conferência é uma validação cruzada — e não altera código: só edita o próprio `BACKLOG.md` quando você pedir na mensagem para salvar algo no backlog), então a sessão principal não é usada; ele fala só do próprio repo e, se a mensagem não for dele ou for dirigida a outro participante, não responde; a análise é rápida e tem orçamento (12 arquivos, 20 passos), e o que exigir análise profunda fica para depois; a tela de conversa mostra o log ao vivo, o passo de cada repo no topo e a caixa **Mensagem:** no rodapé (cresce conforme o texto e mantém o fim visível; digite e Enter envia para todos); Ctrl+X ou Esc encerram a conferência. `options` no `config.yaml` acrescenta mais |
| `ctrl+b r` | recarrega a config sem sair |
| `ctrl+b d` | desanexa (o workspace continua rodando) |

Nos painéis de lista: `j/k` ou setas, PgUp/PgDn, `g/G`, mouse. Enter abre/ativa.

## Barra de status (topo)

`repo • modelo • consumo`

- **repo**: repo da janela atual.
- **modelo**: modelo da última resposta do agente nesse repo.
- **consumo**: `5h X% · 7d Y%` do plano; com `extra_usage` ligado, mostra também o gasto extra do mês (`$usado/$limite`).

## SESSÕES (aba ao lado de REPOS)

Sessões do Claude Code abertas em todos os repos do workspace, agrupadas por repo: título, estado, última fala, mensagens, modelo. Subagentes em execução aparecem como linha `↳` dentro da sessão pai. Enter foca o painel da sessão. Sessões OpenCode não aparecem aqui; a tira de abas mostra todas.

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
- Enter pede confirmação (y/n) e manda a mensagem para a sessão ativa: refinada = implementar, a refinar/pausada = refinar, a validar = "o que preciso validar?". Bloqueada e ciclo não despacham.
- "sem formato" = o `BACKLOG.md` não segue o formato padrão. "nenhum backlog encontrado" = não existe o arquivo.

## SESSÕES e TERMINAL

No painel de sessões:

- A janela abre as abas de sessão persistidas; sem nenhuma, abre uma sessão do perfil padrão (se ela falhar, aparece o aviso para abrir uma sessão pelo `+` na tira de abas).
- `+` (ou `ctrl+b t`) abre o menu de perfis aprovados, com o perfil padrão primeiro; a primeira sessão ocupa o painel principal e as seguintes viram abas. Clique/Enter numa aba (ou `ctrl+b n`/`p`) troca; sair da última sessão abre uma nova aba do perfil padrão, ou volta ao aviso se ela falhar.
- Cada aba guarda o próprio id de sessão: Claude abre com `--session-id` na primeira vez e `--resume` ao restaurar; OpenCode usa uma sessão nova e restaura com `--session`. Id inválido cai para sessão nova. `reload`, `restart` e fechar/abrir o workspace restauram as abas na ordem.
- O catálogo do instalador tem `claude`, `opencode-claude`, `opencode-deepseek`, `opencode-openrouter` e `opencode-free`; só o `claude` usa o Claude Code, os demais sobem pelo OpenCode. O instalador pergunta quais incluir e qual é o padrão entre os marcados (sugere o primeiro) e grava a resposta em `agent`.
- Com `manage: true`, o Claude usa a settings gerada; um perfil `harness: opencode` com `config` usa `OPENCODE_CONFIG`. A credencial dos perfis OpenCode fica no próprio OpenCode (`opencode auth login anthropic`, `opencode auth login openrouter` ou `opencode auth login deepseek`), nunca no `config.yaml`. O `agent` do `config.yaml` é o perfil padrão das novas sessões; mudá-lo não reinicia o workspace.
- Foco na tira (`ctrl+b ↑`): ←→ move, Enter ativa.

No TERMINAL, a tira de abas funciona como antes: `+` (ou `ctrl+b t`, com o foco no painel) abre uma aba temporária no mesmo repo. As abas de sessão são restauradas no `restart`; as de terminal não.

## Lado direito (barra: ARQUIVOS · DIFF · GITHUB)

Clique na aba, ou foque a barra (`ctrl+b ↑`) e use ←→ + Enter.

- **ARQUIVOS**: árvore do repo. Enter abre/fecha pasta ou abre o arquivo no nvim (mídia e documentos abrem no programa padrão do sistema; HTML/HTM/XHTML abrem no navegador padrão). Espaço (ou clique direito) abre o menu; "Copiar arquivo" copia o arquivo selecionado para o clipboard como arquivo: é só colar (Ctrl+V) em chat, e-mail, upload etc., sem abrir o Finder. Segue o `cd` do terminal ativo. Amarelo = modificado, verde = novo.
- **DIFF**: branch, `+/-` totais e arquivos mudados (`U` novo verde, `M` modificado amarelo, `D` apagado vermelho). Enter abre o diff (ou o arquivo novo) num popup; `q` fecha.
- **GITHUB**: via `gh`: repo, PR da branch com checks, PRs, issues, CI, release. Enter abre no navegador.

## Comandos (shell)

`workspace [perfil]` · `restart [perfil]` · `stop [perfil]` · `install` · `install-hooks` · `doctor`

`restart` só é preciso depois de mudar hooks do Claude ou plugins; o resto o `ctrl+b r` resolve.
