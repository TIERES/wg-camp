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
# Motor do jogo (executável que o fork lê na RAM) -> jogo base (game_titles.code):
# o logo do jogo base mesmo com uma ISO que o site não conhece.
ENGINE_GAMES = {"SLPM_870.56": "we2002", "SLPM_862.91": "we4", "SLPM_861.62": "we3fv"}
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
    """JSON do fork -> dict validado (ValueError se não serve). Do v2 em
    diante o nome de cada time vem da tela de seleção de times (o que os
    jogadores viram, ex. "PAL99"), com o código de 3 letras; no v1 vinha de
    uma tabela que muitos patches não renomeiam - não vale."""
    data = json.loads(raw)
    version = _int(data.get("v") or 1, 1, 99)
    teams = []
    for team in data["teams"][:2]:
        scorers = []
        for item in team.get("scorers", [])[:32]:
            scorers.append([_text(item[0], 24), _int(item[1], 0, 200), _text(item[2] if len(item) > 2 else "", 24)])
        teams.append({"id": _int(team["id"], 0, 255), "name": _text(team.get("name"), 24) if version >= 2 else "",
                      "code": _text(team.get("code"), 3), "goals": _int(team["goals"], 0, 99), "scorers": scorers})
    if len(teams) != 2:
        raise ValueError("teams")
    return {
        "v": version,
        "engine": _text(data.get("engine"), 16),
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
        # Partida nova dos mesmos jogadores e ISO: a anterior que ficou aberta foi
        # abandonada (WE4: saíram pelo menu de pausa; o fork não manda o fim dela).
        db.execute("UPDATE live_matches SET status = 'abandoned', finished_at = ? WHERE status = 'live' "
                   "AND content_id = ? AND players_json = ? AND kickoff_frame != ? AND ? != 0",
                   (stamp, state["content"], json.dumps(players), state["kickoff"], state["kickoff"]))
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
        ("finished" if finished else ("abandoned" if match["status"] == "abandoned" else "live"), best["period"], best["clock"], best["half"], best["teams"][0]["id"],
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
    content = match["content_id"]
    taken = ("SELECT tournament_match_id FROM live_matches WHERE tournament_match_id IS NOT NULL AND id != ? "
             "AND is_test = ?")
    # A ISO tem que ser a do campeonato (content_id dela ou uma ligada pelo
    # admin/aprendida em iso_profiles); campeonato sem ISO identificada vale.
    row = db.execute(
        "SELECT m.id FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
        "JOIN tournaments t ON t.id = s.tournament_id LEFT JOIN tournament_isos ti ON ti.tournament_id = t.id "
        "JOIN tournament_entries he ON he.id = m.home_entry_id JOIN tournament_entries ae ON ae.id = m.away_entry_id "
        "WHERE t.status = 'running' AND s.status = 'running' AND m.status != 'confirmed' "
        "AND ((he.player_id = ? AND ae.player_id = ?) OR (he.player_id = ? AND ae.player_id = ?)) "
        "AND (ti.content_id IS NULL OR ti.content_id = ? COLLATE NOCASE OR t.id IN "
        "(SELECT tournament_id FROM iso_profiles WHERE content_id = ? COLLATE NOCASE)) "
        f"AND m.id NOT IN ({taken}) ORDER BY s.seq, m.round, m.leg, m.id LIMIT 1",
        (a, b, b, a, content, content, match["id"], match["is_test"])).fetchone()
    if row:
        db.execute("UPDATE live_matches SET tournament_match_id = ? WHERE id = ?", (row["id"], match["id"]))
        if not match["is_test"]:
            learn_slots(db, match["id"])
        return
    # Amistoso: a ISO escolhida no convite (a de um campeonato ou uma do
    # cadastro) tem que bater; sem ISO escolhida ou sem identificação, vale.
    row = db.execute(
        "SELECT f.id FROM friendly_matches f LEFT JOIN tournament_isos ti ON ti.tournament_id = f.iso_tournament_id "
        "LEFT JOIN catalog_isos ci ON ci.id = f.iso_catalog_id "
        "WHERE f.status IN ('accepted', 'reported', 'suspended') "
        "AND ((f.home_id = ? AND f.away_id = ?) OR (f.home_id = ? AND f.away_id = ?)) "
        "AND (COALESCE(ti.content_id, ci.content_id) IS NULL OR COALESCE(ti.content_id, ci.content_id) = ? COLLATE NOCASE "
        "OR EXISTS (SELECT 1 FROM iso_profiles p WHERE p.content_id = ? COLLATE NOCASE "
        "AND (p.tournament_id = f.iso_tournament_id OR p.catalog_iso_id = f.iso_catalog_id))) "
        "AND f.id NOT IN (SELECT friendly_id FROM live_matches WHERE friendly_id IS NOT NULL AND id != ? AND is_test = ?) "
        "ORDER BY f.created_at LIMIT 1", (a, b, b, a, content, content, match["id"], match["is_test"])).fetchone()
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
    nome do slot desta ISO, e o campeonato ensina o jogo base e o campeonato
    da ISO (sem mexer no que o admin cadastrou)."""
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
    tournament = db.execute(
        "SELECT t.id, t.game_title_id FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
        "JOIN tournaments t ON t.id = s.tournament_id WHERE m.id = ?", (match["tournament_match_id"],)).fetchone()
    if tournament:
        db.execute("INSERT OR IGNORE INTO iso_profiles (content_id, updated_at) VALUES (?, ?)",
                   (match["content_id"], now()))
        db.execute("UPDATE iso_profiles SET game_title_id = ?, tournament_id = ?, source = 'aprendido', updated_at = ? "
                   "WHERE content_id = ? AND source != 'admin'",
                   (tournament["game_title_id"], tournament["id"], now(), match["content_id"]))


# --- Cards ----------------------------------------------------------------------

UNKNOWN_TEAM = "Não identificado"
CARDS_LIMIT = 10


def reap(db):
    """Partidas sem informe há muito tempo: encerradas (se paradas no fim do
    2º tempo) ou abandonadas."""
    db.execute("UPDATE live_matches SET status = CASE WHEN period = 1 AND clock_left = 0 THEN 'finished' "
               "ELSE 'abandoned' END, finished_at = COALESCE(finished_at, updated_at) "
               "WHERE status = 'live' AND updated_at < ?", (_ago(GONE),))
    db.commit()


def _team_view(db, match, side, team_state, nicks):
    """Time de um lado: o da inscrição (jogo de campeonato), o cadastrado para
    o slot desta ISO ou o nome da tela de seleção de times (estado v2, ou o
    último visto deste slot); sem nenhum, "Não identificado"."""
    from .tournaments import team_image
    slot = team_state.get("id")
    name, team = (None, None)
    if match["tournament_match_id"] and len(nicks) == 1:
        name, team = _entry_team(db, match, nicks[0])
    if not name:
        row = db.execute("SELECT s.*, t.name AS team_name FROM iso_team_slots s LEFT JOIN teams t ON t.id = s.team_id "
                         "WHERE s.content_id = ? AND s.slot = ?", (match["content_id"], slot)).fetchone()
        if row and (row["name"] or row["team_id"]):
            name = row["name"] or row["team_name"]
            team = db.execute("SELECT * FROM teams WHERE id = ?", (row["team_id"],)).fetchone() if row["team_id"] else None
        elif NAME_RE.match(team_state.get("name") or ""):
            name = team_state["name"]
        elif row and row["ram_name"]:
            name = row["ram_name"]
    image = team_image(team) if team else generated_badge(name or "?")
    return {"name": name or UNKNOWN_TEAM, "known": bool(name), "image": image, "goals": team_state.get("goals", 0),
            "players": list(nicks), "scorers": team_state.get("scorers", [])}


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


def _file_name(match):
    return re.sub(r"^PCSX ReARMed:\s*", "", match["room"] or "")


def _profile(db, match):
    """Jogo base e campeonato da ISO da partida (os logos do card): o do jogo
    de campeonato ligado, o do amistoso, o cadastrado/aprendido para a ISO ou
    o da ISO de campeonato/amistoso com o mesmo nome de arquivo."""
    game_id = tournament_id = None
    if match["tournament_match_id"]:
        row = db.execute("SELECT t.id, t.game_title_id FROM tournament_matches m JOIN tournament_stages s "
                         "ON s.id = m.stage_id JOIN tournaments t ON t.id = s.tournament_id WHERE m.id = ?",
                         (match["tournament_match_id"],)).fetchone()
        if row:
            game_id, tournament_id = row["game_title_id"], row["id"]
    if game_id is None and match["friendly_id"]:
        row = db.execute("SELECT game_title_id, iso_tournament_id FROM friendly_matches WHERE id = ?",
                         (match["friendly_id"],)).fetchone()
        if row:
            game_id, tournament_id = row["game_title_id"], row["iso_tournament_id"]
    if game_id is None:
        row = db.execute("SELECT * FROM iso_profiles WHERE content_id = ?", (match["content_id"],)).fetchone()
        if row:
            game_id, tournament_id = row["game_title_id"], row["tournament_id"]
            if game_id is None and row["catalog_iso_id"]:
                catalog = db.execute("SELECT game_title_id FROM catalog_isos WHERE id = ?",
                                     (row["catalog_iso_id"],)).fetchone()
                game_id = catalog["game_title_id"] if catalog else None
    if game_id is None:
        # A ISO exata (CRC32:TAMANHO) de um campeonato ou do cadastro de amistosos.
        row = db.execute("SELECT t.id, t.game_title_id FROM tournament_isos i JOIN tournaments t ON t.id = i.tournament_id "
                         "WHERE i.content_id = ? COLLATE NOCASE AND t.status != 'cancelled' "
                         "ORDER BY t.updated_at DESC LIMIT 1", (match["content_id"],)).fetchone()
        if row:
            game_id, tournament_id = row["game_title_id"], row["id"]
        else:
            row = db.execute("SELECT game_title_id FROM catalog_isos WHERE content_id = ? COLLATE NOCASE "
                             "AND is_active = 1 LIMIT 1", (match["content_id"],)).fetchone()
            game_id = row["game_title_id"] if row else None
    if game_id is None:
        engine = json.loads(match["state_json"] or "{}").get("engine")
        row = db.execute("SELECT id FROM game_titles WHERE code = ?", (ENGINE_GAMES.get(engine),)).fetchone()
        game_id = row["id"] if row else None
    file_name = _file_name(match)
    if game_id is None and file_name:
        row = db.execute("SELECT t.id, t.game_title_id FROM tournament_isos i JOIN tournaments t ON t.id = i.tournament_id "
                         "WHERE i.original_filename = ? COLLATE NOCASE AND t.status != 'cancelled' "
                         "ORDER BY t.updated_at DESC LIMIT 1", (file_name,)).fetchone()
        if row:
            game_id, tournament_id = row["game_title_id"], row["id"]
        else:
            row = db.execute("SELECT game_title_id FROM catalog_isos WHERE original_filename = ? COLLATE NOCASE "
                             "AND is_active = 1 LIMIT 1", (file_name,)).fetchone()
            game_id = row["game_title_id"] if row else None
    game = db.execute("SELECT name, code FROM game_titles WHERE id = ?", (game_id,)).fetchone() if game_id else None
    tournament = db.execute("SELECT name, slug, image FROM tournaments WHERE id = ?",
                            (tournament_id,)).fetchone() if tournament_id else None
    return game, tournament


def card(db, match):
    from .images import url as image_url
    from .timeutil import format_local
    from .tournament.games import cover_url
    state = json.loads(match["state_json"] or "{}")
    teams = state.get("teams") or [{}, {}]
    sides = (json.loads(match["local_players_json"]), json.loads(match["visita_players_json"]))
    label, link = _context(db, match)
    stale = match["status"] == "live" and match["updated_at"] < _ago(STALE)
    game, tournament = _profile(db, match)
    views = [_team_view(db, match, i, teams[i] if i < len(teams) else {}, sides[i]) for i in (0, 1)]
    # Lado sem controle: a CPU (o outro lado tem jogador) ou, sem a tabela de controles, "Player N".
    nobody = not any(view["players"] for view in views)
    for number, view in enumerate(views, 1):
        if not view["players"]:
            view["players"] = ["Player %d" % number] if nobody else ["CPU"]
    return {"id": match["id"], "status": match["status"], "stale": stale, "is_test": match["is_test"],
            "clock": "Sem sinal" if stale else _clock_label(match), "context": label, "link": link,
            "agreed": match["agreed"], "reporters": match["reporters"], "file": _file_name(match),
            "when": format_local(match["started_at"], "%d/%m/%Y %H:%M"),
            "game": {"name": game["name"], "cover": cover_url(game["code"])} if game else None,
            "tournament": {"name": tournament["name"], "image": image_url("torneio", tournament["image"]),
                           "url": url_for("tournaments.show", slug=tournament["slug"])} if tournament else None,
            "teams": views}


def _local_day(value, end=False):
    """AAAA-MM-DD (dia em São Paulo) -> início do dia (ou do dia seguinte) em UTC ISO."""
    from .timeutil import LOCAL_TZ
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    moment = day.replace(tzinfo=LOCAL_TZ) + (timedelta(days=1) if end else timedelta())
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def list_cards(db, tournament_id=None, friendlies=False, only_live=False, recent_only=False, player="", date_from="",
               date_to="", limit=CARDS_LIMIT):
    """As últimas partidas (ao vivo primeiro), com os filtros da página:
    tournament_id = os jogos daquele campeonato; friendlies = o que não é jogo
    de campeonato (amistosos cadastrados e partidas livres)."""
    reap(db)
    where, params = [], []
    if only_live:
        where.append("m.status = 'live'")
    elif recent_only:
        where.append("(m.status = 'live' OR m.finished_at >= ?)")
        params.append(_ago(RECENT))
    if not _is_admin():
        where.append("m.is_test = 0")
    if tournament_id:
        where.append("m.tournament_match_id IN (SELECT tm.id FROM tournament_matches tm JOIN tournament_stages ts "
                     "ON ts.id = tm.stage_id WHERE ts.tournament_id = ?)")
        params.append(tournament_id)
    elif friendlies:
        where.append("m.tournament_match_id IS NULL")
    if player:
        where.append("m.players_json LIKE ?")
        params.append("%" + player.replace("%", "").replace("_", "") + "%")
    start, end = _local_day(date_from), _local_day(date_to, end=True)
    if start:
        where.append("m.started_at >= ?")
        params.append(start)
    if end:
        where.append("m.started_at < ?")
        params.append(end)
    rows = db.execute(
        "SELECT m.* FROM live_matches m %s ORDER BY m.status = 'live' DESC, m.started_at DESC LIMIT ?"
        % ("WHERE " + " AND ".join(where) if where else ""), (*params, limit)).fetchall()
    return [card(db, row) for row in rows]


FRIENDLIES = "amistosos"  # o valor de "Amistosos" no filtro de campeonato


def _filters():
    args = request.args
    championship = args.get("campeonato", "")
    return {"player": args.get("jogador", "").strip()[:32], "friendlies": championship == FRIENDLIES,
            "tournament_id": int(championship) if championship.isdigit() else None,
            "date_from": args.get("de", ""), "date_to": args.get("ate", "")}


@bp.get("/ao-vivo")
def index():
    db = get_db()
    filters = _filters()
    tournaments = db.execute(
        "SELECT t.id, t.name FROM tournaments t WHERE t.id IN ("
        "SELECT s.tournament_id FROM live_matches m JOIN tournament_matches tm ON tm.id = m.tournament_match_id "
        "JOIN tournament_stages s ON s.id = tm.stage_id) ORDER BY t.name COLLATE NOCASE").fetchall()
    return render_template("live/index.html", cards=list_cards(db, **filters), filters=filters,
                           tournaments=tournaments, is_admin=_is_admin(), limit=CARDS_LIMIT,
                           cards_url=url_for("live.cards_fragment", **request.args.to_dict()))


@bp.get("/ao-vivo/cards")
def cards_fragment():
    """Só os cards (as páginas atualizam com isto a cada poucos segundos): a
    inicial pede so_ao_vivo=1, a do campeonato as recentes dele (recentes=1)."""
    recent = request.args.get("recentes") == "1"
    cards = list_cards(get_db(), only_live=request.args.get("so_ao_vivo") == "1", recent_only=recent,
                       limit=4 if recent else CARDS_LIMIT, **_filters())
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


# --- Admin: times, jogo base e campeonato de cada ISO ----------------------------

def _save_slot(db, content_id, slot, name):
    """Nome de um slot: um time da base (com escudo/bandeira) ou um nome livre; vazio apaga."""
    team = db.execute("SELECT id, name FROM teams WHERE name = ? COLLATE NOCASE", (name,)).fetchone() if name else None
    db.execute("UPDATE iso_team_slots SET name = ?, team_id = ?, source = ?, updated_at = ? "
               "WHERE content_id = ? AND slot = ?",
               (team["name"] if team else name, team["id"] if team else None, "admin" if name else "", now(),
                content_id, slot))


@bp.route("/admin/ao-vivo/times", methods=("GET", "POST"))
@login_required
def admin_slots():
    db = get_db()
    if request.method == "POST":
        validate_csrf()
        content_id, action = request.form.get("content_id", ""), request.form.get("acao", "slot")
        if action == "perfil":
            game_id = request.form.get("game_title_id", type=int) or None
            tournament_id = request.form.get("tournament_id", type=int) or None
            catalog_id = request.form.get("catalog_iso_id", type=int) or None
            db.execute("INSERT OR IGNORE INTO iso_profiles (content_id, updated_at) VALUES (?, ?)", (content_id, now()))
            db.execute("UPDATE iso_profiles SET game_title_id = ?, tournament_id = ?, catalog_iso_id = ?, source = ?, "
                       "updated_at = ? WHERE content_id = ?",
                       (game_id, tournament_id, catalog_id, "admin" if game_id or tournament_id or catalog_id else "",
                        now(), content_id))
            flash("Jogo base e campeonato da ISO salvos.", "success")
        elif action == "nomes_do_jogo":
            rows = db.execute("SELECT slot, ram_name FROM iso_team_slots WHERE content_id = ? AND ram_name != '' "
                              "AND name = '' AND team_id IS NULL", (content_id,)).fetchall()
            for row in rows:
                _save_slot(db, content_id, row["slot"], row["ram_name"].title())
            flash("%d slot(s) com o nome do jogo." % len(rows), "success")
        else:
            _save_slot(db, content_id, request.form.get("slot", type=int), request.form.get("name", "").strip()[:40])
            flash("Time salvo.", "success")
        db.commit()
        return redirect(url_for("live.admin_slots"))
    rows = db.execute("SELECT s.*, t.name AS team_name FROM iso_team_slots s LEFT JOIN teams t ON t.id = s.team_id "
                      "ORDER BY s.room, s.content_id, s.slot").fetchall()
    groups = {}
    for row in rows:
        groups.setdefault((row["content_id"], re.sub(r"^PCSX ReARMed:\s*", "", row["room"] or "")), []).append(row)
    profiles = {row["content_id"]: row for row in db.execute("SELECT * FROM iso_profiles")}
    teams = [r["name"] for r in db.execute("SELECT DISTINCT name FROM teams WHERE is_active = 1 ORDER BY name")]
    games = db.execute("SELECT id, name FROM game_titles WHERE is_active = 1 ORDER BY year, name").fetchall()
    tournaments = db.execute("SELECT id, name FROM tournaments WHERE status != 'cancelled' "
                             "ORDER BY name COLLATE NOCASE").fetchall()
    catalog = db.execute("SELECT id, name FROM catalog_isos WHERE is_active = 1 ORDER BY name COLLATE NOCASE").fetchall()
    # Qual ISO cadastrada tem cada content_id visto (a do campeonato ou a do cadastro de amistosos).
    known = {}
    for row in db.execute("SELECT i.content_id, t.name FROM tournament_isos i JOIN tournaments t ON t.id = i.tournament_id "
                          "WHERE i.content_id IS NOT NULL"):
        known.setdefault(row["content_id"].upper(), []).append("ISO do campeonato " + row["name"])
    for row in db.execute("SELECT content_id, name FROM catalog_isos WHERE content_id IS NOT NULL"):
        known.setdefault(row["content_id"].upper(), []).append("ISO de amistoso " + row["name"])
    unidentified = db.execute("SELECT (SELECT COUNT(*) FROM tournament_isos WHERE content_id IS NULL) + "
                              "(SELECT COUNT(*) FROM catalog_isos WHERE content_id IS NULL "
                              "AND (file_id IS NOT NULL OR original_filename IS NOT NULL))").fetchone()[0]
    return render_template("live/admin_slots.html", groups=groups, teams=teams, profiles=profiles, games=games,
                           tournaments=tournaments, catalog=catalog, known=known, unidentified=unidentified)


@bp.post("/admin/ao-vivo/identificar-isos")
@login_required
def identify_isos():
    """CRC32:TAMANHO das ISOs de campeonato/amistoso que ainda não têm (o
    mesmo botão de "Jogos do Memory Card online" - app/admin.py)."""
    from flask import current_app
    import threading
    from .admin import _identify_isos, _identify_isos_lock
    validate_csrf()
    app = current_app._get_current_object()
    if app.config.get("TESTING"):
        found, missing = _identify_isos(app) or (0, [])
        flash("%d ISO(s) identificada(s).%s" % (found, " Sem identificação: " + ", ".join(missing) if missing else ""),
              "success" if not missing else "error")
    elif _identify_isos_lock.locked():
        flash("A identificação das ISOs já está em andamento.", "error")
    else:
        threading.Thread(target=_identify_isos, args=(app,), daemon=True).start()
        flash("Identificando as ISOs (pelo archive.org ou pela cópia deste servidor). Recarregue em alguns segundos.",
              "success")
    return redirect(url_for("live.admin_slots"))


@bp.app_template_filter("scorer")
def scorer_line(item):
    name, minute, assist = item[0], item[1], item[2] if len(item) > 2 else ""
    return Markup("{} {}'{}").format(name, minute, Markup(' <small>(assist. {})</small>').format(assist) if assist else "")
