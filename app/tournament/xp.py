"""Rank por experiência (XP), estilo Fightcade: de F (todos começam aqui) a A.

O XP sai das partidas confirmadas (torneios e amistosos que valem -
friendly.py), refeitas em ordem cronológica (como em
stats.py, não há tabela: correção de placar, placar apagado ou torneio
cancelado recalculam tudo sozinhos). O rank de cada jogador é o do XP que ele
tinha antes de cada partida.

Regras (fixas no sistema):
- Vitória entre jogadores do mesmo rank: +BASE para quem ganha, -BASE para quem perde.
- Ranks diferentes: diff = rank do perdedor - rank do vencedor (em níveis).
  Ganha/perde BASE + STEP * diff, nunca menos que MIN_POINTS: zebra (o mais
  fraco ganha) vale mais, o favorito vencendo vale menos.
- Empate no mesmo rank: ninguém ganha nem perde. Ranks diferentes: o mais
  fraco ganha STEP * diferença e o mais forte perde o mesmo.
- O XP nunca fica negativo; perder XP pode fazer o jogador cair de rank.
- Pênaltis contam como empate (vale o placar do jogo, como no W$ e no V/E/D).
- Amistoso em que um jogador saiu da sessão no meio de um jogo (quitter_id,
  live.py): o que ele perder é multiplicado por QUIT_FACTOR; quem ganhou
  ganha o normal.
"""
from flask import g

from ..db import get_db

RANKS = ("F", "E", "D", "C", "B", "A")
# XP mínimo de cada rank: cada degrau pede mais que o anterior (100, 150, 200, 250, 300).
THRESHOLDS = (0, 100, 250, 450, 700, 1000)
COLORS = {"F": "#9ca3af", "E": "#22c55e", "D": "#3b82f6", "C": "#a855f7", "B": "#ef4444", "A": "#f59e0b"}
BASE = 20
STEP = 5
MIN_POINTS = 5
QUIT_FACTOR = 3

# Jogos de torneio (id = id do jogo) e amistosos que valem (id = 'a<id>',
# como o match_key de stats.py), na ordem em que foram confirmados.
_MATCHES = """
SELECT id, home, away, home_goals, away_goals, quitter FROM (
    SELECT m.id, he.player_id AS home, ae.player_id AS away, m.home_goals, m.away_goals, NULL AS quitter,
           COALESCE(m.confirmed_at, m.updated_at) AS at, 0 AS kind, m.id AS seq
    FROM tournament_matches m
    JOIN tournament_stages s ON s.id = m.stage_id
    JOIN tournaments t ON t.id = s.tournament_id
    JOIN tournament_entries he ON he.id = m.home_entry_id
    JOIN tournament_entries ae ON ae.id = m.away_entry_id
    WHERE m.status = 'confirmed' AND t.status != 'cancelled'
    UNION ALL
    SELECT 'a' || f.id, f.home_id, f.away_id, f.home_goals, f.away_goals, f.quitter_id, f.confirmed_at, 1, f.id
    FROM friendly_matches f WHERE f.status = 'confirmed' AND f.ranked = 1
)
ORDER BY at, kind, seq
"""


def level(xp):
    """Índice do rank (0 = F ... 5 = A) para esse XP."""
    index = 0
    for i, minimum in enumerate(THRESHOLDS):
        if xp >= minimum:
            index = i
    return index


def points(winner_level, loser_level):
    """XP que o vencedor ganha (e o perdedor perde) numa vitória."""
    return max(MIN_POINTS, BASE + STEP * (loser_level - winner_level))


def compute(db):
    """{player_id: xp} e {match_id: {player_id: variação}} de todas as partidas."""
    return replay(db.execute(_MATCHES))


def tournament_peaks(db):
    """{player_id: maior XP que o jogador já teve contando só os jogos de torneio}
    - libera os amistosos valendo (friendly.py): quem chegou lá uma vez continua
    liberado, mesmo perdendo XP depois."""
    peaks = {}
    replay([m for m in db.execute(_MATCHES) if not str(m["id"]).startswith("a")], peaks=peaks)
    return peaks


def replay(matches, xp=None, peaks=None):
    """Aplica as partidas (id, home, away, home_goals, away_goals), em ordem, ao XP
    de cada jogador (`xp`, começando do zero se não vier). Com `peaks`, guarda
    nele o maior XP de cada jogador ao longo das partidas."""
    xp, deltas = dict(xp or {}), {}
    for m in matches:
        home, away = m["home"], m["away"]
        if home == away:
            continue
        a, b = xp.get(home, 0), xp.get(away, 0)
        la, lb = level(a), level(b)
        if m["home_goals"] > m["away_goals"]:
            gain = points(la, lb)
            d_home, d_away = gain, -min(gain, b)
        elif m["home_goals"] < m["away_goals"]:
            gain = points(lb, la)
            d_home, d_away = -min(gain, a), gain
        elif la == lb:
            d_home = d_away = 0
        else:
            gain = STEP * abs(la - lb)
            if la < lb:  # mandante é o mais fraco
                d_home, d_away = gain, -min(gain, b)
            else:
                d_home, d_away = -min(gain, a), gain
        # Quem saiu no meio de um jogo perde o triplo (o XP nunca fica negativo).
        quitter = m["quitter"] if "quitter" in m.keys() else None
        if quitter == home and d_home < 0:
            d_home = -min(-d_home * QUIT_FACTOR, a)
        elif quitter == away and d_away < 0:
            d_away = -min(-d_away * QUIT_FACTOR, b)
        xp[home], xp[away] = a + d_home, b + d_away
        deltas[m["id"]] = {home: d_home, away: d_away}
        if peaks is not None:
            for player in (home, away):
                peaks[player] = max(peaks.get(player, 0), xp[player])
    return xp, deltas


def _cached():
    if "xp_table" not in g:
        g.xp_table = compute(get_db())
    return g.xp_table


def player_xp(player_id):
    return _cached()[0].get(player_id, 0)


def match_deltas(match_id):
    return _cached()[1].get(match_id, {})


def info(player_id):
    """Rank, cor e a barra de experiência até o próximo rank."""
    xp = player_xp(player_id)
    index = level(xp)
    rank = RANKS[index]
    result = {"xp": xp, "rank": rank, "color": COLORS[rank], "level": index, "max": index == len(RANKS) - 1}
    if result["max"]:
        result.update(next=None, floor=THRESHOLDS[index], goal=None, have=xp - THRESHOLDS[index], need=0, missing=0,
                      percent=100)
    else:
        floor, goal = THRESHOLDS[index], THRESHOLDS[index + 1]
        result.update(next=RANKS[index + 1], floor=floor, goal=goal, have=xp - floor, need=goal - floor,
                      missing=goal - xp, percent=round(100 * (xp - floor) / (goal - floor)))
    return result


def table():
    """Os degraus (rank, XP mínimo, cor), para a explicação das regras."""
    return [{"rank": r, "min": THRESHOLDS[i], "color": COLORS[r],
             "step": THRESHOLDS[i + 1] - THRESHOLDS[i] if i + 1 < len(RANKS) else None}
            for i, r in enumerate(RANKS)]
