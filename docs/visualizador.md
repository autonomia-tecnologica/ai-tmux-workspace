# Visualizador de arquivos

Um módulo só abre arquivo fora do editor: `src/opener.py`, função `open_file(path)`. O painel de
arquivos (`src/panel_tree.py`) e o comando `workspace view <arquivo>` chamam essa função; nenhum outro
módulo abre arquivo por conta própria. A sessão de origem só entrega o caminho.

## Ordem

1. **Visualizador configurado** para o tipo do arquivo (`viewer:` no `config.yaml`): `html` (HTML, HTM,
   XHTML), `image`, `video`, `audio`.
2. **Programa padrão do sistema** para documentos e arquivos compactados (`open`, `xdg-open`,
   `wslview`/`explorer.exe`, `start`), e Blender para IFC.
3. **Editor**: texto e código (e tudo que as duas etapas acima não abriram). Na árvore, Enter abre o
   editor quando nenhuma etapa abre; a tecla `e` escolhe o editor para qualquer arquivo, inclusive HTML e
   mídia.

## Config

```yaml
viewer:
  html: chromium --app={url} --window-size=1000,760
  image: imv {file}
  video: mpv --force-window=yes --geometry=640x360 --title={name} {file}
  audio: mpv --no-video {file}
```

- Cada valor é uma linha de comando, dividida como shell mas executada sem shell. Marcadores: `{file}`
  (caminho absoluto), `{url}` (`file://`, para navegador), `{name}` (nome do arquivo). Sem marcador, o
  arquivo entra no fim do comando.
- O programa precisa estar no PATH. Se não estiver, ou se o tipo não tem linha, a etapa seguinte roda.
- Nenhum programa é fixo no código: chromium, mpv e imv são só exemplos do `config.example.yaml`.

## Janelas

- HTML: `--app=` do Chromium abre sem abas, sem barra de endereço e sem menus.
- Vídeo: janela pequena do mpv; os controles são os do próprio mpv.
- Imagem e áudio: o visualizador escolhido, sem o programa completo.

## Uso

```
workspace view ~/Documentos/relatorio.html
```

Sai com 0 quando abriu; com 1 e uma mensagem (i18n) quando nada abriu.
