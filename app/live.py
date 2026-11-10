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
from pathlib import Path
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
            scorer = [_text(item[0], 24), _int(item[1], 0, 200), _text(item[2] if len(item) > 2 else "", 24)]
            # 4º item (fork): "og" = gol contra, com o autor no lugar da assistência; "pen" = de pênalti.
            if len(item) > 3 and item[3] in ("og", "pen"):
                scorer.append(item[3])
            scorers.append(scorer)
        teams.append({"id": _int(team["id"], 0, 255), "name": _text(team.get("name"), 24) if version >= 2 else "",
                      "code": _text(team.get("code"), 3), "goals": _int(team["goals"], 0, 99), "scorers": scorers})
    if len(teams) != 2:
        raise ValueError("teams")
    pens = data.get("pens")
    pens = [_int(pens[0], 0, 99), _int(pens[1], 0, 99)] if isinstance(pens, list) and len(pens) == 2 else None
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
        # Disputa de pênaltis (LOCAL, VISITA): o período vem 4; prorrogação = 2 e 3.
        "pens": pens,
        # O juiz encerrou o tempo (fim do 1º/2º tempo, da prorrogação...): None
        # para o fork que não manda - aí o "Intervalo" vem do relógio em 0.
        "over": bool(data["over"]) if "over" in data else None,
    }


def match_key(content, players, kickoff):
    raw = "%s|%s|%d" % (content, ",".join(p.lower() for p in players), kickoff)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@api_bp.post("/match")
def api_match():
    """Estado da partida mandado pelo DLL: form com state (JSON do fork),
    players (nicks em ordem 1P, 2P...), room (o jogo da sala) e test=1.
    Só com o token da conta WE Camp de um dos jogadores (o DLL não manda sem
    login); a chave do DLL só vale para as simulações (test=1, só o admin vê,
    reporter = o nick)."""
    player = _api_player()
    is_test = request.form.get("test") == "1"
    if player is None and not (is_test and _api_key_ok()):
        return _error(401, "invalid_token", "Entre na sua conta WE Camp (botao Conta WE Camp) para o placar ao vivo.")
    try:
        state = parse_state(request.form.get("state", ""))
    except (KeyError, TypeError, ValueError, IndexError):
        return _error(400, "bad_state", "Estado da partida invalido.")
    players = [p.strip()[:32] for p in request.form.get("players", "").split(",") if p.strip()][:MAX_PLAYERS]
    reporter = player["username"] if player else _text(request.form.get("reporter"), 32)
    if not players or not reporter or not state["content"]:
        return _error(400, "bad_state", "Faltam jogadores ou o jogo.")
    if player and reporter.casefold() not in {nick.casefold() for nick in players}:
        return _error(400, "not_a_player", "O seu nick no Kaillera tem que ser o usuario da sua conta WE Camp.")
    db = get_db()
    live_id = ingest(db, state, players, reporter, player["id"] if player else None,
                     _text(request.form.get("room"), 120), is_test)
    if request.form.get("left") == "1":
        # A DLL avisa que o jogador saiu (Leave, fechou o Kaillera) com o último
        # estado que mandou: a partida que ainda corria fica interrompida na
        # hora (sem esperar o tempo sem sinal) - um fim que chegue depois, do PC
        # de quem ficou, ainda a encerra.
        db.execute("UPDATE live_matches SET status = 'abandoned', finished_at = COALESCE(finished_at, ?), "
                   "updated_at = ? WHERE id = ? AND status = 'live'", (now(), now(), live_id))
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
    if row is None and state["kickoff"]:
        # Partida nova dos mesmos jogadores e ISO: a anterior que ficou aberta foi
        # abandonada (WE4: saíram pelo menu de pausa; o fork não manda o fim dela;
        # ou um saiu/caiu e voltaram a jogar - com 1P e 2P em qualquer ordem).
        same = sorted(nick.casefold() for nick in players)
        for old in db.execute("SELECT id, players_json FROM live_matches WHERE status = 'live' AND content_id = ? "
                              "AND kickoff_frame != ?", (state["content"], state["kickoff"])).fetchall():
            if sorted(nick.casefold() for nick in json.loads(old["players_json"])) == same:
                db.execute("UPDATE live_matches SET status = 'abandoned', finished_at = ? WHERE id = ?", (stamp, old["id"]))
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


def _all_logged_in(db, players, reports):
    """Cada jogador da partida mandou o placar do próprio PC, logado na conta
    dele (o nick dele é o usuário dessa conta)?"""
    senders = {r["player_id"] for r in reports if r["player_id"]}
    return bool(players) and all(_player_id(db, nick) in senders for nick in players)


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
    verified = 1 if match["is_test"] else int(_all_logged_in(db, json.loads(match["players_json"]), reports))
    db.execute(
        "UPDATE live_matches SET status = ?, period = ?, clock_left = ?, half_frames = ?, local_slot = ?, visita_slot = ?, "
        "local_goals = ?, visita_goals = ?, state_json = ?, local_players_json = ?, visita_players_json = ?, "
        "reporters = ?, agreed = ?, verified = ?, updated_at = ?, finished_at = COALESCE(finished_at, ?) WHERE id = ?",
        ("finished" if finished else ("abandoned" if match["status"] == "abandoned" else "live"), best["period"], best["clock"], best["half"], best["teams"][0]["id"],
         best["teams"][1]["id"], best["teams"][0]["goals"], best["teams"][1]["goals"], json.dumps(best),
         json.dumps(local), json.dumps(visita), len(reports), int(len(finals) >= 2 and len(scores) == 1), verified,
         now(), now() if finished else None, live_id))
    for side, team in enumerate(best["teams"]):
        ram_name = team["name"] if NAME_RE.match(team["name"]) else ""
        db.execute("INSERT OR IGNORE INTO iso_team_slots (content_id, slot, updated_at) VALUES (?, ?, ?)",
                   (match["content_id"], team["id"], now()))
        db.execute("UPDATE iso_team_slots SET room = ?, ram_name = CASE WHEN ? != '' THEN ? ELSE ram_name END "
                   "WHERE content_id = ? AND slot = ?",
                   (match["room"], ram_name, ram_name, match["content_id"], team["id"]))
    match = db.execute("SELECT * FROM live_matches WHERE id = ?", (live_id,)).fetchone()
    if (verified and match["tournament_match_id"] is None and match["friendly_id"] is None
            and len(local) == 1 and len(visita) == 1):
        detect_context(db, match, local[0], visita[0])


# --- Campeonato, amistoso ou partida livre ------------------------------------

def _player_id(db, nick):
    row = db.execute("SELECT id FROM players WHERE username = ? COLLATE NOCASE", (nick,)).fetchone()
    return row["id"] if row else None


def detect_context(db, match, local_nick, visita_nick):
    """Liga a partida a um amistoso aceito entre os dois (prioridade: com
    amistoso marcado, as partidas vão para ele, mesmo com jogo de campeonato
    em aberto - todas as da sessão, somadas no fim, _report_friendlies) ou ao
    próximo jogo de campeonato entre eles (ainda sem placar e sem outra
    partida ao vivo ligada). A ISO tem que ser a do amistoso/campeonato. As
    simulações (is_test) só disputam os jogos entre si e não ensinam slots."""
    a, b = _player_id(db, local_nick), _player_id(db, visita_nick)
    if not a or not b or a == b:
        return
    content = match["content_id"]
    # Amistoso: a ISO escolhida no convite (a de um campeonato ou uma do
    # cadastro) tem que bater; sem ISO escolhida ou sem identificação, vale.
    row = db.execute(
        "SELECT f.id FROM friendly_matches f LEFT JOIN tournament_isos ti ON ti.tournament_id = f.iso_tournament_id "
        "LEFT JOIN catalog_isos ci ON ci.id = f.iso_catalog_id "
        "WHERE f.status = 'accepted' "
        "AND ((f.home_id = ? AND f.away_id = ?) OR (f.home_id = ? AND f.away_id = ?)) "
        "AND (COALESCE(ti.content_id, ci.content_id) IS NULL OR COALESCE(ti.content_id, ci.content_id) = ? COLLATE NOCASE "
        "OR EXISTS (SELECT 1 FROM iso_profiles p WHERE p.content_id = ? COLLATE NOCASE "
        "AND (p.tournament_id = f.iso_tournament_id OR p.catalog_iso_id = f.iso_catalog_id))) "
        "ORDER BY f.created_at LIMIT 1", (a, b, b, a, content, content)).fetchone()
    if row:
        db.execute("UPDATE live_matches SET friendly_id = ? WHERE id = ?", (row["id"], match["id"]))
        return
    # Uma partida interrompida que ainda não virou reporte não prende o jogo:
    # voltaram e jogaram de novo (_report_abandoned).
    taken = ("SELECT tournament_match_id FROM live_matches WHERE tournament_match_id IS NOT NULL AND id != ? "
             "AND is_test = ? AND (status != 'abandoned' OR game_report LIKE 'reportado%')")
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
    report_finished(db)


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


# WE3: o relógio fica em 0 durante o acréscimo (não é o intervalo).
_ADDED_TIME_AT_ZERO = {"SLPM_861.62"}


def _match_state(match):
    try:
        return json.loads(match["state_json"] or "{}")
    except ValueError:
        return {}


def _interval(match):
    """Entre dois tempos: o juiz apitou o fim do 1º tempo (ou do 1º da
    prorrogação) ou o seguinte ainda não começou (relógio cheio), até o fork
    mandar o relógio andando. Fork sem o "over": o relógio em 0 (o WE 2002
    ainda joga um pouco depois dele - um gol nesse tempo aparecia no
    "Intervalo")."""
    if match["status"] != "live":
        return False
    period, left, half = match["period"], match["clock_left"], match["half_frames"] or 18000
    if 1 <= period <= 3 and left >= half:
        return True
    if period in (0, 2) and left == 0:
        state = _match_state(match)
        if state.get("over") is not None:
            return bool(state["over"])
        return state.get("engine") not in _ADDED_TIME_AT_ZERO
    return False


def _period_over(match):
    """O juiz apitou o fim do 2º tempo ou da prorrogação e a partida segue
    (empate: prorrogação ou pênaltis a seguir) - o fim, de fato, vem no final."""
    return match["status"] == "live" and match["period"] in (1, 3) and bool(_match_state(match).get("over"))


def _clock_label(match):
    period, left, half = match["period"], match["clock_left"], match["half_frames"] or 18000
    if match["status"] != "live":
        return "Encerrado" if match["status"] == "finished" else "Interrompido"
    if period >= 4:
        return "Pênaltis"
    if _interval(match):
        return "Intervalo"
    if _period_over(match):
        return "Fim do tempo normal" if period == 1 else "Fim da prorrogação"
    if period > 1:  # prorrogação: 2 tempos de 15 minutos
        half_no = period - 1
        if left == 0:
            return "Prorrogação · %dº tempo · %d+'" % (half_no, 90 + 15 * half_no)
        return "Prorrogação · %dº tempo · %d'" % (half_no, 90 + 15 * (half_no - 1) + int(15 * (1 - left / half)))
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
            return " · ".join(p for p in parts if p), url_for("tournaments.show", slug=row["slug"], aba="tabela")
    if match["friendly_id"]:
        # A página do amistoso é só dos dois jogadores: o link, só para eles.
        row = db.execute("SELECT home_id, away_id FROM friendly_matches WHERE id = ?", (match["friendly_id"],)).fetchone()
        mine = row is not None and session.get("player_id") in (row["home_id"], row["away_id"])
        return "Amistoso", url_for("tournaments.friendly_show", friendly_id=match["friendly_id"]) if mine else None
    return "Partida livre", None


def _tie_view(db, match, sides, goals):
    """Mata-mata de ida e volta: "Final · Jogo 2 de 2" e, no jogo de volta, o
    total somando o jogo de ida, na ordem do card (LOCAL, VISITA). O placar da
    ida vale confirmado ou, enquanto não, o reportado (provisional)."""
    if not match["tournament_match_id"]:
        return None
    row = db.execute(
        "SELECT m.leg, m.tie_id, m.home_entry_id, m.away_entry_id, s.name AS stage_name, tie.legs, "
        "hp.username AS home_nick, ap.username AS away_nick FROM tournament_matches m "
        "JOIN tournament_stages s ON s.id = m.stage_id LEFT JOIN tournament_ties tie ON tie.id = m.tie_id "
        "JOIN tournament_entries he ON he.id = m.home_entry_id JOIN players hp ON hp.id = he.player_id "
        "JOIN tournament_entries ae ON ae.id = m.away_entry_id JOIN players ap ON ap.id = ae.player_id "
        "WHERE m.id = ?", (match["tournament_match_id"],)).fetchone()
    if row is None or not row["tie_id"] or row["legs"] != 2:
        return None
    view = {"label": "%s · Jogo %d de 2" % (row["stage_name"], row["leg"]), "aggregate": None, "provisional": False}
    if row["leg"] != 2:
        return view
    first = db.execute("SELECT * FROM tournament_matches WHERE tie_id = ? AND leg = 1", (row["tie_id"],)).fetchone()
    score = None
    if first is not None and first["status"] == "confirmed":
        score = (first["home_goals"], first["away_goals"])
    elif first is not None and first["status"] in ("reported", "disputed"):
        from .tournament.reports import last_report
        reported = last_report(db, first["id"])
        if reported and reported["home_goals"] is not None:
            score, view["provisional"] = (reported["home_goals"], reported["away_goals"]), True
    entry_of = {row["home_nick"].casefold(): row["home_entry_id"], row["away_nick"].casefold(): row["away_entry_id"]}
    entries = [next((entry_of[n.casefold()] for n in nicks if n.casefold() in entry_of), None) for nicks in sides]
    if score is None or None in entries or entries[0] == entries[1]:
        return view
    first_goals = {first["home_entry_id"]: score[0], first["away_entry_id"]: score[1]}
    view["aggregate"] = (goals[0] + first_goals.get(entries[0], 0), goals[1] + first_goals.get(entries[1], 0))
    return view


def _file_name(match):
    return re.sub(r"^PCSX ReARMed:\s*", "", match["room"] or "")


def _catalog_iso(db, match):
    """A ISO do cadastro de amistosos da partida (a imagem do card quando não
    há campeonato com imagem): a do perfil da ISO (admin), a mesma ISO
    (CRC32:TAMANHO) ou, sem identificação, o mesmo nome de arquivo."""
    row = db.execute("SELECT catalog_iso_id FROM iso_profiles WHERE content_id = ? COLLATE NOCASE "
                     "AND catalog_iso_id IS NOT NULL", (match["content_id"],)).fetchone()
    if row:
        return row["catalog_iso_id"]
    for column, value in (("content_id", match["content_id"]), ("original_filename", _file_name(match))):
        row = db.execute(f"SELECT id FROM catalog_isos WHERE {column} = ? COLLATE NOCASE AND is_active = 1 "
                         "ORDER BY approved_at IS NULL, id DESC LIMIT 1", (value,)).fetchone() if value else None
        if row:
            return row["id"]
    return None


def _profile(db, match):
    """Jogo base, campeonato e ISO do cadastro da partida (os logos do card):
    o do jogo de campeonato ligado, o do amistoso (com a ISO escolhida no
    convite), o cadastrado/aprendido para a ISO ou o da ISO de
    campeonato/amistoso com o mesmo nome de arquivo. O card mostra a imagem do
    campeonato ou, sem ela, a da ISO do cadastro."""
    game_id = tournament_id = catalog_id = None
    if match["tournament_match_id"]:
        row = db.execute("SELECT t.id, t.game_title_id FROM tournament_matches m JOIN tournament_stages s "
                         "ON s.id = m.stage_id JOIN tournaments t ON t.id = s.tournament_id WHERE m.id = ?",
                         (match["tournament_match_id"],)).fetchone()
        if row:
            game_id, tournament_id = row["game_title_id"], row["id"]
    if game_id is None and match["friendly_id"]:
        row = db.execute("SELECT game_title_id, iso_tournament_id, iso_catalog_id FROM friendly_matches WHERE id = ?",
                         (match["friendly_id"],)).fetchone()
        if row:
            game_id, tournament_id, catalog_id = row["game_title_id"], row["iso_tournament_id"], row["iso_catalog_id"]
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
    catalog = None
    if not (tournament and tournament["image"]):
        catalog_id = catalog_id or _catalog_iso(db, match)
        catalog = db.execute("SELECT name, image FROM catalog_isos WHERE id = ?",
                             (catalog_id,)).fetchone() if catalog_id else None
    return game, tournament, catalog


def card(db, match):
    from .images import url as image_url
    from .timeutil import format_local
    from .tournament.games import cover_url
    state = json.loads(match["state_json"] or "{}")
    teams = state.get("teams") or [{}, {}]
    sides = (json.loads(match["local_players_json"]), json.loads(match["visita_players_json"]))
    label, link = _context(db, match)
    stale = match["status"] == "live" and match["updated_at"] < _ago(STALE)
    game, tournament, catalog = _profile(db, match)
    views = [_team_view(db, match, i, teams[i] if i < len(teams) else {}, sides[i]) for i in (0, 1)]
    # Lado sem controle: a CPU (o outro lado tem jogador) ou, sem a tabela de controles, "Player N".
    nobody = not any(view["players"] for view in views)
    for number, view in enumerate(views, 1):
        if not view["players"]:
            view["players"] = ["Player %d" % number] if nobody else ["CPU"]
    return {"id": match["id"], "status": match["status"], "stale": stale, "is_test": match["is_test"],
            "clock": "Sem sinal" if stale else _clock_label(match),
            "interval": _interval(match) or _period_over(match),  # sem o risco animado
            "context": label, "link": link,
            "agreed": match["agreed"], "reporters": match["reporters"], "file": _file_name(match),
            "when": format_local(match["started_at"], "%d/%m/%Y %H:%M"),
            "game": {"name": game["name"], "cover": cover_url(game["code"])} if game else None,
            "tournament": {"name": tournament["name"], "image": image_url("torneio", tournament["image"]),
                           "url": url_for("tournaments.show", slug=tournament["slug"], aba="tabela")} if tournament else None,
            # Sem campeonato com imagem: a ISO do cadastro (a do amistoso ou a mesma ISO).
            "iso": {"name": catalog["name"], "image": image_url("torneio", catalog["image"])} if catalog else None,
            "pens": state.get("pens"), "teams": views,
            "tie": _tie_view(db, match, sides, (views[0]["goals"], views[1]["goals"]))}


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
               date_to="", match_id=None, limit=CARDS_LIMIT, count=False):
    """As últimas partidas (ao vivo primeiro), com os filtros da página:
    tournament_id = os jogos daquele campeonato; friendlies = o que não é jogo
    de campeonato (amistosos cadastrados e partidas livres); match_id = só
    aquela partida (o placar ao lado do time na tabela do campeonato)."""
    reap(db)
    where, params = ["m.verified = 1"], []  # só com os dois jogadores logados (refresh)
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
    if match_id:
        where.append("m.id = ?")
        params.append(match_id)
    if player:
        # O nick de uma conta (o que a sugestão do campo preenche): só as
        # partidas dele ("TIERES" não traz as do TIERES_ART); um pedaço do
        # nick procura em todos. "_" e "%" valem como letras (listing.like).
        from .listing import like
        exact = db.execute("SELECT 1 FROM players WHERE username = ? COLLATE NOCASE", (player,)).fetchone()
        where.append("m.players_json LIKE ? ESCAPE '\\'")
        params.append(like('"%s"' % player if exact else player))
    start, end = _local_day(date_from), _local_day(date_to, end=True)
    if start:
        where.append("m.started_at >= ?")
        params.append(start)
    if end:
        where.append("m.started_at < ?")
        params.append(end)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    if count:
        return db.execute("SELECT COUNT(*) FROM live_matches m " + where_sql, params).fetchone()[0]
    rows = db.execute(
        "SELECT m.* FROM live_matches m %s ORDER BY m.status = 'live' DESC, m.started_at DESC LIMIT ?"
        % where_sql, (*params, limit)).fetchall()
    return [card(db, row) for row in rows]


_TOURNAMENT_LIVE_SQL = (
    "SELECT m.*, tm.group_id, tm.home_entry_id, tm.away_entry_id, hp.username AS home_nick, "
    "ap.username AS away_nick, s.tournament_id FROM live_matches m "
    "JOIN tournament_matches tm ON tm.id = m.tournament_match_id JOIN tournament_stages s ON s.id = tm.stage_id "
    "JOIN tournament_entries he ON he.id = tm.home_entry_id JOIN players hp ON hp.id = he.player_id "
    "JOIN tournament_entries ae ON ae.id = tm.away_entry_id JOIN players ap ON ap.id = ae.player_id ")


def _home_side(row):
    """(lado do mandante, do visitante) - 0 LOCAL, 1 VISITA - pelo que cada
    um controla no jogo, ou None."""
    sides = [{nick.casefold() for nick in json.loads(row[key] or "[]")}
             for key in ("local_players_json", "visita_players_json")]
    home = next((i for i in (0, 1) if row["home_nick"].casefold() in sides[i]), None)
    away = next((i for i in (0, 1) if row["away_nick"].casefold() in sides[i]), None)
    if home is None or away is None or home == away:
        return None
    return home, away


def _home_away(row, goals):
    """(gols do mandante, do visitante) do jogo de campeonato a partir dos gols
    (LOCAL, VISITA) do jogo e de quem controla cada lado, ou None."""
    sides = _home_side(row)
    return (goals[sides[0]], goals[sides[1]]) if sides else None


def tournament_live(db, tournament_id):
    """Jogos do campeonato rolando agora (com sinal), para a tabela: os grupos
    com jogo ao vivo, por inscrição o placar do ponto de vista do time ("1-0",
    ganhando/perdendo/empatando, link para a partida no Ao vivo) e, por jogo,
    o placar (mandante, visitante) que a tabela soma como não confirmado e o
    link da partida no Ao vivo (aba Jogos).
    As simulações só aparecem para o admin, como nos cards."""
    rows = db.execute(
        _TOURNAMENT_LIVE_SQL + "WHERE s.tournament_id = ? AND m.status = 'live' AND m.verified = 1 AND m.updated_at >= ?"
        + ("" if _is_admin() else " AND m.is_test = 0"), (tournament_id, _ago(STALE))).fetchall()
    groups, entries, matches, match_urls = set(), {}, {}, {}
    for row in rows:
        score = _home_away(row, (row["local_goals"] or 0, row["visita_goals"] or 0))
        if score is None:
            continue
        matches[row["tournament_match_id"]] = score
        link = url_for("live.index", partida=row["id"])
        match_urls[row["tournament_match_id"]] = link
        for entry, mine, theirs in ((row["home_entry_id"], score[0], score[1]), (row["away_entry_id"], score[1], score[0])):
            entries[entry] = {"score": "%d-%d" % (mine, theirs), "url": link,
                              "result": "win" if mine > theirs else "loss" if mine < theirs else "draw"}
        if row["group_id"]:
            groups.add(row["group_id"])
    return {"groups": groups, "entries": entries, "matches": matches, "match_urls": match_urls}


# O 2º PC manda o fim logo depois do 1º: espera isso antes de reportar.
REPORT_SETTLE = timedelta(minutes=1)
GAME_REPORT_NOTE = "Placar lido do jogo pelo RetroArch TIERES (Ao vivo nº %d)."


def report_finished(db):
    """Jogo de campeonato que terminou no RetroArch TIERES (algum PC mandou o
    fim, e os que mandaram concordam): o placar lido vira o reporte do jogo
    (tournament/reports.py report_from_game). Os jogadores confirmam ou
    contestam; sem resposta, confirma sozinho no prazo (torneios_confirmacao_horas,
    24 h, contado do fim do jogo). Simulações nunca."""
    from .tournament import notify, reports, store
    from .tournament.store import TournamentError
    rows = db.execute(_TOURNAMENT_LIVE_SQL + "WHERE m.status = 'finished' AND m.is_test = 0 AND m.verified = 1 "
                      "AND m.game_report IS NULL AND m.finished_at <= ?", (_ago(REPORT_SETTLE),)).fetchall()
    for row in rows:
        finals = {(s["teams"][0]["goals"], s["teams"][1]["goals"], tuple(s.get("pens") or ())) for s in (
            json.loads(r["state_json"]) for r in db.execute(
                "SELECT state_json FROM live_match_reports WHERE live_match_id = ? AND final = 1", (row["id"],)))}
        final_state = next(iter(finals)) if len(finals) == 1 else None
        score = _home_away(row, final_state[:2]) if final_state else None
        pens = (_home_away(row, final_state[2]) if final_state and len(final_state[2]) == 2 else None) or (None, None)
        outcome = ("sem o fim mandado pelos PCs" if not finals else "os PCs discordam do placar" if len(finals) > 1
                   else "não deu para saber quem é o mandante" if score is None else None)
        tournament = store.get_tournament(db, row["tournament_id"])
        if outcome is None:
            # Modo Server: quem ficou segue jogando e o PC dele manda o fim sozinho.
            outcome = _report_game(db, tournament, row, (*score, *pens), GAME_REPORT_NOTE % row["id"])
        db.execute("UPDATE live_matches SET game_report = ? WHERE id = ?", (outcome, row["id"]))
        db.commit()
        if outcome.startswith("reportado"):
            notify.score_from_game(db, tournament, row["tournament_match_id"])
    _report_abandoned(db)
    _report_friendlies(db)


ABANDONED_NOTE = "Partida interrompida no RetroArch TIERES (Ao vivo nº %d): vale o placar de quando parou."


def _report_game(db, tournament, row, score, note):
    """Reporta o placar lido do jogo de campeonato (reports.report_from_game)
    com quem saiu no meio dele (_game_quitter); devolve a anotação da partida."""
    from .tournament import reports
    from .tournament.store import TournamentError
    quitter = _game_quitter(db, (row["home_nick"], row["away_nick"]), row)
    if quitter:
        note += " %s saiu no meio do jogo: perde o triplo de XP se este placar valer." % quitter
    try:
        reports.report_from_game(db, tournament, row["tournament_match_id"], score,
                                 row["finished_at"] or row["updated_at"], note,
                                 _player_id(db, quitter) if quitter else None)
    except TournamentError as error:
        return str(error)[:200]
    return "reportado" + ("; %s saiu no meio do jogo" % quitter if quitter else "")


def _report_abandoned(db):
    """Jogo de campeonato interrompido (alguém saiu ou caiu: reap, ou partida
    nova dos dois no ingest): vale o placar de quando parou, como o de um jogo
    terminado (os dois confirmam ou o prazo), e quem saiu no meio perde o
    triplo de XP se ele valer. Se voltaram e jogaram de novo o mesmo jogo do
    campeonato, vale a partida nova - a interrompida espera ela acabar."""
    from .tournament import notify, store
    rows = db.execute(_TOURNAMENT_LIVE_SQL + "WHERE m.status = 'abandoned' AND m.is_test = 0 AND m.verified = 1 "
                      "AND m.game_report IS NULL AND COALESCE(m.finished_at, m.updated_at) <= ?",
                      (_ago(REPORT_SETTLE),)).fetchall()
    for row in rows:
        newer = db.execute("SELECT status FROM live_matches WHERE tournament_match_id = ? AND is_test = 0 AND id > ?",
                           (row["tournament_match_id"], row["id"])).fetchall()
        if any(n["status"] == "live" for n in newer):
            continue  # jogando de novo
        tournament = store.get_tournament(db, row["tournament_id"])
        score = _home_away(row, (row["local_goals"] or 0, row["visita_goals"] or 0))
        if newer:
            outcome = "jogaram de novo: vale a partida seguinte"
        elif score is None:
            outcome = "não deu para saber quem é o mandante"
        else:
            outcome = _report_game(db, tournament, row, (*score, None, None), ABANDONED_NOTE % row["id"])
        db.execute("UPDATE live_matches SET game_report = ? WHERE id = ?", (outcome, row["id"]))
        db.commit()
        if outcome.startswith("reportado"):
            notify.score_from_game(db, tournament, row["tournament_match_id"])


# Sem a transmissão do Stream ao vivo para dizer que a sessão do Kaillera
# acabou, ela acaba depois disso sem um jogo novo entre os dois.
SESSION_GAP = timedelta(minutes=20)


def _session_over(db, nicks, last_finished):
    """A sessão do Kaillera em que os dois jogaram já acabou (um deles saiu)?
    A transmissão do Stream ao vivo dos dois terminou depois do último jogo,
    ou passou SESSION_GAP sem jogo novo."""
    if last_finished <= _ago(SESSION_GAP):
        return True
    for row in db.execute("SELECT player_names FROM live_sessions WHERE status = 'finished' AND ended_at >= ?",
                          (last_finished,)):
        names = {n.strip().casefold() for n in (row["player_names"] or "").split(",")}
        if all(nick.casefold() in names for nick in nicks):
            return True
    return False


def _krec_drops(path):
    """(nick, quadro) de quem saiu da partida, em ordem, no replay (.krec) da
    transmissão: registros 0x14 (ver kaillera-client tools/krec_harness/krec.py).
    O quadro é o número de registros de controles (0x12, um por quadro) antes
    dele - o mesmo "frame" que o fork manda no placar ao vivo (os dois contam
    do início do jogo no Kaillera)."""
    import struct
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    pos, drops, frame = (400 if data[:4] == b"KRC1" else 272), [], 0
    while pos < len(data):
        kind = data[pos]
        pos += 1
        try:
            if kind == 0x12:
                (size,) = struct.unpack_from("<h", data, pos)
                if size < 0:
                    break
                pos += 2 + size
                frame += 1
            elif kind == 0x08:
                pos = data.index(b"\0", data.index(b"\0", pos) + 1) + 1
            elif kind == 0x14:
                end = data.index(b"\0", pos)
                drops.append((data[pos:end].decode("latin-1"), frame))
                pos = end + 1 + 4
            else:
                break
        except (ValueError, struct.error):
            break
    return drops


# Interrompido: a saída pode vir até um informe do fork depois do último
# (ele manda a cada mudança e pelo menos a cada 15 s), com folga.
QUIT_MARGIN_FRAMES = 1200


def _game_quitter(db, nicks, game):
    """Quem saiu da sessão do Kaillera no meio deste jogo: o primeiro dos dois
    com saída no replay da transmissão do Stream ao vivo (obrigatório com 2+
    logados) entre o pontapé inicial e o fim - o apito final (no modo Server
    o jogo segue no PC de quem ficou, que manda o fim sozinho) ou, sem fim,
    o último informe dos PCs. None sem a transmissão."""
    from flask import current_app
    finals = [r["frame"] for r in db.execute(
        "SELECT frame FROM live_match_reports WHERE live_match_id = ? AND final = 1", (game["id"],))]
    if finals:
        until = min(finals) - 1
    else:
        last = db.execute("SELECT MAX(frame) AS frame FROM live_match_reports WHERE live_match_id = ?",
                          (game["id"],)).fetchone()["frame"] or 0
        until = last + QUIT_MARGIN_FRAMES
    wanted = {nick.casefold(): nick for nick in nicks}
    for row in db.execute("SELECT player_names, stored_name FROM live_sessions WHERE started_at <= ? "
                          "AND (status = 'live' OR ended_at >= ?) ORDER BY started_at DESC",
                          (game["started_at"], game["updated_at"])):
        names = {n.strip().casefold() for n in (row["player_names"] or "").split(",")}
        if not all(nick in names for nick in wanted):
            continue
        for nick, frame in _krec_drops(Path(current_app.config["LIVE_DIR"]) / row["stored_name"]):
            if nick.casefold() in wanted and game["kickoff_frame"] <= frame <= until:
                return wanted[nick.casefold()]
        return None  # a transmissão desta sessão: ninguém saiu no meio
    return None


def _friendly_numbers(db, friendly_id):
    """{id da partida: nº do jogo no amistoso} - a ordem da lista de partidas
    (friendly_games) e da chave do XP de cada jogo (friendly_games da tabela)."""
    rows = db.execute("SELECT id FROM live_matches WHERE friendly_id = ? AND is_test = 0 AND verified = 1 "
                      "ORDER BY started_at, id", (friendly_id,)).fetchall()
    return {row["id"]: number for number, row in enumerate(rows, 1)}


def _friendly_game_scores(db, nicks, games):
    """O placar de cada partida do amistoso do ponto de vista dele: os
    terminados com o placar final de acordo nos PCs e os interrompidos com o
    de quando pararam. Devolve ({id: (gols do mandante, do visitante, pênaltis
    ou None)}, {id: motivo de não contar})."""
    scores, notes = {}, {}
    for g in games:
        row = dict(g, home_nick=nicks[0], away_nick=nicks[1])
        if g["status"] == "abandoned":
            # Interrompido: vale o placar de quando parou.
            finals = {(g["local_goals"], g["visita_goals"])}
        else:
            finals = {(s["teams"][0]["goals"], s["teams"][1]["goals"]) for s in (
                json.loads(r["state_json"]) for r in db.execute(
                    "SELECT state_json FROM live_match_reports WHERE live_match_id = ? AND final = 1", (g["id"],)))}
        score = _home_away(row, next(iter(finals))) if len(finals) == 1 else None
        if score is None:
            notes[g["id"]] = ("sem o fim mandado pelos PCs" if not finals else "os PCs discordam do placar"
                              if len(finals) > 1 else "não deu para saber quem é o mandante")
            continue
        pens = json.loads(g["state_json"] or "{}").get("pens") if (g["period"] or 0) >= 4 else None
        scores[g["id"]] = (*score, _home_away(row, pens) if pens and len(pens) == 2 else None)
    return scores, notes


def _store_friendly_games(db, friendly_id, games, scores, quit_game_id=None):
    """Grava os jogos que contam (friendly.set_games): cada um vale sozinho no XP."""
    from .tournament import friendly
    numbers = _friendly_numbers(db, friendly_id)
    friendly.set_games(db, friendly_id, [
        {"number": numbers.get(g["id"], index), "live_match_id": g["id"], "home_goals": scores[g["id"]][0],
         "away_goals": scores[g["id"]][1], "home_pens": (scores[g["id"]][2] or (None, None))[0],
         "away_pens": (scores[g["id"]][2] or (None, None))[1], "quit": g["id"] == quit_game_id}
        for index, g in enumerate(games, 1) if g["id"] in scores])


def _report_friendlies(db):
    """Amistoso: as partidas da sessão (cada uma com o seu card) entram juntas
    quando a sessão acaba - um dos jogadores saiu. Contam os jogos terminados
    (placar final de acordo nos PCs) e os interrompidos, com o placar de quando
    pararam: cada um vale sozinho no XP e no V/E/D (friendly_games) e a soma
    vira o placar do amistoso (friendly.report_from_game - mostrada só com
    "Placar agregado"). Quem saiu no meio do último jogo fica marcado e perde
    o triplo de XP nele se o amistoso for confirmado (xp.QUIT_FACTOR). Os dois
    confirmam ou ele vale sozinho no prazo, contado do fim do último jogo."""
    from .tournament import friendly
    from .tournament.store import TournamentError
    for f in db.execute(
            "SELECT f.id, hp.username AS home_nick, ap.username AS away_nick FROM friendly_matches f "
            "JOIN players hp ON hp.id = f.home_id JOIN players ap ON ap.id = f.away_id WHERE f.status = 'accepted' "
            "AND EXISTS (SELECT 1 FROM live_matches m WHERE m.friendly_id = f.id AND m.is_test = 0 AND m.verified = 1 "
            "AND m.game_report IS NULL)").fetchall():
        games = db.execute(
            "SELECT * FROM live_matches WHERE friendly_id = ? AND is_test = 0 AND verified = 1 AND game_report IS NULL "
            "ORDER BY started_at, id", (f["id"],)).fetchall()
        if any(g["status"] == "live" for g in games):
            continue  # ainda jogando
        last = max((g["finished_at"] or g["updated_at"] for g in games), default=None)
        nicks = (f["home_nick"], f["away_nick"])
        if not last or last > _ago(REPORT_SETTLE) or not _session_over(db, nicks, last):
            continue
        # Quem saiu no meio do último jogo (terminado no PC de quem ficou, ou interrompido).
        quitter = _game_quitter(db, nicks, games[-1])
        scores, notes = _friendly_game_scores(db, nicks, games)
        total = [sum(score[0] for score in scores.values()), sum(score[1] for score in scores.values())]
        counted = len(scores)
        outcome = "nenhum jogo para somar"
        if counted:
            quitter_id = _player_id(db, quitter) if quitter else None
            try:
                friendly.report_from_game(db, f["id"], total[0], total[1], last, quitter_id)
                _store_friendly_games(db, f["id"], games, scores, games[-1]["id"] if quitter_id else None)
                outcome = "reportado (soma de %d jogo%s)" % (counted, "s" if counted > 1 else "")
                if quitter:
                    outcome += "; %s saiu no meio do último jogo" % quitter
            except TournamentError as error:
                outcome = str(error)[:200]
        for g in games:
            db.execute("UPDATE live_matches SET game_report = ? WHERE id = ?", (notes.get(g["id"]) or outcome, g["id"]))
        db.commit()


def backfill_friendly_games(db):
    """Amistosos com placar lido do jogo de antes da tabela friendly_games:
    grava os jogos deles (refeitos das partidas, como em _report_friendlies;
    quem saiu, no último). Roda a cada início (db.migrate_db) e só mexe nos
    que ainda não têm jogos gravados."""
    for f in db.execute(
            "SELECT f.id, f.quitter_id, hp.username AS home_nick, ap.username AS away_nick FROM friendly_matches f "
            "JOIN players hp ON hp.id = f.home_id JOIN players ap ON ap.id = f.away_id "
            "WHERE f.game_report = 1 AND NOT EXISTS (SELECT 1 FROM friendly_games fg WHERE fg.friendly_id = f.id) "
            "AND EXISTS (SELECT 1 FROM live_matches m WHERE m.friendly_id = f.id AND m.is_test = 0 "
            "AND m.verified = 1 AND m.status != 'live')").fetchall():
        games = db.execute("SELECT * FROM live_matches WHERE friendly_id = ? AND is_test = 0 AND verified = 1 "
                           "AND status != 'live' ORDER BY started_at, id", (f["id"],)).fetchall()
        scores, _ = _friendly_game_scores(db, (f["home_nick"], f["away_nick"]), games)
        _store_friendly_games(db, f["id"], games, scores, games[-1]["id"] if f["quitter_id"] else None)


# Anotações de _report_friendlies de um jogo que ficou fora da soma.
_NOT_SUMMED = ("sem o fim mandado pelos PCs", "os PCs discordam do placar", "não deu para saber quem é o mandante")


def friendly_games(db, friendly_row):
    """As partidas do amistoso (cada uma com o seu card no Ao vivo), em ordem,
    do ponto de vista dele (mandante x visitante): times, placar (o final, ou
    o de quando parou), pênaltis, situação, se entrou na soma e, nas que
    valem sozinhas no XP (friendly_games, com o placar lido do jogo), a chave
    do XP (xp_key, 'a<id>-<nº>') e se foi a de que quem saiu no meio saiu
    (quit) - para os jogadores conferirem. Devolve (jogos, soma)."""
    from .timeutil import format_local
    nicks = {"home_nick": friendly_row["home_name"], "away_nick": friendly_row["away_name"]}
    games, total = [], [0, 0]
    rows = db.execute("SELECT * FROM live_matches WHERE friendly_id = ? AND is_test = 0 AND verified = 1 "
                      "ORDER BY started_at, id", (friendly_row["id"],)).fetchall()
    # O placar informado pelos jogadores (game_report = 0) vale como um jogo só: os jogos não têm XP próprio.
    stored = {row["live_match_id"]: row for row in db.execute(
        "SELECT * FROM friendly_games WHERE friendly_id = ?", (friendly_row["id"],))} if friendly_row["game_report"] else {}
    for number, match in enumerate(rows, 1):
        sides = _home_side(dict(match, **nicks))
        state = json.loads(match["state_json"] or "{}")
        teams = state.get("teams") or []
        players = [json.loads(match[key] or "[]") for key in ("local_players_json", "visita_players_json")]
        views = [_team_view(db, match, i, teams[i] if i < len(teams) else {}, players[i]) for i in (0, 1)]
        goals = (match["local_goals"] or 0, match["visita_goals"] or 0)
        pens = state.get("pens") if (match["period"] or 0) >= 4 else None
        note = match["game_report"] or ""
        summed = sides is not None and match["status"] != "live" and not note.startswith(_NOT_SUMMED)
        game = {"number": number, "id": match["id"], "url": url_for("live.index", partida=match["id"]),
                "when": format_local(match["started_at"], "%d/%m/%Y %H:%M"),
                "date": format_local(match["started_at"], "%d/%m/%Y"),
                "time": format_local(match["started_at"], "%H:%M"),
                "status": {"finished": "Encerrado", "abandoned": "Interrompido"}.get(match["status"], "Ao vivo"),
                "summed": summed, "note": note if note.startswith(_NOT_SUMMED) else "",
                "xp_key": None, "quit": False}
        if match["id"] in stored:
            game.update(xp_key="a%d-%d" % (friendly_row["id"], stored[match["id"]]["number"]),
                        quit=bool(stored[match["id"]]["quit"]))
        if sides:
            home, away = sides
            game.update(home_team=views[home]["name"], away_team=views[away]["name"],
                        home_image=views[home]["image"], away_image=views[away]["image"],
                        home_goals=goals[home], away_goals=goals[away],
                        pens=(pens[home], pens[away]) if pens and len(pens) == 2 else None)
            if summed:
                total[0] += goals[home]
                total[1] += goals[away]
        else:
            game.update(home_team=views[0]["name"], away_team=views[1]["name"], home_image=views[0]["image"],
                        away_image=views[1]["image"], home_goals=goals[0],
                        away_goals=goals[1], pens=None)
        games.append(game)
    return games, tuple(total)


# Artilharia e assistências (campeonato; amistoso com mais de um jogo): os gols
# que o fork lê da RAM - [autor, minuto, assistência, "og"/"pen"] por time -
# das partidas que contam. Gol contra não entra (nem quem o fez, que vem no
# lugar da assistência); gol de pênalti conta (e aparece à parte). Os pênaltis
# da disputa não estão na lista de gols. O mesmo jogador do jogo controlado
# por dois jogadores do WE Camp (ex.: os dois de Brasil no amistoso) são dois.
OWN_GOAL_NAME = "Gol contra"


def goal_events(db, match):
    """Os gols da partida para a artilharia, em ordem: quem fez, quem deu a
    assistência, se foi de pênalti e o lado (nicks, time e escudo do card)."""
    state = _match_state(match)
    teams = state.get("teams") or []
    events = []
    for side, team in enumerate(teams[:2]):
        nicks = json.loads(match[("local_players_json", "visita_players_json")[side]] or "[]")
        view = None
        for item in team.get("scorers") or []:
            name = (item[0] or "").strip()
            kind = item[3] if len(item) > 3 else ""
            # Japoneses em Shift-JIS saem sem nome; o fork antigo mandava o gol contra só pelo nome.
            if kind == "og" or not name or name == OWN_GOAL_NAME:
                continue
            view = view or _team_view(db, match, side, team, nicks)
            events.append({"name": name, "assist": ((item[2] if len(item) > 2 else "") or "").strip(),
                           "pen": kind == "pen", "nick": " / ".join(nicks), "team": view["name"],
                           "image": view["image"], "match_id": match["id"]})
    return events


def leaderboard(events):
    """Artilharia e assistências dos gols (goal_events, em ordem dos jogos):
    por jogador do jogo e quem o controlou, com o time do último gol; empate
    no número fica na mesma posição. Devolve {"scorers": [...], "assists": [...]}."""
    tables = ({}, {})
    for e in events:
        for table, name, field in ((tables[0], e["name"], "goals"), (tables[1], e["assist"], "assists")):
            if not name:
                continue
            row = table.setdefault((name.casefold(), e["nick"].casefold()),
                                   {"name": name, "nick": e["nick"], "goals": 0, "assists": 0, "pens": 0, "games": set()})
            row.update(team=e["team"], image=e["image"])
            row[field] += 1
            row["games"].add(e["match_id"])
            if field == "goals" and e["pen"]:
                row["pens"] += 1

    def ranked(table, field):
        rows = sorted(table.values(), key=lambda r: (-r[field], r["pens"] if field == "goals" else 0,
                                                     len(r["games"]), r["name"].casefold()))
        position, last = 0, None
        for index, row in enumerate(rows, 1):
            if row[field] != last:
                position, last = index, row[field]
            row.update(position=position, games=len(row["games"]))
        return rows
    return {"scorers": ranked(tables[0], "goals"), "assists": ranked(tables[1], "assists")}


def tournament_scorers(db, tournament_id):
    """Artilharia do campeonato: os jogos confirmados cuja partida lida do jogo
    (a mais recente desse jogo, se jogaram de novo) tem o mesmo placar do
    resultado oficial - um placar corrigido à mão não traz gols de outro jogo.
    Simulações nunca. Devolve o leaderboard com "games" (jogos que contaram)."""
    rows = db.execute(
        "SELECT m.*, tm.home_goals AS official_home, tm.away_goals AS official_away, hp.username AS home_nick, "
        "ap.username AS away_nick FROM live_matches m JOIN tournament_matches tm ON tm.id = m.tournament_match_id "
        "JOIN tournament_stages s ON s.id = tm.stage_id "
        "JOIN tournament_entries he ON he.id = tm.home_entry_id JOIN players hp ON hp.id = he.player_id "
        "JOIN tournament_entries ae ON ae.id = tm.away_entry_id JOIN players ap ON ap.id = ae.player_id "
        "WHERE s.tournament_id = ? AND tm.status = 'confirmed' AND m.is_test = 0 AND m.verified = 1 "
        "AND m.status != 'live' ORDER BY m.started_at DESC, m.id DESC", (tournament_id,)).fetchall()
    seen, counted = set(), []
    for row in rows:
        if row["tournament_match_id"] in seen:
            continue
        if _home_away(row, (row["local_goals"] or 0, row["visita_goals"] or 0)) == (row["official_home"],
                                                                                   row["official_away"]):
            seen.add(row["tournament_match_id"])
            counted.append(row)
    counted.reverse()
    board = leaderboard([e for row in counted for e in goal_events(db, row)])
    board["games"] = len(counted)
    return board


def friendly_scorers(db, friendly_row, games):
    """Artilharia do amistoso: os jogos que entram na soma (friendly_games:
    summed), só com mais de um - com um, o card da partida já diz tudo. None
    sem isso."""
    ids = [g["id"] for g in games if g["summed"]]
    if len(ids) < 2:
        return None
    rows = db.execute("SELECT * FROM live_matches WHERE id IN (%s) ORDER BY started_at, id" % ",".join("?" * len(ids)),
                      ids).fetchall()
    board = leaderboard([e for row in rows for e in goal_events(db, row)])
    board["games"] = len(rows)
    return board


FRIENDLIES = "amistosos"  # o valor de "Amistosos" no filtro de campeonato


def _filters():
    args = request.args
    championship = args.get("campeonato", "")
    return {"player": args.get("jogador", "").strip()[:32], "friendlies": championship == FRIENDLIES,
            "tournament_id": int(championship) if championship.isdigit() else None,
            "match_id": int(args["partida"]) if args.get("partida", "").isdigit() else None,
            "date_from": args.get("de", ""), "date_to": args.get("ate", "")}


@bp.get("/ao-vivo")
def index():
    db = get_db()
    filters = _filters()
    tournaments = db.execute(
        "SELECT t.id, t.name FROM tournaments t WHERE t.id IN ("
        "SELECT s.tournament_id FROM live_matches m JOIN tournament_matches tm ON tm.id = m.tournament_match_id "
        "JOIN tournament_stages s ON s.id = tm.stage_id) ORDER BY t.name COLLATE NOCASE").fetchall()
    # Sugestões do filtro "Jogador": quem já mandou placar logado (aparece nos cards).
    players = db.execute("SELECT DISTINCT p.id, p.username FROM live_match_reports r JOIN players p ON p.id = r.player_id "
                         "ORDER BY p.username COLLATE NOCASE").fetchall()
    # Quantas: 20, 50 ou 100 (o "1–3 de [20 ▾]" do carrossel - app/listing.py).
    from .listing import page_size
    limit = page_size()
    return render_template("live/index.html", cards=list_cards(db, limit=limit, **filters),
                           total=list_cards(db, count=True, **filters), filters=filters, players=players,
                           tournaments=tournaments, is_admin=_is_admin(), limit=limit,
                           cards_url=url_for("live.cards_fragment", **dict(request.args.to_dict(), mostrar=limit)))


@bp.get("/ao-vivo/cards")
def cards_fragment():
    """Só os cards (as páginas atualizam com isto a cada poucos segundos): a
    inicial pede so_ao_vivo=1, a do campeonato as recentes dele (recentes=1)."""
    from .listing import page_size
    recent = request.args.get("recentes") == "1"
    # A página Ao vivo manda o "mostrar" dela (cards_url): a recarga traz a mesma quantidade.
    cards = list_cards(get_db(), only_live=request.args.get("so_ao_vivo") == "1", recent_only=recent,
                       limit=4 if recent else (page_size() if request.args.get("mostrar") else CARDS_LIMIT),
                       **_filters())
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
    # Filtro pelo nome do jogo/ISO ou identificador, e no máximo page_size() ISOs na tela.
    from .listing import cap
    search = request.args.get("iso", "").strip().lower()
    if search:
        groups = {key: rows for key, rows in groups.items()
                  if search in key[0].lower() or search in key[1].lower()
                  or any(search in name.lower() for name in known.get(key[0].upper(), []))}
    shown, total = cap(groups.items())
    groups = dict(shown)
    return render_template("live/admin_slots.html", groups=groups, total=total, search=search,
                           teams=teams, profiles=profiles, games=games,
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
    kind = item[3] if len(item) > 3 else ""
    if kind == "og":  # o autor do gol contra (no lugar da assistência), entre parênteses
        return Markup("Gol contra {}' <small>({})</small>").format(minute, assist or name)
    if kind == "pen":
        return Markup("{} {}' <small>(Pênalti)</small>").format(name, minute)
    return Markup("{} {}'{}").format(name, minute, Markup(' <small>(assist. {})</small>').format(assist) if assist else "")
