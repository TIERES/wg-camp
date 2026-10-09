"""Discord da WE Camp: conta vinculada e canal de voz por partida.

Sem bot conectado ao gateway (o servidor tem 1 GB de RAM): tudo é REST,
feito pelo próprio Flask com o token do bot, só quando algo acontece.

 - "Vincular Discord" (página da conta): OAuth2 com os escopos identify
   (o ID do Discord, verificado) e guilds.join (o bot já coloca o jogador no
   servidor da WE Camp, onde ficam os canais).
 - /api/voice/join: o kailleraclient.dll de cada jogador chama no início de
   uma partida com a lista de nicks da sala. A primeira chamada de um grupo
   de jogadores cria um canal de voz privado na categoria "Partidas", aberto
   só para os Discords vinculados dessa lista; as seguintes reaproveitam o
   canal (inclusive na revanche). Cada chamada move SÓ quem chamou - e só se
   ele já estiver em algum canal de voz do servidor (o Discord não deixa
   ninguém entrar numa chamada sozinho); senão o DLL mostra o link.
   Ninguém consegue puxar outra pessoa para uma chamada: a lista de nicks
   só dá permissão de ver o canal.
 - Faxina (reap_voice_channels): canais sem nenhum dos jogadores dentro são
   apagados depois de alguns minutos.

Configuração (ARENA17_DISCORD_*): sem o token do bot o recurso fica
desligado e a API responde error=disabled.
"""
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from flask import (Blueprint, current_app, flash, redirect, request, session, url_for)

from .accounts import parse_iso, utcnow
from .db import get_db, now
from .security import validate_csrf

API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
USER_AGENT = "DiscordBot (https://we2002.wgs.dev.br, 1.0)"

VIEW_CHANNEL = 1 << 10
CONNECT = 1 << 20
MANAGE_CHANNELS = 1 << 4
MOVE_MEMBERS = 1 << 24
# Só dá para liberar no canal o que o bot tem no servidor (as 5 permissões
# do convite). Falar, transmitir etc. vêm do @everyone do servidor.
PLAYER_ALLOW = VIEW_CHANNEL | CONNECT
BOT_ALLOW = VIEW_CHANNEL | CONNECT | MANAGE_CHANNELS | MOVE_MEMBERS

# Códigos de erro JSON do Discord.
NOT_IN_VOICE = 40032        # Target user is not connected to voice
UNKNOWN_MEMBER = 10007
UNKNOWN_CHANNEL = 10003
UNKNOWN_VOICE_STATE = 10065

# Tempo para os jogadores entrarem antes de um canal vazio ser apagado.
REAP_GRACE = timedelta(minutes=5)
# Um canal nunca dura mais que isso, mesmo ocupado (alguém esqueceu o
# Discord aberto na sala).
REAP_MAX_AGE = timedelta(hours=12)
REAP_INTERVAL_SECONDS = 120

bp = Blueprint("discord", __name__)
api_bp = Blueprint("voice_api", __name__, url_prefix="/api/voice")

_create_lock = threading.Lock()
_reap_lock = threading.Lock()
_last_reap = [0.0]
_bot_user_id = [None]


class DiscordError(Exception):
    def __init__(self, status, code=0, message=""):
        super().__init__(f"Discord {status} ({code}): {message}")
        self.status = status
        self.code = code


def config(key):
    return current_app.config.get(f"DISCORD_{key}") or ""


def enabled():
    return bool(config("BOT_TOKEN") and config("GUILD_ID"))


def oauth_enabled():
    return bool(config("CLIENT_ID") and config("CLIENT_SECRET"))


def redirect_uri():
    if config("REDIRECT_URI"):
        return config("REDIRECT_URI")
    base = (current_app.config.get("PUBLIC_URL") or "").rstrip("/")
    return (base + "/discord/callback") if base else url_for("discord.callback", _external=True)


def channel_url(channel_id):
    return f"https://discord.com/channels/{config('GUILD_ID')}/{channel_id}"


def app_url(channel_id):
    return f"discord://-/channels/{config('GUILD_ID')}/{channel_id}"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http(method, path, body=None, auth="bot", form=None, basic=None):
    """Uma chamada à API do Discord. Devolve o JSON (ou None em 204).
    Em 429 espera o retry_after (se curto) e tenta de novo uma vez."""
    url = API + path
    headers = {"User-Agent": USER_AGENT}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if basic:
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
    elif auth == "bot":
        headers["Authorization"] = "Bot " + config("BOT_TOKEN")
    elif auth:
        headers["Authorization"] = "Bearer " + auth
    for attempt in range(2):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {}
            if error.code == 429 and attempt == 0:
                wait = float(payload.get("retry_after", 1) or 1)
                if wait <= 3:
                    time.sleep(wait)
                    continue
            raise DiscordError(error.code, payload.get("code", 0), payload.get("message", ""))
        except (urllib.error.URLError, OSError) as error:
            raise DiscordError(0, 0, str(error))
    raise DiscordError(429, 0, "rate limited")


def bot_user_id():
    if not _bot_user_id[0]:
        _bot_user_id[0] = http("GET", "/users/@me")["id"]
    return _bot_user_id[0]


# --------------------------------------------------------------------------
# Vincular Discord (OAuth2)
# --------------------------------------------------------------------------

def _player_required():
    if "player_id" not in session:
        return redirect(url_for("players.login", next="/conta/"))
    return None


@bp.post("/conta/discord/vincular")
def link():
    validate_csrf()
    if (response := _player_required()):
        return response
    if not oauth_enabled():
        flash("O Discord ainda não está configurado no WE Camp.", "error")
        return redirect(url_for("players.account"))
    state = secrets.token_urlsafe(24)
    session["discord_state"] = state
    query = urllib.parse.urlencode({
        "client_id": config("CLIENT_ID"),
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "scope": "identify guilds.join",
        "state": state,
    })
    return redirect(f"{AUTHORIZE_URL}?{query}")


@bp.get("/discord/callback")
def callback():
    if (response := _player_required()):
        return response
    state = session.pop("discord_state", None)
    if not state or not secrets.compare_digest(state, request.args.get("state", "")):
        flash("Vinculação com o Discord expirou. Tente de novo.", "error")
        return redirect(url_for("players.account"))
    if request.args.get("error") or not request.args.get("code"):
        flash("Vinculação com o Discord cancelada.", "error")
        return redirect(url_for("players.account"))
    try:
        token = http("POST", "/oauth2/token", auth=None, basic=(config("CLIENT_ID"), config("CLIENT_SECRET")), form={
            "grant_type": "authorization_code",
            "code": request.args["code"],
            "redirect_uri": redirect_uri(),
        })
        user = http("GET", "/users/@me", auth=token["access_token"])
    except (DiscordError, KeyError, TypeError) as error:
        current_app.logger.warning("Discord OAuth falhou: %s", error)
        flash("Não foi possível falar com o Discord agora. Tente de novo em alguns minutos.", "error")
        return redirect(url_for("players.account"))

    db = get_db()
    other = db.execute("SELECT username FROM players WHERE discord_id = ? AND id != ?",
                       (user["id"], session["player_id"])).fetchone()
    if other:
        flash(f"Este Discord já está vinculado à conta {other['username']}. "
              "Desvincule-o nela primeiro.", "error")
        return redirect(url_for("players.account"))
    name = user.get("global_name") or user.get("username") or user["id"]
    db.execute("UPDATE players SET discord_id = ?, discord_name = ?, discord_linked_at = ?, updated_at = ? WHERE id = ?",
               (user["id"], name[:64], now(), now(), session["player_id"]))
    db.commit()

    joined = ""
    if enabled():
        try:
            # 201 = entrou agora, 204 = já era membro.
            http("PUT", f"/guilds/{config('GUILD_ID')}/members/{user['id']}",
                 body={"access_token": token["access_token"]})
            joined = " Você já está no servidor da WE Camp no Discord."
        except DiscordError as error:
            current_app.logger.warning("Discord: não consegui pôr %s no servidor: %s", user["id"], error)
            joined = " Entre no servidor da WE Camp no Discord para usar o canal de voz das partidas."
    flash(f"Discord vinculado: {name}.{joined}", "success")
    return redirect(url_for("players.account"))


@bp.post("/conta/discord/desvincular")
def unlink():
    validate_csrf()
    if (response := _player_required()):
        return response
    db = get_db()
    db.execute("UPDATE players SET discord_id = NULL, discord_name = NULL, discord_linked_at = NULL, updated_at = ? "
               "WHERE id = ?", (now(), session["player_id"]))
    db.commit()
    flash("Discord desvinculado.", "success")
    return redirect(url_for("players.account"))


# --------------------------------------------------------------------------
# Canal de voz da partida
# --------------------------------------------------------------------------

def room_key(names):
    """O mesmo grupo de jogadores = o mesmo canal, em qualquer servidor
    Kaillera ou no P2P, e na revanche."""
    return ",".join(sorted({name.lower() for name in names}))


def channel_name(names):
    return ("🎮 " + " x ".join(names))[:100]


def _members(row):
    return [member for member in (row["member_ids"] or "").split(",") if member]


def _create_channel(names, discord_ids):
    guild = config("GUILD_ID")
    overwrites = [
        {"id": guild, "type": 0, "deny": str(VIEW_CHANNEL | CONNECT), "allow": "0"},
        {"id": bot_user_id(), "type": 1, "allow": str(BOT_ALLOW), "deny": "0"},
    ]
    overwrites += [{"id": member, "type": 1, "allow": str(PLAYER_ALLOW), "deny": "0"} for member in discord_ids]
    body = {"name": channel_name(names), "type": 2, "permission_overwrites": overwrites}
    if config("CATEGORY_ID"):
        body["parent_id"] = config("CATEGORY_ID")
    return http("POST", f"/guilds/{guild}/channels", body=body)["id"]


def _channel_alive(channel_id):
    try:
        http("GET", f"/channels/{channel_id}")
        return True
    except DiscordError as error:
        if error.status == 404:
            return False
        raise


def ensure_channel(db, names, caller_discord_id):
    """O canal deste grupo (criado se preciso). Devolve o ID do canal."""
    key = room_key(names)
    with _create_lock:
        row = db.execute("SELECT * FROM voice_channels WHERE room_key = ?", (key,)).fetchone()
        if row and _channel_alive(row["channel_id"]):
            members = _members(row)
            if caller_discord_id not in members:
                # Vinculou o Discord depois que o canal foi criado. Mudar as
                # permissões de um canal exige "Gerenciar cargos" - sem ela,
                # o mover abaixo ainda funciona.
                try:
                    http("PUT", f"/channels/{row['channel_id']}/permissions/{caller_discord_id}",
                         body={"type": 1, "allow": str(PLAYER_ALLOW), "deny": "0"})
                except DiscordError as error:
                    current_app.logger.info("Discord: sem permissão para liberar o canal: %s", error)
                members.append(caller_discord_id)
            db.execute("UPDATE voice_channels SET member_ids = ?, last_join_at = ? WHERE id = ?",
                       (",".join(members), now(), row["id"]))
            db.commit()
            return row["channel_id"]
        if row:
            db.execute("DELETE FROM voice_channels WHERE id = ?", (row["id"],))

        placeholders = ",".join("?" * len(names))
        linked = [r["discord_id"] for r in db.execute(
            f"SELECT discord_id FROM players WHERE username COLLATE NOCASE IN ({placeholders}) "
            "AND email_verified_at IS NOT NULL AND discord_id IS NOT NULL", names)]
        if caller_discord_id not in linked:
            linked.append(caller_discord_id)
        channel_id = _create_channel(names, linked)
        db.execute(
            "INSERT INTO voice_channels (room_key, channel_id, name, member_ids, created_at, last_join_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, channel_id, channel_name(names), ",".join(linked), now(), now()),
        )
        db.commit()
        return channel_id


def move_to(discord_id, channel_id):
    """True se moveu; False se a pessoa não está em nenhum canal de voz do
    servidor (ou saiu do servidor)."""
    try:
        http("PATCH", f"/guilds/{config('GUILD_ID')}/members/{discord_id}", body={"channel_id": channel_id})
        return True
    except DiscordError as error:
        if error.code in (NOT_IN_VOICE, UNKNOWN_MEMBER) or error.status in (400, 404):
            return False
        raise


def _voice_channel_of(discord_id):
    try:
        state = http("GET", f"/guilds/{config('GUILD_ID')}/voice-states/{discord_id}")
    except DiscordError as error:
        if error.status == 404:
            return None
        raise
    return (state or {}).get("channel_id")


def reap_voice_channels(db):
    """Apaga os canais de partida vazios (passado o tempo de tolerância) e os
    muito antigos. Devolve quantos apagou."""
    removed = 0
    current = utcnow()
    for row in db.execute("SELECT * FROM voice_channels").fetchall():
        created = parse_iso(row["created_at"]) or current
        last_join = parse_iso(row["last_join_at"]) or created
        if current - last_join < REAP_GRACE:
            continue
        expired = current - created > REAP_MAX_AGE
        try:
            occupied = not expired and any(_voice_channel_of(member) == row["channel_id"] for member in _members(row))
            if occupied:
                continue
            try:
                http("DELETE", f"/channels/{row['channel_id']}")
            except DiscordError as error:
                if error.status != 404:
                    raise
        except DiscordError as error:
            current_app.logger.warning("Discord: faxina do canal %s falhou: %s", row["channel_id"], error)
            continue
        db.execute("DELETE FROM voice_channels WHERE id = ?", (row["id"],))
        db.commit()
        removed += 1
    return removed


def _reap_thread(app):
    with app.app_context():
        try:
            reap_voice_channels(get_db())
        finally:
            from .db import close_db
            close_db()
            _reap_lock.release()


def schedule_reap():
    """Faxina em segundo plano, no máximo a cada REAP_INTERVAL_SECONDS."""
    if not enabled() or time.monotonic() - _last_reap[0] < REAP_INTERVAL_SECONDS:
        return
    if not _reap_lock.acquire(blocking=False):
        return
    _last_reap[0] = time.monotonic()
    try:
        if not get_db().execute("SELECT 1 FROM voice_channels LIMIT 1").fetchone():
            _reap_lock.release()
            return
    except Exception:
        _reap_lock.release()
        raise
    threading.Thread(target=_reap_thread, args=(current_app._get_current_object(),), daemon=True).start()


@bp.before_app_request
def _maybe_reap():
    if current_app.config.get("TESTING"):
        return
    try:
        schedule_reap()
    except Exception as error:  # a faxina nunca derruba uma página
        current_app.logger.warning("Discord: faxina não agendada: %s", error)


# --------------------------------------------------------------------------
# API do kailleraclient.dll
# --------------------------------------------------------------------------

@api_bp.post("/join")
def api_join():
    """Início de partida. Form: players (nicks da sala separados por
    vírgula, incluindo o de quem chama), game_name (opcional).
    Responde ok, status (moved / link), channel_id, url (https) e app_url
    (discord://, abre direto o app)."""
    from .memcards import _api_player, _error, _player_list, _reply
    player = _api_player()
    if not player:
        return _error(401, "invalid_token", "Faca login novamente.")
    if not enabled():
        return _error(503, "disabled", "Canal de voz do Discord desligado no WE Camp.")
    names = _player_list(request.form.get("players"))[:8]
    if player["username"].lower() not in (name.lower() for name in names):
        return _error(400, "not_in_players", "Voce nao esta na lista de jogadores.")
    if len({name.lower() for name in names}) < 2:
        return _error(400, "solo", "Partida sem outros jogadores.")
    if not player["discord_id"]:
        return _error(409, "not_linked", "Vincule seu Discord na sua conta do WE Camp.")

    db = get_db()
    try:
        channel_id = ensure_channel(db, names, player["discord_id"])
        moved = move_to(player["discord_id"], channel_id)
    except DiscordError as error:
        current_app.logger.warning("Discord: canal de voz para %s falhou: %s", ",".join(names), error)
        return _error(502, "discord_error", "O Discord nao respondeu.")
    return _reply(ok=1, status="moved" if moved else "link", channel_id=channel_id,
                  url=channel_url(channel_id), app_url=app_url(channel_id))
