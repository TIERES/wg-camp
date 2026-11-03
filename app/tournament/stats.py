"""Estatísticas dos jogadores: vitórias/empates/derrotas, ranking, histórico
de partidas e comparação entre dois jogadores.

Tudo sai dos jogos confirmados (tournament_matches.status = 'confirmed'),
que é onde o placar fica gravado - assim o V/E/D acompanha sozinho qualquer
correção ou placar apagado pelo organizador. Torneios cancelados não contam.
Como no W$ (store._pay_match), vale o placar do jogo: decisão por pênaltis
conta como empate.
"""

# Uma linha por jogador por jogo confirmado, do ponto de vista dele.
_SIDES = """
WITH sides AS (
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
    SELECT sides.*, t.slug, t.name AS tournament_name, t.game_title_id
    FROM sides JOIN tournaments t ON t.id = sides.tournament_id
    WHERE t.status != 'cancelled'
)
"""

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
    """'V' / 'E' / 'D' de uma linha de histórico (as letras da tabela, engine.py)."""
    if row["goals_for"] > row["goals_against"]:
        return "V"
    return "E" if row["goals_for"] == row["goals_against"] else "D"


def player_record(db, player_id, game_id=None, opponent_id=None):
    where, params = _filters(game_id, opponent_id)
    row = db.execute(f"{_SIDES} SELECT {_TOTALS} FROM results WHERE player_id = ?{where}",
                     (player_id, *params)).fetchone()
    return _record(row)


def ranking(db, game_id=None, limit=10):
    """Top jogadores: pontos (3/1/0), depois vitórias, saldo, gols pró e
    menos jogos (mesmos pontos em menos jogos = melhor aproveitamento)."""
    where, params = _filters(game_id)
    rows = db.execute(
        f"{_SIDES} SELECT r.player_id, p.username, {_TOTALS} FROM results r JOIN players p ON p.id = r.player_id "
        f"WHERE 1 = 1{where} GROUP BY r.player_id "
        "ORDER BY SUM(goals_for > goals_against) * 3 + SUM(goals_for = goals_against) DESC, "
        "SUM(goals_for > goals_against) DESC, SUM(goals_for) - SUM(goals_against) DESC, SUM(goals_for) DESC, "
        "COUNT(*), p.username COLLATE NOCASE LIMIT ?", (*params, limit)).fetchall()
    return [{"player_id": row["player_id"], "username": row["username"], **_record(row)} for row in rows]


def history(db, player_id, game_id=None, opponent_id=None, limit=100):
    """Jogos confirmados do jogador, do mais recente para o mais antigo."""
    where, params = _filters(game_id, opponent_id)
    return db.execute(
        f"{_SIDES} SELECT r.*, op.username AS opponent_name FROM results r "
        f"JOIN players op ON op.id = r.opponent_id WHERE r.player_id = ?{where} "
        "ORDER BY r.played_at DESC, r.match_id DESC LIMIT ?", (player_id, *params, limit)).fetchall()


def form(rows, size=5):
    """Últimos resultados (V/E/D), do mais antigo ao mais recente."""
    return [outcome(row) for row in rows[:size]][::-1]


def opponents(db, player_id, game_id=None, limit=10):
    """Adversários mais enfrentados, com o retrospecto contra cada um."""
    where, params = _filters(game_id)
    rows = db.execute(
        f"{_SIDES} SELECT r.opponent_id, op.username, {_TOTALS} FROM results r "
        f"JOIN players op ON op.id = r.opponent_id WHERE r.player_id = ?{where} GROUP BY r.opponent_id "
        "ORDER BY COUNT(*) DESC, op.username COLLATE NOCASE LIMIT ?", (player_id, *params, limit)).fetchall()
    return [{"player_id": row["opponent_id"], "username": row["username"], **_record(row)} for row in rows]


def position(db, player_id, game_id=None):
    """Posição do jogador no ranking completo (None se ainda não jogou)."""
    for index, row in enumerate(ranking(db, game_id, limit=-1), 1):
        if row["player_id"] == player_id:
            return index
    return None


def players_with_matches(db, game_id=None):
    where, params = _filters(game_id)
    return db.execute(
        f"{_SIDES} SELECT DISTINCT p.id, p.username FROM results r JOIN players p ON p.id = r.player_id "
        f"WHERE 1 = 1{where} ORDER BY p.username COLLATE NOCASE", params).fetchall()


def compare(db, a, b, game_id=None):
    """Dois jogadores lado a lado: campanha geral de cada um, confronto
    direto (do ponto de vista de `a`) e os jogos entre eles."""
    return {
        "a": player_record(db, a, game_id), "b": player_record(db, b, game_id),
        "a_position": position(db, a, game_id), "b_position": position(db, b, game_id),
        "head_to_head": player_record(db, a, game_id, opponent_id=b),
        "matches": history(db, a, game_id, opponent_id=b, limit=5),
        "a_form": form(history(db, a, game_id, limit=5)), "b_form": form(history(db, b, game_id, limit=5)),
    }
