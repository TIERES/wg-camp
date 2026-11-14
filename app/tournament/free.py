"""Partidas livres: as partidas do Ao vivo (app/live.py) entre duas contas do
WE Camp que não são jogo de campeonato nem de amistoso cadastrado.

Encerrada ('finished'): vale o placar lido do jogo. Interrompida
('abandoned'): só vale depois que os dois combinam o placar final
(free_match_scores) - um informa (report), o outro confirma (confirm) ou não
aceita (reject, com o motivo: aí qualquer um propõe outro placar). Até lá
ela aparece nos Últimos resultados do perfil em laranja, pedindo o placar
final, e não conta no V/E/D.

    (sem placar) --um informa--> reported --o outro confirma--> confirmed
                                     +--o outro não aceita--> suspended --um informa--> reported

Contam só no card "Partidas livres" do perfil (stats.summary): não mexem no
Ranking geral nem no XP.
"""
import json

from flask import g

from ..db import now
from .store import TournamentError

KIND = "l"  # o "kind" das linhas do histórico (stats.py): 't' torneio, 'a' amistoso, 'l' partida livre
LABEL = "Partida livre"

_SQL = ("SELECT m.id, m.status, m.content_id, m.room, m.state_json, m.local_players_json, m.visita_players_json, "
        "m.local_goals, m.visita_goals, m.tournament_match_id, m.friendly_id, "
        "COALESCE(m.finished_at, m.updated_at) AS played_at, "
        "s.status AS score_status, s.local_goals AS score_local, s.visita_goals AS score_visita, "
        "s.reported_by, s.reported_at, s.rejected_by, s.rejected_note, s.confirmed_at "
        "FROM live_matches m LEFT JOIN free_match_scores s ON s.live_match_id = m.id "
        "WHERE m.verified = 1 AND m.is_test = 0 AND m.tournament_match_id IS NULL AND m.friendly_id IS NULL "
        "AND m.status IN ('finished', 'abandoned')")


def _accounts(db, nicks):
    """{nick em minúsculas: (id, username)} das contas com esses nicks."""
    found, nicks = {}, sorted(nicks)
    for start in range(0, len(nicks), 500):
        chunk = nicks[start:start + 500]
        for row in db.execute(f"SELECT id, username FROM players WHERE username COLLATE NOCASE IN "
                              f"({','.join('?' * len(chunk))})", chunk):
            found[row["username"].casefold()] = (row["id"], row["username"])
    return found


def _side_nick(raw):
    try:
        nicks = json.loads(raw or "[]")
    except ValueError:
        return None
    return nicks[0] if len(nicks) == 1 and isinstance(nicks[0], str) else None


def _build(db, rows):
    """Linhas de live_matches (+ placar combinado) -> partidas livres 1 x 1
    entre duas contas diferentes (lado sem controle, 2 x 2 etc. ficam de fora)."""
    pairs = [(row, _side_nick(row["local_players_json"]), _side_nick(row["visita_players_json"])) for row in rows]
    accounts = _accounts(db, {nick.casefold() for _, a, b in pairs for nick in (a, b) if nick})
    found = []
    for row, local, visita in pairs:
        a, b = accounts.get((local or "").casefold()), accounts.get((visita or "").casefold())
        if not a or not b or a[0] == b[0]:
            continue
        try:
            pens = json.loads(row["state_json"] or "{}").get("pens")
        except ValueError:
            pens = None
        interrupted = row["status"] == "abandoned"
        settled = interrupted and row["score_status"] == "confirmed"
        goals = (row["score_local"], row["score_visita"]) if settled else (row["local_goals"], row["visita_goals"])
        found.append({
            "id": row["id"], "local_id": a[0], "local_name": a[1], "visita_id": b[0], "visita_name": b[1],
            "goals": goals, "stop_goals": (row["local_goals"], row["visita_goals"]),
            # Pênaltis só no placar lido do jogo (o combinado é só o placar).
            "pens": tuple(pens) if isinstance(pens, list) and len(pens) == 2 and not settled else None,
            "interrupted": interrupted, "counted": not interrupted or settled, "pending": interrupted and not settled,
            "score_status": row["score_status"] if interrupted else None,
            "score": (row["score_local"], row["score_visita"]) if row["score_status"] else None,
            "reported_by": row["reported_by"], "reported_at": row["reported_at"], "rejected_by": row["rejected_by"],
            "rejected_note": row["rejected_note"] or "", "confirmed_at": row["confirmed_at"],
            "played_at": row["played_at"], "content_id": row["content_id"], "room": row["room"],
            "state_json": row["state_json"], "tournament_match_id": None, "friendly_id": None,
        })
    return found


def matches(db, game_id=None):
    """Todas as partidas livres (encerradas e interrompidas), da mais recente
    para a mais antiga - uma vez por requisição. game_id: só as daquele jogo
    base (o mesmo do card do Ao vivo - live._profile)."""
    if "free_matches" not in g:
        g.free_matches = _build(db, db.execute(_SQL + " ORDER BY played_at DESC, m.id DESC").fetchall())
    if not game_id:
        return g.free_matches
    return [m for m in g.free_matches if _game_id(db, m) == game_id]


def _game_id(db, match):
    """Jogo base da partida (como o logo do card do Ao vivo), com cache por ISO."""
    from .. import live
    cache = g.setdefault("free_games", {})
    key = (match["content_id"], match["room"])
    if key not in cache:
        game = live._profile(db, match)[0]
        row = db.execute("SELECT id FROM game_titles WHERE code = ?", (game["code"],)).fetchone() if game else None
        cache[key] = row["id"] if row else None
    return cache[key]


def get(db, live_id):
    """Uma partida livre (ou None)."""
    found = _build(db, db.execute(_SQL + " AND m.id = ?", (live_id,)).fetchall())
    return found[0] if found else None


def side_of(match, player_id):
    """0 = o jogador controlou o LOCAL, 1 = o VISITA, None = não jogou."""
    if player_id == match["local_id"]:
        return 0
    return 1 if player_id == match["visita_id"] else None


def opponent_of(match, player_id):
    return (match["visita_id"], match["visita_name"]) if side_of(match, player_id) == 0 \
        else (match["local_id"], match["local_name"])


def row_for(match, side):
    """A partida do ponto de vista de um lado, com as colunas do histórico
    (stats.history) - o "kind" 'l' e o que o placar final precisa."""
    me, other = ("local", "visita") if side == 0 else ("visita", "local")
    goals = match["goals"] if side == 0 else match["goals"][::-1]
    pens = (match["pens"] if side == 0 else match["pens"][::-1]) if match["pens"] else (None, None)
    return {
        "kind": KIND, "match_id": match["id"], "match_key": f"l{match['id']}", "tournament_id": None,
        "stage_name": LABEL, "round": None, "leg": None, "tie_id": None, "played_at": match["played_at"],
        "player_id": match[f"{me}_id"], "opponent_id": match[f"{other}_id"], "opponent_name": match[f"{other}_name"],
        "entry_id": None, "opponent_entry_id": None, "goals_for": goals[0], "goals_against": goals[1],
        "pens_for": pens[0], "pens_against": pens[1], "is_home": int(side == 0), "slug": None,
        "tournament_name": LABEL, "game_title_id": None, "game_number": None,
        "interrupted": match["interrupted"], "pending": match["pending"], "score_status": match["score_status"],
        "reported_by": match["reported_by"], "tip": status_tip(match) if match["pending"] else "",
    }


def sides(db, game_id=None):
    """As partidas que contam (encerradas e interrompidas com o placar final
    combinado), uma linha por jogador - para o V/E/D e o ranking do card."""
    return [row_for(m, side) for m in matches(db, game_id) if m["counted"] for side in (0, 1)]


def player_rows(db, player_id, game_id=None, pending=True):
    """As partidas livres do jogador, da mais recente; pending=True traz
    também as interrompidas ainda sem placar final (em laranja no perfil)."""
    rows = []
    for match in matches(db, game_id):
        side = side_of(match, player_id)
        if side is not None and (match["counted"] or pending):
            rows.append(row_for(match, side))
    return rows


# --- Placar final das interrompidas -------------------------------------------

def _mine(db, live_id, me):
    match = get(db, live_id)
    if match is None or side_of(match, me) is None:
        raise TournamentError("Partida não encontrada.")
    if not match["interrupted"]:
        raise TournamentError("Esta partida terminou normalmente: vale o placar lido do jogo.")
    return match


def _goals(value):
    try:
        goals = int(value)
    except (TypeError, ValueError):
        raise TournamentError("Informe o placar final da partida.") from None
    if not 0 <= goals <= 99:
        raise TournamentError("Placar inválido.")
    return goals


def report(db, live_id, me, local_goals, visita_goals):
    """Um dos dois informa (ou, depois de não aceito, propõe de novo) o
    placar final, na ordem do card (LOCAL x VISITA)."""
    match = _mine(db, live_id, me)
    if match["score_status"] == "confirmed":
        raise TournamentError("O placar final desta partida já foi combinado.")
    if match["score_status"] == "reported":
        raise TournamentError("Você já informou o placar final: falta o adversário confirmar."
                              if match["reported_by"] == me else
                              "O adversário já informou o placar final: confirme ou não aceite.")
    db.execute("INSERT OR REPLACE INTO free_match_scores (live_match_id, status, local_goals, visita_goals, "
               "reported_by, reported_at, rejected_by, rejected_note, confirmed_at) "
               "VALUES (?, 'reported', ?, ?, ?, ?, NULL, '', NULL)",
               (live_id, _goals(local_goals), _goals(visita_goals), me, now()))
    return get(db, live_id)


def _waiting_for_me(match, me):
    if match["score_status"] != "reported" or match["reported_by"] == me:
        raise TournamentError("Não há placar final do adversário para você responder nesta partida.")


def confirm(db, live_id, me):
    """O outro jogador aceita o placar final: a partida passa a contar."""
    match = _mine(db, live_id, me)
    _waiting_for_me(match, me)
    db.execute("UPDATE free_match_scores SET status = 'confirmed', confirmed_at = ? WHERE live_match_id = ?",
               (now(), live_id))
    return get(db, live_id)


def reject(db, live_id, me, note=""):
    """Não aceita o placar final (com o motivo): qualquer um propõe outro."""
    match = _mine(db, live_id, me)
    _waiting_for_me(match, me)
    note = (note or "").strip()
    if len(note) < 3:
        raise TournamentError("Explique por que você não aceita o placar final.")
    db.execute("UPDATE free_match_scores SET status = 'suspended', rejected_by = ?, rejected_note = ? "
               "WHERE live_match_id = ?", (me, note[:300], live_id))
    return get(db, live_id)


def waiting_for(db, me):
    """Partidas interrompidas com o placar final do adversário esperando a minha resposta."""
    return [m for m in matches(db) if m["score_status"] == "reported" and m["reported_by"] != me
            and side_of(m, me) is not None]


def todo_count(db, me):
    """Quantas esperam a minha confirmação (o número no menu, junto dos amistosos)."""
    rows = db.execute(
        "SELECT m.local_players_json, m.visita_players_json FROM free_match_scores s "
        "JOIN live_matches m ON m.id = s.live_match_id WHERE s.status = 'reported' AND s.reported_by != ? "
        "AND m.status = 'abandoned' AND m.verified = 1 AND m.is_test = 0 "
        "AND m.tournament_match_id IS NULL AND m.friendly_id IS NULL", (me,)).fetchall()
    if not rows:
        return 0
    name = db.execute("SELECT username FROM players WHERE id = ?", (me,)).fetchone()
    mine = name["username"].casefold() if name else None
    return sum(1 for row in rows if mine in {(_side_nick(row["local_players_json"]) or "").casefold(),
                                             (_side_nick(row["visita_players_json"]) or "").casefold()})


def status_tip(match):
    """O texto do (i) de uma partida interrompida ainda sem placar final."""
    if match["score_status"] == "reported" and match["score"]:
        reporter, waiting = ((match["local_name"], match["visita_name"]) if match["reported_by"] == match["local_id"]
                             else (match["visita_name"], match["local_name"]))
        return (f"Partida interrompida! {reporter} informou o placar final {match['local_name']} {match['score'][0]} "
                f"x {match['score'][1]} {match['visita_name']}: falta {waiting} confirmar.")
    if match["score_status"] == "suspended":
        return ("Partida interrompida! O placar final proposto não foi aceito: registrem um placar final "
                "que os dois aceitem.")
    return ("Partida interrompida! Registrem um placar final: um dos dois informa e o outro confirma. "
            "Até lá ela não conta.")
