"""Rank por experiência (XP), estilo Fightcade: de F (todos começam aqui) a A.

O XP sai das partidas confirmadas (torneios e amistosos que valem -
friendly.py), refeitas em ordem cronológica (como em
stats.py, não há tabela: correção de placar, placar apagado ou torneio
cancelado recalculam tudo sozinhos). O rank de cada jogador é o do XP que ele
tinha antes de cada partida.

Regras (fixas no sistema):
- Vitória: quem ganha leva BASE no mesmo rank; com ranks diferentes,
  BASE + STEP * (rank do perdedor - rank do vencedor), nunca menos que
  MIN_POINTS: zebra (o mais fraco ganha) vale mais, o favorito vencendo vale menos.
- Derrota (loss): antes do Rank E ninguém perde XP - para todo mundo chegar
  ao E o quanto antes. Do E em diante a perda é pequena e cresce com o XP:
  1 logo que chega ao E, até 3 perto do D, e cada rank acima soma 2 (D 3-5,
  C 5-7, B 7-9, A 9-11) - não depende do adversário.
- Empate: ninguém perde. Com ranks diferentes, o mais fraco ganha
  STEP * diferença.
- O XP nunca fica negativo; perder XP pode fazer o jogador cair de rank.
- Pênaltis contam como empate (vale o placar do jogo, como no W$ e no V/E/D).
- Jogo de campeonato ou amistoso em que um jogador saiu da sessão no meio
  de um jogo (quitter_id, live.py) e não ganhou: ele perde QUIT_FACTOR vezes
  a perda de uma derrota, no mínimo QUIT_FACTOR (também antes do Rank E e no
  empate); quem ganhou ganha o normal. No amistoso, só no jogo de que ele saiu.
- Amistoso: cada jogo lido do jogo vale sozinho, pelo placar dele
  (FRIENDLY_RESULTS), com ou sem "Placar agregado" - o placar agregado é
  só para mostrar.
"""
from flask import g

from ..db import get_db

def friendly_results(ranked=1):
    """Resultados dos amistosos confirmados que valem (ranked=1 - friendly.py)
    ou dos que não valem (ranked=0, o card "Amistosos" do perfil - stats.py),
    do ponto de vista do amistoso (mandante x visitante): um por jogo lido do
    jogo pelo RetroArch TIERES (friendly_games, chave 'a<id>-<nº do jogo>')
    ou, sem eles, o placar informado pelos jogadores, como um jogo só (chave
    'a<id>')."""
    ranked = int(bool(ranked))
    return f"""
SELECT 'a' || f.id || '-' || fg.number AS match_key, f.id AS friendly_id, fg.number AS game_number,
       f.home_id, f.away_id, fg.home_goals, fg.away_goals, fg.home_pens, fg.away_pens,
       CASE WHEN fg.quit = 1 THEN f.quitter_id END AS quitter_id, f.confirmed_at, f.game_title_id
FROM friendly_matches f JOIN friendly_games fg ON fg.friendly_id = f.id
WHERE f.status = 'confirmed' AND f.ranked = {ranked} AND f.game_report = 1
UNION ALL
SELECT 'a' || f.id, f.id, NULL, f.home_id, f.away_id, f.home_goals, f.away_goals, NULL, NULL,
       f.quitter_id, f.confirmed_at, f.game_title_id
FROM friendly_matches f
WHERE f.status = 'confirmed' AND f.ranked = {ranked}
  AND NOT (f.game_report = 1 AND EXISTS (SELECT 1 FROM friendly_games fg WHERE fg.friendly_id = f.id))
"""


# Os que valem: usados aqui (XP) e no V/E/D/Ranking (stats.py).
FRIENDLY_RESULTS = friendly_results(1)

RANKS = ("F", "E", "D", "C", "B", "A")
# XP mínimo de cada rank: cada degrau pede mais que o anterior (100, 150, 200, 250, 300).
THRESHOLDS = (0, 100, 250, 450, 700, 1000)
COLORS = {"F": "#9ca3af", "E": "#22c55e", "D": "#3b82f6", "C": "#a855f7", "B": "#ef4444", "A": "#f59e0b"}
BASE = 20
STEP = 5
MIN_POINTS = 5
QUIT_FACTOR = 3

# Jogos de torneio (id = id do jogo) e resultados dos amistosos que valem
# (id = 'a<id>' ou 'a<id>-<nº do jogo>', como o match_key de stats.py), na
# ordem em que foram confirmados (os jogos do amistoso, na ordem em que foram jogados).
_MATCHES = f"""
SELECT id, home, away, home_goals, away_goals, quitter FROM (
    SELECT m.id, he.player_id AS home, ae.player_id AS away, m.home_goals, m.away_goals, m.quitter_id AS quitter,
           COALESCE(m.confirmed_at, m.updated_at) AS at, 0 AS kind, m.id AS seq, 0 AS sub
    FROM tournament_matches m
    JOIN tournament_stages s ON s.id = m.stage_id
    JOIN tournaments t ON t.id = s.tournament_id
    JOIN tournament_entries he ON he.id = m.home_entry_id
    JOIN tournament_entries ae ON ae.id = m.away_entry_id
    WHERE m.status = 'confirmed' AND t.status != 'cancelled'
    UNION ALL
    SELECT r.match_key, r.home_id, r.away_id, r.home_goals, r.away_goals, r.quitter_id, r.confirmed_at, 1,
           r.friendly_id, COALESCE(r.game_number, 0)
    FROM ({FRIENDLY_RESULTS}) r
)
ORDER BY at, kind, seq, sub
"""


def level(xp):
    """Índice do rank (0 = F ... 5 = A) para esse XP."""
    index = 0
    for i, minimum in enumerate(THRESHOLDS):
        if xp >= minimum:
            index = i
    return index


def points(winner_level, loser_level):
    """XP que o vencedor ganha numa vitória."""
    return max(MIN_POINTS, BASE + STEP * (loser_level - winner_level))


# Derrota: a perda no começo de cada rank (E 1, D 3, C 5, B 7, A 9) e quanto ela sobe até o
# rank seguinte (2: E vai de 1 a 3 perto do D). No A, sobe ao longo de A_SPAN XP e para.
LOSS_START, LOSS_RISE, A_SPAN = 1, 2, 300


def loss(xp):
    """XP que o jogador perde numa derrota, pelo XP dele antes do jogo: 0 antes do
    Rank E; do E em diante, de LOSS_START + LOSS_RISE * (rank - 1) no começo do rank
    até LOSS_RISE a mais perto do próximo."""
    index = level(xp)
    if index == 0:
        return 0
    low = THRESHOLDS[index]
    high = THRESHOLDS[index + 1] if index + 1 < len(THRESHOLDS) else low + A_SPAN
    progress = min(1.0, (xp - low) / (high - low))
    return round(LOSS_START + LOSS_RISE * (index - 1) + LOSS_RISE * progress)


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


def replay(matches, xp=None, peaks=None, tripled=None):
    """Aplica as partidas (id, home, away, home_goals, away_goals), em ordem, ao XP
    de cada jogador (`xp`, começando do zero se não vier). Com `peaks`, guarda
    nele o maior XP de cada jogador ao longo das partidas; com `tripled` (um
    set), os ids das partidas em que quem saiu no meio perdeu o triplo."""
    xp, deltas = dict(xp or {}), {}
    for m in matches:
        home, away = m["home"], m["away"]
        if home == away:
            continue
        a, b = xp.get(home, 0), xp.get(away, 0)
        la, lb = level(a), level(b)
        if m["home_goals"] > m["away_goals"]:
            d_home, d_away = points(la, lb), -min(loss(b), b)
        elif m["home_goals"] < m["away_goals"]:
            d_home, d_away = -min(loss(a), a), points(lb, la)
        else:
            # Empate: ninguém perde; com ranks diferentes, o mais fraco ganha.
            gain = STEP * abs(la - lb)
            d_home, d_away = (gain if la < lb else 0), (gain if lb < la else 0)
        # Quem saiu no meio de um jogo e não ganhou perde o triplo da perda de uma derrota,
        # no mínimo QUIT_FACTOR - também antes do Rank E e no empate (o XP nunca fica negativo).
        quitter = m["quitter"] if "quitter" in m.keys() else None
        if quitter == home and m["home_goals"] <= m["away_goals"]:
            d_home = -min(QUIT_FACTOR * max(loss(a), 1), a)
        elif quitter == away and m["away_goals"] <= m["home_goals"]:
            d_away = -min(QUIT_FACTOR * max(loss(b), 1), b)
        if tripled is not None and quitter in (home, away) and (d_home if quitter == home else d_away) < 0:
            tripled.add(m["id"])
        xp[home], xp[away] = a + d_home, b + d_away
        deltas[m["id"]] = {home: d_home, away: d_away}
        if peaks is not None:
            for player in (home, away):
                peaks[player] = max(peaks.get(player, 0), xp[player])
    return xp, deltas


def _cached():
    if "xp_table" not in g:
        tripled = set()
        g.xp_table = replay(get_db().execute(_MATCHES), tripled=tripled)
        g.xp_tripled = tripled
        g.pop("xp_friendlies", None)
    return g.xp_table


def player_xp(player_id):
    return _cached()[0].get(player_id, 0)


def match_deltas(match_id):
    return _cached()[1].get(match_id, {})


def match_tripled(match_id):
    """Na partida, quem saiu no meio perdeu o triplo de XP?"""
    _cached()
    return match_id in g.get("xp_tripled", ())


def friendly_deltas(friendly_id):
    """O XP de um amistoso que valeu: {"deltas": {player_id: soma dos jogos},
    "games": quantos resultados, "tripled": quem saiu no meio perdeu o
    triplo em algum} - ou None (não valeu, ou ainda não confirmado)."""
    deltas = _cached()[1]
    if "xp_friendlies" not in g:
        found = {}
        for key, change in deltas.items():
            if not (isinstance(key, str) and key.startswith("a")):
                continue
            item = found.setdefault(int(key[1:].split("-")[0]), {"deltas": {}, "games": 0, "tripled": False})
            item["games"] += 1
            item["tripled"] = item["tripled"] or key in g.xp_tripled
            for player, delta in change.items():
                item["deltas"][player] = item["deltas"].get(player, 0) + delta
        g.xp_friendlies = found
    return g.xp_friendlies.get(friendly_id)


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
