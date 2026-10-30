"""Torneios no banco: inscrição, início, resultados, avanço de fase,
encerramento com prêmio e cancelamento com reembolso.

Recebe a conexão sqlite3 (com row_factory = Row) e não faz commit - a view
faz commit no fim, ou rollback se vier TournamentError / InsufficientFunds.
"""
import json
import random
import secrets

from ..db import now
from ..wallet import ensure_wallet, post, setting
from . import engine, formats


class TournamentError(Exception):
    pass


def settings_of(row):
    try:
        return json.loads(row["settings_json"] or "{}")
    except ValueError:
        return {}


def get_tournament(db, tournament_id):
    return db.execute("SELECT * FROM tournaments WHERE id = ?", (tournament_id,)).fetchone()


def entries_of(db, tournament_id):
    return db.execute(
        "SELECT e.*, p.username FROM tournament_entries e JOIN players p ON p.id = e.player_id "
        "WHERE e.tournament_id = ? ORDER BY e.id", (tournament_id,)).fetchall()


def stages_of(db, tournament_id):
    return db.execute("SELECT * FROM tournament_stages WHERE tournament_id = ? ORDER BY seq",
                      (tournament_id,)).fetchall()


def prize_pool(db, tournament_id):
    row = db.execute("SELECT COALESCE(-SUM(amount_cents), 0) FROM wallet_entries "
                     "WHERE tournament_id = ? AND kind IN ('inscricao', 'reembolso')", (tournament_id,)).fetchone()
    return row[0]


# --- Inscrição ----------------------------------------------------------------

def enroll(db, tournament, player_id, team_name=""):
    if tournament["status"] != "registration":
        raise TournamentError("As inscrições deste torneio não estão abertas.")
    count = db.execute("SELECT COUNT(*) FROM tournament_entries WHERE tournament_id = ?",
                       (tournament["id"],)).fetchone()[0]
    if count >= tournament["max_players"]:
        raise TournamentError("Não há mais vagas neste torneio.")
    if db.execute("SELECT 1 FROM tournament_entries WHERE tournament_id = ? AND player_id = ?",
                  (tournament["id"], player_id)).fetchone():
        raise TournamentError("Você já está inscrito neste torneio.")
    ensure_wallet(db, player_id)
    fee_ref = None
    if tournament["entry_fee_cents"]:
        fee_ref = f"inscricao:{tournament['id']}:{player_id}:{secrets.token_hex(6)}"
        post(db, player_id, -tournament["entry_fee_cents"], "inscricao", fee_ref, tournament_id=tournament["id"],
             note=tournament["name"])
    db.execute("INSERT INTO tournament_entries (tournament_id, player_id, team_name, fee_ref, created_at) "
               "VALUES (?, ?, ?, ?, ?)", (tournament["id"], player_id, team_name.strip()[:60], fee_ref, now()))


def _refund(db, tournament, entry):
    if entry["fee_ref"]:
        post(db, entry["player_id"], tournament["entry_fee_cents"], "reembolso", "reembolso:" + entry["fee_ref"],
             tournament_id=tournament["id"], note=tournament["name"])


def withdraw(db, tournament, entry):
    """Sai do torneio (ou é removido pelo organizador) antes do início, com
    a inscrição devolvida."""
    if tournament["status"] not in ("draft", "registration"):
        raise TournamentError("O torneio já começou: não dá mais para sair.")
    _refund(db, tournament, entry)
    db.execute("DELETE FROM tournament_entries WHERE id = ?", (entry["id"],))


def cancel(db, tournament):
    if tournament["status"] in ("finished", "cancelled"):
        raise TournamentError("Este torneio já foi encerrado.")
    for entry in entries_of(db, tournament["id"]):
        _refund(db, tournament, entry)
    db.execute("UPDATE tournaments SET status = 'cancelled', finished_at = ?, updated_at = ? WHERE id = ?",
               (now(), now(), tournament["id"]))


# --- Criação das fases --------------------------------------------------------

def _new_stage(db, tournament, seq, kind, name, settings=None):
    return db.execute(
        "INSERT INTO tournament_stages (tournament_id, seq, kind, name, settings_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tournament["id"], seq, kind, name, json.dumps(settings or {}), now())).lastrowid


def _new_group(db, stage_id, seq, name, members):
    group_id = db.execute("INSERT INTO tournament_groups (stage_id, name, seq) VALUES (?, ?, ?)",
                          (stage_id, name, seq)).lastrowid
    for order, entry_id in enumerate(members):
        db.execute("INSERT INTO tournament_group_members (group_id, entry_id, draw_order) VALUES (?, ?, ?)",
                   (group_id, entry_id, order))
    return group_id


def _new_match(db, stage_id, round_no, home, away, group_id=None, tie_id=None, leg=1):
    db.execute(
        "INSERT INTO tournament_matches (stage_id, group_id, tie_id, round, leg, home_entry_id, away_entry_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (stage_id, group_id, tie_id, round_no, leg, home, away, now(), now()))


def _round_robin_stage(db, tournament, seq, kind, name, groups, legs):
    stage_id = _new_stage(db, tournament, seq, kind, name, {"legs": legs})
    for index, members in enumerate(groups):
        label = "Tabela" if kind == "league" else f"Grupo {engine.group_name(index)}"
        group_id = _new_group(db, stage_id, index, label, members)
        for round_no, home, away in engine.round_robin(members, legs):
            _new_match(db, stage_id, round_no, home, away, group_id=group_id)
    return stage_id


def _swiss_stage(db, tournament, seq, entries, config, rng):
    pots_count = config["pots"]
    size = len(entries) // pots_count
    pots = [entries[i * size:(i + 1) * size] for i in range(pots_count)]
    stage_id = _new_stage(db, tournament, seq, "swiss", "Fase de liga", {"pots": pots, "per_pot": config["per_pot"]})
    group_id = _new_group(db, stage_id, 0, "Fase de liga", entries)
    for round_no, home, away in engine.swiss_fixtures(pots, config["per_pot"], rng):
        _new_match(db, stage_id, round_no, home, away, group_id=group_id)
    return stage_id


def _knockout_stage(db, tournament, seq, pairs, legs, final_legs, third_place, first_round_name=None):
    stage_id = _new_stage(db, tournament, seq, "knockout", "Mata-mata", {
        "legs": legs, "final_legs": final_legs, "third_place": bool(third_place),
        "first_round_name": first_round_name})
    rounds = (len(pairs) * 2).bit_length() - 1
    ties = {}
    for round_no in range(rounds, 0, -1):
        for position in range(len(pairs) * 2 >> round_no):
            is_final = round_no == rounds
            ties[(round_no, position)] = db.execute(
                "INSERT INTO tournament_ties (stage_id, round, position, legs, next_tie_id, next_slot) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (stage_id, round_no, position, final_legs if is_final else legs,
                 None if is_final else ties[(round_no + 1, position // 2)],
                 None if is_final else ("a" if position % 2 == 0 else "b"))).lastrowid
    # Chave de 4 com alguém passando direto: só um semifinalista perde, não
    # há disputa de 3º (o 3º sai pela campanha, em final_ranking).
    if third_place and rounds >= 2 and not (rounds == 2 and any(None in pair for pair in pairs)):
        third = db.execute("INSERT INTO tournament_ties (stage_id, round, position, legs, is_third_place) "
                           "VALUES (?, ?, 1, 1, 1)", (stage_id, rounds)).lastrowid
        for position, slot in ((0, "a"), (1, "b")):
            db.execute("UPDATE tournament_ties SET loser_next_tie_id = ?, loser_next_slot = ? WHERE id = ?",
                       (third, slot, ties[(rounds - 1, position)]))
    for position, (entry_a, entry_b) in enumerate(pairs):
        tie_id = ties[(1, position)]
        if entry_a is None or entry_b is None:
            winner = entry_a if entry_b is None else entry_b
            db.execute("UPDATE tournament_ties SET entry_a = ?, is_bye = 1 WHERE id = ?", (winner, tie_id))
            _set_winner(db, _tie(db, tie_id), winner)
        else:
            _fill_slot(db, tie_id, "a", entry_a)
            _fill_slot(db, tie_id, "b", entry_b)
    return stage_id


def start(db, tournament, rng=random):
    if tournament["status"] != "registration":
        raise TournamentError("Abra as inscrições antes de iniciar o torneio.")
    entries = [row["id"] for row in entries_of(db, tournament["id"])]
    config = settings_of(tournament)
    error = formats.config_error(tournament["format"], config, len(entries))
    if error:
        raise TournamentError(error)
    rng.shuffle(entries)  # o sorteio: ordem da tabela, grupos, potes e chave
    fmt = tournament["format"]
    if fmt == "league":
        _round_robin_stage(db, tournament, 1, "league", "Pontos corridos", [entries], config["legs"])
    elif fmt in ("world_cup", "libertadores"):
        _round_robin_stage(db, tournament, 1, "groups", "Fase de grupos",
                           engine.draw_groups(entries, config["groups"], rng), config["group_legs"])
    elif fmt == "ucl":
        _swiss_stage(db, tournament, 1, entries, config, rng)
    else:
        _knockout_stage(db, tournament, 1, engine.seeded_pairs(entries), config["ko_legs"],
                        config["final_legs"], config["third_place"])
    db.execute("UPDATE tournaments SET status = 'running', started_at = ?, updated_at = ? WHERE id = ?",
               (now(), now(), tournament["id"]))


# --- Mata-mata: confrontos ----------------------------------------------------

def _tie(db, tie_id):
    return db.execute("SELECT * FROM tournament_ties WHERE id = ?", (tie_id,)).fetchone()


def _fill_slot(db, tie_id, slot, entry_id):
    db.execute(f"UPDATE tournament_ties SET entry_{slot} = ? WHERE id = ?", (entry_id, tie_id))
    tie = _tie(db, tie_id)
    if tie["entry_a"] and tie["entry_b"]:
        a, b = tie["entry_a"], tie["entry_b"]
        if tie["legs"] == 1:
            _new_match(db, tie["stage_id"], tie["round"], a, b, tie_id=tie_id)
        else:
            # O melhor colocado (entry_a) decide em casa.
            _new_match(db, tie["stage_id"], tie["round"], b, a, tie_id=tie_id, leg=1)
            _new_match(db, tie["stage_id"], tie["round"], a, b, tie_id=tie_id, leg=2)


def _clear_slot(db, tie_id, slot):
    db.execute("DELETE FROM tournament_matches WHERE tie_id = ?", (tie_id,))
    db.execute(f"UPDATE tournament_ties SET entry_{slot} = NULL, winner_entry_id = NULL WHERE id = ?", (tie_id,))


def _has_results(db, tie_id):
    return db.execute("SELECT 1 FROM tournament_matches WHERE tie_id = ? AND status = 'confirmed'",
                      (tie_id,)).fetchone() is not None


def _set_winner(db, tie, winner):
    """Grava o vencedor e leva vencedor/perdedor para os próximos confrontos."""
    if tie["winner_entry_id"] == winner:
        return
    for target in (tie["next_tie_id"], tie["loser_next_tie_id"]):
        if target and _has_results(db, target):
            raise TournamentError("Esse resultado muda quem avançou, mas o confronto seguinte já tem resultado. "
                                  "Corrija primeiro o resultado da fase seguinte.")
    if tie["next_tie_id"] and tie["winner_entry_id"]:
        _clear_slot(db, tie["next_tie_id"], tie["next_slot"])
    if tie["loser_next_tie_id"] and tie["winner_entry_id"]:
        _clear_slot(db, tie["loser_next_tie_id"], tie["loser_next_slot"])
    db.execute("UPDATE tournament_ties SET winner_entry_id = ? WHERE id = ?", (winner, tie["id"]))
    if winner is None:
        return
    if tie["next_tie_id"]:
        _fill_slot(db, tie["next_tie_id"], tie["next_slot"], winner)
    if tie["loser_next_tie_id"] and not tie["is_bye"]:
        loser = tie["entry_b"] if winner == tie["entry_a"] else tie["entry_a"]
        _fill_slot(db, tie["loser_next_tie_id"], tie["loser_next_slot"], loser)


def _results(rows):
    return [engine.Result(r["home_entry_id"], r["away_entry_id"], r["home_goals"], r["away_goals"]) for r in rows]


def tie_matches(db, tie_id):
    return db.execute("SELECT * FROM tournament_matches WHERE tie_id = ? ORDER BY leg", (tie_id,)).fetchall()


def needs_pens(db, match):
    """Último jogo de um confronto de mata-mata empatado no agregado (contando
    este placar)? Então os pênaltis decidem."""
    if not match["tie_id"]:
        return False
    tie = _tie(db, match["tie_id"])
    return match["leg"] == tie["legs"]


def _tie_outcome(db, tie):
    matches = tie_matches(db, tie["id"])
    confirmed = [m for m in matches if m["status"] == "confirmed"]
    last = matches[-1] if matches else None
    pens = (last["home_pens"], last["away_pens"]) if last is not None and last["status"] == "confirmed" else None
    return engine.tie_outcome(tie["entry_a"], tie["entry_b"], tie["legs"], _results(confirmed), pens), len(confirmed)


# --- Resultados e W$ por jogo -------------------------------------------------

def _pay_match(db, tournament, match):
    match_id, version = match["id"], match["result_version"]
    nets = db.execute("SELECT player_id, SUM(amount_cents) AS net FROM wallet_entries WHERE match_id = ? "
                      "GROUP BY player_id", (match_id,)).fetchall()
    for row in nets:
        if row["net"]:
            post(db, row["player_id"], -row["net"], "estorno", f"estorno:partida:{match_id}:v{version}:{row['player_id']}",
                 tournament_id=tournament["id"], match_id=match_id, note=tournament["name"], allow_negative=True)
    if match["status"] != "confirmed":
        return
    for entry_id, scored, conceded in ((match["home_entry_id"], match["home_goals"], match["away_goals"]),
                                       (match["away_entry_id"], match["away_goals"], match["home_goals"])):
        kind = "vitoria" if scored > conceded else "empate" if scored == conceded else "derrota"
        amount = setting(db, "torneios_" + kind)
        player_id = db.execute("SELECT player_id FROM tournament_entries WHERE id = ?", (entry_id,)).fetchone()[0]
        ensure_wallet(db, player_id)
        if amount:
            post(db, player_id, amount, kind, f"partida:{match_id}:v{version}:{player_id}",
                 tournament_id=tournament["id"], match_id=match_id, note=tournament["name"])


def record_result(db, tournament, match_id, home_goals, away_goals, home_pens=None, away_pens=None):
    """Grava (ou corrige) o placar de um jogo, paga o W$ do jogo (estornando o
    resultado anterior, se havia) e, no mata-mata, avança o vencedor."""
    match = db.execute(
        "SELECT m.*, s.status AS stage_status, s.tournament_id FROM tournament_matches m "
        "JOIN tournament_stages s ON s.id = m.stage_id WHERE m.id = ?", (match_id,)).fetchone()
    if match is None or match["tournament_id"] != tournament["id"]:
        raise TournamentError("Jogo não encontrado.")
    if tournament["status"] != "running":
        raise TournamentError("O torneio não está em andamento.")
    if match["stage_status"] != "running":
        raise TournamentError("Esta fase já foi encerrada: os resultados dela não podem mais mudar.")
    for value in (home_goals, away_goals):
        if value is None or not 0 <= value <= 99:
            raise TournamentError("Placar inválido.")
    if not needs_pens(db, match):
        home_pens = away_pens = None
    db.execute(
        "UPDATE tournament_matches SET home_goals = ?, away_goals = ?, home_pens = ?, away_pens = ?, "
        "status = 'confirmed', result_version = result_version + 1, confirmed_at = ?, updated_at = ? WHERE id = ?",
        (home_goals, away_goals, home_pens, away_pens, now(), now(), match_id))
    _pay_match(db, tournament, db.execute("SELECT * FROM tournament_matches WHERE id = ?", (match_id,)).fetchone())
    if match["tie_id"]:
        tie = _tie(db, match["tie_id"])
        legs = tie_matches(db, tie["id"])
        if all(m["status"] == "confirmed" for m in legs):
            goals = {tie["entry_a"]: 0, tie["entry_b"]: 0}
            for m in legs:
                goals[m["home_entry_id"]] += m["home_goals"]
                goals[m["away_entry_id"]] += m["away_goals"]
            if goals[tie["entry_a"]] != goals[tie["entry_b"]]:
                # Decidido no tempo normal: pênaltis não valem.
                db.execute("UPDATE tournament_matches SET home_pens = NULL, away_pens = NULL WHERE tie_id = ?",
                           (tie["id"],))
        winner, confirmed = _tie_outcome(db, tie)
        if winner is None and confirmed == tie["legs"]:
            raise TournamentError("Empate no agregado: informe os pênaltis do último jogo do confronto "
                                  "(e eles não podem terminar empatados).")
        _set_winner(db, tie, winner)
    db.execute("UPDATE tournaments SET updated_at = ? WHERE id = ?", (now(), tournament["id"]))


def clear_result(db, tournament, match_id):
    """Apaga o placar de um jogo (lançado por engano), estornando o W$."""
    match = db.execute(
        "SELECT m.*, s.status AS stage_status, s.tournament_id FROM tournament_matches m "
        "JOIN tournament_stages s ON s.id = m.stage_id WHERE m.id = ?", (match_id,)).fetchone()
    if match is None or match["tournament_id"] != tournament["id"] or match["status"] != "confirmed":
        raise TournamentError("Jogo não encontrado ou sem resultado.")
    if match["stage_status"] != "running" or tournament["status"] != "running":
        raise TournamentError("Esta fase já foi encerrada: os resultados dela não podem mais mudar.")
    if match["tie_id"]:
        _set_winner(db, _tie(db, match["tie_id"]), None)
    db.execute("UPDATE tournament_matches SET home_goals = NULL, away_goals = NULL, home_pens = NULL, "
               "away_pens = NULL, status = 'scheduled', result_version = result_version + 1, confirmed_at = NULL, "
               "updated_at = ? WHERE id = ?", (now(), match_id))
    _pay_match(db, tournament, db.execute("SELECT * FROM tournament_matches WHERE id = ?", (match_id,)).fetchone())


# --- Classificação, avanço de fase e encerramento ----------------------------

def group_tables(db, stage_id):
    """[(grupo, classificação)] de uma fase de liga/grupos/suíça."""
    tables = []
    for group in db.execute("SELECT * FROM tournament_groups WHERE stage_id = ? ORDER BY seq", (stage_id,)):
        members = [row["entry_id"] for row in db.execute(
            "SELECT entry_id FROM tournament_group_members WHERE group_id = ? ORDER BY draw_order", (group["id"],))]
        played = db.execute("SELECT * FROM tournament_matches WHERE group_id = ? AND status = 'confirmed' "
                            "ORDER BY round, confirmed_at", (group["id"],)).fetchall()
        tables.append((group, engine.standings(members, _results(played))))
    return tables


def stage_complete(db, stage):
    if stage["kind"] != "knockout":
        return db.execute("SELECT 1 FROM tournament_matches WHERE stage_id = ? AND status != 'confirmed'",
                          (stage["id"],)).fetchone() is None
    final_round = db.execute("SELECT MAX(round) FROM tournament_ties WHERE stage_id = ?", (stage["id"],)).fetchone()[0]
    return db.execute("SELECT 1 FROM tournament_ties WHERE stage_id = ? AND round = ? AND winner_entry_id IS NULL",
                      (stage["id"], final_round)).fetchone() is None


def has_next_stage(tournament, stage):
    return tournament["format"] in ("world_cup", "libertadores", "ucl") and stage["kind"] != "knockout"


def advance(db, tournament):
    """Encerra a fase atual (os resultados dela ficam travados) e sorteia /
    monta a próxima a partir da classificação."""
    stage = stages_of(db, tournament["id"])[-1]
    if tournament["status"] != "running" or not has_next_stage(tournament, stage):
        raise TournamentError("Não há próxima fase para gerar.")
    if not stage_complete(db, stage):
        raise TournamentError("Ainda há jogos sem resultado nesta fase.")
    config = settings_of(tournament)
    tables = group_tables(db, stage["id"])
    if stage["kind"] == "swiss":
        table = tables[0][1]
        seeded = [row["entry_id"] for row in table[:config["direct"] * 3]]
        pairs, first_round_name = engine.seeded_pairs(seeded), "Playoff"
    else:
        qualifiers = config["qualifiers"]
        groups = len(tables)
        if (tournament["format"] == "world_cup" and qualifiers == 2 and groups >= 2
                and groups & (groups - 1) == 0):
            pairs = engine.cross_pairs([[row["entry_id"] for row in table] for _, table in tables])
        else:
            # Classificados ordenados por colocação no grupo e depois campanha
            # (estilo Libertadores: 1ºs contra 2ºs, melhor campanha decide em casa).
            ranked = [(row["position"], -row["points"], -row["goal_diff"], -row["goals_for"], -row["won"], index,
                       row["entry_id"])
                      for index, (_, table) in enumerate(tables) for row in table[:qualifiers]]
            pairs = engine.seeded_pairs([item[-1] for item in sorted(ranked)])
        first_round_name = None
    db.execute("UPDATE tournament_stages SET status = 'done' WHERE id = ?", (stage["id"],))
    _knockout_stage(db, tournament, stage["seq"] + 1, pairs, config["ko_legs"], config["final_legs"],
                    config["third_place"], first_round_name)


def final_ranking(db, tournament):
    """Ids das inscrições em 1º, 2º e 3º (menos se não houver)."""
    stage = stages_of(db, tournament["id"])[-1]
    if stage["kind"] != "knockout":
        return [row["entry_id"] for row in group_tables(db, stage["id"])[0][1][:3]]
    final_round = db.execute("SELECT MAX(round) FROM tournament_ties WHERE stage_id = ?", (stage["id"],)).fetchone()[0]
    final = db.execute("SELECT * FROM tournament_ties WHERE stage_id = ? AND round = ? AND is_third_place = 0",
                       (stage["id"], final_round)).fetchone()
    winner = final["winner_entry_id"]
    ranking = [winner, final["entry_b"] if winner == final["entry_a"] else final["entry_a"]]
    third = db.execute("SELECT winner_entry_id FROM tournament_ties WHERE stage_id = ? AND is_third_place = 1",
                       (stage["id"],)).fetchone()
    if third:
        ranking.append(third["winner_entry_id"])
        return ranking
    # Sem disputa de 3º: melhor campanha no torneio entre os eliminados na semifinal.
    semis = db.execute("SELECT * FROM tournament_ties WHERE stage_id = ? AND round = ? AND is_bye = 0",
                       (stage["id"], final_round - 1)).fetchall()
    losers = [t["entry_b"] if t["winner_entry_id"] == t["entry_a"] else t["entry_a"] for t in semis]
    if losers:
        played = db.execute(
            "SELECT m.* FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
            "WHERE s.tournament_id = ? AND m.status = 'confirmed'", (tournament["id"],)).fetchall()
        ranking.append(engine.standings(sorted(losers), _results(played))[0]["entry_id"])
    return ranking


def finish(db, tournament):
    """Encerra o torneio e paga o prêmio (as inscrições) aos 3 primeiros."""
    if tournament["status"] != "running":
        raise TournamentError("O torneio não está em andamento.")
    stage = stages_of(db, tournament["id"])[-1]
    if has_next_stage(tournament, stage):
        raise TournamentError("Gere a próxima fase antes de encerrar.")
    if not stage_complete(db, stage):
        raise TournamentError("Ainda há jogos sem resultado.")
    ranking = final_ranking(db, tournament)
    shares = engine.split_prize(prize_pool(db, tournament["id"]), engine.parse_split(tournament["prize_split"]),
                                len(ranking))
    for position, entry_id in enumerate(ranking, 1):
        entry = db.execute("SELECT * FROM tournament_entries WHERE id = ?", (entry_id,)).fetchone()
        db.execute("UPDATE tournament_entries SET final_position = ? WHERE id = ?", (position, entry_id))
        if position <= len(shares) and shares[position - 1]:
            post(db, entry["player_id"], shares[position - 1], "premio", f"premio:{tournament['id']}:{position}",
                 tournament_id=tournament["id"], note=f"{position}º lugar - {tournament['name']}")
    db.execute("UPDATE tournament_stages SET status = 'done' WHERE id = ?", (stage["id"],))
    db.execute("UPDATE tournaments SET status = 'finished', finished_at = ?, updated_at = ? WHERE id = ?",
               (now(), now(), tournament["id"]))
