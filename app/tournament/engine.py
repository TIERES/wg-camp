"""Regras puras dos torneios. Os participantes são ids quaisquer (no banco,
tournament_entries.id); nada aqui sabe de jogador, conta ou W$."""
import random
from collections import namedtuple

# Um jogo com placar confirmado (gols sem os pênaltis).
Result = namedtuple("Result", "home away home_goals away_goals")

POINTS_WIN, POINTS_DRAW, POINTS_LOSS = 3, 1, 0


# --- Pontos corridos / grupos -------------------------------------------------

def round_robin(entries, legs=1):
    """Tabela de jogos de todos contra todos (método do círculo).

    Retorna [(rodada, mandante, visitante)], rodadas a partir de 1. Com número
    ímpar de participantes, um folga em cada rodada. Com legs=2 o returno
    repete o turno com o mando invertido."""
    players = list(entries)
    if len(players) < 2:
        return []
    if len(players) % 2:
        players.append(None)
    n = len(players)
    fixed, rotating = players[0], players[1:]
    first_leg = []
    for r in range(n - 1):
        current = [fixed] + rotating
        for i in range(n // 2):
            a, b = current[i], current[n - 1 - i]
            if a is None or b is None:
                continue
            # O fixo alterna o mando a cada rodada; nos outros pares o da
            # esquerda joga em casa - com o giro, ninguém fica com mais de um
            # jogo em casa a mais que os outros no turno.
            if i == 0 and r % 2:
                a, b = b, a
            first_leg.append((r + 1, a, b))
        rotating = rotating[-1:] + rotating[:-1]
    fixtures = list(first_leg)
    if legs == 2:
        fixtures += [(rnd + n - 1, away, home) for rnd, home, away in first_leg]
    return fixtures


def _blank_row(entry_id):
    return {"entry_id": entry_id, "played": 0, "won": 0, "drawn": 0, "lost": 0,
            "goals_for": 0, "goals_against": 0, "goal_diff": 0, "points": 0, "form": []}


def _table(entry_ids, results):
    rows = {entry_id: _blank_row(entry_id) for entry_id in entry_ids}
    for result in results:
        home, away = rows.get(result.home), rows.get(result.away)
        if home is None or away is None:
            continue
        for row, scored, conceded in ((home, result.home_goals, result.away_goals),
                                      (away, result.away_goals, result.home_goals)):
            row["played"] += 1
            row["goals_for"] += scored
            row["goals_against"] += conceded
            if scored > conceded:
                row["won"] += 1
                row["points"] += POINTS_WIN
                row["form"].append("V")
            elif scored == conceded:
                row["drawn"] += 1
                row["points"] += POINTS_DRAW
                row["form"].append("E")
            else:
                row["lost"] += 1
                row["points"] += POINTS_LOSS
                row["form"].append("D")
    for row in rows.values():
        row["goal_diff"] = row["goals_for"] - row["goals_against"]
        row["form"] = row["form"][-5:]
    return rows


def _base_key(row):
    return (-row["points"], -row["won"], -row["goal_diff"], -row["goals_for"])


def standings(entry_ids, results):
    """Classificação. Desempate: pontos, vitórias, saldo, gols pró, confronto
    direto (pontos, saldo e gols só entre os empatados) e, por fim, a ordem do
    sorteio (a ordem de entry_ids). `results` em ordem cronológica (a coluna
    "últimos jogos" usa essa ordem)."""
    entry_ids = list(entry_ids)
    draw_order = {entry_id: index for index, entry_id in enumerate(entry_ids)}
    results = list(results)
    rows = _table(entry_ids, results)
    ordered = sorted(rows.values(), key=lambda row: (_base_key(row), draw_order[row["entry_id"]]))
    table = []
    i = 0
    while i < len(ordered):
        j = i
        while j < len(ordered) and _base_key(ordered[j]) == _base_key(ordered[i]):
            j += 1
        tied = ordered[i:j]
        if len(tied) > 1:
            ids = {row["entry_id"] for row in tied}
            mini = _table(ids, [r for r in results if r.home in ids and r.away in ids])
            tied.sort(key=lambda row: (-mini[row["entry_id"]]["points"], -mini[row["entry_id"]]["goal_diff"],
                                       -mini[row["entry_id"]]["goals_for"], draw_order[row["entry_id"]]))
        table.extend(tied)
        i = j
    for position, row in enumerate(table, 1):
        row["position"] = position
    return table


# --- Sorteio de grupos e potes -----------------------------------------------

def draw_groups(entries, group_count, rng=random):
    """Distribui os participantes em `group_count` grupos, em sorteio (os
    tamanhos diferem no máximo em 1)."""
    shuffled = list(entries)
    rng.shuffle(shuffled)
    groups = [[] for _ in range(group_count)]
    for index, entry_id in enumerate(shuffled):
        groups[index % group_count].append(entry_id)
    return groups


def group_name(index):
    return chr(ord("A") + index) if index < 26 else str(index + 1)


# --- Fase de liga da Liga dos Campeões (formato a partir de 2024) ------------

def swiss_fixtures(pots, per_pot, rng=random):
    """Sorteio da fase de liga: `pots` é a lista de potes (todos do mesmo
    tamanho). Cada participante enfrenta `per_pot` adversários de cada pote,
    inclusive do seu. Com per_pot=2, um desses jogos é em casa e o outro fora.

    Retorna [(rodada, mandante, visitante)]. As rodadas são montadas para que
    ninguém jogue duas vezes na mesma rodada."""
    size = len(pots[0])
    pairs = []  # (mandante, visitante)
    for a in range(len(pots)):
        for b in range(a, len(pots)):
            if a == b:
                pot = list(pots[a])
                rng.shuffle(pot)
                if per_pot == 1:
                    for i in range(0, size, 2):
                        pairs.append((pot[i], pot[i + 1]) if rng.random() < 0.5 else (pot[i + 1], pot[i]))
                else:
                    # Um ciclo: cada um recebe o anterior e visita o seguinte.
                    for i in range(size):
                        pairs.append((pot[i], pot[(i + 1) % size]))
            else:
                first, second = list(pots[a]), list(pots[b])
                rng.shuffle(first)
                rng.shuffle(second)
                for i in range(size):
                    if per_pot == 1:
                        # Alterna o mando entre os dois potes.
                        pair = (first[i], second[i])
                        pairs.append(pair if i % 2 == 0 else pair[::-1])
                    else:
                        # Dois emparelhamentos diferentes: no primeiro o pote a
                        # joga em casa, no segundo (deslocado de 1) fora.
                        pairs.append((first[i], second[i]))
                        pairs.append((second[(i + 1) % size], first[i]))
    return _assign_rounds(pairs, rng)


def _assign_rounds(pairs, rng):
    """Põe cada jogo na primeira rodada em que nenhum dos dois já joga."""
    pairs = list(pairs)
    rng.shuffle(pairs)
    busy = []  # busy[r] = participantes que já jogam na rodada r
    fixtures = []
    for home, away in pairs:
        for r, players in enumerate(busy):
            if home not in players and away not in players:
                break
        else:
            busy.append(set())
            r = len(busy) - 1
        busy[r].update((home, away))
        fixtures.append((r + 1, home, away))
    fixtures.sort(key=lambda fixture: fixture[0])
    return fixtures


# --- Mata-mata ----------------------------------------------------------------

def bracket_size(count):
    size = 1
    while size < count:
        size *= 2
    return size


def seed_positions(size):
    """Ordem padrão de chave com cabeças de chave: [1, 16, 8, 9, ...] - o 1º e
    o 2º só se encontram na final."""
    order = [1]
    while len(order) < size:
        n = len(order) * 2
        order = [seed for s in order for seed in (s, n + 1 - s)]
    return order


def seeded_pairs(seeded):
    """Confrontos da primeira rodada, na ordem da chave, a partir da lista do
    melhor para o pior. Quem sobra quando não é potência de 2 enfrenta None
    (passa direto) - sempre os melhores colocados."""
    size = bracket_size(max(len(seeded), 2))
    slots = [seeded[s - 1] if s <= len(seeded) else None for s in seed_positions(size)]
    return [(slots[i], slots[i + 1]) for i in range(0, size, 2)]


def cross_pairs(group_tables):
    """Cruzamento estilo Copa do Mundo (2 classificados por grupo, número de
    grupos potência de 2): 1ºA x 2ºB e 1ºC x 2ºD numa metade da chave, 1ºB x
    2ºA e 1ºD x 2ºC na outra - quem é do mesmo grupo só se reencontra na final."""
    top, bottom = [], []
    for g in range(0, len(group_tables), 2):
        a, b = group_tables[g], group_tables[g + 1]
        top.append((a[0], b[1]))
        bottom.append((b[0], a[1]))
    return top + bottom


def round_name(ties_in_round, first_round_name=None, is_first_round=False):
    if is_first_round and first_round_name:
        return first_round_name
    return {1: "Final", 2: "Semifinal", 4: "Quartas de final", 8: "Oitavas de final"}.get(
        ties_in_round, f"{ties_in_round * 2}-avos de final")


def tie_outcome(entry_a, entry_b, legs, results, decisive_pens=None, away_goals=False):
    """Vencedor de um confronto de mata-mata, ou None se ainda não decidido.

    `results`: os jogos confirmados do confronto (Result). `legs`: 1 ou 2 -
    só decide com todos os jogos confirmados. Empate no agregado: gol fora
    (se `away_goals`) e depois os pênaltis do último jogo, `decisive_pens` =
    (pênaltis de entry_a, pênaltis de entry_b)."""
    results = list(results)
    if len(results) < legs:
        return None
    goals = {entry_a: 0, entry_b: 0}
    away = {entry_a: 0, entry_b: 0}
    for result in results:
        goals[result.home] += result.home_goals
        goals[result.away] += result.away_goals
        away[result.away] += result.away_goals
    if goals[entry_a] != goals[entry_b]:
        return entry_a if goals[entry_a] > goals[entry_b] else entry_b
    if away_goals and legs == 2 and away[entry_a] != away[entry_b]:
        return entry_a if away[entry_a] > away[entry_b] else entry_b
    if decisive_pens and None not in decisive_pens and decisive_pens[0] != decisive_pens[1]:
        return entry_a if decisive_pens[0] > decisive_pens[1] else entry_b
    return None


# --- Prêmio -------------------------------------------------------------------

def parse_split(text):
    """"60,30,10" -> [60, 30, 10]; ValueError se não forem 3 inteiros >= 0 e
    decrescentes somando 100."""
    try:
        parts = [int(part) for part in str(text).replace(" ", "").split(",")]
    except ValueError:
        raise ValueError("Divisão do prêmio inválida: use três números, ex. 60,30,10.")
    if len(parts) != 3 or any(p < 0 for p in parts) or sum(parts) != 100 or parts != sorted(parts, reverse=True):
        raise ValueError("A divisão do prêmio precisa de 3 percentuais em ordem decrescente que somem 100.")
    return parts


def split_prize(pool_cents, percents, places):
    """Valor de cada colocação (1º, 2º, 3º...). Sobra de centavos e a parte de
    colocações que não existem (torneio com 2 jogadores, sem 3º) vão para o 1º."""
    if places <= 0 or pool_cents <= 0:
        return []
    shares = [pool_cents * p // 100 for p in percents[:places]]
    shares[0] += pool_cents - sum(shares)
    return shares
