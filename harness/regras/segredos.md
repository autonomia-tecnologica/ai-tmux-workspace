# Segredos e commits: detalhe

Detalhe do que o `HARNESS.md` resume. A decisão de rotina está lá; aqui fica a varredura e o canal.

## Varredura

- Só se commita com a varredura limpa.
- A varredura lê os arquivos como estão (sem respeitar `.gitignore`) e a história do git.
- Achado: diga onde (regra, arquivo, linha), nunca o valor; não abra o arquivo para "conferir".
- Arquivo ignorado não viaja no commit, mas não é lugar para segredo em claro.

## Onde mora

- Estado derivado (perfil de navegador, cache) fica em cache, não versionado.
- Segredo novo entra por canal seguro: popup de entrada ou cofre cifrado.
- A chave entra no processo na hora de subir o agente, vinda de fora.
- Chave que já existe pode ser reescrita pelo mesmo caminho.
