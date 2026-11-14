"""Estatísticas dos jogadores: vitórias/empates/derrotas, ranking, histórico
de partidas e comparação entre dois jogadores.

Tudo sai dos jogos confirmados (tournament_matches.status = 'confirmed',
mais os amistosos confirmados que valem - friendly.py), que é onde o placar fica gravado - assim o V/E/D acompanha sozinho qualquer
correção ou placar apagado pelo organizador. Torneios cancelados não contam.
Como no W$ (store._pay_match), vale o placar do jogo: decisão por pênaltis
conta como empate.

No amistoso, cada jogo lido pelo RetroArch TIERES conta sozinho (como no XP -
xp.FRIENDLY_RESULTS); o placar informado pelos jogadores conta como um jogo.

O perfil separa três cards (SCOPES - summary): "ranked" (campeonatos e
amistosos rankeados: o Ranking geral e o XP), "friendly" (amistosos que não
valem para o Ranking) e "free" (partidas livres do Ao vivo - free.py). Os
Últimos resultados juntam os três (all_history), com as partidas livres
interrompidas ainda sem placar final (pending, em laranja).
"""
from unicodedata import normalize

from flask import g

from . import free
from .xp import FRIENDLY_RESULTS, friendly_results

SCOPES = (("ranked", "Campeonatos e amistosos rankeados"), ("friendly", "Amistosos"), ("free", "Partidas livres"))

# O filtro "Campeonato" (Histórico do perfil e Ao vivo): as três opções fixas,
# sempre no topo do autocompletar (ac.js data-fixed), uma por card; os nomes
# dos campeonatos vêm abaixo delas.
KIND_FILTERS = (("Rankeado", "ranked"), ("Amistoso", "friendly"), ("Partida livre", "free"))
_KIND_TEXTS = {"rankeado": "ranked", "rankeados": "ranked", "amistoso": "friendly", "amistosos": "friendly",
               "partida livre": "free", "partidas livres": "free"}


def kind_filter(text):
    """O texto do filtro "Campeonato" é uma das opções fixas (sem diferenciar
    acento, maiúscula ou plural)? -> o escopo ("ranked", "friendly", "free") ou None."""
    plain = " ".join(normalize("NFKD", text or "").encode("ascii", "ignore").decode().casefold().split())
    return _KIND_TEXTS.get(plain)

# Uma linha por jogador por jogo confirmado, do ponto de vista dele: jogos de
# torneio (kind 't') e jogos de amistoso que valem para o ranking (kind 'a' -
# friendly.py). match_key identifica a partida nos dois (o id do jogo de
# torneio; 'a<id>' ou 'a<id>-<nº do jogo>' no amistoso, que tem também o
# game_number) - é a chave do XP (xp.py) e dos popups. match_id do amistoso = o id dele.
_SIDES = f"""
WITH friendly_results AS ({FRIENDLY_RESULTS}), sides AS (
    SELECT m.id AS match_id, s.tournament_id, s.name AS stage_name, m.round, m.leg, m.tie_id,
           COALESCE(m.confirmed_at, m.updated_at) AS played_at,
           me.player_id, op.player_id AS opponent_id, me.id AS entry_id, op.id AS opponent_entry_id,
           m.home_goals AS goals_for, m.away_goals AS goals_against,
           m.home_pens AS pens_for, m.away_pens AS pens_against, 1 AS is_home
    FROM tournament_matches m
    JOIN tournament_stages s ON s.id = m.stage_id
    JOIN tournament_entries me ON me.id = m.home_entry_id
    JOIN tournament_entries op ON op.id = m.away_entry_id
    WHERE m.status = 'confirmed'
    UNION ALL
    SELECT m.id, s.tournament_id, s.name, m.round, m.leg, m.tie_id, COALESCE(m.confirmed_at, m.updated_at),
           me.player_id, op.player_id, me.id, op.id,
           m.away_goals, m.home_goals, m.away_pens, m.home_pens, 0
    FROM tournament_matches m
    JOIN tournament_stages s ON s.id = m.stage_id
    JOIN tournament_entries me ON me.id = m.away_entry_id
    JOIN tournament_entries op ON op.id = m.home_entry_id
    WHERE m.status = 'confirmed'
), results AS (
    SELECT sides.*, 't' AS kind, sides.match_id AS match_key, t.slug, t.name AS tournament_name, t.game_title_id,
           NULL AS game_number
    FROM sides JOIN tournaments t ON t.id = sides.tournament_id
    WHERE t.status != 'cancelled'
    UNION ALL
    SELECT r.friendly_id, NULL, 'Amistoso', NULL, NULL, NULL, r.confirmed_at, r.home_id, r.away_id, NULL, NULL,
           r.home_goals, r.away_goals, r.home_pens, r.away_pens, 1, 'a', r.match_key, NULL, 'Amistoso',
           r.game_title_id, r.game_number
    FROM friendly_results r
    UNION ALL
    SELECT r.friendly_id, NULL, 'Amistoso', NULL, NULL, NULL, r.confirmed_at, r.away_id, r.home_id, NULL, NULL,
           r.away_goals, r.home_goals, r.away_pens, r.home_pens, 0, 'a', r.match_key, NULL, 'Amistoso',
           r.game_title_id, r.game_number
    FROM friendly_results r
)
"""

# O card "Amistosos": os amistosos confirmados que não valem para o Ranking
# (ranked = 0 - friendly.py), com as mesmas colunas de _SIDES.
_FRIENDLY_SIDES = f"""
WITH friendly_results AS ({friendly_results(0)}), results AS (
    SELECT r.friendly_id AS match_id, NULL AS tournament_id, 'Amistoso' AS stage_name, NULL AS round, NULL AS leg,
           NULL AS tie_id, r.confirmed_at AS played_at, r.home_id AS player_id, r.away_id AS opponent_id,
           NULL AS entry_id, NULL AS opponent_entry_id, r.home_goals AS goals_for, r.away_goals AS goals_against,
           r.home_pens AS pens_for, r.away_pens AS pens_against, 1 AS is_home, 'a' AS kind, r.match_key,
           NULL AS slug, 'Amistoso' AS tournament_name, r.game_title_id, r.game_number
    FROM friendly_results r
    UNION ALL
    SELECT r.friendly_id, NULL, 'Amistoso', NULL, NULL, NULL, r.confirmed_at, r.away_id, r.home_id, NULL, NULL,
           r.away_goals, r.home_goals, r.away_pens, r.home_pens, 0, 'a', r.match_key, NULL, 'Amistoso',
           r.game_title_id, r.game_number
    FROM friendly_results r
)
"""

_SQL_SCOPES = {"ranked": _SIDES, "friendly": _FRIENDLY_SIDES}

_TOTALS = ("COUNT(*) AS played, "
           "SUM(goals_for > goals_against) AS wins, "
           "SUM(goals_for = goals_against) AS draws, "
           "SUM(goals_for < goals_against) AS losses, "
           "SUM(goals_for) AS goals_for, SUM(goals_against) AS goals_against")


def _filters(game_id=None, opponent_id=None):
    sql, params = "", []
    if game_id:
        sql += " AND game_title_id = ?"
        params.append(game_id)
    if opponent_id:
        sql += " AND opponent_id = ?"
        params.append(opponent_id)
    return sql, params


def _record(row):
    """Linha de totais -> dict com saldo, pontos e aproveitamento."""
    played = row["played"] if row else 0
    record = {key: (row[key] or 0) if row else 0
              for key in ("played", "wins", "draws", "losses", "goals_for", "goals_against")}
    record["goal_diff"] = record["goals_for"] - record["goals_against"]
    record["points"] = record["wins"] * 3 + record["draws"]
    # Aproveitamento como no futebol: pontos ganhos / pontos disputados.
    record["efficiency"] = round(100 * record["points"] / (3 * played)) if played else 0
    return record


def outcome(row):
    """'V' / 'E' / 'D' de uma linha de histórico (as letras da tabela, engine.py).
    Na partida livre interrompida ainda sem placar final (pending - free.py),
    a do placar de quando parou: as telas a pintam de laranja."""
    if row["goals_for"] > row["goals_against"]:
        return "V"
    return "E" if row["goals_for"] == row["goals_against"] else "D"


def player_record(db, player_id, game_id=None, opponent_id=None, scope="ranked"):
    if scope == "free":
        return _free_records(db, game_id).get(player_id) or _record(None)
    where, params = _filters(game_id, opponent_id)
    row = db.execute(f"{_SQL_SCOPES[scope]} SELECT {_TOTALS} FROM results WHERE player_id = ?{where}",
                     (player_id, *params)).fetchone()
    return _record(row)


def ranking(db, game_id=None, limit=10, scope="ranked"):
    """Top jogadores: pontos (3/1/0), depois vitórias, saldo, gols pró e
    menos jogos (mesmos pontos em menos jogos = melhor aproveitamento)."""
    if scope == "free":
        rows = sorted(_free_records(db, game_id).items(), key=lambda item: _rank_key(item[1]))
        return [{"player_id": pid, **record} for pid, record in (rows if limit < 0 else rows[:limit])]
    where, params = _filters(game_id)
    rows = db.execute(
        f"{_SQL_SCOPES[scope]} SELECT r.player_id, p.username, {_TOTALS} FROM results r JOIN players p ON p.id = r.player_id "
        f"WHERE 1 = 1{where} GROUP BY r.player_id "
        "ORDER BY SUM(goals_for > goals_against) * 3 + SUM(goals_for = goals_against) DESC, "
        "SUM(goals_for > goals_against) DESC, SUM(goals_for) - SUM(goals_against) DESC, SUM(goals_for) DESC, "
        "COUNT(*), p.username COLLATE NOCASE LIMIT ?", (*params, limit)).fetchall()
    return [{"player_id": row["player_id"], "username": row["username"], **_record(row)} for row in rows]


def history(db, player_id, game_id=None, opponent_id=None, limit=100, scope="ranked"):
    """Jogos confirmados do jogador, do mais recente para o mais antigo."""
    where, params = _filters(game_id, opponent_id)
    return db.execute(
        f"{_SQL_SCOPES[scope]} SELECT r.*, op.username AS opponent_name FROM results r "
        f"JOIN players op ON op.id = r.opponent_id WHERE r.player_id = ?{where} "
        "ORDER BY r.played_at DESC, r.match_id DESC, r.game_number DESC LIMIT ?",
        (player_id, *params, limit)).fetchall()


def form(rows, size=5):
    """Últimos resultados (V/E/D), do mais antigo ao mais recente."""
    return [outcome(row) for row in rows[:size]][::-1]


def row_key(row):
    """Identifica uma linha do histórico entre os três tipos (o ponto do
    gráfico de evolução e a linha da tabela do perfil)."""
    return f"{row['kind']}{row['match_id']}-{row['game_number'] or 0}"


def evolution(rows):
    """Aproveitamento acumulado jogo a jogo, do mais antigo ao mais recente
    (`rows` = history()/all_history(), que vêm do mais recente para o mais
    antigo - sem as partidas livres interrompidas ainda sem placar final)."""
    points, points_list = 0, []
    for index, row in enumerate(reversed(rows), 1):
        result = outcome(row)
        points += {"V": 3, "E": 1, "D": 0}[result]
        points_list.append({"n": index, "outcome": result, "efficiency": round(100 * points / (3 * index)),
                            "score": f"{row['goals_for']} x {row['goals_against']}",
                            "opponent": row["opponent_name"], "played_at": row["played_at"],
                            "key": row_key(row), "match_key": row["match_key"], "kind": row["kind"]})
    return points_list


def opponents(db, player_id, game_id=None, limit=10):
    """Adversários mais enfrentados, com o retrospecto contra cada um."""
    where, params = _filters(game_id)
    rows = db.execute(
        f"{_SIDES} SELECT r.opponent_id, op.username, {_TOTALS} FROM results r "
        f"JOIN players op ON op.id = r.opponent_id WHERE r.player_id = ?{where} GROUP BY r.opponent_id "
        "ORDER BY COUNT(*) DESC, op.username COLLATE NOCASE LIMIT ?", (player_id, *params, limit)).fetchall()
    return [{"player_id": row["opponent_id"], "username": row["username"], **_record(row)} for row in rows]


def position(db, player_id, game_id=None, scope="ranked"):
    """Posição do jogador no ranking completo (None se ainda não jogou)."""
    for index, row in enumerate(ranking(db, game_id, limit=-1, scope=scope), 1):
        if row["player_id"] == player_id:
            return index
    return None


def players_with_matches(db, game_id=None):
    where, params = _filters(game_id)
    return db.execute(
        f"{_SIDES} SELECT DISTINCT p.id, p.username FROM results r JOIN players p ON p.id = r.player_id "
        f"WHERE 1 = 1{where} ORDER BY p.username COLLATE NOCASE", params).fetchall()


def compare(db, a, b, game_id=None):
    """Dois jogadores lado a lado: campanha geral de cada um e o confronto
    direto (do ponto de vista de `a`)."""
    return {
        "a": player_record(db, a, game_id), "b": player_record(db, b, game_id),
        "a_position": position(db, a, game_id), "b_position": position(db, b, game_id),
        "head_to_head": player_record(db, a, game_id, opponent_id=b),
        "a_form": form(history(db, a, game_id, limit=5)), "b_form": form(history(db, b, game_id, limit=5)),
    }


def trophies(db, player_id, game_id=None):
    """Galeria de troféus: os 1º, 2º e 3º lugares do jogador nos campeonatos
    encerrados (store.finish grava final_position), do mais recente ao mais
    antigo, com o time que ele usou (escudo: teams) e o total de inscritos."""
    sql = ("SELECT e.final_position, e.team_name, tm.name AS team_db_name, tm.logo, tm.flag, t.id AS tournament_id, "
           "t.slug, t.name AS tournament_name, t.finished_at, g.name AS game_name, g.code AS game_code, "
           "(SELECT COUNT(*) FROM tournament_entries x WHERE x.tournament_id = t.id) AS players "
           "FROM tournament_entries e JOIN tournaments t ON t.id = e.tournament_id "
           "JOIN game_titles g ON g.id = t.game_title_id LEFT JOIN teams tm ON tm.id = e.team_id "
           "WHERE e.player_id = ? AND t.status = 'finished' AND e.final_position BETWEEN 1 AND 3")
    params = [player_id]
    if game_id:
        sql += " AND t.game_title_id = ?"
        params.append(game_id)
    return db.execute(sql + " ORDER BY t.finished_at DESC, t.id DESC", params).fetchall()


# --- Os três cards do perfil (SCOPES) -----------------------------------------

def _rank_key(record):
    """A ordem do ranking (a mesma do SQL de ranking()), para os cards calculados aqui."""
    return (-record["points"], -record["wins"], -record["goal_diff"], -record["goals_for"], record["played"],
            (record.get("username") or "").casefold())


def _free_records(db, game_id=None):
    """{player_id: record (com username)} das partidas livres que contam (free.py),
    uma vez por requisição (o card e a posição no ranking usam os dois)."""
    cache = g.setdefault("free_records", {})
    if game_id not in cache:
        cache[game_id] = _count_free(db, game_id)
    return cache[game_id]


def _count_free(db, game_id):
    totals = {}
    for row in free.sides(db, game_id):
        item = totals.setdefault(row["player_id"], {"played": 0, "wins": 0, "draws": 0, "losses": 0,
                                                    "goals_for": 0, "goals_against": 0})
        item["played"] += 1
        item["wins"] += row["goals_for"] > row["goals_against"]
        item["draws"] += row["goals_for"] == row["goals_against"]
        item["losses"] += row["goals_for"] < row["goals_against"]
        item["goals_for"] += row["goals_for"]
        item["goals_against"] += row["goals_against"]
    names = {}
    for match in free.matches(db):
        names[match["local_id"]], names[match["visita_id"]] = match["local_name"], match["visita_name"]
    return {pid: {"username": names.get(pid, ""), **_record(item)} for pid, item in totals.items()}


def summary(db, player_id, game_id=None):
    """Os três cards do jogador (SCOPES): título, V/E/D e posição no ranking de
    cada um e o texto do filtro "Campeonato" do Ao vivo que mostra as partidas
    dele (KIND_FILTERS - o link do card)."""
    labels = {scope: label for label, scope in KIND_FILTERS}
    return [{"key": key, "title": title, "record": player_record(db, player_id, game_id, scope=key),
             "position": position(db, player_id, game_id, scope=key), "filter": labels[key]}
            for key, title in SCOPES]


def all_history(db, player_id, game_id=None, limit=-1):
    """Os jogos dos três cards juntos, do mais recente: os de campeonato e os
    amistosos (rankeados ou não) confirmados, as partidas livres e, entre
    elas, as interrompidas ainda sem placar final (pending)."""
    rows = []
    for scope, _ in SCOPES:
        if scope == "free":
            items = free.player_rows(db, player_id, game_id)
        else:
            items = [dict(row) for row in history(db, player_id, game_id, limit=-1, scope=scope)]
        for item in items:
            item["scope"] = scope
            item.setdefault("pending", False)
            item.setdefault("interrupted", False)
            item.setdefault("tip", "")
            rows.append(item)
    rows.sort(key=lambda r: (r["played_at"] or "", r["kind"], r["match_id"], r["game_number"] or 0), reverse=True)
    return rows if limit < 0 else rows[:limit]
