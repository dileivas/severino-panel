# severino-panel

O que o Fire TV precisa saber do servidor de mídia — numa API só, com uma UI de TV.

| Peça | Onde | |
|---|---|---|
| API + UI | `server/` — Python 3, stdlib, systemd | porta **9124** |
| App da TV | `android/` — wrapper WebView (base: seerr-tv, MIT) | `com.severino.panel` |

## Por que existe

O Radarr não tem app de TV, o Jellyfin TV não exclui mídia e o Seerr não gerencia biblioteca. Este painel junta os
três num lugar só, com as **chaves no servidor** (a TV nunca as vê).

- **Pedidos** — Seerr + fila do Radarr/Sonarr: estado real e ETA ("baixando 45% · 12 min")
- **Biblioteca** — tamanho por título, ordenado; excluir segurando OK (1,5 s)
- **Legendas** — o que o Bazarr diz estar sem pt-BR, com busca por segurar OK
- **Alerta de disco** — barra verde/âmbar/vermelho (>80%, >90%)

## Servidor

```sh
sudo mkdir -p /opt/severino-panel /etc/severino-panel && sudo chmod 700 /etc/severino-panel
sudo install -m 755 server/panel.py server/index.html /opt/severino-panel/
sudo install -m 600 keys.env /etc/severino-panel/keys.env      # veja server/keys.env.example
sudo install -m 644 server/severino-panel.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now severino-panel
curl -s localhost:9124/overview | jq .
```

`keys.env`: `JELLYFIN_URL/KEY/USER_ID`, `RADARR_URL/KEY`, `SONARR_URL/KEY`, `SEERR_URL/KEY`, `BAZARR_URL/KEY`,
`DISK_PATH`, `SUBTITLE_LANG` (Bazarr chama pt-BR de `pb`) e `PANEL_TOKEN` (exigido em POST/DELETE).

## App da TV

`android/` é o wrapper WebView do [seerr-tv](https://github.com/Vermino/seerr-tv) (MIT) com outro
`applicationId`, nome **Severino** e o `spatnav.js` vazio — a página traz a própria navegação por D-pad,
exposta como `window.__seerrNav` para a ponte do wrapper.

```sh
cd android && ./gradlew assembleRelease     # keystore.properties + keystore.jks (não versionados)
adb install -r app/build/outputs/apk/release/app-release.apk
```

A URL do painel já vem preenchida em `SetupActivity`.

## Lixeira do Radarr

O Radarr manda o arquivo para `recycle/` em vez de apagar: o espaço **só volta ao esvaziar**. O painel mostra o
tamanho e oferece "Segure: esvaziar" (13,8 GB liberados na primeira vez). `RECYCLE_PATH` no `keys.env`.

## Atualização

- A página recarrega a cada **10 s** (progresso/ETA vêm da fila do Radarr/Sonarr).
- **Webhooks**: Radarr e Sonarr → `http://<pi>:9124/hook/radarr|sonarr` (eventos `Grab` e `Download`).
  A página escuta `/events` a cada 5 s e recarrega na hora — "começou a baixar" e "ficou disponível"
  aparecem em até ~5 s, sem esperar o polling.
- O progresso em si não é push (o Radarr não emite evento por %), por isso o polling de 10 s.

## Ações destrutivas

Excluir e buscar legenda exigem **segurar OK por 1,5 s**. O `PANEL_TOKEN` vai embutido na página servida (LAN),
então qualquer um que consiga abrir o painel pode excluir — é uma rede doméstica, não exponha na internet.
