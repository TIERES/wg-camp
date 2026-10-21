"""Memory Card online (PCSX ReARMed / PlayStation).

Cada jogador cadastrado tem, por jogo, um cartão com histórico de versões.
Numa partida, o slot 1 do console recebe o cartão do 1P e o slot 2 o do 2P
- em todos os PCs, igual a um PS1 de verdade onde cada amigo traz o seu.
No "M. Card da ISO" é o contrário: um cartão só do jogo, sem dono, no slot 1
de todos (ex.: a Master League de um campeonato, uma partida por vez).

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


ISO_CARD_SLOTS = (1, 2)


def get_iso_memcard(db, game_id, slot=1):
    return db.execute(
        "SELECT * FROM memcards WHERE player_id IS NULL AND game_id = ? AND iso_slot = ?", (game_id, slot)
    ).fetchone()


def get_or_create_iso_memcard(db, game_id, slot=1):
    """O "M. Card da ISO" do jogo: cartões sem dono, os mesmos para todos que
    jogam nesse modo (ex.: a Master League de um campeonato) - um para o slot
    1 e outro para o slot 2."""
    card = get_iso_memcard(db, game_id, slot)
    if card:
        return card
    db.execute(
        "INSERT INTO memcards (player_id, game_id, iso_slot, created_at, updated_at) VALUES (NULL, ?, ?, ?, ?)",
        (game_id, slot, now(), now()),
    )
    card = get_iso_memcard(db, game_id, slot)
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


def _api_key_ok():
    """A chave compartilhada do DLL (X-Api-Key) - replays, Ao Vivo e os
    convidados sem login de uma sala "M. Card da ISO"."""
    expected = current_app.config.get("SPECTATE_API_KEY", "")
    return bool(expected) and request.headers.get("X-Api-Key", "") == expected


ISO_SLOT_PLAYER = "ISO"

# Bases que um envio pode citar (o cartão do checkout + os saves da partida).
MAX_COMMIT_BASES = 32

# M. Card da ISO: uma partida por vez. O checkout (mode=iso, lock=1) prende
# os cartões do jogo para a partida - identificada pela lista de jogadores,
# a mesma em todos os PCs dela -, o DLL renova a cada minuto (iso-lock
# action=renew) e solta no fim (action=release). Um PC que fechou sem soltar
# deixa a trava expirar sozinha. Antes do Start, o DLL pergunta pelo nome do
# jogo da sala (action=status) - o content_id só existe depois que o
# RetroArch abre o jogo -, e o checkout confere de novo pelo content_id.
ISO_LOCK_TTL_SECONDS = 5 * 60


def _lock_holder(names):
    return ",".join(name.lower() for name in names)


def iso_lock(db, game_id):
    """A trava em vigor do M. Card da ISO do jogo, ou None."""
    return db.execute(
        "SELECT * FROM iso_card_locks WHERE game_id = ? AND expires_at > datetime('now')", (game_id,)
    ).fetchone()


def take_iso_lock(db, game_id, names, room_game="", server=""):
    """Prende (ou renova) o M. Card da ISO do jogo para a partida de `names`.
    Devolve None, ou a trava da outra partida que está com ele."""
    holder = _lock_holder(names)
    current = iso_lock(db, game_id)
    if current and current["holder"] != holder:
        return current
    renewing = current is not None
    db.execute(
        "INSERT INTO iso_card_locks (game_id, holder, players, room_game, server, started_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now', ?)) "
        "ON CONFLICT(game_id) DO UPDATE SET holder = excluded.holder, players = excluded.players, "
        "room_game = CASE WHEN excluded.room_game != '' OR NOT ? THEN excluded.room_game ELSE room_game END, "
        "server = CASE WHEN excluded.server != '' OR NOT ? THEN excluded.server ELSE server END, "
        "started_at = CASE WHEN ? THEN started_at ELSE excluded.started_at END, "
        "expires_at = excluded.expires_at",
        (game_id, holder, ", ".join(names), room_game[:200], server[:200], f"+{ISO_LOCK_TTL_SECONDS} seconds",
         renewing, renewing, renewing),
    )
    return None


def release_iso_lock(db, game_id, names):
    db.execute("DELETE FROM iso_card_locks WHERE game_id = ? AND holder = ?", (game_id, _lock_holder(names)))


def _lock_busy_fields(lock):
    """A outra partida, para o DLL avisar a sala (texto sem acentos, como as
    mensagens do DLL)."""
    where = f" no servidor {lock['server']}" if lock["server"] else ""
    return {
        "lock_players": lock["players"],
        "lock_server": lock["server"],
        "lock_started_at": lock["started_at"],
        "message": f"O M. Card da ISO deste jogo esta em uso na partida de {lock['players']}{where}.",
    }


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
    """O host confere o ingresso de quem entrou na sala dele - logado, ou com
    a chave do DLL (no M. Card Online basta um jogador com conta, então um
    host sem conta também confere a do convidado). Uso único: um ingresso
    visto no chat não serve para mais ninguém."""
    host = _api_player()
    if not host and not _api_key_ok():
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
        (host["id"] if host else None, row["id"]),
    )
    db.commit()
    return _reply(ok=1, username=row["username"])


@api_bp.post("/checkout")
def api_checkout():
    """Início de partida. Form: content_id ("CRC32:TAMANHO"), game_name,
    players (nicks na ordem 1P,2P,... separados por vírgula). Devolve, para
    os slots 1 e 2, o dono e o SHA-256 do cartão atual (criando um cartão
    formatado para quem ainda não tem um desse jogo).

    mode=iso ("M. Card da ISO"): os cartões do jogo, iguais para todos. Só o
    host precisa estar logado - os convidados sem conta usam a chave do DLL
    (X-Api-Key) e recebem os mesmos cartões. Com iso_slots=2 (DLL que usa os
    dois slots) vêm os cartões dos slots 1 e 2; sem ele, só o do slot 1 - um
    DLL que não conhece o slot 2 o enviaria de volta como se fosse o 1.

    multi_base=1 na resposta: o commit aceita uma lista de bases (o DLL envia
    cada save durante a partida, ver api_commit).

    accounts (nicks com a conta confirmada na sala, separados por vírgula):
    basta um jogador com conta - quem não está nessa lista fica com o slot
    vazio (slotN_player e slotN_sha256 em branco), sem cartão e sem nada
    para salvar. Como os PCs sem login também pedem (com a chave do DLL),
    todos recebem os mesmos slots. Sem esse campo (DLL antigo), os dois
    jogadores precisam ter conta, como antes.

    mode=iso com lock=1 (e room_game = nome do jogo da sala, server = onde
    ela está): prende os cartões do jogo para esta partida (ver
    take_iso_lock). Se outra partida já está com eles: 409 iso_in_use, com
    quem está jogando (lock_players / lock_server) - e a resposta normal traz
    lock=1."""
    iso = request.form.get("mode") == "iso"
    accounts_field = request.form.get("accounts")
    player = _api_player()
    if not player and not ((iso or accounts_field is not None) and _api_key_ok()):
        return _error(401, "invalid_token", "Faca login novamente.")
    content_id = normalize_content_id(request.form.get("content_id"))
    if not content_id:
        return _error(400, "bad_content_id")
    names = _player_list(request.form.get("players"))
    if not names or (player and player["username"].lower() not in (name.lower() for name in names)):
        return _error(400, "not_in_players", "Voce nao esta na lista de jogadores.")

    db = get_db()
    if iso:
        game = get_or_create_game(db, content_id, request.form.get("game_name", ""))
        locking = request.form.get("lock") == "1"
        if locking:
            busy = take_iso_lock(db, game["id"], names, (request.form.get("room_game") or "").strip(),
                                 (request.form.get("server") or "").strip())
            if busy:
                db.commit()
                return _reply(409, ok=0, error="iso_in_use", **_lock_busy_fields(busy))
        slots = ISO_CARD_SLOTS if request.form.get("iso_slots") == "2" else ISO_CARD_SLOTS[:1]
        fields = {"ok": 1, "mode": "iso", "game_id": game["id"], "game_name": game["name"], "slots": len(slots),
                  "multi_base": 1}
        if locking:
            fields["lock"] = 1  # o DLL renova e solta a trava (iso-lock)
            fields["lock_ttl"] = ISO_LOCK_TTL_SECONDS
        for slot in slots:
            card = get_or_create_iso_memcard(db, game["id"], slot)
            version = current_version(db, card["id"])
            fields[f"slot{slot}_player"] = ISO_SLOT_PLAYER
            fields[f"slot{slot}_version"] = version["version"]
            fields[f"slot{slot}_sha256"] = version["sha256"]
        db.commit()
        return _reply(**fields)

    accounts = None
    if accounts_field is not None:
        accounts = {name.lower() for name in _player_list(accounts_field)}
        if not accounts & {name.lower() for name in names[:2]}:
            return _error(409, "no_account", "Nenhum dos jogadores 1 e 2 tem conta WE Camp confirmada na sala.")
    owners = []
    missing = []
    for name in names[:2]:
        if accounts is not None and name.lower() not in accounts:
            owners.append(None)  # sem conta confirmada: slot vazio
            continue
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
    fields = {"ok": 1, "game_id": game["id"], "game_name": game["name"], "slots": len(owners), "multi_base": 1}
    if accounts is not None:
        fields["rule"] = "accounts"  # o DLL confere que o servidor entendeu o campo
    for slot, owner in enumerate(owners, start=1):
        if not owner:
            fields[f"slot{slot}_player"] = ""
            fields[f"slot{slot}_version"] = 0
            fields[f"slot{slot}_sha256"] = ""
            continue
        card = get_or_create_memcard(db, owner["id"], game["id"])
        version = current_version(db, card["id"])
        fields[f"slot{slot}_player"] = owner["username"]
        fields[f"slot{slot}_version"] = version["version"]
        fields[f"slot{slot}_sha256"] = version["sha256"]
    db.commit()
    return _reply(**fields)


@api_bp.post("/iso-lock")
def api_iso_lock():
    """A trava do M. Card da ISO (ver take_iso_lock). Form: action, players
    (nicks da partida, na ordem 1P,2P,...) e:
      - status: room_game - o host pergunta antes do Start se outra partida
        do jogo com esse nome está com o M. Card da ISO (busy=1 + quem);
      - renew: content_id (+ room_game, server) - durante a partida, a cada
        minuto; 409 iso_in_use se a trava expirou e outra partida pegou;
      - release: content_id - fim da partida.
    Com o token de quem está logado ou a chave do DLL."""
    if not _api_player() and not _api_key_ok():
        return _error(401, "invalid_token", "Faca login novamente.")
    action = request.form.get("action", "")
    names = _player_list(request.form.get("players"))
    if not names or action not in {"status", "renew", "release"}:
        return _error(400, "bad_request")
    db = get_db()
    if action == "status":
        room_game = (request.form.get("room_game") or "").strip()
        if not room_game:
            return _error(400, "bad_request")
        lock = db.execute(
            "SELECT * FROM iso_card_locks WHERE room_game = ? COLLATE NOCASE AND holder != ? "
            "AND expires_at > datetime('now') ORDER BY started_at LIMIT 1",
            (room_game, _lock_holder(names)),
        ).fetchone()
        if not lock:
            return _reply(ok=1, busy=0)
        return _reply(ok=1, busy=1, **_lock_busy_fields(lock))

    content_id = normalize_content_id(request.form.get("content_id"))
    game = content_id and db.execute("SELECT * FROM games WHERE content_id = ?", (content_id,)).fetchone()
    if not game:
        return _error(404, "unknown_game")
    if action == "release":
        release_iso_lock(db, game["id"], names)
        db.commit()
        return _reply(ok=1)
    busy = take_iso_lock(db, game["id"], names, (request.form.get("room_game") or "").strip(),
                         (request.form.get("server") or "").strip())
    db.commit()
    if busy:
        return _reply(409, ok=0, error="iso_in_use", **_lock_busy_fields(busy))
    return _reply(ok=1, lock_ttl=ISO_LOCK_TTL_SECONDS)


@api_bp.get("/card/<sha>")
def api_card(sha):
    """Conteúdo de uma versão de cartão pelo SHA-256 - para os jogadores e,
    com a chave do DLL (X-Api-Key), para replays e o Ao Vivo."""
    if not _api_key_ok() and not _api_player():
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

    base_sha256 também pode ser uma lista separada por vírgulas: o cartão do
    checkout e cada versão que o PC viu o jogo salvar durante a partida. O DLL
    envia cada save logo que o jogo termina de gravar, e o envio vale se a
    versão atual do WE Camp é qualquer uma dessas - assim o convidado que
    envia no fim (o host caiu) não perde o save só porque o host já tinha
    gravado uma versão intermediária.

    Como a emulação é determinística, todos os PCs terminam com os mesmos
    cartões - basta um envio. O DLL do host envia na hora e os outros só
    depois de alguns segundos (se o host caiu); o primeiro envio válido grava
    a nova versão, anotando quem enviou, e os seguintes com o mesmo conteúdo
    não mudam nada ("already"). Um envio diferente do que já foi gravado a
    partir da mesma base (PC dessincronizado) ou de uma base que já não é a
    atual (upload pelo site, outra partida) é recusado ("conflict") em vez de
    sobrescrever - e o dono sempre pode restaurar uma versão pelo histórico.

    mode=iso: um "M. Card da ISO" do jogo - o do slot 1, ou o do `slot` 2
    (slot_player não é usado). Quem não tem login envia com a chave do DLL e
    diz quem é em `reporter`. Duas partidas ao mesmo tempo com o mesmo
    cartão: a segunda a terminar recebe "conflict" e não apaga o que a
    primeira gravou."""
    iso = request.args.get("mode") == "iso"
    iso_slot = request.args.get("slot", "1")
    player = _api_player()
    if player:
        reporter = player["username"]
    elif iso and _api_key_ok():
        reporter = (request.args.get("reporter") or "").strip()
    else:
        return _error(401, "invalid_token", "Faca login novamente.")
    content_id = normalize_content_id(request.args.get("content_id"))
    bases = [value.strip() for value in (request.args.get("base_sha256") or "").lower().split(",")]
    slot_player = (request.args.get("slot_player") or "").strip()
    names = _player_list(request.args.get("players"))
    lowered = [name.lower() for name in names]
    if (not content_id or not (slot_player or iso) or len(bases) > MAX_COMMIT_BASES
            or not all(SHA256_RE.match(value) for value in bases)):
        return _error(400, "bad_request")
    if iso and iso_slot not in {str(slot) for slot in ISO_CARD_SLOTS}:
        return _error(400, "bad_slot")
    if reporter.lower() not in lowered or not (iso or slot_player.lower() in lowered[:2]):
        return _error(400, "not_in_players")

    data = request.get_data()
    error = card_error(data)
    if error:
        return _error(400, "bad_card", error)

    db = get_db()
    game = db.execute("SELECT * FROM games WHERE content_id = ?", (content_id,)).fetchone()
    if iso:
        card_name = f"{ISO_SLOT_PLAYER} slot {iso_slot}"
        card = game and get_iso_memcard(db, game["id"], int(iso_slot))
    else:
        owner = db.execute("SELECT * FROM players WHERE username = ? COLLATE NOCASE", (slot_player,)).fetchone()
        card_name = owner["username"] if owner else slot_player
        card = owner and game and db.execute(
            "SELECT * FROM memcards WHERE player_id = ? AND game_id = ?", (owner["id"], game["id"])
        ).fetchone()
    if not card:
        return _error(404, "unknown_card")
    current = current_version(db, card["id"])
    new_sha = sha256_of(data)
    if current["sha256"] == new_sha:
        # Nada mudou nesta partida, ou outro jogador já enviou este conteúdo.
        status = "unchanged" if new_sha in bases else "already"
        return _reply(ok=1, status=status, version=current["version"])
    if current["sha256"] not in bases:
        current_app.logger.warning(
            "Memory Card %s/%s: envio de %s recusado (base %s, atual %s, enviado %s)",
            card_name, game["content_id"], reporter, ",".join(value[:12] for value in bases),
            current["sha256"][:12], new_sha[:12])
        return _reply(409, ok=0, status="conflict", version=current["version"])

    note = f"Partida: {', '.join(names)} · enviado por {reporter}"
    version = add_version(db, card["id"], data, "match", note)
    db.commit()
    return _reply(ok=1, status="committed", version=version)
