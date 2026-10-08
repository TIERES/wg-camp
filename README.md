# Arena17 Downloads

Aplicação leve para publicar campeonatos de Winning Eleven 2002, suas ligas e arquivos.

O Flask guarda metadados e administra uploads. O Nginx entrega os arquivos de
`/var/www/arena17/downloads` diretamente, sem transferi-los pelo processo Python.

Localmente, na ausência de variáveis de ambiente, os arquivos são mantidos em
`storage/`. Em produção, o serviço systemd define os diretórios em `/var/www/arena17/`.

## Desenvolvimento local

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:ARENA17_SECRET_KEY = "troque-por-um-segredo-longo"
flask --app wsgi init-db
flask --app wsgi create-admin admin
flask --app wsgi run --debug
```

Para criar administradores posteriormente, execute `python create_admin.py` no
diretório da aplicação. O script solicita o usuário e a senha duas vezes.

Em produção, configure as variáveis no arquivo de ambiente do systemd. Não use
o servidor de desenvolvimento Flask.

## Variáveis de ambiente

- `ARENA17_SECRET_KEY`: obrigatório em produção.
- `ARENA17_INSTANCE_PATH`: diretório do SQLite e da chave, padrão `instance/`.
- `ARENA17_DOWNLOADS_DIR`: diretório final dos downloads.
- `ARENA17_UPLOAD_TMP_DIR`: diretório temporário, fora do diretório público.
- `ARENA17_MAX_UPLOAD_BYTES`: máximo por upload, padrão 8 GiB.
- `ARENA17_SERVE_DOWNLOADS_LOCALLY`: use `true` somente em testes locais sem Nginx.
- `ARENA17_LIVE_DIR`: diretório dos arquivos `.krec` de partidas ao vivo/recebidas via `/spectate/ingest`, padrão `storage/live/`.
- `ARENA17_SPECTATE_KEY`: chave compartilhada exigida (header `X-Api-Key`) pelo endpoint `/spectate/ingest`. Obrigatório em produção, já que o endpoint fica acessível publicamente pelo domínio; sem ela, o endpoint aceita qualquer chamada.

## Live spectate ingest

`POST /spectate/ingest` recebe, em lotes periódicos, a mesma gravação que o
`kailleraclient.dll` (projeto `kaillera-client`) grava localmente em `.krec`
durante uma partida. Ver `common/n02_stream.h` naquele repositório para o
contrato completo (headers `X-Session-Id`/`X-Sequence`/`X-Session-End`/
`X-Owner-Name`, corpo = registros no formato `.krec`). Os arquivos ficam em
`ARENA17_LIVE_DIR`, como `<session_id>.krec.part` enquanto a partida está em
andamento e `<session_id>.krec` quando termina; metadados (jogo, dono,
jogadores, status) ficam na tabela `live_sessions`.

Do lado da leitura (para o recurso "Watch Live" no cliente), o `game_name`
gravado é o nome da sala do lobby Kaillera (não o nome da ROM), então:

- `GET /spectate/lookup?room=<nome da sala>&owner=<dono>` devolve JSON com o
  `session_id`/`status` mais recente para essa sala (preferindo uma partida
  ainda `live` a uma já `finished`), 404 se nada for encontrado. `owner` é
  opcional mas recomendado: como nomes de sala não são únicos (dois hosts
  podem nomear a sala igual, ex. "We2002.bin"), ele desambigua usando a
  coluna "owner" que a lista do lobby já mostra ao lado do nome da sala -
  um usuário só hospeda uma sala por vez, então o par (room, owner) é
  efetivamente único.
- `GET /spectate/stream/<session_id>?offset=<bytes>` devolve, em
  `application/octet-stream`, a fatia da gravação a partir de `offset` (até
  `MAX_STREAM_CHUNK_BYTES` por chamada), com os headers `X-Status`
  (`live`/`finished`) e `X-Next-Offset` para a próxima chamada. Concatenar os
  corpos em ordem de offset reproduz o mesmo stream de `/spectate/ingest`.
  Isso permite ao espectador dar fast-forward desde o frame 0 até alcançar o
  "live edge" da partida.

Ambos exigem `X-Api-Key` quando `ARENA17_SPECTATE_KEY` está configurada, igual
ao `/spectate/ingest`.

Os diretórios de upload precisam pertencer ao usuário da aplicação. O diretório
do banco não deve ser exposto pelo Nginx.

Em produção, o botão de download chama o Flask somente para autorizar o arquivo.
Ele responde com `X-Accel-Redirect`, e o Nginx entrega o arquivo internamente;
assim, links de arquivos não ficam expostos como diretórios públicos.

## Replays

`GET /replays` lista, publicamente, as partidas com status `finished` e pelo
menos 5 minutos de duração (`duration_seconds >= 300`, calculado a partir de
`started_at`/`ended_at` quando o `/spectate/ingest` recebe `X-Session-End`).
Mostra jogadores, horário, ISO (`game_name`) e duração. `GET
/replays/<session_id>/download` entrega o `.krec` correspondente, usando o
mesmo mecanismo de `ARENA17_SERVE_DOWNLOADS_LOCALLY`/`X-Accel-Redirect` dos
downloads de campeonato (ver `deploy/nginx-arena17.conf` para o caso de uso
com Nginx; com `ARENA17_SERVE_DOWNLOADS_LOCALLY=true`, como neste deploy com
Caddy, o Flask serve o arquivo diretamente e nenhuma configuração de proxy
adicional é necessária). Depois de atualizar o banco de um deploy existente,
rode `flask --app wsgi migrate-db` para criar a coluna `duration_seconds` e
preencher a duração das sessões já finalizadas.

`GET /replays/list.txt?limit=20` devolve a mesma lista em texto simples,
uma linha por replay (`session_id\thorário\tISO\tjogadores\tduration_seconds\tnome_de_download\tbytes_received`,
sem cabeçalho), para o kaillera-client consumir na tela "Replays Online" do
Playback sem precisar de um parser de JSON em C. `limit` é opcional (padrão
20, máximo 50). Como o cliente fala HTTP puro (sem TLS), `/replays/list.txt`
e `/replays/<id>/download` também precisam estar liberados no listener
`http://:8080` do Caddy (ver `deploy/Caddyfile`), do mesmo jeito que
`/spectate/*`.

## Contas de jogadores e Memory Card online

Jogadores se cadastram em `/conta/cadastro` (usuário = nick do Kaillera, que
**não pode ser alterado** depois, e-mail e senha). O cadastro só é ativado pelo
link de confirmação enviado por e-mail (válido por 48 h; cadastros nunca
confirmados liberam o nome/e-mail depois disso). Pelo mesmo e-mail o jogador
recupera o usuário e redefine a senha (`/conta/recuperar`, link válido por 1 h).
Configure o SMTP com as variáveis `ARENA17_SMTP_*`, `ARENA17_MAIL_FROM` e
`ARENA17_PUBLIC_URL` (ver `deploy/arena17-downloads.env.example`).

Em `/conta/` o jogador vê seus Memory Cards (um por jogo), envia um `.mcd`/
`.mcr`/`.srm` de 128 KB, baixa qualquer versão e restaura versões antigas.
Os jogos são identificados por `CRC32:TAMANHO` do conteúdo, o mesmo valor que
o anti-desync do retroarch-k3-ffw calcula; o admin cadastra/renomeia em
`/admin/jogos` (ou `flask --app wsgi register-game caminho.cue "Nome"`), e um
jogo novo também é cadastrado sozinho na primeira partida.

API do `kailleraclient.dll` (`/api/mc/*`, respostas `chave=valor` em texto):
**somente pelo site HTTPS** - leva senha e token, então não é liberada no
listener `http://:8080` do Caddy.

- `POST /api/mc/login` (`username`, `password`) → `token`. Só contas confirmadas.
  Trocar a senha desconecta todos os tokens.
- `GET /api/mc/whoami`, `POST /api/mc/logout` (header `Authorization: Bearer`).
- `POST /api/mc/checkout` (`content_id`, `game_name`, `players` = nicks na
  ordem 1P,2P,... separados por vírgula) → dono, versão e `sha256` dos cartões
  dos slots 1 e 2 (cria um cartão formatado para quem não tem). Todos os
  donos precisam ter conta confirmada (`409 missing_account`).
- `POST /api/mc/ticket` (`room` = id da sala no Kaillera) → `ticket`: ingresso
  curto, de uso único, válido por 5 min. Numa sala "Só logados WE Camp", o DLL
  de quem entra o manda no chat da sala; `POST /api/mc/verify-ticket`
  (`ticket`, `room`, token do host) → `username` da conta, e o DLL do host
  expulsa quem não tem ingresso válido ou cujo nick não é o da conta.
- `GET /api/mc/card/<sha256>` → conteúdo (token do jogador ou `X-Api-Key` do
  DLL, para replays/Ao Vivo).
- `POST /api/mc/commit?content_id=&slot_player=&base_sha256=&players=` (corpo =
  128 KB) → todos os PCs terminam com os mesmos cartões, então o primeiro
  envio válido grava a nova versão (`committed`, anotando quem enviou); o DLL
  do host envia na hora e os outros só alguns segundos depois, caso o host
  tenha caído. Um envio com o mesmo conteúdo já gravado devolve `already`;
  um envio diferente, ou de uma base que não é mais a atual, devolve
  `conflict` (409) sem sobrescrever.
