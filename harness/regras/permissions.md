# Permissões: detalhe

Detalhe do que o `HARNESS.md` resume. A decisão de rotina está lá; aqui fica como classificar o caso
novo.

## Formas de uso

- As formas: ler repo, editar repo, ler git, escrever git, rodar teste, rodar build, rede, instalar,
  administrar, configuração e `/tmp`.
- Cada forma tem a própria allowlist; `git` e `python` sozinhos não são permissão.
- Só entra na regra o que dá para centralizar; configuração que só existe numa ferramenta não vira
  regra do ambiente.

## Irmãos

- Repo irmão é o `siblings` declarado no `config.yaml` (num `groups` ou no mapa raiz, simétrico). Não é inferido por grupo nem por pasta.
- Ler arquivo de repo irmão é `repo.read` (allow) nos dois sentidos; escrever nele é `repo.outside` (deny) e a edição segue por `workspace request send`.
- Segredo dentro do irmão continua `secrets` (deny); pasta de controle e código do workspace vêm antes e não mudam.
- Caminho que sai do irmão por symlink ou `..` não passa. Config ausente ou inválida: sem irmãos.

## Caso desconhecido

- Comando desconhecido ou ambíguo cai em `ask`.
- Sem pessoa para responder, cai em `deny`.
- `auto` não amplia capacidade.
- Falha do motor com papel declarado: `deny`, não `ask`.

## Administração

- Comando administrativo (programa `admin`, como `sudo`, `doas`, `su`, `systemctl`, `mount`) tem a decisão `privileged` para todos os papéis; `auto` não pula a pergunta.
- A aprovação é uma janela gráfica do desktop com o comando inteiro; só o Sim roda, com `pkexec` (o polkit pede a senha). Sem desktop ou sem ferramenta de janela, nada roda e o comando volta para colar.

## Rastro

- Toda decisão registra comando, forma de uso, decisão, sessão e pasta.
