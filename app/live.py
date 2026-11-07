"""Partidas ao vivo (/ao-vivo): o placar que o RetroArch TIERES lê da RAM do
Winning Eleven e o kailleraclient.dll manda para cá durante a partida.

O fork (retroarch-k3, kaillera/kaillera_sync.c) acompanha a partida - times,
quem controla cada lado, placar, tempo e autores dos gols - e o DLL de cada PC
envia o estado (POST /api/live/match) a cada gol, troca de tempo, fim de jogo
e a cada ~15 s. Os PCs da mesma partida veem a mesma RAM (o jogo roda em
sincronia), então a partida é uma só: a chave junta a ISO, os jogadores e o
frame do pontapé inicial, que é igual em todos eles.

Cada PC tem o seu informe (live_match_reports); a partida mostra o mais
adiantado e, no fim, diz se os PCs concordaram no placar.

Contexto: com cada lado controlado por uma conta do site, a partida vai para o
próximo jogo ainda sem placar entre os dois num campeonato em andamento (ida,
depois volta); sem jogo de campeonato, para um amistoso aceito entre eles; sem
nenhum dos dois, é uma partida livre.

Nomes dos times: o jogo só tem o número do time (slot) na tabela da ISO, e os
patches nem sempre renomeiam a tabela (no CAN 2025 o slot 16 ainda se chama
FINLAND e é Camarões). Ordem: o time da inscrição de quem controla o lado
(jogo de campeonato - e a ISO aprende o slot), o cadastro por ISO
(iso_team_slots: aprendido ou do admin), o nome da RAM e, por fim, "Time N".
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from markupsafe import Markup

from .db import get_db, now
from .memcards import _api_key_ok, _api_player, _error, _reply
from .security import login_required, validate_csrf
from .tournament.teams import generated_badge

api_bp = Blueprint("live_api", __name__, url_prefix="/api/live")
bp = Blueprint("live", __name__)

# Sem informe há mais que isso: o card diz "sem sinal"; bem mais: a partida fecha.
STALE = timedelta(minutes=2)
GONE = timedelta(minutes=20)
# Partidas encerradas que ainda aparecem nos cards.
RECENT = timedelta(hours=6)
# Um PC que entrou no meio da partida (sem o pontapé inicial) junta-se à
# partida ao vivo dos mesmos jogadores/ISO atualizada há menos que isso.
JOIN_WINDOW = timedelta(minutes=10)
MAX_PLAYERS = 8
NAME_RE = re.compile(r"^[A-Za-z0-9 .'&/-]{2,24}$")


def _ago(delta):
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def _is_admin():
    return "user_id" in session


# --- Estado que chega do DLL --------------------------------------------------

def _int(value, low, high):
    value = int(value)
    if not low <= value <= high:
        raise ValueError(value)
    return value


def _text(value, cap):
    return str(value or "").strip()[:cap]


def parse_state(raw):
    """JSON do fork -> dict validado (ValueError se não serve)."""
    data = json.loads(raw)
    teams = []
    for team in data["teams"][:2]:
        scorers = []
        for item in team.get("scorers", [])[:32]:
            scorers.append([_text(item[0], 24), _int(item[1], 0, 200), _text(item[2] if len(item) > 2 else "", 24)])
        teams.append({"id": _int(team["id"], 0, 255), "name": _text(team.get("name"), 24),
                      "goals": _int(team["goals"], 0, 99), "scorers": scorers})
    if len(teams) != 2:
        raise ValueError("teams")
    return {
        "content": _text(data["content"], 32),
        "kickoff": _int(data.get("kickoff") or 0, 0, 2 ** 31),
        "frame": _int(data.get("frame") or 0, 0, 2 ** 31),
        "period": _int(data.get("period") or 0, 0, 9),
        "clock": _int(data.get("clock") or 0, 0, 65535),
        "half": _int(data.get("half") or 18000, 1, 65535),
        "final": bool(data.get("final")),
        "teams": teams,
        "pads": [_int(p, 0, 255) for p in data.get("pads", [])[:MAX_PLAYERS]],
    }


def match_key(content, players, kickoff):
    raw = "%s|%s|%d" % (content, ",".join(p.lower() for p in players), kickoff)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@api_bp.post("/match")
def api_match():
    """Estado da partida mandado pelo DLL: form com state (JSON do fork),
    players (nicks em ordem 1P, 2P...), reporter (o nick de quem manda, quando
    sem login), room (o jogo da sala) e test=1 (simulações - só o admin vê)."""
    player = _api_player()
    if player is None and not _api_key_ok():
        return _error(401, "invalid_token", "Faca login novamente.")
    try:
        state = parse_state(request.form.get("state", ""))
    except (KeyError, TypeError, ValueError, IndexError):
        return _error(400, "bad_state", "Estado da partida invalido.")
    players = [p.strip()[:32] for p in request.form.get("players", "").split(",") if p.strip()][:MAX_PLAYERS]
    reporter = player["username"] if player else _text(request.form.get("reporter"), 32)
    if not players or not reporter or not state["content"]:
        return _error(400, "bad_state", "Faltam jogadores ou o jogo.")
    db = get_db()
    live_id = ingest(db, state, players, reporter, player["id"] if player else None,
                     _text(request.form.get("room"), 120), request.form.get("test") == "1")
    db.commit()
    return _reply(ok=1, id=live_id)


def ingest(db, state, players, reporter, player_id, room, is_test=False):
    key = match_key(state["content"], players, state["kickoff"])
    row = db.execute("SELECT * FROM live_matches WHERE match_key = ?", (key,)).fetchone()
    if row is None and not state["kickoff"]:
        row = db.execute(
            "SELECT * FROM live_matches WHERE content_id = ? AND players_json = ? AND status = 'live' "
            "AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1",
            (state["content"], json.dumps(players), _ago(JOIN_WINDOW))).fetchone()
    stamp = now()
    if row is None:
        live_id = db.execute(
            "INSERT INTO live_matches (match_key, content_id, room, players_json, kickoff_frame, is_test, started_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, state["content"], room, json.dumps(players), state["kickoff"], int(is_test), stamp, stamp)).lastrowid
    else:
        live_id = row["id"]
    db.execute(
        "INSERT OR REPLACE INTO live_match_reports (live_match_id, reporter, player_id, frame, final, state_json, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (live_id, reporter, player_id, state["frame"], int(state["final"]), json.dumps(state), stamp))
    refresh(db, live_id)
    return live_id


def _sides(players, pads):
    """Nicks que controlam o LOCAL (0) e o VISITA (1): controle i = jogador i+1."""
    sides = ([], [])
    for i, side in enumerate(pads):
        if side in (0, 1) and i < len(players):
            sides[side].append(players[i])
    return sides


def refresh(db, live_id):
    """Recalcula a partida a partir dos informes dos PCs."""
    match = db.execute("SELECT * FROM live_matches WHERE id = ?", (live_id,)).fetchone()
    reports = db.execute("SELECT * FROM live_match_reports WHERE live_match_id = ? ORDER BY frame DESC, updated_at DESC",
                         (live_id,)).fetchall()
    best = json.loads(reports[0]["state_json"])
    finals = [json.loads(r["state_json"]) for r in reports if r["final"]]
    scores = {(s["teams"][0]["goals"], s["teams"][1]["goals"]) for s in finals}
    local, visita = _sides(json.loads(match["players_json"]), best["pads"])
    finished = bool(finals)
    db.execute(
        "UPDATE live_matches SET status = ?, period = ?, clock_left = ?, half_frames = ?, local_slot = ?, visita_slot = ?, "
        "local_goals = ?, visita_goals = ?, state_json = ?, local_players_json = ?, visita_players_json = ?, "
        "reporters = ?, agreed = ?, updated_at = ?, finished_at = COALESCE(finished_at, ?) WHERE id = ?",
        ("finished" if finished else "live", best["period"], best["clock"], best["half"], best["teams"][0]["id"],
         best["teams"][1]["id"], best["teams"][0]["goals"], best["teams"][1]["goals"], json.dumps(best),
         json.dumps(local), json.dumps(visita), len(reports), int(len(finals) >= 2 and len(scores) == 1), now(),
         now() if finished else None, live_id))
    for side, team in enumerate(best["teams"]):
        ram_name = team["name"] if NAME_RE.match(team["name"]) else ""
        db.execute("INSERT OR IGNORE INTO iso_team_slots (content_id, slot, updated_at) VALUES (?, ?, ?)",
                   (match["content_id"], team["id"], now()))
        db.execute("UPDATE iso_team_slots SET room = ?, ram_name = CASE WHEN ? != '' THEN ? ELSE ram_name END "
                   "WHERE content_id = ? AND slot = ?",
                   (match["room"], ram_name, ram_name, match["content_id"], team["id"]))
    match = db.execute("SELECT * FROM live_matches WHERE id = ?", (live_id,)).fetchone()
    if match["tournament_match_id"] is None and match["friendly_id"] is None and len(local) == 1 and len(visita) == 1:
        detect_context(db, match, local[0], visita[0])


# --- Campeonato, amistoso ou partida livre ------------------------------------

def _player_id(db, nick):
    row = db.execute("SELECT id FROM players WHERE username = ? COLLATE NOCASE", (nick,)).fetchone()
    return row["id"] if row else None


def detect_context(db, match, local_nick, visita_nick):
    """Liga a partida ao próximo jogo de campeonato entre os dois (ainda sem
    placar e sem outra partida ao vivo ligada) ou a um amistoso aceito. As
    simulações (is_test) só disputam os jogos entre si e não ensinam slots."""
    a, b = _player_id(db, local_nick), _player_id(db, visita_nick)
    if not a or not b or a == b:
        return
    taken = ("SELECT tournament_match_id FROM live_matches WHERE tournament_match_id IS NOT NULL AND id != ? "
             "AND is_test = ?")
    row = db.execute(
        "SELECT m.id FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
        "JOIN tournaments t ON t.id = s.tournament_id "
        "JOIN tournament_entries he ON he.id = m.home_entry_id JOIN tournament_entries ae ON ae.id = m.away_entry_id "
        "WHERE t.status = 'running' AND s.status = 'running' AND m.status != 'confirmed' "
        "AND ((he.player_id = ? AND ae.player_id = ?) OR (he.player_id = ? AND ae.player_id = ?)) "
        f"AND m.id NOT IN ({taken}) ORDER BY s.seq, m.round, m.leg, m.id LIMIT 1",
        (a, b, b, a, match["id"], match["is_test"])).fetchone()
    if row:
        db.execute("UPDATE live_matches SET tournament_match_id = ? WHERE id = ?", (row["id"], match["id"]))
        if not match["is_test"]:
            learn_slots(db, match["id"])
        return
    row = db.execute(
        "SELECT id FROM friendly_matches WHERE status IN ('accepted', 'reported', 'suspended') "
        "AND ((home_id = ? AND away_id = ?) OR (home_id = ? AND away_id = ?)) "
        "AND id NOT IN (SELECT friendly_id FROM live_matches WHERE friendly_id IS NOT NULL AND id != ? AND is_test = ?) "
        "ORDER BY created_at LIMIT 1", (a, b, b, a, match["id"], match["is_test"])).fetchone()
    if row:
        db.execute("UPDATE live_matches SET friendly_id = ? WHERE id = ?", (row["id"], match["id"]))


def _entry_team(db, match, nick):
    """Time inscrito no campeonato por quem controla um lado: (nome, linha de teams)."""
    row = db.execute(
        "SELECT e.team_name, e.team_id FROM tournament_matches m JOIN tournament_entries e "
        "ON e.id IN (m.home_entry_id, m.away_entry_id) JOIN players p ON p.id = e.player_id "
        "WHERE m.id = ? AND p.username = ? COLLATE NOCASE", (match["tournament_match_id"], nick)).fetchone()
    if not row or not row["team_name"]:
        return None, None
    team = db.execute("SELECT * FROM teams WHERE id = ?", (row["team_id"],)).fetchone() if row["team_id"] else None
    return row["team_name"], team


def learn_slots(db, live_id):
    """Jogo de campeonato: o time inscrito de quem controla cada lado ensina o
    nome do slot desta ISO (sem mexer no que o admin cadastrou)."""
    match = db.execute("SELECT * FROM live_matches WHERE id = ?", (live_id,)).fetchone()
    for slot, nicks in ((match["local_slot"], json.loads(match["local_players_json"])),
                        (match["visita_slot"], json.loads(match["visita_players_json"]))):
        if slot is None or len(nicks) != 1:
            continue
        name, team = _entry_team(db, match, nicks[0])
        if name:
            db.execute("UPDATE iso_team_slots SET name = ?, team_id = ?, source = 'aprendido', updated_at = ? "
                       "WHERE content_id = ? AND slot = ? AND source != 'admin'",
                       (name, team["id"] if team else None, now(), match["content_id"], slot))


# --- Cards ----------------------------------------------------------------------

def reap(db):
    """Partidas sem informe há muito tempo: encerradas (se paradas no fim do
    2º tempo) ou abandonadas."""
    db.execute("UPDATE live_matches SET status = CASE WHEN period = 1 AND clock_left = 0 THEN 'finished' "
               "ELSE 'abandoned' END, finished_at = COALESCE(finished_at, updated_at) "
               "WHERE status = 'live' AND updated_at < ?", (_ago(GONE),))
    db.commit()


def _team_view(db, match, side, team_state, nicks):
    from .tournaments import team_image
    slot = team_state.get("id")
    name, team = (None, None)
    if match["tournament_match_id"] and len(nicks) == 1:
        name, team = _entry_team(db, match, nicks[0])
    row = None
    if not name:
        row = db.execute("SELECT s.*, t.name AS team_name FROM iso_team_slots s LEFT JOIN teams t ON t.id = s.team_id "
                         "WHERE s.content_id = ? AND s.slot = ?", (match["content_id"], slot)).fetchone()
        if row and (row["name"] or row["team_id"]):
            name = row["name"] or row["team_name"]
            team = db.execute("SELECT * FROM teams WHERE id = ?", (row["team_id"],)).fetchone() if row["team_id"] else None
    if not name:
        # A tabela de nomes nem sempre está na RAM (no 2º tempo some): vale o último nome visto deste slot.
        ram = team_state.get("name", "")
        ram = ram if NAME_RE.match(ram) else (row["ram_name"] if row else "")
        name = ram.title() if ram else "Time %s" % slot
    image = team_image(team) if team else generated_badge(name)
    return {"name": name, "image": image, "goals": team_state.get("goals", 0), "players": nicks,
            "scorers": team_state.get("scorers", [])}


def _clock_label(match):
    period, left, half = match["period"], match["clock_left"], match["half_frames"] or 18000
    if match["status"] != "live":
        return "Encerrado" if match["status"] == "finished" else "Interrompido"
    if period > 1:
        return "Prorrogação"
    if left == 0:
        return "%dº tempo · %d+'" % (period + 1, 45 * (period + 1))
    return "%dº tempo · %d'" % (period + 1, int(45 * (1 - left / half)) + 45 * period)


def _context(db, match):
    """(rótulo, link) do campeonato/amistoso da partida."""
    if match["tournament_match_id"]:
        row = db.execute(
            "SELECT m.round, m.leg, m.id, s.name AS stage_name, s.kind, t.name, t.slug, tie.legs FROM tournament_matches m "
            "JOIN tournament_stages s ON s.id = m.stage_id JOIN tournaments t ON t.id = s.tournament_id "
            "LEFT JOIN tournament_ties tie ON tie.id = m.tie_id WHERE m.id = ?", (match["tournament_match_id"],)).fetchone()
        if row:
            parts = [row["name"], row["stage_name"]]
            if row["kind"] != "knockout":
                parts.append("Rodada %d" % row["round"])
            if row["legs"] == 2:
                parts.append("Ida" if row["leg"] == 1 else "Volta")
            return " · ".join(p for p in parts if p), url_for("tournaments.match", slug=row["slug"], match_id=row["id"])
    if match["friendly_id"]:
        return "Amistoso", url_for("tournaments.friendly_show", friendly_id=match["friendly_id"])
    return "Partida livre", None


def card(db, match):
    state = json.loads(match["state_json"] or "{}")
    teams = state.get("teams") or [{}, {}]
    sides = (json.loads(match["local_players_json"]), json.loads(match["visita_players_json"]))
    label, link = _context(db, match)
    stale = match["status"] == "live" and match["updated_at"] < _ago(STALE)
    return {"id": match["id"], "status": match["status"], "stale": stale, "is_test": match["is_test"],
            "clock": "Sem sinal" if stale else _clock_label(match), "context": label, "link": link,
            "agreed": match["agreed"], "reporters": match["reporters"],
            "game": re.sub(r"^PCSX ReARMed:\s*", "", match["room"] or ""),
            "teams": [_team_view(db, match, i, teams[i] if i < len(teams) else {}, sides[i]) for i in (0, 1)]}


def list_cards(db, tournament_id=None, only_live=False, limit=20):
    reap(db)
    where = ["(m.status = 'live' OR m.finished_at >= ?)"]
    params = [_ago(RECENT)]
    if only_live:
        where = ["m.status = 'live'"]
        params = []
    if not _is_admin():
        where.append("m.is_test = 0")
    join = ""
    if tournament_id:
        join = "JOIN tournament_matches tm ON tm.id = m.tournament_match_id JOIN tournament_stages ts ON ts.id = tm.stage_id"
        where.append("ts.tournament_id = ?")
        params.append(tournament_id)
    rows = db.execute(
        f"SELECT m.* FROM live_matches m {join} WHERE {' AND '.join(where)} "
        "ORDER BY m.status = 'live' DESC, m.updated_at DESC LIMIT ?", (*params, limit)).fetchall()
    return [card(db, row) for row in rows]


@bp.get("/ao-vivo")
def index():
    return render_template("live/index.html", cards=list_cards(get_db()), is_admin=_is_admin())


@bp.get("/ao-vivo/cards")
def cards_fragment():
    """Só os cards (a página atualiza com isto a cada poucos segundos)."""
    cards = list_cards(get_db(), tournament_id=request.args.get("campeonato", type=int),
                       only_live=request.args.get("so_ao_vivo") == "1")
    return render_template("live/_cards.html", cards=cards)


@bp.post("/ao-vivo/limpar-testes")
@login_required
def clear_tests():
    validate_csrf()
    db = get_db()
    count = db.execute("DELETE FROM live_matches WHERE is_test = 1").rowcount
    db.commit()
    flash("%d partida(s) de teste apagada(s)." % count, "success")
    return redirect(url_for("live.index"))


# --- Admin: times de cada ISO ---------------------------------------------------

@bp.route("/admin/ao-vivo/times", methods=("GET", "POST"))
@login_required
def admin_slots():
    db = get_db()
    if request.method == "POST":
        validate_csrf()
        content_id, slot = request.form.get("content_id", ""), request.form.get("slot", type=int)
        name = request.form.get("name", "").strip()[:40]
        team = db.execute("SELECT id, name FROM teams WHERE name = ? COLLATE NOCASE", (name,)).fetchone() if name else None
        db.execute("UPDATE iso_team_slots SET name = ?, team_id = ?, source = ?, updated_at = ? "
                   "WHERE content_id = ? AND slot = ?",
                   (team["name"] if team else name, team["id"] if team else None, "admin" if name else "", now(),
                    content_id, slot))
        db.commit()
        flash("Time salvo." if name else "Cadastro do slot apagado.", "success")
        return redirect(url_for("live.admin_slots"))
    rows = db.execute("SELECT s.*, t.name AS team_name FROM iso_team_slots s LEFT JOIN teams t ON t.id = s.team_id "
                      "ORDER BY s.room, s.content_id, s.slot").fetchall()
    groups = {}
    for row in rows:
        groups.setdefault((row["content_id"], re.sub(r"^PCSX ReARMed:\s*", "", row["room"] or "")), []).append(row)
    teams = [r["name"] for r in db.execute("SELECT DISTINCT name FROM teams WHERE is_active = 1 ORDER BY name")]
    return render_template("live/admin_slots.html", groups=groups, teams=teams)


@bp.app_template_filter("scorer")
def scorer_line(item):
    name, minute, assist = item[0], item[1], item[2] if len(item) > 2 else ""
    return Markup("{} {}'{}").format(name, minute, Markup(' <small>(assist. {})</small>').format(assist) if assist else "")
