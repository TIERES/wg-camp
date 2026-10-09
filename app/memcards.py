"""Memory Card online (PCSX ReARMed / PlayStation).

Cada jogador cadastrado tem, por jogo, um cartão com histórico de versões.
Numa partida, o slot 1 do console recebe o cartão do 1P e o slot 2 o do 2P
- em todos os PCs, igual a um PS1 de verdade onde cada amigo traz o seu.

API para o kailleraclient.dll (só no site HTTPS - leva senha e token):
respostas em texto "chave=valor" por linha, como /replays/list.txt, para o
cliente em C não precisar de parser de JSON. Ver API_* abaixo.
"""
import hashlib
import os
import re
import zlib
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, request, send_file
from werkzeug.security import check_password_hash

from .accounts import find_token, issue_token, token_hash
from .db import get_db, now

MCD_SIZE = 128 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTENT_ID_RE = re.compile(r"^([0-9A-Fa-f]{8}):([0-9A-Fa-f]{1,16})$")

api_bp = Blueprint("memcards_api", __name__, url_prefix="/api/mc")


# --------------------------------------------------------------------------
# Cartões
# --------------------------------------------------------------------------

def _frame(first_bytes):
    frame = bytearray(128)
    frame[: len(first_bytes)] = first_bytes
    checksum = 0
    for byte in frame[:127]:
        checksum ^= byte
    frame[127] = checksum
    return bytes(frame)


def blank_card():
    """Cartão de PS1 formatado e vazio - o mesmo que o PCSX ReARMed cria
    (CreateMcd): cabeçalho "MC", 15 entradas de diretório livres e 20
    entradas da lista de setores defeituosos vazias; o resto zerado."""
    data = bytearray(MCD_SIZE)
    data[0:128] = _frame(b"MC")
    for i in range(15):
        data[128 * (1 + i): 128 * (2 + i)] = _frame(bytes([0xA0]) + bytes(7) + b"\xff\xff")
    for i in range(20):
        data[128 * (16 + i): 128 * (17 + i)] = _frame(b"\xff\xff\xff\xff" + bytes(4) + b"\xff\xff")
    return bytes(data)


def card_error(data):
    """Mensagem de erro, ou None se `data` é um cartão de PS1 cru (.mcd/.mcr/.srm)."""
    if len(data) != MCD_SIZE:
        return ("O arquivo precisa ser um Memory Card de PlayStation com exatamente 128 KB "
                "(.mcd, .mcr ou .srm do PCSX ReARMed).")
    if data[0:2] != b"MC":
        return "O arquivo não parece um Memory Card de PlayStation (cabeçalho \"MC\" ausente)."
    return None


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


def card_path(sha):
    return Path(current_app.config["MEMCARDS_DIR"]) / f"{sha}.mcd"


def store_card(data):
    """Grava o conteúdo (se ainda não existe) e devolve o SHA-256."""
    sha = sha256_of(data)
    path = card_path(sha)
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return sha


def normalize_content_id(value):
    match = CONTENT_ID_RE.match((value or "").strip())
    if not match:
        return None
    return f"{match.group(1).upper()}:{int(match.group(2), 16):X}"


def content_id_from(crc32, size):
    return f"{crc32 & 0xFFFFFFFF:08X}:{size:X}"


def content_id_for_path(path):
    """"CRC32:TAMANHO" de um arquivo de jogo, como o anti-desync do
    retroarch-k3-ffw calcula (kaillera_sync.c, ksync_hash_content): .cue e
    .m3u são resolvidos para os arquivos que referenciam, em ordem."""
    crc, size = 0, 0

    def add_file(file_path, depth=0):
        nonlocal crc, size
        ext = file_path.suffix.lower()
        if ext in (".cue", ".m3u") and depth < 2:
            for line in file_path.read_text(errors="replace").splitlines():
                line = line.strip()
                name = None
                if ext == ".cue":
                    if line[:4].upper() == "FILE" and line[4:5] in (" ", "\t"):
                        rest = line[4:].strip()
                        if rest.startswith('"'):
                            end = rest.find('"', 1)
                            name = rest[1:end] if end > 0 else None
                        else:
                            name = rest.split()[0] if rest else None
                elif line and not line.startswith("#"):
                    name = line
                if name:
                    add_file(file_path.parent / name, depth + 1)
            return
        with open(file_path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                crc = zlib.crc32(chunk, crc)
                size += len(chunk)

    add_file(Path(path))
    return content_id_from(crc, size)


# ISOs dos campeonatos (tabela files): o "Enviar um Memory Card" da conta
# lista as publicadas. files.content_id é o mesmo CRC32:TAMANHO de games -
# calculado no envio do arquivo (admin.file_upload) ou depois por
# fill_championship_content_ids(). Só imagens de disco cruas: o RetroArch lê
# o .bin/.chd inteiro; um .zip/.7z teria de ser descompactado antes.
GAME_FILE_EXTENSIONS = {"bin", "iso", "img", "chd"}


def is_game_file(filename):
    return filename.rsplit(".", 1)[-1].lower() in GAME_FILE_EXTENSIONS


def fill_championship_content_ids(db):
    """Identifica as ISOs de campeonato ainda sem content_id: pela cópia
    deste servidor ou, nas migradas, pelo crc32 que o archive.org lista.
    Devolve (quantas identificou, nomes das que não deu)."""
    from urllib.parse import unquote, urlparse

    from .archive_org import ArchiveOrgError, list_item_files

    rows = db.execute("SELECT * FROM files WHERE file_type = 'iso' AND content_id IS NULL ORDER BY id").fetchall()
    listings = {}
    found, missing = 0, []
    for entry in rows:
        content_id = None
        local = Path(current_app.config["DOWNLOADS_DIR"]) / entry["stored_name"]
        if is_game_file(entry["stored_name"]):
            if not entry["local_removed_at"] and local.is_file():
                content_id = content_id_for_path(local)
            elif entry["archive_url"]:
                # https://archive.org/download/<item>/<arquivo> (archive_org.download_url)
                parts = urlparse(entry["archive_url"]).path.split("/", 3)
                if len(parts) == 4 and parts[1] == "download":
                    identifier, name = unquote(parts[2]), unquote(parts[3])
                    if identifier not in listings:
                        try:
                            listings[identifier] = list_item_files(identifier)
                        except ArchiveOrgError:
                            listings[identifier] = {}
                    info = listings[identifier].get(name)
                    if info and info.get("crc32") and info.get("size"):
                        content_id = content_id_from(int(info["crc32"], 16), info["size"])
        if content_id:
            db.execute("UPDATE files SET content_id = ? WHERE id = ?", (content_id, entry["id"]))
            db.commit()
            found += 1
        else:
            missing.append(entry["display_name"])
    return found, missing


def championship_isos(db):
    """ISOs identificadas dos campeonatos publicados, uma por conteúdo (dois
    campeonatos com a mesma ISO dividem o cartão), dos campeonatos mais novos
    para os mais antigos: [{"content_id", "name", "label"}]."""
    rows = db.execute(
        "SELECT f.content_id, f.original_filename, c.name AS championship_name "
        "FROM files f JOIN championships c ON c.id = f.championship_id "
        "WHERE f.file_type = 'iso' AND f.is_published = 1 AND c.is_published = 1 AND f.content_id IS NOT NULL "
        "ORDER BY c.start_date DESC, c.created_at DESC, f.id DESC"
    ).fetchall()
    isos = {}
    for row in rows:
        iso = isos.setdefault(row["content_id"].upper(), {
            "content_id": row["content_id"].upper(), "name": row["original_filename"], "championships": [],
        })
        if row["championship_name"] not in iso["championships"]:
            iso["championships"].append(row["championship_name"])
    for iso in isos.values():
        iso["label"] = f"{' / '.join(iso['championships'])} — {iso['name']}"
    return list(isos.values())


# Extensões tiradas do nome do jogo para chegar ao nome do .srm: o RetroArch
# salva o cartão como saves\<núcleo>\<nome do conteúdo sem extensão>.srm.
CONTENT_EXTENSIONS = GAME_FILE_EXTENSIONS | {"cue", "m3u", "pbp", "zip", "7z"}
SRM_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def srm_basename(db, content_id, game_name):
    """Nome (sem extensão) do .srm que o PCSX ReARMed do RetroArch lê para
    este jogo: o nome da ISO do campeonato com esse conteúdo, se houver; senão
    o nome que o jogo tem em `games` (o que a DLL mandou ou a ISO do envio)."""
    row = db.execute(
        "SELECT original_filename FROM files WHERE file_type = 'iso' AND content_id = ? COLLATE NOCASE "
        "ORDER BY is_published DESC, id DESC LIMIT 1", (content_id,)
    ).fetchone()
    name = (row["original_filename"] if row else game_name) or ""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, ext = name.rpartition(".")
    if dot and stem and ext.lower() in CONTENT_EXTENSIONS:
        name = stem
    name = SRM_INVALID_CHARS.sub("_", name).strip(" .")
    return name or "Memory Card"


def get_or_create_game(db, content_id, name):
    game = db.execute("SELECT * FROM games WHERE content_id = ?", (content_id,)).fetchone()
    if game:
        return game
    name = (name or "").strip()[:128] or content_id
    db.execute(
        "INSERT INTO games (content_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (content_id, name, now(), now()),
    )
    return db.execute("SELECT * FROM games WHERE content_id = ?", (content_id,)).fetchone()


def add_version(db, memcard_id, data, source, note=""):
    sha = store_card(data)
    row = db.execute("SELECT current_version FROM memcards WHERE id = ?", (memcard_id,)).fetchone()
    version = (row["current_version"] if row else 0) + 1
    db.execute(
        "INSERT INTO memcard_versions (memcard_id, version, sha256, size, source, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (memcard_id, version, sha, len(data), source, note[:200], now()),
    )
    db.execute(
        "UPDATE memcards SET current_version = ?, updated_at = ? WHERE id = ?",
        (version, now(), memcard_id),
    )
    return version


def get_or_create_memcard(db, player_id, game_id):
    card = db.execute(
        "SELECT * FROM memcards WHERE player_id = ? AND game_id = ?", (player_id, game_id)
    ).fetchone()
    if card:
        return card
    db.execute(
        "INSERT INTO memcards (player_id, game_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (player_id, game_id, now(), now()),
    )
    card = db.execute(
        "SELECT * FROM memcards WHERE player_id = ? AND game_id = ?", (player_id, game_id)
    ).fetchone()
    add_version(db, card["id"], blank_card(), "blank", "Cartão novo, formatado")
    return db.execute("SELECT * FROM memcards WHERE id = ?", (card["id"],)).fetchone()


def current_version(db, memcard_id):
    return db.execute(
        "SELECT v.* FROM memcard_versions v JOIN memcards m ON m.id = v.memcard_id "
        "WHERE m.id = ? AND v.version = m.current_version",
        (memcard_id,),
    ).fetchone()


# --------------------------------------------------------------------------
# API do kailleraclient.dll
# --------------------------------------------------------------------------

def _reply(_http_status=200, **fields):
    body = "".join(f"{key}={value}\n" for key, value in fields.items())
    return Response(body, status=_http_status, mimetype="text/plain; charset=utf-8")


def _error(http_status, code, message=""):
    return _reply(http_status, ok=0, error=code, message=message)


def _api_player():
    """Jogador do token de API (header Authorization: Bearer <token>), ou None."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    db = get_db()
    row = find_token(db, header[7:].strip(), "api")
    if not row or not row["email_verified_at"]:
        return None
    db.execute("UPDATE player_tokens SET last_used_at = ? WHERE id = ?", (now(), row["token_id"]))
    db.commit()
    return row


def _discord_fields(player):
    """Discord vinculado (app/discord.py), para o DLL ligar sozinho a
    chamada de voz na primeira vez. O nome vai numa linha só."""
    name = " ".join((player["discord_name"] or "").split())
    return {"discord_linked": 1 if player["discord_id"] else 0, "discord_name": name}


def _player_list(value):
    names = [name.strip() for name in (value or "").split(",")]
    return [name for name in names if name]


@api_bp.post("/login")
def api_login():
    """username (ou e-mail) + password -> token de API para o DLL guardar.
    Só contas com e-mail confirmado."""
    db = get_db()
    login = request.form.get("username", "").strip()
    player = db.execute(
        "SELECT * FROM players WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE",
        (login, login),
    ).fetchone()
    if not player or not check_password_hash(player["password_hash"], request.form.get("password", "")):
        return _error(401, "invalid_login", "Usuario ou senha invalidos.")
    if not player["email_verified_at"]:
        return _error(403, "not_verified", "Confirme o cadastro pelo link enviado ao seu e-mail.")
    token = issue_token(db, player["id"], "api")
    db.execute("UPDATE players SET last_login_at = ? WHERE id = ?", (now(), player["id"]))
    db.commit()
    return _reply(ok=1, token=token, username=player["username"], email=player["email"], **_discord_fields(player))


@api_bp.get("/whoami")
def api_whoami():
    player = _api_player()
    if not player:
        return _error(401, "invalid_token", "Faca login novamente.")
    return _reply(ok=1, username=player["username"], **_discord_fields(player))


@api_bp.post("/logout")
def api_logout():
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        db = get_db()
        db.execute(
            "UPDATE player_tokens SET used_at = ? WHERE token_hash = ? AND kind = 'api'",
            (now(), token_hash(header[7:].strip())),
        )
        db.commit()
    return _reply(ok=1)


# --------------------------------------------------------------------------
# Salas "Só logados": ingressos
# --------------------------------------------------------------------------
#
# O protocolo Kaillera não tem senha - qualquer um entra com qualquer nick.
# Numa sala "Só logados", o DLL de quem entra pede aqui um ingresso (curto,
# de uso único, válido por poucos minutos e amarrado ao número da sala) e o
# manda no chat da sala; o DLL do host o confere em verify-ticket e expulsa
# quem não tem ingresso válido ou cujo nick não é o da conta.

TICKET_TTL_SECONDS = 5 * 60
ROOM_RE = re.compile(r"^[A-Za-z0-9_.:#@-]{1,64}$")


@api_bp.post("/ticket")
def api_ticket():
    player = _api_player()
    if not player:
        return _error(401, "invalid_token", "Faca login novamente.")
    room = (request.form.get("room") or "").strip()
    if not ROOM_RE.match(room):
        return _error(400, "bad_room")
    import secrets
    ticket = secrets.token_urlsafe(18)
    db = get_db()
    db.execute("DELETE FROM room_tickets WHERE expires_at < datetime('now', '-1 day')")
    db.execute(
        "INSERT INTO room_tickets (player_id, token_hash, room, created_at, expires_at) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now', ?))",
        (player["id"], token_hash(ticket), room, f"+{TICKET_TTL_SECONDS} seconds"),
    )
    db.commit()
    return _reply(ok=1, ticket=ticket, username=player["username"], expires_in=TICKET_TTL_SECONDS)


@api_bp.post("/verify-ticket")
def api_verify_ticket():
    """O host (logado) confere o ingresso de quem entrou na sala dele.
    Uso único: um ingresso visto no chat não serve para mais ninguém."""
    host = _api_player()
    if not host:
        return _error(401, "invalid_token", "Faca login novamente.")
    ticket = (request.form.get("ticket") or "").strip()
    room = (request.form.get("room") or "").strip()
    db = get_db()
    row = db.execute(
        "SELECT t.*, p.username, p.email_verified_at, "
        "t.expires_at < datetime('now') AS expired FROM room_tickets t "
        "JOIN players p ON p.id = t.player_id WHERE t.token_hash = ?",
        (token_hash(ticket),),
    ).fetchone()
    if not row or not row["email_verified_at"]:
        return _error(404, "invalid_ticket", "Ingresso invalido.")
    if row["room"] != room:
        return _error(409, "wrong_room", "Ingresso de outra sala.")
    if row["used_at"]:
        return _error(409, "used_ticket", "Ingresso ja usado.")
    if row["expired"]:
        return _error(409, "expired_ticket", "Ingresso expirado.")
    db.execute(
        "UPDATE room_tickets SET used_at = datetime('now'), verified_by = ? WHERE id = ?",
        (host["id"], row["id"]),
    )
    db.commit()
    return _reply(ok=1, username=row["username"])


@api_bp.post("/checkout")
def api_checkout():
    """Início de partida. Form: content_id ("CRC32:TAMANHO"), game_name,
    players (nicks na ordem 1P,2P,... separados por vírgula). Devolve, para
    os slots 1 e 2, o dono e o SHA-256 do cartão atual (criando um cartão
    formatado para quem ainda não tem um desse jogo)."""
    player = _api_player()
    if not player:
        return _error(401, "invalid_token", "Faca login novamente.")
    content_id = normalize_content_id(request.form.get("content_id"))
    if not content_id:
        return _error(400, "bad_content_id")
    names = _player_list(request.form.get("players"))
    if not names or player["username"].lower() not in (name.lower() for name in names):
        return _error(400, "not_in_players", "Voce nao esta na lista de jogadores.")

    db = get_db()
    owners = []
    missing = []
    for name in names[:2]:
        owner = db.execute(
            "SELECT * FROM players WHERE username = ? COLLATE NOCASE AND email_verified_at IS NOT NULL",
            (name,),
        ).fetchone()
        owners.append(owner)
        if not owner:
            missing.append(name)
    if missing:
        return _error(409, "missing_account", ",".join(missing))

    game = get_or_create_game(db, content_id, request.form.get("game_name", ""))
    fields = {"ok": 1, "game_id": game["id"], "game_name": game["name"], "slots": len(owners)}
    for slot, owner in enumerate(owners, start=1):
        card = get_or_create_memcard(db, owner["id"], game["id"])
        version = current_version(db, card["id"])
        fields[f"slot{slot}_player"] = owner["username"]
        fields[f"slot{slot}_version"] = version["version"]
        fields[f"slot{slot}_sha256"] = version["sha256"]
    db.commit()
    return _reply(**fields)


@api_bp.get("/card/<sha>")
def api_card(sha):
    """Conteúdo de uma versão de cartão pelo SHA-256 - para os jogadores e,
    com a chave do DLL (X-Api-Key), para replays e o Ao Vivo."""
    expected = current_app.config.get("SPECTATE_API_KEY", "")
    provided = request.headers.get("X-Api-Key", "")
    key_ok = bool(expected) and provided == expected
    if not key_ok and not _api_player():
        return _error(401, "invalid_token")
    if not SHA256_RE.match(sha):
        abort(404)
    known = get_db().execute("SELECT 1 FROM memcard_versions WHERE sha256 = ? LIMIT 1", (sha,)).fetchone()
    path = card_path(sha)
    if not known or not path.exists():
        abort(404)
    return send_file(path, mimetype="application/octet-stream", download_name=f"{sha}.mcd")


@api_bp.post("/commit")
def api_commit():
    """Fim de partida: o conteúdo final de UM cartão, enviado por um jogador.

    Query: content_id, slot_player (dono do cartão), base_sha256 (o que o
    checkout entregou), players (a mesma lista do checkout). Corpo: os 128 KB.

    Como a emulação é determinística, todos os PCs terminam com os mesmos
    cartões - basta um envio. O DLL do host envia na hora e os outros só
    depois de alguns segundos (se o host caiu); o primeiro envio válido grava
    a nova versão, anotando quem enviou, e os seguintes com o mesmo conteúdo
    não mudam nada ("already"). Um envio diferente do que já foi gravado a
    partir da mesma base (PC dessincronizado) ou de uma base que já não é a
    atual (upload pelo site, outra partida) é recusado ("conflict") em vez de
    sobrescrever - e o dono sempre pode restaurar uma versão pelo histórico."""
    reporter = _api_player()
    if not reporter:
        return _error(401, "invalid_token", "Faca login novamente.")
    content_id = normalize_content_id(request.args.get("content_id"))
    base = (request.args.get("base_sha256") or "").lower()
    slot_player = (request.args.get("slot_player") or "").strip()
    names = _player_list(request.args.get("players"))
    lowered = [name.lower() for name in names]
    if not content_id or not SHA256_RE.match(base) or not slot_player:
        return _error(400, "bad_request")
    if reporter["username"].lower() not in lowered or slot_player.lower() not in lowered[:2]:
        return _error(400, "not_in_players")

    data = request.get_data()
    error = card_error(data)
    if error:
        return _error(400, "bad_card", error)

    db = get_db()
    game = db.execute("SELECT * FROM games WHERE content_id = ?", (content_id,)).fetchone()
    owner = db.execute("SELECT * FROM players WHERE username = ? COLLATE NOCASE", (slot_player,)).fetchone()
    card = owner and game and db.execute(
        "SELECT * FROM memcards WHERE player_id = ? AND game_id = ?", (owner["id"], game["id"])
    ).fetchone()
    if not card:
        return _error(404, "unknown_card")
    current = current_version(db, card["id"])
    new_sha = sha256_of(data)
    if current["sha256"] == new_sha:
        # Nada mudou nesta partida, ou outro jogador já enviou este conteúdo.
        status = "unchanged" if new_sha == base else "already"
        return _reply(ok=1, status=status, version=current["version"])
    if current["sha256"] != base:
        current_app.logger.warning(
            "Memory Card %s/%s: envio de %s recusado (base %s, atual %s, enviado %s)",
            owner["username"], game["content_id"], reporter["username"], base[:12],
            current["sha256"][:12], new_sha[:12])
        return _reply(409, ok=0, status="conflict", version=current["version"])

    note = f"Partida: {', '.join(names)} · enviado por {reporter['username']}"
    version = add_version(db, card["id"], data, "match", note)
    db.commit()
    return _reply(ok=1, status="committed", version=version)
