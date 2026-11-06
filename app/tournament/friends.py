"""Amigos e convites para torneios.

Amizade: um pedido (friendships.status = 'pending') que o outro jogador aceita
ou recusa; recusado ou desfeito, a linha é apagada e dá para pedir de novo.

Convite: o organizador chama jogadores para um torneio com inscrições abertas.
Só pode convidar os amigos dele e quem já jogou algum torneio criado por ele
(torneio "Somente para amigos": só os amigos).
O convite não inscreve ninguém - o convidado entra pela página do torneio,
como qualquer jogador, ou recusa o convite.
"""
from ..db import now
from .store import TournamentError

INVITE_LABELS = {"pending": "convidado", "declined": "recusou", "enrolled": "inscrito"}


def _pair(db, a, b):
    return db.execute("SELECT * FROM friendships WHERE (requester_id = ? AND addressee_id = ?) "
                      "OR (requester_id = ? AND addressee_id = ?)", (a, b, b, a)).fetchone()


def relation(db, me, other):
    """None, 'friends', 'sent' (pedido meu esperando) ou 'received' (pedido dele para mim)."""
    row = _pair(db, me, other) if me and other and me != other else None
    if row is None:
        return None
    if row["status"] == "accepted":
        return "friends"
    return "sent" if row["requester_id"] == me else "received"


def find_player(db, name):
    return db.execute("SELECT id, username, email FROM players WHERE username = ? COLLATE NOCASE "
                      "AND email_verified_at IS NOT NULL", ((name or "").strip(),)).fetchone()


def request(db, me, other_id):
    """Pede amizade. Se o outro já tinha pedido, aceita. Devolve 'sent' ou 'accepted'."""
    if me == other_id:
        raise TournamentError("Você não pode adicionar você mesmo.")
    if db.execute("SELECT 1 FROM players WHERE id = ?", (other_id,)).fetchone() is None:
        raise TournamentError("Jogador não encontrado.")
    row = _pair(db, me, other_id)
    if row is None:
        db.execute("INSERT INTO friendships (requester_id, addressee_id, created_at) VALUES (?, ?, ?)",
                   (me, other_id, now()))
        return "sent"
    if row["status"] == "accepted":
        raise TournamentError("Vocês já são amigos.")
    if row["requester_id"] == me:
        raise TournamentError("Pedido de amizade já enviado; falta o outro jogador aceitar.")
    db.execute("UPDATE friendships SET status = 'accepted', responded_at = ? WHERE id = ?", (now(), row["id"]))
    return "accepted"


def respond(db, me, other_id, accept):
    row = db.execute("SELECT * FROM friendships WHERE requester_id = ? AND addressee_id = ? AND status = 'pending'",
                     (other_id, me)).fetchone()
    if row is None:
        raise TournamentError("Pedido de amizade não encontrado.")
    if accept:
        db.execute("UPDATE friendships SET status = 'accepted', responded_at = ? WHERE id = ?", (now(), row["id"]))
    else:
        db.execute("DELETE FROM friendships WHERE id = ?", (row["id"],))


def remove(db, me, other_id):
    """Desfaz a amizade ou cancela o pedido que eu enviei."""
    db.execute("DELETE FROM friendships WHERE (requester_id = ? AND addressee_id = ?) "
               "OR (requester_id = ? AND addressee_id = ? AND status = 'accepted')", (me, other_id, other_id, me))


def friends(db, me):
    return db.execute(
        "SELECT p.id, p.username, f.responded_at AS since FROM friendships f "
        "JOIN players p ON p.id = CASE WHEN f.requester_id = ? THEN f.addressee_id ELSE f.requester_id END "
        "WHERE f.status = 'accepted' AND (f.requester_id = ? OR f.addressee_id = ?) ORDER BY p.username COLLATE NOCASE",
        (me, me, me)).fetchall()


def pending_received(db, me):
    return db.execute("SELECT p.id, p.username, f.created_at FROM friendships f JOIN players p ON p.id = f.requester_id "
                      "WHERE f.addressee_id = ? AND f.status = 'pending' ORDER BY f.id DESC", (me,)).fetchall()


def pending_sent(db, me):
    return db.execute("SELECT p.id, p.username, f.created_at FROM friendships f JOIN players p ON p.id = f.addressee_id "
                      "WHERE f.requester_id = ? AND f.status = 'pending' ORDER BY f.id DESC", (me,)).fetchall()


def pending_count(db, me):
    return db.execute("SELECT COUNT(*) FROM friendships WHERE addressee_id = ? AND status = 'pending'",
                      (me,)).fetchone()[0]


# --- Convites -----------------------------------------------------------------

def candidates(db, organizer_id, exclude_tournament_id=None, friends_only=False):
    """Quem o organizador pode convidar: os amigos e quem jogou torneios criados
    por ele (com `friends_only`, só os amigos). `tournaments` = esses torneios com
    os ids dos jogadores, para o botão "jogadores de um torneio seu"."""
    players = {f["id"]: {"id": f["id"], "username": f["username"], "friend": True} for f in friends(db, organizer_id)}
    tournaments = []
    if friends_only:
        return {"players": sorted(players.values(), key=lambda p: p["username"].casefold()), "tournaments": tournaments}
    for t in db.execute("SELECT id, name FROM tournaments WHERE organizer_id = ? AND id != ? AND status != 'draft' "
                        "ORDER BY id DESC", (organizer_id, exclude_tournament_id or 0)).fetchall():
        rows = db.execute("SELECT p.id, p.username FROM tournament_entries e JOIN players p ON p.id = e.player_id "
                          "WHERE e.tournament_id = ? AND p.id != ?", (t["id"], organizer_id)).fetchall()
        if not rows:
            continue
        for row in rows:
            players.setdefault(row["id"], {"id": row["id"], "username": row["username"], "friend": False})
        tournaments.append({"id": t["id"], "name": t["name"], "player_ids": [r["id"] for r in rows]})
    return {"players": sorted(players.values(), key=lambda p: p["username"].casefold()), "tournaments": tournaments}


def invite_status(db, tournament_id):
    """player_id -> 'pending' / 'declined' / 'enrolled' (inscrito, convidado ou não)."""
    status = {r["player_id"]: r["status"] for r in db.execute(
        "SELECT player_id, status FROM tournament_invites WHERE tournament_id = ?", (tournament_id,))}
    for row in db.execute("SELECT player_id FROM tournament_entries WHERE tournament_id = ?", (tournament_id,)):
        status[row["player_id"]] = "enrolled"
    return status


def invite(db, tournament, invited_by, player_ids):
    """Convida os jogadores escolhidos (só candidatos, sem repetir convite pendente
    nem chamar quem já está inscrito; quem recusou pode ser convidado de novo,
    quantas vezes o organizador quiser). Devolve as linhas (id, username, email) dos
    convidados agora, para o e-mail."""
    if tournament["status"] != "registration":
        raise TournamentError("Só dá para convidar enquanto as inscrições estão abertas.")
    allowed = {p["id"] for p in candidates(db, tournament["organizer_id"], tournament["id"],
                                           tournament["friends_only"])["players"]}
    taken = invite_status(db, tournament["id"])
    wanted = [pid for pid in dict.fromkeys(player_ids)
              if pid in allowed and taken.get(pid) not in ("pending", "enrolled")]
    for pid in wanted:
        db.execute("INSERT INTO tournament_invites (tournament_id, player_id, invited_by, created_at) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT (tournament_id, player_id) DO UPDATE SET status = 'pending', "
                   "invited_by = excluded.invited_by, created_at = excluded.created_at",
                   (tournament["id"], pid, invited_by, now()))
    if not wanted:
        return []
    return db.execute(f"SELECT id, username, email FROM players WHERE id IN ({','.join('?' * len(wanted))})",
                      wanted).fetchall()


def my_invites(db, me):
    """Convites ainda valendo: torneio com inscrições abertas e sem a minha inscrição."""
    return db.execute(
        "SELECT i.*, t.slug, t.name, t.max_players, t.entry_fee_cents, t.starts_on, g.name AS game_name, "
        "g.code AS game_code, o.username AS organizer_name, "
        "(SELECT COUNT(*) FROM tournament_entries e WHERE e.tournament_id = t.id) AS entries "
        "FROM tournament_invites i JOIN tournaments t ON t.id = i.tournament_id "
        "JOIN game_titles g ON g.id = t.game_title_id JOIN players o ON o.id = t.organizer_id "
        "WHERE i.player_id = ? AND i.status = 'pending' AND t.status = 'registration' "
        "AND NOT EXISTS (SELECT 1 FROM tournament_entries e WHERE e.tournament_id = t.id AND e.player_id = ?) "
        "ORDER BY i.id DESC", (me, me)).fetchall()


def my_invite(db, tournament_id, me):
    return next((i for i in my_invites(db, me) if i["tournament_id"] == tournament_id), None)


def remove_invite(db, tournament_id, player_id):
    db.execute("DELETE FROM tournament_invites WHERE tournament_id = ? AND player_id = ?", (tournament_id, player_id))


def decline(db, tournament_id, me):
    db.execute("UPDATE tournament_invites SET status = 'declined' WHERE tournament_id = ? AND player_id = ?",
               (tournament_id, me))
