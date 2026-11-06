"""Amistosos: jogos entre dois amigos, fora dos torneios.

  invited --aceitar--> accepted --um informa o placar--> reported --o outro confirma--> confirmed
     |                     |                                 +--o outro não aceita--> suspended
     +--recusar/cancelar   +--cancelar                            (qualquer um propõe outro placar -> reported)

O placar só vale quando os dois aceitaram. Não há confirmação automática
nem W$.

Proteção contra contas fake ("farm" de XP): na confirmação o amistoso é
marcado como `ranked` (vale para Ranking, V/E/D e XP - stats.py e xp.py)
só se os dois jogadores já tiverem chegado alguma vez a MIN_XP contando só
os jogos de torneio (xp.tournament_peaks: XP de amistoso não libera, e quem
chegou lá continua liberado mesmo perdendo XP depois) e a dupla não
tiver passado de PAIR_DAILY_LIMIT amistosos valendo nas últimas 24 h.
Os que não valem continuam registrados como amistoso, com o motivo.
"""
from datetime import datetime, timedelta, timezone

from ..db import now
from . import iso_catalog, xp
from .store import TournamentError, are_friends

MIN_XP = 100            # os dois precisam ter chegado ao Rank E em torneios (uma vez basta)
PAIR_DAILY_LIMIT = 3    # amistosos valendo por dupla a cada 24 h
OPEN = ("invited", "accepted", "reported", "suspended")

STATUS_LABELS = {
    "invited": "Convite enviado", "declined": "Convite recusado", "cancelled": "Cancelado",
    "accepted": "Aguardando o placar", "reported": "Placar para confirmar", "suspended": "Suspenso",
    "confirmed": "Confirmado",
}

# Com a ISO escolhida (iso_catalog.py): nome, arquivo e imagem dela (a do torneio ou a do cadastro).
_SELECT = ("SELECT f.*, h.username AS home_name, a.username AS away_name, g.name AS game_name, g.code AS game_code, "
           "r.username AS reported_name, x.username AS rejected_name, "
           "COALESCE(ci.name, it.name) AS iso_title, COALESCE(ci.image, it.image) AS iso_image, "
           "COALESCE(ti.original_filename, ci.original_filename, cf.original_filename) AS iso_filename, "
           "it.slug AS iso_tournament_slug FROM friendly_matches f "
           "JOIN players h ON h.id = f.home_id JOIN players a ON a.id = f.away_id "
           "LEFT JOIN game_titles g ON g.id = f.game_title_id "
           "LEFT JOIN players r ON r.id = f.reported_by LEFT JOIN players x ON x.id = f.rejected_by "
           "LEFT JOIN tournaments it ON it.id = f.iso_tournament_id "
           "LEFT JOIN tournament_isos ti ON ti.tournament_id = f.iso_tournament_id "
           "LEFT JOIN catalog_isos ci ON ci.id = f.iso_catalog_id LEFT JOIN files cf ON cf.id = ci.file_id ")


def get(db, friendly_id):
    return db.execute(_SELECT + "WHERE f.id = ?", (friendly_id,)).fetchone()


def _mine(db, friendly_id, me):
    row = get(db, friendly_id)
    if row is None or me not in (row["home_id"], row["away_id"]):
        raise TournamentError("Amistoso não encontrado.")
    return row


def opponent_id(row, me):
    return row["away_id"] if row["home_id"] == me else row["home_id"]


def _set(db, friendly_id, **values):
    values["updated_at"] = now()
    db.execute(f"UPDATE friendly_matches SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
               (*values.values(), friendly_id))


def invite(db, me, other_id, iso_choice=None, message=""):
    """Convida um amigo, com a ISO escolhida (opcional - "t:<torneio>" ou
    "c:<cadastro>", iso_catalog.py); devolve o id do amistoso."""
    if other_id == me:
        raise TournamentError("Escolha um amigo para o amistoso.")
    if not other_id or not are_friends(db, me, other_id):
        raise TournamentError("Amistosos só podem ser marcados com amigos.")
    iso_tournament_id, iso_catalog_id, game_id = iso_catalog.resolve(db, me, iso_choice)
    if db.execute(f"SELECT 1 FROM friendly_matches WHERE status IN ({','.join('?' * len(OPEN))}) AND "
                  "((home_id = ? AND away_id = ?) OR (home_id = ? AND away_id = ?))",
                  (*OPEN, me, other_id, other_id, me)).fetchone():
        raise TournamentError("Vocês já têm um amistoso em aberto. Termine (ou cancele) esse antes de marcar outro.")
    stamp = now()
    return db.execute(
        "INSERT INTO friendly_matches (iso_tournament_id, iso_catalog_id, game_title_id, home_id, away_id, message, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (iso_tournament_id, iso_catalog_id, game_id, me, other_id, (message or "").strip()[:300], stamp,
         stamp)).lastrowid


def respond(db, friendly_id, me, accept):
    row = _mine(db, friendly_id, me)
    if row["status"] != "invited" or row["away_id"] != me:
        raise TournamentError("Não há convite de amistoso para você responder.")
    _set(db, friendly_id, status="accepted" if accept else "declined")
    return row


def cancel(db, friendly_id, me):
    """Quem convidou cancela o convite; depois de aceito, qualquer um cancela
    enquanto ninguém informou placar."""
    row = _mine(db, friendly_id, me)
    if not (row["status"] == "accepted" or (row["status"] == "invited" and row["home_id"] == me)):
        raise TournamentError("Este amistoso não pode mais ser cancelado.")
    _set(db, friendly_id, status="cancelled")
    return row


def _goals(value):
    try:
        goals = int(value)
    except (TypeError, ValueError):
        raise TournamentError("Informe o placar do amistoso.") from None
    if not 0 <= goals <= 99:
        raise TournamentError("Placar inválido.")
    return goals


def report(db, friendly_id, me, home_goals, away_goals):
    """Um dos dois informa (ou, com o amistoso suspenso, propõe de novo) o placar."""
    row = _mine(db, friendly_id, me)
    if row["status"] == "reported":
        raise TournamentError("Já há um placar esperando confirmação." if row["reported_by"] == me
                              else "O adversário já informou o placar: confirme ou não aceite.")
    if row["status"] not in ("accepted", "suspended"):
        raise TournamentError("Este amistoso não está aberto para placar.")
    _set(db, friendly_id, status="reported", home_goals=_goals(home_goals), away_goals=_goals(away_goals),
         reported_by=me, reported_at=now())
    return row


def _waiting_for_me(row, me):
    if row["status"] != "reported" or row["reported_by"] == me:
        raise TournamentError("Não há placar do adversário para você responder neste amistoso.")


def confirm(db, friendly_id, me, at=None):
    row = _mine(db, friendly_id, me)
    _waiting_for_me(row, me)
    ranked, reason = eligibility(db, row, at)
    _set(db, friendly_id, status="confirmed", confirmed_at=(at or datetime.now(timezone.utc)).replace(
        microsecond=0).isoformat(), ranked=int(ranked), unranked_reason=reason)
    return get(db, friendly_id)


def reject(db, friendly_id, me, note=""):
    """Não aceita o placar: o amistoso fica suspenso até um placar que os dois aceitem."""
    row = _mine(db, friendly_id, me)
    _waiting_for_me(row, me)
    _set(db, friendly_id, status="suspended", rejected_by=me, rejected_note=(note or "").strip()[:300])
    return row


def eligibility(db, row, at=None):
    """(vale, motivo) - se o amistoso entra no Ranking/XP, decidido na confirmação."""
    peaks = xp.tournament_peaks(db)
    low = [name for pid, name in ((row["home_id"], row["home_name"]), (row["away_id"], row["away_name"]))
           if peaks.get(pid, 0) < MIN_XP]
    if low:
        who = " e ".join(low)
        return False, (f"{who} ainda não {'chegaram' if len(low) > 1 else 'chegou'} a {MIN_XP} XP em torneios "
                       f"(Rank {xp.RANKS[xp.level(MIN_XP)]}): amistoso só vale para o Ranking entre jogadores "
                       "que já pontuaram em torneios")
    since = ((at or datetime.now(timezone.utc)) - timedelta(hours=24)).replace(microsecond=0).isoformat()
    count = db.execute(
        "SELECT COUNT(*) FROM friendly_matches WHERE status = 'confirmed' AND ranked = 1 AND confirmed_at > ? "
        "AND ((home_id = ? AND away_id = ?) OR (home_id = ? AND away_id = ?))",
        (since, row["home_id"], row["away_id"], row["away_id"], row["home_id"])).fetchone()[0]
    if count >= PAIR_DAILY_LIMIT:
        return False, f"Limite de {PAIR_DAILY_LIMIT} amistosos valendo por dia entre os mesmos jogadores"
    return True, ""


def preview(db, me, other_id):
    """Se um amistoso entre os dois valeria agora (para avisar antes do convite)."""
    names = dict(db.execute("SELECT id, username FROM players WHERE id IN (?, ?)", (me, other_id)).fetchall())
    row = {"home_id": me, "away_id": other_id, "home_name": names.get(me, ""), "away_name": names.get(other_id, "")}
    return eligibility(db, row)


def for_player(db, me):
    """Amistosos do jogador: os que esperam alguma ação primeiro, depois os mais recentes."""
    return db.execute(
        _SELECT + "WHERE f.home_id = ? OR f.away_id = ? ORDER BY "
        "CASE f.status WHEN 'invited' THEN 0 WHEN 'reported' THEN 0 WHEN 'suspended' THEN 1 WHEN 'accepted' THEN 1 "
        "WHEN 'confirmed' THEN 2 ELSE 3 END, f.updated_at DESC, f.id DESC", (me, me)).fetchall()


def needs_me(row, me):
    """O amistoso está esperando uma ação deste jogador?"""
    status = row["status"]
    return ((status == "invited" and row["away_id"] == me) or status in ("accepted", "suspended")
            or (status == "reported" and row["reported_by"] != me))


def todo_count(db, me):
    return db.execute(
        "SELECT COUNT(*) FROM friendly_matches WHERE (home_id = ? OR away_id = ?) AND ("
        "(status = 'invited' AND away_id = ?) OR status IN ('accepted', 'suspended') OR "
        "(status = 'reported' AND reported_by != ?))", (me, me, me, me)).fetchone()[0]
