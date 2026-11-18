"""Transmissão das partidas no YouTube (o broadcaster).

O broadcaster é um PC (hoje o notebook do WE Camp) com o RetroArch TIERES em
modo broadcaster (kaillera-client: tools/broadcaster). Ele assiste a partida
pelo Watch Live, sem janela, e manda o jogo para o YouTube. Quem decide o que
transmitir é o site:

- os jogos de campeonato, sozinhos (ajuste "broadcast_auto", ligado por padrão);
- as partidas que o administrador marca no card do Ao vivo ("Transmitir").

O agente do PC pergunta a cada poucos segundos (POST /api/broadcaster/poll,
com o token dele - broadcast_agents): conta o que está rodando e recebe o que
começar e o que parar. A live no YouTube é ele que cria, com o título e a
descrição daqui (_meta): as credenciais do Google ficam só no PC. Ele devolve o
link, que aparece no card ("Assistir no YouTube").

O card (live_matches) e o Stream ao vivo (live_sessions) são coisas diferentes:
a sessão começa nos menus, o card no pontapé inicial. A transmissão assiste a
sessão dos mesmos jogadores e do mesmo jogo (find_session) e começa LEAD_FRAMES
antes do pontapé (o RetroArch avança rápido até lá, sem transmitir). Quando o
card termina, ela para TAIL_FRAMES depois do último quadro informado - a sala
pode seguir com outra partida.
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for

from .db import get_db, get_setting, now, set_setting
from .security import login_required, validate_csrf

api_bp = Blueprint("broadcaster_api", __name__, url_prefix="/api/broadcaster")
bp = Blueprint("broadcaster", __name__)

FPS = 60
LEAD_FRAMES = 10 * FPS        # a live começa 10 s antes do pontapé
TAIL_FRAMES = 20 * FPS        # e acaba 20 s depois do último quadro informado (o placar final na tela)
QUEUE_WINDOW = timedelta(hours=3)  # card de campeonato mais antigo que isso não entra na fila sozinho
LOST = timedelta(minutes=2)   # dada a um agente que não falou dela por esse tempo: perdida
ONLINE = timedelta(seconds=60)
POLL_SECONDS = 5
TITLE_MAX = 100
ACTIVE = ("assigned", "live")
PRIVACY = {"unlisted": "Não listado", "public": "Público", "private": "Privado"}
STATUS_LABELS = {"queued": "Na fila", "assigned": "Começando", "live": "Ao vivo", "done": "Encerrada",
                 "failed": "Falhou", "cancelled": "Cancelada"}
REASON_LABELS = {"campeonato": "Jogo de campeonato", "admin": "Marcada pelo administrador"}


def _ago(delta):
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def _shift(stamp, delta):
    return (datetime.fromisoformat(stamp) + delta).isoformat(timespec="seconds")


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _nicks(names):
    return {name.strip().casefold() for name in names if name and name.strip()}


def _clean(text, cap):
    """O YouTube recusa "<" e ">" no título e na descrição."""
    return str(text or "").replace("<", "").replace(">", "").strip()[:cap]


def settings():
    return {"auto": get_setting("broadcast_auto", "1") == "1",
            "privacy": get_setting("broadcast_privacy", "unlisted"),
            "version": get_setting("broadcast_retroarch_version", "0.13")}


# --- Fila --------------------------------------------------------------------------

def _insert(db, live_id, reason, replay=0):
    stamp = now()
    return db.execute("INSERT OR IGNORE INTO broadcasts (live_match_id, reason, replay, created_at, updated_at) "
                      "VALUES (?, ?, ?, ?, ?)", (live_id, reason, replay, stamp, stamp)).rowcount


def queue_tournament_matches(db):
    """Os jogos de campeonato ao vivo (verificados, visíveis) entram na fila sozinhos."""
    if get_setting("broadcast_auto", "1") != "1":
        return
    for row in db.execute(
            "SELECT m.id FROM live_matches m WHERE m.status = 'live' AND m.verified = 1 AND m.is_test = 0 "
            "AND m.hidden = 0 AND m.tournament_match_id IS NOT NULL AND m.started_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM broadcasts b WHERE b.live_match_id = m.id)",
            (_ago(QUEUE_WINDOW),)).fetchall():
        _insert(db, row["id"], "campeonato")


def get_for_match(db, live_id):
    return db.execute("SELECT * FROM broadcasts WHERE live_match_id = ?", (live_id,)).fetchone()


def request_broadcast(db, live_id):
    """Administrador: "Transmitir" no card. Ao vivo, a partida vai ao vivo; encerrada
    (campeonato que não foi ao ar, amistoso, partida livre), vai como reprise: o
    replay inteiro do Stream ao vivo, se ele ainda estiver no servidor. (ok, mensagem)."""
    match = db.execute("SELECT * FROM live_matches WHERE id = ?", (live_id,)).fetchone()
    if match is None:
        return False, "Partida não encontrada."
    if match["status"] not in ("live", "finished"):
        return False, "Só dá para transmitir uma partida ao vivo ou encerrada."
    replay = int(match["status"] == "finished")
    if replay and find_session(db, match) is None:
        return False, "O replay desta partida não está no servidor: não dá para transmitir a reprise."
    row = get_for_match(db, live_id)
    if row is None:
        _insert(db, live_id, "admin", replay)
    elif row["status"] in ("cancelled", "failed"):
        db.execute("UPDATE broadcasts SET status = 'queued', reason = 'admin', replay = ?, agent_id = NULL, "
                   "session_id = NULL, start_frame = NULL, stop_frame = NULL, youtube_id = NULL, youtube_url = NULL, "
                   "error = '', assigned_at = NULL, started_at = NULL, ended_at = NULL, updated_at = ? WHERE id = ?",
                   (replay, now(), row["id"]))
    else:
        return False, "Esta partida já está na transmissão (%s)." % STATUS_LABELS[row["status"]].lower()
    return True, ("Reprise da partida na fila da transmissão no YouTube" if replay else
                  "Partida na fila da transmissão no YouTube") + ": começa em alguns segundos, se houver um broadcaster livre."


def cancel_broadcast(db, broadcast_id):
    """Para a transmissão: a da fila sai dela; a que está rodando para já (stop_frame 0)."""
    row = db.execute("SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,)).fetchone()
    if row is None or row["status"] not in ("queued",) + ACTIVE:
        return False
    db.execute("UPDATE broadcasts SET status = 'cancelled', stop_frame = CASE WHEN ? THEN 0 ELSE stop_frame END, "
               "ended_at = CASE WHEN ? THEN ? ELSE ended_at END, updated_at = ? WHERE id = ?",
               (int(row["status"] in ACTIVE), int(row["status"] == "queued"), now(), now(), row["id"]))
    return True


# --- O que o agente precisa --------------------------------------------------------

def find_session(db, match):
    """O Stream ao vivo que tem a partida: começado antes do card (com 1 min de
    folga), com o mesmo jogo da sala e todos os jogadores do card. Partida ao
    vivo: a sessão ao vivo (None enquanto não aparecer - o cabeçalho com os nomes
    chega no 1º envio). Encerrada (reprise): a que ainda estava no ar quando a
    partida começou, com o replay ainda no servidor."""
    players = _nicks(json.loads(match["players_json"] or "[]"))
    if not players:
        return None
    replay = match["status"] != "live"
    for row in db.execute("SELECT session_id, game_name, player_names, status, stored_name, ended_at FROM live_sessions "
                          "WHERE started_at <= ? %s ORDER BY started_at DESC LIMIT 20"
                          % ("" if replay else "AND status = 'live'"),
                          (_shift(match["started_at"], timedelta(minutes=1)),)).fetchall():
        if not row["game_name"]:
            continue
        if replay:
            if row["status"] != "live" and (row["ended_at"] or "") < match["started_at"]:
                continue  # terminou antes da partida
            if not (Path(current_app.config["LIVE_DIR"]) / row["stored_name"]).is_file():
                continue  # replay apagado (limpeza dos replays antigos)
        if match["room"] and row["game_name"].casefold() != match["room"].casefold():
            continue
        if players <= _nicks((row["player_names"] or "").split(",")):
            return row["session_id"]
    return None


def start_frame(match):
    """LEAD_FRAMES antes do pontapé; a partida retomada pelo Reconectar (os
    quadros recomeçam na sessão nova) desde o começo."""
    if match["frame_base"] or match["reconnect_session"]:
        return 0
    return max(0, (match["kickoff_frame"] or 0) - LEAD_FRAMES)


def stop_frame(db, match):
    top = db.execute("SELECT COALESCE(MAX(frame), 0) AS frame FROM live_match_reports WHERE live_match_id = ?",
                     (match["id"],)).fetchone()["frame"]
    return max(0, top - (match["frame_base"] or 0)) + TAIL_FRAMES


def _iso_entry(ref, name, filename, md5, size, url, content_id):
    if not (md5 and url and filename):
        return None  # ainda sem o archive.org (ou sem conferência): o agente espera
    return {"ref": ref, "name": name, "file": filename, "md5": md5.lower(), "size": size or 0, "url": url,
            "content_id": content_id or ""}


def _tournament_iso(db, tournament_id):
    row = db.execute("SELECT i.*, t.name FROM tournament_isos i JOIN tournaments t ON t.id = i.tournament_id "
                     "WHERE i.tournament_id = ?", (tournament_id,)).fetchone()
    if row is None:
        return None
    return _iso_entry("torneio:%d" % tournament_id, row["name"], row["original_filename"], row["md5"],
                      row["file_size"], row["archive_url"], row["content_id"])


def _catalog_iso(db, catalog_id):
    row = db.execute("SELECT c.*, f.md5 AS f_md5, f.archive_url AS f_url, f.original_filename AS f_name, "
                     "f.file_size AS f_size, f.content_id AS f_content FROM catalog_isos c "
                     "LEFT JOIN files f ON f.id = c.file_id WHERE c.id = ?", (catalog_id,)).fetchone()
    if row is None:
        return None
    if row["file_id"]:
        return _iso_entry("cadastro:%d" % catalog_id, row["name"], row["f_name"], row["f_md5"], row["f_size"],
                          row["f_url"], row["f_content"])
    return _iso_entry("cadastro:%d" % catalog_id, row["name"], row["original_filename"], row["md5"],
                      row["file_size"], row["archive_url"], row["content_id"])


def iso_for_match(db, match):
    """A ISO da partida, do cadastro (campeonato ou amistosos): a do jogo de
    campeonato, a do amistoso, a ligada pelo admin/aprendida (iso_profiles), a
    do cadastro com a mesma ISO ou nome de arquivo, ou a de campeonato com a
    mesma ISO. None: o agente usa a que tiver com o nome da sala."""
    from .live import _catalog_iso as catalog_of
    if match["tournament_match_id"]:
        row = db.execute("SELECT s.tournament_id FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
                         "WHERE m.id = ?", (match["tournament_match_id"],)).fetchone()
        found = row and _tournament_iso(db, row["tournament_id"])
        if found:
            return found
    if match["friendly_id"]:
        row = db.execute("SELECT iso_tournament_id, iso_catalog_id FROM friendly_matches WHERE id = ?",
                         (match["friendly_id"],)).fetchone()
        if row and row["iso_tournament_id"]:
            found = _tournament_iso(db, row["iso_tournament_id"])
            if found:
                return found
        if row and row["iso_catalog_id"]:
            found = _catalog_iso(db, row["iso_catalog_id"])
            if found:
                return found
    profile = db.execute("SELECT tournament_id, catalog_iso_id FROM iso_profiles WHERE content_id = ? COLLATE NOCASE",
                         (match["content_id"],)).fetchone()
    if profile and profile["catalog_iso_id"]:
        found = _catalog_iso(db, profile["catalog_iso_id"])
        if found:
            return found
    catalog_id = catalog_of(db, match)
    if catalog_id:
        found = _catalog_iso(db, catalog_id)
        if found:
            return found
    if profile and profile["tournament_id"]:
        found = _tournament_iso(db, profile["tournament_id"])
        if found:
            return found
    row = db.execute("SELECT tournament_id FROM tournament_isos WHERE content_id = ? COLLATE NOCASE LIMIT 1",
                     (match["content_id"],)).fetchone() if match["content_id"] else None
    return _tournament_iso(db, row["tournament_id"]) if row else None


def all_isos(db):
    """O cadastro inteiro (campeonatos e amistosos liberados), para o agente
    manter as ISOs atualizadas: a mesma referência com outro MD5 = arquivo novo."""
    found = [_tournament_iso(db, row["tournament_id"]) for row in db.execute("SELECT tournament_id FROM tournament_isos")]
    found += [_catalog_iso(db, row["id"]) for row in db.execute(
        "SELECT id FROM catalog_isos WHERE is_active = 1 AND approved_at IS NOT NULL")]
    return [iso for iso in found if iso]


def _meta(db, match, replay=False):
    """Título, descrição e o link do card para a live no YouTube (reprise: a
    partida já encerrada, com "Reprise" e a data dela)."""
    from .accounts import absolute_url
    from .live import card
    info = card(db, match)
    sides = [" / ".join(person["name"] for person in view["people"]) for view in info["teams"]]
    players = " x ".join(sides)
    context = " - ".join(info["context"])
    version = settings()["version"]
    title = ("Reprise: " if replay else "") + players
    for extra in (context, "WE Camp"):
        if extra and len(title) + 3 + len(extra) <= TITLE_MAX:
            title += " | " + extra
    card_url = absolute_url(url_for("live.index", partida=match["id"]))
    if replay:
        lines = ["Reprise da partida de %s: %s%s" % (info["when"], players, " - " + context if context else ""), "",
                 "Partida do portal WE Camp, jogada no emulador RetroArch TIERES V.%s com Kaillera Netplay." % version,
                 "", "Portal WE Camp: " + absolute_url("/"), "Placar da partida: " + card_url]
    else:
        lines = [players + (" - " + context if context else ""), "",
                 "Partida transmitida ao vivo pelo portal WE Camp, jogada no emulador RetroArch TIERES V.%s "
                 "com Kaillera Netplay." % version, "",
                 "Portal WE Camp: " + absolute_url("/"),
                 "Placar ao vivo da partida: " + card_url]
    return _clean(title, TITLE_MAX), _clean("\n".join(lines), 4900), card_url


def _start_job(db, broadcast, match):
    title, description, card_url = _meta(db, match, bool(broadcast["replay"]))
    return {"id": broadcast["id"], "action": "start", "session_id": broadcast["session_id"], "room": match["room"],
            "game_file": _file_name(match), "start_frame": broadcast["start_frame"],
            # Partida que terminou antes de o agente confirmar: o quadro do fim já vem aqui
            # (a reprise não tem: vai até o fim do replay).
            "stop_frame": broadcast["stop_frame"], "replay": bool(broadcast["replay"]), "title": title,
            "description": description, "privacy": settings()["privacy"], "card_url": card_url,
            "iso": iso_for_match(db, match)}


def _file_name(match):
    from .live import _file_name as file_name
    return file_name(match)


# --- API do agente -----------------------------------------------------------------

def _agent():
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    if not token:
        return None
    return get_db().execute("SELECT * FROM broadcast_agents WHERE token_hash = ?", (_hash(token),)).fetchone()


def _apply_reports(db, agent, jobs):
    """O que o agente conta das transmissões dele: começou (live, com o link
    do YouTube), acabou (ended) ou deu erro (failed)."""
    reported = set()
    for job in jobs:
        try:
            job_id = int(job.get("id"))
        except (TypeError, ValueError):
            continue
        row = db.execute("SELECT * FROM broadcasts WHERE id = ? AND agent_id = ?", (job_id, agent["id"])).fetchone()
        if row is None:
            continue
        reported.add(job_id)
        state = str(job.get("state") or "")
        youtube_id = str(job.get("youtube_id") or "")[:32] or row["youtube_id"]
        youtube_url = str(job.get("youtube_url") or "")[:200] or row["youtube_url"]
        error = str(job.get("error") or "")[:1000]
        stamp = now()
        if state in ("starting", "live") and row["status"] in ACTIVE:
            db.execute("UPDATE broadcasts SET status = ?, youtube_id = ?, youtube_url = ?, "
                       "started_at = CASE WHEN ? = 'live' THEN COALESCE(started_at, ?) ELSE started_at END, "
                       "updated_at = ? WHERE id = ?",
                       ("live" if state == "live" else row["status"], youtube_id, youtube_url, state, stamp, stamp,
                        job_id))
        elif state in ("ended", "failed"):
            status = row["status"] if row["status"] in ("cancelled", "done", "failed") else (
                "done" if state == "ended" else "failed")
            db.execute("UPDATE broadcasts SET status = ?, youtube_id = ?, youtube_url = ?, error = ?, "
                       "ended_at = COALESCE(ended_at, ?), updated_at = ? WHERE id = ?",
                       (status, youtube_id, youtube_url, error or row["error"], stamp, stamp, job_id))
        else:
            db.execute("UPDATE broadcasts SET youtube_id = ?, youtube_url = ?, updated_at = ? WHERE id = ?",
                       (youtube_id, youtube_url, stamp, job_id))
    return reported


def _stop_at(db, row, match):
    """O quadro em que a transmissão para (guardado na 1ª vez)."""
    if row["stop_frame"] is not None:
        return row["stop_frame"]
    stop = stop_frame(db, match) if match is not None else 0
    db.execute("UPDATE broadcasts SET stop_frame = ?, updated_at = ? WHERE id = ?", (stop, now(), row["id"]))
    return stop


def _live_waiting(db):
    """Uma partida ao vivo esperando vaga, já com o Stream ao vivo dela."""
    for row in db.execute("SELECT m.* FROM broadcasts b JOIN live_matches m ON m.id = b.live_match_id "
                          "WHERE b.status = 'queued' AND b.replay = 0 AND m.status = 'live'").fetchall():
        if find_session(db, row):
            return True
    return False


def _actions(db, agent, reported):
    """Para cada transmissão do agente: parar (o card terminou ou foi cancelada);
    as perdidas (não falou delas há LOST) falham; uma reprise dá a vaga a uma
    partida ao vivo; e, com vaga, as próximas da fila (as ao vivo antes das reprises)."""
    actions = []
    running = []
    for row in db.execute("SELECT * FROM broadcasts WHERE agent_id = ? AND status IN ('assigned', 'live', 'cancelled') "
                          "AND ended_at IS NULL ORDER BY id", (agent["id"],)).fetchall():
        lost = row["id"] not in reported and (row["updated_at"] or "") < _ago(LOST)
        if row["status"] == "cancelled":
            if row["id"] in reported:
                actions.append({"id": row["id"], "action": "stop", "stop_frame": 0})
            elif lost:
                db.execute("UPDATE broadcasts SET ended_at = ?, updated_at = ? WHERE id = ?", (now(), now(), row["id"]))
            continue
        if lost:
            db.execute("UPDATE broadcasts SET status = 'failed', error = 'O broadcaster deixou de informar esta "
                       "transmissão.', ended_at = ?, updated_at = ? WHERE id = ?", (now(), now(), row["id"]))
            continue
        running.append(row)
        match = db.execute("SELECT * FROM live_matches WHERE id = ?", (row["live_match_id"],)).fetchone()
        if match is not None and row["id"] not in reported:
            # Dada há pouco e o agente ainda não confirmou: manda de novo (a resposta pode ter se perdido).
            actions.append(_start_job(db, row, match))
        if (match is None or match["status"] != "live") and not row["replay"]:
            # (A reprise vai até o fim do replay: o RetroArch sai sozinho quando ele acaba.)
            actions.append({"id": row["id"], "action": "stop", "stop_frame": _stop_at(db, row, match)})
    slots = agent["slots"] if agent["slots"] is not None else 1
    free = max(0, slots - len(running))
    if not free and _live_waiting(db):
        # Uma vaga só e uma partida ao vivo esperando: a reprise em andamento para.
        replays = [row for row in running if row["replay"]]
        if replays:
            row = replays[0]
            db.execute("UPDATE broadcasts SET status = 'cancelled', stop_frame = 0, error = 'Interrompida para "
                       "transmitir uma partida ao vivo.', updated_at = ? WHERE id = ?", (now(), row["id"]))
            actions = [a for a in actions if a["id"] != row["id"]]
            actions.append({"id": row["id"], "action": "stop", "stop_frame": 0})
    for row in db.execute("SELECT * FROM broadcasts WHERE status = 'queued' "
                          "ORDER BY replay, reason = 'admin' DESC, created_at, id").fetchall():
        match = db.execute("SELECT * FROM live_matches WHERE id = ?", (row["live_match_id"],)).fetchone()
        if match is None or (match["status"] != "live" and not row["replay"]):
            db.execute("UPDATE broadcasts SET status = 'cancelled', error = 'A partida terminou antes de a transmissão "
                       "começar.', ended_at = ?, updated_at = ? WHERE id = ?", (now(), now(), row["id"]))
            continue
        if not free:
            continue
        session_id = find_session(db, match)
        if session_id is None:
            if row["replay"]:
                db.execute("UPDATE broadcasts SET status = 'failed', error = 'O replay da partida não está mais no "
                           "servidor.', ended_at = ?, updated_at = ? WHERE id = ?", (now(), now(), row["id"]))
            continue  # ao vivo: o Stream ao vivo da partida ainda não chegou - fica na fila
        stamp = now()
        # Reprise: o .krec inteiro, do quadro 0 ao fim (sem avanço rápido nem corte - Walter, 2026-11-18).
        db.execute("UPDATE broadcasts SET status = 'assigned', agent_id = ?, session_id = ?, start_frame = ?, "
                   "assigned_at = ?, updated_at = ? WHERE id = ?",
                   (agent["id"], session_id, 0 if row["replay"] else start_frame(match), stamp, stamp, row["id"]))
        actions.append(_start_job(db, db.execute("SELECT * FROM broadcasts WHERE id = ?", (row["id"],)).fetchone(), match))
        free -= 1
    return actions


@api_bp.post("/poll")
def api_poll():
    """O agente, a cada POLL_SECONDS: JSON {version, slots, status, jobs: [{id,
    state, youtube_id, youtube_url, error}]} -> {jobs: [start/stop]}."""
    agent = _agent()
    if agent is None:
        return jsonify(ok=0, error="invalid_token"), 401
    data = request.get_json(silent=True) or {}
    db = get_db()
    slots = data.get("slots")
    slots = slots if isinstance(slots, int) and 0 <= slots <= 8 else agent["slots"]
    status = data.get("status") if isinstance(data.get("status"), dict) else {}
    db.execute("UPDATE broadcast_agents SET last_seen_at = ?, version = ?, slots = ?, status_json = ? WHERE id = ?",
               (now(), str(data.get("version") or "")[:40], slots, json.dumps(status)[:4000], agent["id"]))
    agent = db.execute("SELECT * FROM broadcast_agents WHERE id = ?", (agent["id"],)).fetchone()
    jobs = data.get("jobs") if isinstance(data.get("jobs"), list) else []
    reported = _apply_reports(db, agent, [job for job in jobs if isinstance(job, dict)])
    queue_tournament_matches(db)
    actions = _actions(db, agent, reported)
    db.commit()
    return jsonify(ok=1, jobs=actions, poll_seconds=POLL_SECONDS)


@api_bp.get("/isos")
def api_isos():
    if _agent() is None:
        return jsonify(ok=0, error="invalid_token"), 401
    return jsonify(ok=1, isos=all_isos(get_db()))


# --- Card e administração ------------------------------------------------------------

def card_view(db, live_id):
    """O link do YouTube no card: ao vivo (Assistir) ou já encerrada (a gravação)."""
    row = get_for_match(db, live_id)
    if row is None:
        return None
    return {"id": row["id"], "status": row["status"], "label": STATUS_LABELS[row["status"]],
            "url": row["youtube_url"] if row["status"] in ("assigned", "live", "done") else None,
            "live": row["status"] == "live", "replay": bool(row["replay"]),
            "active": row["status"] in ("queued",) + ACTIVE}


@bp.post("/ao-vivo/<int:live_id>/transmitir")
@login_required
def toggle(live_id):
    """Administrador: "Transmitir" (transmitir=1) ou "Parar transmissão" (0) no card."""
    from .live import _redirect_back
    validate_csrf()
    db = get_db()
    if request.form.get("transmitir") == "1":
        ok, message = request_broadcast(db, live_id)
    else:
        row = get_for_match(db, live_id)
        ok = bool(row) and cancel_broadcast(db, row["id"])
        message = "Transmissão parada." if ok else "Esta partida não está sendo transmitida."
    db.commit()
    flash(message, "success" if ok else "error")
    return _redirect_back()


def _agent_views(db):
    views = []
    for row in db.execute("SELECT * FROM broadcast_agents ORDER BY name COLLATE NOCASE").fetchall():
        status = json.loads(row["status_json"] or "{}")
        views.append({"row": row, "status": status,
                      "online": bool(row["last_seen_at"]) and row["last_seen_at"] >= _ago(ONLINE)})
    return views


@bp.route("/admin/transmissoes", methods=("GET", "POST"))
@login_required
def admin_page():
    from .timeutil import format_local
    db = get_db()
    if request.method == "POST":
        validate_csrf()
        action = request.form.get("acao")
        if action == "ajustes":
            set_setting("broadcast_auto", "1" if request.form.get("auto") == "1" else "0")
            privacy = request.form.get("privacidade")
            if privacy in PRIVACY:
                set_setting("broadcast_privacy", privacy)
            version = (request.form.get("versao") or "").strip()[:20]
            if version:
                set_setting("broadcast_retroarch_version", version)
            flash("Ajustes da transmissão salvos.", "success")
        elif action == "agente":
            name = (request.form.get("nome") or "").strip()[:40]
            if not name:
                flash("Dê um nome ao broadcaster.", "error")
            else:
                token = secrets.token_urlsafe(32)
                existing = db.execute("SELECT id FROM broadcast_agents WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
                if existing:
                    db.execute("UPDATE broadcast_agents SET token_hash = ? WHERE id = ?", (_hash(token), existing["id"]))
                else:
                    db.execute("INSERT INTO broadcast_agents (name, token_hash, created_at) VALUES (?, ?, ?)",
                               (name, _hash(token), now()))
                db.commit()
                flash("Token do broadcaster %s (aparece só agora - coloque no config.json do agente): %s"
                      % (name, token), "success")
        elif action == "cancelar":
            if request.form.get("id", "").isdigit() and cancel_broadcast(db, int(request.form["id"])):
                db.commit()
                flash("Transmissão cancelada.", "success")
        return redirect(url_for("broadcaster.admin_page"))
    rows = db.execute(
        "SELECT b.*, m.players_json, m.room, m.status AS match_status, a.name AS agent_name FROM broadcasts b "
        "JOIN live_matches m ON m.id = b.live_match_id LEFT JOIN broadcast_agents a ON a.id = b.agent_id "
        "ORDER BY b.status IN ('queued', 'assigned', 'live') DESC, b.created_at DESC LIMIT 50").fetchall()
    broadcasts = [{"row": row, "players": " x ".join(json.loads(row["players_json"] or "[]")),
                   "when": format_local(row["created_at"], "%d/%m/%Y %H:%M"),
                   "status": STATUS_LABELS[row["status"]],
                   "reason": REASON_LABELS[row["reason"]] + (" (reprise)" if row["replay"] else "")} for row in rows]
    return render_template("admin/broadcaster.html", agents=_agent_views(db), broadcasts=broadcasts,
                           settings=settings(), privacy_options=PRIVACY, format_local=format_local)
