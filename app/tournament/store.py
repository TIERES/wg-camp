"""Torneios no banco: inscrição, início, resultados, avanço de fase,
encerramento com prêmio e cancelamento com reembolso.

Recebe a conexão sqlite3 (com row_factory = Row) e não faz commit - a view
faz commit no fim, ou rollback se vier TournamentError / InsufficientFunds.
"""
import json
import random
import secrets

from ..db import now
from ..wallet import ensure_wallet, format_cents, post, setting
from . import engine, formats
from .teams import find_by_name, plain_name


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

def available_teams(db, tournament_id):
    """Times da lista do torneio que ainda não têm dono."""
    return db.execute(
        "SELECT t.* FROM tournament_teams tt JOIN teams t ON t.id = tt.team_id WHERE tt.tournament_id = ? "
        "AND t.id NOT IN (SELECT team_id FROM tournament_entries WHERE tournament_id = ? AND team_id IS NOT NULL) "
        "ORDER BY t.name COLLATE NOCASE", (tournament_id, tournament_id)).fetchall()


def _entry_team(db, tournament, team_name, team_id):
    """(team_id, team_name) da inscrição conforme o modo de times do torneio."""
    mode = tournament["team_mode"]
    if mode in ("draw", "round"):
        return None, ""  # sorteado em start() / escolhido no jogo, a cada partida (set_match_teams)
    if mode == "free":
        name = " ".join((team_name or "").split())
        if len(name) < 2:
            raise TournamentError("Informe o nome do seu time (pelo menos 2 letras).")
        # Nome de um time da base vira esse time (nome oficial e escudo).
        team = find_by_name(db, name)
        name = team["name"] if team is not None else name[:40]
        taken = db.execute("SELECT team_name, team_id FROM tournament_entries WHERE tournament_id = ?",
                           (tournament["id"],)).fetchall()
        if any((team is not None and row["team_id"] == team["id"]) or plain_name(row["team_name"]) == plain_name(name)
               for row in taken):
            raise TournamentError(f"Já existe um time chamado {name} neste campeonato. Escolha outro nome.")
        return (team["id"] if team is not None else None), name
    team = next((t for t in available_teams(db, tournament["id"]) if t["id"] == team_id), None)
    if team is None:
        raise TournamentError("Escolha um time da lista (ele pode ter acabado de ser escolhido por outro jogador).")
    return team["id"], team["name"]


def match_team(db, name):
    """(nome, team_id) do time informado para um jogo (campeonato com um time
    diferente a cada rodada): o nome de um time da base (ignorando acentos e
    maiúsculas) vira esse time - nome oficial e escudo -; sem nome, ('', None)."""
    name = " ".join((name or "").split())[:40]
    if not name:
        return "", None
    team = find_by_name(db, name)
    return (team["name"], team["id"]) if team is not None else (name, None)


def set_match_teams(db, tournament, match_id, home=None, away=None):
    """O time que cada um usou no jogo, no campeonato com um time diferente a
    cada rodada (nos outros modos o time é o da inscrição): home/away =
    (nome, team_id) - ('', None) tira -, None mantém o que está."""
    if tournament["team_mode"] != "round":
        return
    for side, team in (("home", home), ("away", away)):
        if team is not None:
            db.execute(f"UPDATE tournament_matches SET {side}_team_name = ?, {side}_team_id = ? WHERE id = ?",
                       (team[0], team[1], match_id))


def are_friends(db, a, b):
    return db.execute("SELECT 1 FROM friendships WHERE status = 'accepted' AND ((requester_id = ? AND addressee_id = ?) "
                      "OR (requester_id = ? AND addressee_id = ?))", (a, b, b, a)).fetchone() is not None


def enroll(db, tournament, player_id, team_name="", team_id=None, group_slot=None):
    if tournament["status"] != "registration":
        raise TournamentError("As inscrições deste campeonato não estão abertas.")
    if (tournament["friends_only"] and player_id != tournament["organizer_id"]
            and not are_friends(db, player_id, tournament["organizer_id"])):
        raise TournamentError("Este campeonato é somente para amigos de quem o criou.")
    count = db.execute("SELECT COUNT(*) FROM tournament_entries WHERE tournament_id = ?",
                       (tournament["id"],)).fetchone()[0]
    if count >= tournament["max_players"]:
        raise TournamentError("Não há mais vagas neste campeonato.")
    if db.execute("SELECT 1 FROM tournament_entries WHERE tournament_id = ? AND player_id = ?",
                  (tournament["id"], player_id)).fetchone():
        raise TournamentError("Você já está inscrito neste campeonato.")
    if is_dynamic(tournament):
        _check_group(db, tournament, group_slot)
    else:
        group_slot = None
    team_id, team_name = _entry_team(db, tournament, team_name, team_id)
    ensure_wallet(db, player_id)
    fee_ref = None
    if tournament["entry_fee_cents"]:
        fee_ref = f"inscricao:{tournament['id']}:{player_id}:{secrets.token_hex(6)}"
        post(db, player_id, -tournament["entry_fee_cents"], "inscricao", fee_ref, tournament_id=tournament["id"],
             note=tournament["name"])
    db.execute("INSERT INTO tournament_entries (tournament_id, player_id, team_name, team_id, fee_ref, group_slot, "
               "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (tournament["id"], player_id, team_name, team_id, fee_ref, group_slot, now()))


# --- Grupos dinâmicos ---------------------------------------------------------
#
# O organizador nomeia os grupos (settings_json: groups, group_names) e cada
# jogador escolhe o seu na inscrição (tournament_entries.group_slot, o índice em
# group_names). Cada grupo aceita até vagas / grupos jogadores (arredondado para
# cima); o jogador troca de grupo enquanto houver vaga, e o organizador move
# quem quiser (sem esse limite) até o início. Quem fica sem grupo (o grupo dele
# saiu ao editar os dados) é avisado e escolhe outro; o sorteio espera.

def is_dynamic(tournament):
    return tournament["format"] == "dynamic_groups"


def group_names(tournament):
    return formats.normalize("dynamic_groups", settings_of(tournament))["group_names"]


def group_capacity(tournament, groups=None):
    groups = groups or len(group_names(tournament))
    return -(-tournament["max_players"] // groups) if groups else 0


def group_views(db, tournament, entries=None):
    """Os grupos com os inscritos de cada um: ([{"slot", "name", "entries",
    "count", "capacity", "full"}], inscritos sem grupo)."""
    names = group_names(tournament)
    capacity = group_capacity(tournament, len(names))
    entries = entries_of(db, tournament["id"]) if entries is None else entries
    views = [{"slot": slot, "name": name, "entries": [], "capacity": capacity} for slot, name in enumerate(names)]
    loose = []
    for entry in entries:
        slot = entry["group_slot"]
        (views[slot]["entries"] if slot is not None and 0 <= slot < len(views) else loose).append(entry)
    for view in views:
        view["count"] = len(view["entries"])
        view["full"] = view["count"] >= capacity
    return views, loose


def _check_group(db, tournament, slot, entry_id=None):
    """O grupo `slot` existe e tem vaga (sem contar a própria inscrição `entry_id`)."""
    names = group_names(tournament)
    if slot is None:
        raise TournamentError("Escolha o seu grupo.")
    if not 0 <= slot < len(names):
        raise TournamentError("Esse grupo não existe neste campeonato.")
    taken = db.execute("SELECT COUNT(*) FROM tournament_entries WHERE tournament_id = ? AND group_slot = ? "
                       "AND id != ?", (tournament["id"], slot, entry_id or 0)).fetchone()[0]
    if taken >= group_capacity(tournament, len(names)):
        raise TournamentError(f"O grupo {names[slot]} está cheio: escolha outro.")


def choose_group(db, tournament, entry, slot):
    """O inscrito escolhe (ou troca) o seu grupo antes do início, se houver vaga."""
    if tournament["status"] != "registration":
        raise TournamentError("O campeonato já começou: os grupos não mudam mais.")
    if not is_dynamic(tournament):
        raise TournamentError("Neste campeonato os grupos são sorteados no início.")
    if slot == entry["group_slot"]:
        return
    _check_group(db, tournament, slot, entry["id"])
    db.execute("UPDATE tournament_entries SET group_slot = ? WHERE id = ?", (slot, entry["id"]))


def arrange_groups(db, tournament, slots, editor_id=None):
    """O organizador põe os inscritos nos grupos antes do início (slots:
    {entry_id: grupo}), sem o limite de vagas por grupo. Devolve os avisos
    [(player_id, assunto, texto)] a quem mudou de grupo (menos quem editou),
    para os e-mails (notify.entry_notices, depois do commit)."""
    if tournament["status"] != "registration":
        raise TournamentError("O campeonato já começou: os grupos não mudam mais.")
    if not is_dynamic(tournament):
        raise TournamentError("Neste campeonato os grupos são sorteados no início.")
    names, notices = group_names(tournament), []
    for entry in entries_of(db, tournament["id"]):
        slot = slots.get(entry["id"], entry["group_slot"])
        if slot == entry["group_slot"]:
            continue
        if slot is None or not 0 <= slot < len(names):
            raise TournamentError(f"Escolha um grupo para {entry['username']}.")
        db.execute("UPDATE tournament_entries SET group_slot = ? WHERE id = ?", (slot, entry["id"]))
        if entry["player_id"] != editor_id:
            before = entry["group_slot"]
            was = f"no grupo {names[before]}" if before is not None and 0 <= before < len(names) else "sem grupo"
            notices.append((entry["player_id"], f"{tournament['name']}: você está no grupo {names[slot]}",
                            f'O organizador do campeonato "{tournament["name"]}" mudou você de grupo: você '
                            f"estava {was} e agora está no grupo {names[slot]}."))
    return notices


def entries_without_group(db, tournament):
    if not is_dynamic(tournament):
        return []
    return group_views(db, tournament)[1]


def _refund(db, tournament, entry):
    if entry["fee_ref"]:
        post(db, entry["player_id"], tournament["entry_fee_cents"], "reembolso", "reembolso:" + entry["fee_ref"],
             tournament_id=tournament["id"], note=tournament["name"])


def withdraw(db, tournament, entry):
    """Sai do torneio (ou é removido pelo organizador) antes do início, com
    a inscrição devolvida."""
    if tournament["status"] not in ("draft", "registration"):
        raise TournamentError("O campeonato já começou: não dá mais para sair.")
    _refund(db, tournament, entry)
    db.execute("DELETE FROM tournament_entries WHERE id = ?", (entry["id"],))


def entries_without_team(db, tournament):
    """Inscritos ainda sem time nos modos em que cada um escolhe o seu (o time
    saiu da lista ou o modo de times mudou: update_before_start)."""
    if tournament["team_mode"] in ("draw", "round"):
        return []
    return [e for e in entries_of(db, tournament["id"]) if not e["team_name"]]


def choose_team(db, tournament, entry, team_name="", team_id=None):
    """O inscrito sem time escolhe (lista) ou digita (livre) o seu, antes do início."""
    if tournament["status"] != "registration":
        raise TournamentError("O campeonato já começou.")
    if tournament["team_mode"] == "draw":
        raise TournamentError("Neste campeonato os times são sorteados no início.")
    if tournament["team_mode"] == "round":
        raise TournamentError("Neste campeonato cada jogador escolhe o time no próprio jogo, a cada partida.")
    if entry["team_name"]:
        raise TournamentError("Você já tem um time neste campeonato.")
    team_id, team_name = _entry_team(db, tournament, team_name, team_id)
    db.execute("UPDATE tournament_entries SET team_id = ?, team_name = ? WHERE id = ?",
               (team_id, team_name, entry["id"]))


def check_before_start(db, tournament, values):
    """Os dados básicos ainda podem mudar para `values`? (update_before_start e a
    alteração que espera confirmação - changes.propose_values.) Devolve os inscritos."""
    if tournament["status"] != "registration":
        raise TournamentError("O campeonato já começou: os dados básicos não mudam mais.")
    entries = entries_of(db, tournament["id"])
    if len(entries) > values["max_players"]:
        raise TournamentError(f"Já há {len(entries)} inscritos: as vagas não podem ser menos que isso "
                              "(remova inscrições antes).")
    return entries


def update_before_start(db, tournament, values, editor_id=None):
    """Os dados básicos do campeonato editados antes do início (values: o
    formulário validado, tournaments._tournament_form). Com inscritos:
      - virou "somente amigos": sai quem não é amigo do organizador;
      - o valor da inscrição mudou: saem todos e se inscrevem de novo, se quiserem;
      - o time de alguém saiu da lista (ou o modo de times mudou): ele fica sem
        time e escolhe outro (choose_team) - o campeonato só começa com todos com time.
    Quem sai recebe de volta o que pagou. Devolve os avisos [(player_id,
    assunto, texto)] para os e-mails (notify.entry_notices, depois do commit),
    sem os de quem editou."""
    entries = check_before_start(db, tournament, values)
    name, old_fee, notices = values["name"], tournament["entry_fee_cents"], []
    refund = f" O valor pago ({format_cents(old_fee)}) voltou para a sua carteira W$." if old_fee else ""

    def notice(entry, subject, text):
        if entry["player_id"] != editor_id:
            notices.append((entry["player_id"], f"{name}: {subject}", text))

    def drop(entry, text):
        _refund(db, tournament, entry)  # o que ele pagou: o valor de antes
        db.execute("DELETE FROM tournament_entries WHERE id = ?", (entry["id"],))
        notice(entry, "sua inscrição foi cancelada", text + refund)

    kept = []
    for entry in entries:
        if (values["friends_only"] and not tournament["friends_only"] and entry["player_id"] != tournament["organizer_id"]
                and not are_friends(db, entry["player_id"], tournament["organizer_id"])):
            drop(entry, f'O campeonato "{name}" passou a ser somente para amigos de quem o criou, e por isso '
                        "a sua inscrição foi cancelada.")
        else:
            kept.append(entry)
    if values["fee"] != old_fee and kept:
        for entry in kept:
            drop(entry, f'O valor da inscrição do campeonato "{name}" mudou de {format_cents(old_fee)} para '
                        f"{format_cents(values['fee'])}, e por isso a sua inscrição foi cancelada. Para continuar "
                        "no campeonato, inscreva-se de novo.")
        kept = []
    mode, old_mode, listed = values["team_mode"], tournament["team_mode"], set(values["team_ids"])
    for entry in kept:
        text = None
        if mode == "choose" and entry["team_id"] not in listed:
            if old_mode == "choose" and not entry["team_name"]:
                continue  # já estava sem time, e foi avisado
            text = (f'O time {entry["team_name"]}, que você escolheu, saiu da lista do campeonato "{name}". '
                    if old_mode == "choose" else f'No campeonato "{name}" cada jogador agora escolhe o time da lista. ')
            text += "A sua inscrição continua: escolha o seu time na página do campeonato."
        elif mode == "draw" and old_mode != "draw" and entry["team_name"]:
            text = (f'No campeonato "{name}" os times agora são sorteados no início; o seu ({entry["team_name"]}) '
                    "volta para o sorteio. A sua inscrição continua.")
        elif mode == "free" and old_mode in ("draw", "round"):
            text = (f'No campeonato "{name}" cada jogador agora escolhe o nome do seu time. A sua inscrição '
                    "continua: escolha o seu time na página do campeonato.")
        elif mode == "round" and old_mode != "round" and entry["team_name"]:
            text = (f'No campeonato "{name}" ninguém fica mais com um time fixo: cada jogador escolhe o time no '
                    f'próprio jogo, a cada partida, e os pontos ficam com o jogador. O seu ({entry["team_name"]}) '
                    "saiu da inscrição, que continua.")
        if text:
            db.execute("UPDATE tournament_entries SET team_id = NULL, team_name = '' WHERE id = ?", (entry["id"],))
            subject = ("os times serão sorteados" if mode == "draw" else "um time diferente a cada rodada"
                       if mode == "round" else "escolha o seu time")
            notice(entry, subject, text)
    # Grupos dinâmicos: virou esse formato, todos escolhem o grupo; o grupo de alguém
    # saiu (menos grupos), ele escolhe outro; deixou de ser, os grupos somem.
    if values["format"] != "dynamic_groups":
        db.execute("UPDATE tournament_entries SET group_slot = NULL WHERE tournament_id = ?", (tournament["id"],))
    else:
        old_names = group_names(tournament) if is_dynamic(tournament) else []
        groups = values["config"]["groups"]
        for entry in kept:
            slot = entry["group_slot"] if old_names else None
            if slot is not None and slot < groups:
                continue
            if old_names and slot is None:
                continue  # já estava sem grupo, e foi avisado
            db.execute("UPDATE tournament_entries SET group_slot = NULL WHERE id = ?", (entry["id"],))
            text = (f'O grupo {old_names[slot]} saiu do campeonato "{name}". ' if slot is not None
                    and slot < len(old_names) else f'No campeonato "{name}" cada jogador agora escolhe o seu grupo. ')
            notice(entry, "escolha o seu grupo",
                   text + "A sua inscrição continua: escolha o seu grupo na página do campeonato.")
    db.execute(
        "UPDATE tournaments SET name = ?, game_title_id = ?, format = ?, max_players = ?, entry_fee_cents = ?, "
        "prize_split = ?, settings_json = ?, rules = ?, team_mode = ?, starts_on = ?, ends_on = ?, friends_only = ?, "
        "updated_at = ? WHERE id = ?",
        (name, values["game_id"], values["format"], values["max_players"], values["fee"], values["split"],
         json.dumps(values["config"]), values["rules"], mode, values["starts_on"], values["ends_on"],
         values["friends_only"], now(), tournament["id"]))
    db.execute("DELETE FROM tournament_teams WHERE tournament_id = ?", (tournament["id"],))
    db.executemany("INSERT INTO tournament_teams (tournament_id, team_id) VALUES (?, ?)",
                   [(tournament["id"], team_id) for team_id in values["team_ids"]])
    return notices


# --- Desistência com o campeonato em andamento --------------------------------
#
# Antes do início, sair é withdraw (com a inscrição devolvida). Com o campeonato
# em andamento, o jogador desiste (withdraw_running), com o motivo para os
# organizadores, e:
#   - a inscrição W$ não volta (e entra no prêmio, como sempre);
#   - os jogos que faltam viram WO: 3 x 0 para o adversário, confirmado na hora
#     (W$ e XP como uma vitória/derrota normal; no mata-mata passa o adversário,
#     seja qual for o agregado - engine.tie_outcome). Na tabela, o placar já
#     reportado em que o adversário venceu fica valendo (ele não perde os gols);
#   - confronto de mata-mata que ainda vai ser montado com ele (aguarda o
#     adversário) vira WO assim que o adversário chega (_fill_slot);
#   - não se classifica para a fase seguinte (advance pula quem desistiu: a vaga
#     é do próximo da tabela) e fica fora do pódio e do prêmio (final_ranking);
#   - já classificado para o mata-mata e sem ter jogado nele: a vaga dele vai
#     para o suplente (o próximo da tabela da fase anterior - _find_substitute),
#     sem WO.

WITHDRAW_REASON_MIN, WITHDRAW_REASON_MAX = 10, 500
WALKOVER_GOALS = 3


def withdrawn_ids(db, tournament_id):
    """Inscrições que desistiram do campeonato."""
    return {row[0] for row in db.execute(
        "SELECT id FROM tournament_entries WHERE tournament_id = ? AND withdrawn_at IS NOT NULL", (tournament_id,))}


def _withdrawn(db, entry_id):
    row = db.execute("SELECT withdrawn_at FROM tournament_entries WHERE id = ?", (entry_id,)).fetchone()
    return row is not None and row[0] is not None


def _log_system(db, match_id, score, comment):
    """Linha do histórico do jogo feita pelo sistema (a página mostra "Sistema definiu o resultado")."""
    db.execute("INSERT INTO match_reports (match_id, action, home_goals, away_goals, comment, created_at) "
               "VALUES (?, 'resolve', ?, ?, ?, ?)", (match_id, *score, comment[:500], now()))


def _username(db, entry_id):
    return db.execute("SELECT p.username FROM tournament_entries e JOIN players p ON p.id = e.player_id "
                      "WHERE e.id = ?", (entry_id,)).fetchone()[0]


def _record_walkover(db, tournament, match, loser):
    """O jogo vira WO: 3 x 0 para o adversário de `loser`, confirmado na hora."""
    score = (0, WALKOVER_GOALS) if match["home_entry_id"] == loser else (WALKOVER_GOALS, 0)
    record_result(db, tournament, match["id"], *score, walkover=loser)
    db.execute("UPDATE tournament_matches SET reported_by_entry_id = NULL, reported_at = NULL, quitter_id = NULL "
               "WHERE id = ?", (match["id"],))
    _log_system(db, match["id"], score, f"WO: {_username(db, loser)} desistiu do campeonato.")


def _walkover_tie(db, tie):
    """Confronto de mata-mata recém-montado (_fill_slot) com quem desistiu: os
    jogos viram WO. Os dois desistiram: passa o entry_a, que leva WO no confronto seguinte."""
    gone = [entry_id for entry_id in (tie["entry_a"], tie["entry_b"]) if _withdrawn(db, entry_id)]
    if not gone:
        return
    loser = gone[0] if len(gone) == 1 else tie["entry_b"]
    tournament = db.execute("SELECT t.* FROM tournaments t JOIN tournament_stages s ON s.tournament_id = t.id "
                            "WHERE s.id = ?", (tie["stage_id"],)).fetchone()
    for match in tie_matches(db, tie["id"]):
        if match["status"] != "confirmed":
            _record_walkover(db, tournament, match, loser)


def _kept_score(db, match, loser):
    """Jogo de tabela (grupos, pontos corridos, liga) já reportado em que o
    adversário de quem desistiu venceu: esse placar fica valendo, em vez do WO
    (o adversário não perde os gols). O placar, ou None."""
    if match["tie_id"] or match["status"] != "reported":
        return None
    reported = db.execute("SELECT * FROM match_reports WHERE match_id = ? AND action = 'report' ORDER BY id DESC "
                          "LIMIT 1", (match["id"],)).fetchone()
    if reported is None or reported["home_goals"] is None:
        return None
    mine, theirs = ((reported["home_goals"], reported["away_goals"]) if match["home_entry_id"] == loser
                    else (reported["away_goals"], reported["home_goals"]))
    return (reported["home_goals"], reported["away_goals"]) if theirs > mine else None


def still_playing(db, tournament, entry):
    """O inscrito ainda está na disputa? Na tabela (grupos, pontos corridos,
    liga) sempre - ainda pode se classificar ou ir ao pódio; no mata-mata, se
    tem um confronto sem vencedor (inclusive esperando o adversário)."""
    stages = stages_of(db, tournament["id"])
    if not stages:
        return False
    stage = stages[-1]
    if stage["kind"] != "knockout":
        return db.execute("SELECT 1 FROM tournament_group_members gm JOIN tournament_groups g ON g.id = gm.group_id "
                          "WHERE g.stage_id = ? AND gm.entry_id = ?", (stage["id"], entry["id"])).fetchone() is not None
    return db.execute("SELECT 1 FROM tournament_ties WHERE stage_id = ? AND winner_entry_id IS NULL "
                      "AND ? IN (entry_a, entry_b)", (stage["id"], entry["id"])).fetchone() is not None


def withdraw_error(db, tournament, entry):
    """Por que o inscrito não pode desistir agora (texto), ou None se pode."""
    if entry is None:
        return "Você não está inscrito neste campeonato."
    if tournament["status"] != "running":
        return "Só dá para desistir de um campeonato em andamento."
    if entry["withdrawn_at"]:
        return "Você já desistiu deste campeonato."
    if not still_playing(db, tournament, entry):
        return "Você já está fora da disputa deste campeonato."
    return None


def _find_substitute(db, tournament, entry):
    """O suplente que fica com a vaga de quem desistiu no mata-mata sem ter
    jogado nele: o próximo da tabela da fase anterior que não se classificou
    (primeiro do grupo dele, depois dos outros grupos pela campanha - como em
    advance) e não desistiu. None se não cabe suplente (ou não há)."""
    stages = stages_of(db, tournament["id"])
    stage = stages[-1]
    if stage["kind"] != "knockout" or len(stages) < 2 or stages[-2]["kind"] == "knockout":
        return None
    if db.execute("SELECT 1 FROM tournament_matches WHERE stage_id = ? AND status != 'scheduled' "
                  "AND ? IN (home_entry_id, away_entry_id)", (stage["id"], entry["id"])).fetchone():
        return None  # já jogou (ou reportou) no mata-mata: os jogos que faltam viram WO
    in_knockout = {entry_id for row in db.execute("SELECT entry_a, entry_b FROM tournament_ties WHERE stage_id = ?",
                                                  (stage["id"],)) for entry_id in row if entry_id}
    if entry["id"] not in in_knockout:
        return None
    skip = in_knockout | withdrawn_ids(db, tournament["id"]) | {entry["id"]}
    tables = [table for _, table in group_tables(db, stages[-2]["id"])]
    own = next((table for table in tables if any(row["entry_id"] == entry["id"] for row in table)), [])
    others = sorted((row for table in tables if table is not own for row in table),
                    key=lambda row: (row["position"], -row["points"], -row["goal_diff"], -row["goals_for"],
                                     -row["won"], row["discipline"]))
    return next((row["entry_id"] for row in list(own) + others if row["entry_id"] not in skip), None)


def _pending_matches(db, tournament, entry_id):
    return db.execute(
        "SELECT m.* FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
        "WHERE s.tournament_id = ? AND s.status = 'running' AND m.status != 'confirmed' "
        "AND ? IN (m.home_entry_id, m.away_entry_id) ORDER BY s.seq, m.round, m.leg, m.id",
        (tournament["id"], entry_id)).fetchall()


def withdraw_preview(db, tournament, entry):
    """O que a desistência faria agora (a janela de confirmação): o suplente
    que fica com a vaga (sem WO) ou quantos jogos viram WO e quantos ficam com
    o placar reportado (derrotas também)."""
    substitute = _find_substitute(db, tournament, entry)
    # table: a fase atual é de tabela (sem jogos pendentes, ele só sai da classificação e do pódio);
    # senão, sem jogos pendentes, ele espera o adversário do próximo confronto do mata-mata.
    table = stages_of(db, tournament["id"])[-1]["kind"] != "knockout"
    if substitute:
        return {"substitute": True, "walkovers": 0, "kept": 0, "table": table}
    pending = _pending_matches(db, tournament, entry["id"])
    kept = sum(1 for match in pending if _kept_score(db, match, entry["id"]))
    return {"substitute": False, "walkovers": len(pending) - kept, "kept": kept, "table": table}


def withdraw_running(db, tournament, entry, reason):
    """O inscrito desiste do campeonato em andamento (as regras acima). Devolve
    {"substitute": inscrição do suplente ou None, "walkovers": [jogos que
    viraram WO], "kept": [jogos com o placar reportado mantido]} para os e-mails."""
    error = withdraw_error(db, tournament, entry)
    if error:
        raise TournamentError(error)
    reason = (reason or "").strip()
    if not WITHDRAW_REASON_MIN <= len(reason) <= WITHDRAW_REASON_MAX:
        raise TournamentError(f"Conte o motivo da desistência (de {WITHDRAW_REASON_MIN} a {WITHDRAW_REASON_MAX} "
                              "caracteres).")
    db.execute("UPDATE tournament_entries SET withdrawn_at = ?, withdraw_reason = ? WHERE id = ?",
               (now(), reason, entry["id"]))
    result = {"substitute": None, "walkovers": [], "kept": []}
    substitute = _find_substitute(db, tournament, entry)
    if substitute:
        stage_id = stages_of(db, tournament["id"])[-1]["id"]
        for column in ("entry_a", "entry_b", "winner_entry_id"):
            db.execute(f"UPDATE tournament_ties SET {column} = ? WHERE stage_id = ? AND {column} = ?",
                       (substitute, stage_id, entry["id"]))
        for column in ("home_entry_id", "away_entry_id"):
            db.execute(f"UPDATE tournament_matches SET {column} = ?, updated_at = ? WHERE stage_id = ? AND {column} = ?",
                       (substitute, now(), stage_id, entry["id"]))
        db.execute("UPDATE tournament_entries SET replaced_by_entry_id = ? WHERE id = ?", (substitute, entry["id"]))
        result["substitute"] = substitute
    else:
        for row in _pending_matches(db, tournament, entry["id"]):
            # Um WO no mata-mata pode ter montado (e decidido) outros confrontos: relê o jogo.
            match = db.execute("SELECT * FROM tournament_matches WHERE id = ?", (row["id"],)).fetchone()
            if match is None or match["status"] == "confirmed":
                continue
            score = _kept_score(db, match, entry["id"])
            if score:
                record_result(db, tournament, match["id"], *score)
                db.execute("UPDATE tournament_matches SET reported_by_entry_id = NULL, reported_at = NULL "
                           "WHERE id = ?", (match["id"],))
                _log_system(db, match["id"], score, f"{_username(db, entry['id'])} desistiu do campeonato: "
                                                    "vale o placar reportado.")
                result["kept"].append(match["id"])
            else:
                _record_walkover(db, tournament, match, entry["id"])
                result["walkovers"].append(match["id"])
    db.execute("UPDATE tournaments SET updated_at = ? WHERE id = ?", (now(), tournament["id"]))
    return result


def cancel(db, tournament):
    if tournament["status"] in ("finished", "cancelled"):
        raise TournamentError("Este campeonato já foi encerrado.")
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


def _round_robin_stage(db, tournament, seq, kind, name, groups, legs, names=None):
    stage_id = _new_stage(db, tournament, seq, kind, name, {"legs": legs})
    for index, members in enumerate(groups):
        label = ("Tabela" if kind == "league" else names[index] if names
                 else f"Grupo {engine.group_name(index)}")
        group_id = _new_group(db, stage_id, index, label, members)
        for round_no, home, away in engine.round_robin(members, legs):
            _new_match(db, stage_id, round_no, home, away, group_id=group_id)
    return stage_id


def _group_stage(db, tournament, groups, config, names=None):
    """Fase de grupos (Copa e Grupos dinâmicos). A classificação é sempre por
    grupo; os jogos seguem group_matchups (formats.MATCHUPS):
      - within: cada grupo é um todos contra todos (_round_robin_stage);
      - others: cada um enfrenta os jogadores dos outros grupos (engine.schedule);
      - all: todos contra todos os inscritos.
    Nos dois últimos os jogos não são de um grupo (group_id NULL): cada um conta
    na tabela do grupo de cada lado (group_tables)."""
    mode, legs = config.get("group_matchups", "within"), config["group_legs"]
    if mode == "within":
        return _round_robin_stage(db, tournament, 1, "groups", "Fase de grupos", groups, legs, names)
    stage_id = _new_stage(db, tournament, 1, "groups", "Fase de grupos", {"legs": legs, "matchups": mode})
    for index, members in enumerate(groups):
        _new_group(db, stage_id, index, names[index] if names else f"Grupo {engine.group_name(index)}", members)
    if mode == "all":
        fixtures = engine.round_robin([entry for group in groups for entry in group], legs)
    else:
        fixtures = engine.schedule(engine.cross_group_pairs(groups), legs)
    for round_no, home, away in fixtures:
        _new_match(db, stage_id, round_no, home, away)
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


def _knockout_stage(db, tournament, seq, pairs, legs, final_legs, third_place, first_round_name=None,
                    repechage=True):
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
    # Chave de 4 incompleta (3 jogadores, com repescagem): não há disputa de 3º;
    # o 3º é quem perde a repescagem (em final_ranking).
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
            if repechage and None not in pairs[position ^ 1]:
                # Repescagem: quem sobrou não passa direto; espera o perdedor do
                # confronto vizinho (o vencedor desse jogo é o adversário seguinte).
                db.execute("UPDATE tournament_ties SET loser_next_tie_id = ?, loser_next_slot = 'b' WHERE id = ?",
                           (tie_id, ties[(1, position ^ 1)]))
                _fill_slot(db, tie_id, "a", winner)
                continue
            # Vizinho também sem adversário (ex.: 5 numa chave de 8): passa direto.
            db.execute("UPDATE tournament_ties SET entry_a = ?, is_bye = 1 WHERE id = ?", (winner, tie_id))
            _set_winner(db, _tie(db, tie_id), winner)
        else:
            _fill_slot(db, tie_id, "a", entry_a)
            _fill_slot(db, tie_id, "b", entry_b)
    return stage_id


def start(db, tournament, rng=random):
    if tournament["status"] != "registration":
        raise TournamentError("Abra as inscrições antes de iniciar o campeonato.")
    missing = entries_without_team(db, tournament)
    if missing:
        raise TournamentError("Ainda sem time: " + ", ".join(e["username"] for e in missing) + ". Eles foram avisados "
                              "por e-mail para escolher; o campeonato começa quando todos tiverem time (ou remova "
                              "a inscrição deles).")
    missing = entries_without_group(db, tournament)
    if missing:
        raise TournamentError("Ainda sem grupo: " + ", ".join(e["username"] for e in missing) + ". Escolha o grupo "
                              "deles em Grupos, aqui em Gerenciar (eles também foram avisados por e-mail).")
    entries = [row["id"] for row in entries_of(db, tournament["id"])]
    config = settings_of(tournament)
    error = formats.config_error(tournament["format"], config, len(entries))
    if error:
        raise TournamentError(error)
    if tournament["team_mode"] == "draw":
        teams = available_teams(db, tournament["id"])
        if len(teams) < len(entries):
            raise TournamentError(f"A lista tem {len(teams)} times para {len(entries)} jogadores.")
        teams = list(teams)
        rng.shuffle(teams)
        for entry_id, team in zip(entries, teams):
            db.execute("UPDATE tournament_entries SET team_id = ?, team_name = ? WHERE id = ?",
                       (team["id"], team["name"], entry_id))
    rng.shuffle(entries)  # o sorteio: ordem da tabela, grupos, potes e chave
    fmt = tournament["format"]
    if fmt in ("league", "league_knockout"):
        _round_robin_stage(db, tournament, 1, "league", "Pontos corridos", [entries], config["legs"])
    elif fmt == "world_cup":
        _group_stage(db, tournament, engine.draw_groups(entries, config["groups"], rng), config)
    elif fmt == "dynamic_groups":
        # Os grupos que os jogadores escolheram (a ordem dentro de cada um, sorteada, monta as rodadas).
        views, _ = group_views(db, tournament)
        slot_of = {entry["id"]: view["slot"] for view in views for entry in view["entries"]}
        members = [[entry_id for entry_id in entries if slot_of[entry_id] == view["slot"]] for view in views]
        needed = max(2, config["qualifiers"])
        short = [f"{view['name']} ({len(group)})" for view, group in zip(views, members) if len(group) < needed]
        if short:
            raise TournamentError(f"Cada grupo precisa de pelo menos {needed} jogadores: " + ", ".join(short) +
                                  ". Mude os jogadores de grupo em Grupos, aqui em Gerenciar.")
        _group_stage(db, tournament, members, config, [view["name"] for view in views])
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
        _walkover_tie(db, tie)


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
    return [engine.Result(r["home_entry_id"], r["away_entry_id"], r["home_goals"], r["away_goals"],
                          r["walkover_entry_id"]) for r in rows]


def tie_matches(db, tie_id):
    return db.execute("SELECT * FROM tournament_matches WHERE tie_id = ? ORDER BY leg", (tie_id,)).fetchall()


def needs_pens(db, match):
    """Último jogo de um confronto de mata-mata empatado no agregado (contando
    este placar)? Então os pênaltis decidem."""
    if not match["tie_id"]:
        return False
    tie = _tie(db, match["tie_id"])
    return match["leg"] == tie["legs"]


def other_legs_goals(db, match):
    """Gols já confirmados nos outros jogos do confronto, como (mandante, visitante)
    deste jogo: a tela só mostra os pênaltis se o agregado com este placar empatar."""
    home = away = 0
    for other in tie_matches(db, match["tie_id"]) if match["tie_id"] else ():
        if other["id"] != match["id"] and other["status"] == "confirmed":
            same = other["home_entry_id"] == match["home_entry_id"]
            home += other["home_goals"] if same else other["away_goals"]
            away += other["away_goals"] if same else other["home_goals"]
    return home, away


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
        ref_key = f"partida:{match_id}:v{version}:{player_id}"
        if amount > 0:
            post(db, player_id, amount, kind, ref_key, tournament_id=tournament["id"], match_id=match_id,
                 note=tournament["name"])
        elif amount < 0:
            # Débito por derrota: só até zerar o saldo, nunca negativo. Zerou: marca para
            # avisar o jogador e a administração (notify.empty_wallets, depois do commit).
            balance = db.execute("SELECT wallet_cents FROM players WHERE id = ?", (player_id,)).fetchone()[0]
            debit = max(amount, -balance)
            if debit < 0 and post(db, player_id, debit, kind, ref_key, tournament_id=tournament["id"],
                                  match_id=match_id, note=tournament["name"]) and debit == -balance:
                db.execute("UPDATE players SET wallet_empty_pending = 1 WHERE id = ?", (player_id,))


def record_result(db, tournament, match_id, home_goals, away_goals, home_pens=None, away_pens=None, walkover=None):
    """Grava (ou corrige) o placar de um jogo, paga o W$ do jogo (estornando o
    resultado anterior, se havia) e, no mata-mata, avança o vencedor.
    walkover: a inscrição que perdeu por WO (_record_walkover); qualquer outro
    placar gravado no jogo (correção do organizador) deixa de ser WO."""
    match = db.execute(
        "SELECT m.*, s.status AS stage_status, s.tournament_id FROM tournament_matches m "
        "JOIN tournament_stages s ON s.id = m.stage_id WHERE m.id = ?", (match_id,)).fetchone()
    if match is None or match["tournament_id"] != tournament["id"]:
        raise TournamentError("Jogo não encontrado.")
    if tournament["status"] != "running":
        raise TournamentError("O campeonato não está em andamento.")
    if match["stage_status"] != "running":
        raise TournamentError("Esta fase já foi encerrada: os resultados dela não podem mais mudar.")
    for value in (home_goals, away_goals):
        if value is None or not 0 <= value <= 99:
            raise TournamentError("Placar inválido.")
    if not needs_pens(db, match) or walkover is not None:
        home_pens = away_pens = None
    db.execute(
        "UPDATE tournament_matches SET home_goals = ?, away_goals = ?, home_pens = ?, away_pens = ?, "
        "status = 'confirmed', result_version = result_version + 1, confirmed_at = ?, updated_at = ?, "
        "walkover_entry_id = ? WHERE id = ?",
        (home_goals, away_goals, home_pens, away_pens, now(), now(), walkover, match_id))
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
               "walkover_entry_id = NULL, updated_at = ? WHERE id = ?", (now(), match_id))
    _pay_match(db, tournament, db.execute("SELECT * FROM tournament_matches WHERE id = ?", (match_id,)).fetchone())


# --- Classificação, avanço de fase e encerramento ----------------------------

def group_tables(db, stage_id, provisional=None):
    """[(grupo, classificação)] de uma fase de liga/grupos/suíça. provisional
    = {jogo: (gols do mandante, do visitante)} de jogos ainda não confirmados
    que entram na conta (depois dos confirmados); cada linha diz em
    "provisional" quantos deles somou. Os cartões dos jogos confirmados da
    fase (live.stage_cards) desempatam e vão em "yellow"/"red" de cada linha."""
    from ..live import stage_cards
    cards = stage_cards(db, stage_id)
    discipline = {entry: total["points"] for entry, total in cards.items()}
    tables = []
    # Os jogos da fase em que um dos lados é do grupo: dentro do grupo e, com o
    # confronto entre outros grupos ou entre todos, também os contra os outros
    # grupos (que contam só para o lado do grupo - engine.standings partial).
    confirmed = db.execute("SELECT * FROM tournament_matches WHERE stage_id = ? AND status = 'confirmed' "
                           "ORDER BY round, confirmed_at", (stage_id,)).fetchall()
    open_matches = db.execute("SELECT * FROM tournament_matches WHERE stage_id = ? AND status != 'confirmed' "
                              "ORDER BY round, id", (stage_id,)).fetchall() if provisional else []
    for group in db.execute("SELECT * FROM tournament_groups WHERE stage_id = ? ORDER BY seq", (stage_id,)):
        members = [row["entry_id"] for row in db.execute(
            "SELECT entry_id FROM tournament_group_members WHERE group_id = ? ORDER BY draw_order", (group["id"],))]
        ids = set(members)
        played = [m for m in confirmed if m["home_entry_id"] in ids or m["away_entry_id"] in ids]
        extra = []
        for match in open_matches:
            if match["id"] in provisional and (match["home_entry_id"] in ids or match["away_entry_id"] in ids):
                home_goals, away_goals = provisional[match["id"]]
                extra.append(engine.Result(match["home_entry_id"], match["away_entry_id"], home_goals, away_goals))
        table = engine.standings(members, _results(played) + extra, discipline, partial=True)
        for row in table:
            row["provisional"] = sum(row["entry_id"] in (r.home, r.away) for r in extra)
            total = cards.get(row["entry_id"], {})
            row["yellow"], row["red"] = total.get("yellow", 0), total.get("red", 0)
        tables.append((group, table))
    return tables


def stage_complete(db, stage):
    if stage["kind"] != "knockout":
        return db.execute("SELECT 1 FROM tournament_matches WHERE stage_id = ? AND status != 'confirmed'",
                          (stage["id"],)).fetchone() is None
    final_round = db.execute("SELECT MAX(round) FROM tournament_ties WHERE stage_id = ?", (stage["id"],)).fetchone()[0]
    return db.execute("SELECT 1 FROM tournament_ties WHERE stage_id = ? AND round = ? AND winner_entry_id IS NULL",
                      (stage["id"], final_round)).fetchone() is None


def has_next_stage(tournament, stage):
    return (tournament["format"] in (*formats.GROUPED, "ucl", "league_knockout")
            and stage["kind"] != "knockout")


def advance(db, tournament):
    """Encerra a fase atual (os resultados dela ficam travados) e sorteia /
    monta a próxima a partir da classificação."""
    stage = stages_of(db, tournament["id"])[-1]
    if tournament["status"] != "running" or not has_next_stage(tournament, stage):
        raise TournamentError("Não há próxima fase para gerar.")
    if not stage_complete(db, stage):
        raise TournamentError("Ainda há jogos sem resultado nesta fase.")
    config = settings_of(tournament)
    # Quem desistiu (withdraw_running) não se classifica: a vaga fica com o
    # próximo da tabela (o suplente).
    gone = withdrawn_ids(db, tournament["id"])
    tables = [(group, [row for row in table if row["entry_id"] not in gone])
              for group, table in group_tables(db, stage["id"])]
    if stage["kind"] == "swiss":
        table = tables[0][1]
        seeded = [row["entry_id"] for row in table[:config["direct"] * 3]]
        pairs, first_round_name = engine.seeded_pairs(seeded), "Playoff"
    elif stage["kind"] == "league":
        # Pontos corridos + mata-mata: os N primeiros da tabela, 1º x último classificado...
        seeded = [row["entry_id"] for row in tables[0][1][:config["playoff_qualifiers"]]]
        pairs, first_round_name = engine.seeded_pairs(seeded), None
    else:
        qualifiers = config["qualifiers"]
        groups = len(tables)
        # Grupos em jogo único: cruzamento da Copa do Mundo quando a chave fecha.
        # Em ida e volta (ou sem fechar a chave), pela campanha, como na Libertadores.
        if (tournament["format"] in formats.GROUPED and config.get("group_legs", 1) == 1 and qualifiers == 2
                and groups >= 2
                and groups & (groups - 1) == 0 and all(len(table) >= 2 for _, table in tables)):
            pairs = engine.cross_pairs([[row["entry_id"] for row in table] for _, table in tables])
        else:
            # Classificados ordenados por colocação no grupo e depois campanha
            # (estilo Libertadores: 1ºs contra 2ºs, melhor campanha decide em casa;
            # depois os cartões, como na tabela).
            ranked = [(row["position"], -row["points"], -row["goal_diff"], -row["goals_for"], -row["won"],
                       row["discipline"], index, row["entry_id"])
                      for index, (_, table) in enumerate(tables) for row in table[:qualifiers]]
            pairs = engine.seeded_pairs([item[-1] for item in sorted(ranked)])
        first_round_name = None
    db.execute("UPDATE tournament_stages SET status = 'done' WHERE id = ?", (stage["id"],))
    # Na Liga dos Campeões os primeiros passam direto por mérito (não há repescagem).
    _knockout_stage(db, tournament, stage["seq"] + 1, pairs, config["ko_legs"], config["final_legs"],
                    config["third_place"], first_round_name, repechage=stage["kind"] != "swiss")


def final_ranking(db, tournament):
    """Ids das inscrições em 1º, 2º e 3º (menos se não houver). Quem desistiu
    (withdraw_running) fica fora do pódio e do prêmio: os de baixo sobem."""
    gone = withdrawn_ids(db, tournament["id"])
    return [entry_id for entry_id in _final_ranking(db, tournament) if entry_id not in gone][:3]


def _final_ranking(db, tournament):
    stage = stages_of(db, tournament["id"])[-1]
    if stage["kind"] != "knockout":
        return [row["entry_id"] for row in group_tables(db, stage["id"])[0][1]]
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
    # Com repescagem, quem perdeu a semifinal pode ter ido à final por ela.
    losers = sorted({t["entry_b"] if t["winner_entry_id"] == t["entry_a"] else t["entry_a"] for t in semis}
                    - set(ranking))
    if len(losers) == 1:
        ranking.append(losers[0])
    elif losers:
        played = db.execute(
            "SELECT m.* FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
            "WHERE s.tournament_id = ? AND m.status = 'confirmed'", (tournament["id"],)).fetchall()
        ranking.append(engine.standings(sorted(losers), _results(played))[0]["entry_id"])
    return ranking


def finish(db, tournament):
    """Encerra o torneio e paga o prêmio (as inscrições) aos 3 primeiros."""
    if tournament["status"] != "running":
        raise TournamentError("O campeonato não está em andamento.")
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
