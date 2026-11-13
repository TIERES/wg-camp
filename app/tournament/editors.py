"""Ajudantes: amigos que ajudam a editar uma ISO dos amistosos ou um campeonato.

Quem enviou a ISO (ou o organizador do campeonato) escolhe, entre os amigos
dele, quem pode editar junto: as informações, a imagem e o arquivo da ISO. O
dono e os ajudantes são os responsáveis.

A regra da confirmação está toda em can_confirm: a alteração feita por um
responsável vale quando OUTRO responsável confirma (o e-mail leva o antes e o
depois - notify.py). Na ISO dos amistosos isso se soma à aprovação da
administração e ao prazo (iso_catalog.py); no campeonato só há confirmação
quando existem ajudantes (changes.py).
"""
from ..db import now
from .store import TournamentError, are_friends

MAX_HELPERS = 10
# tipo -> (tabela, coluna com o id do registro)
TABLES = {"iso": ("catalog_iso_editors", "catalog_iso_id"), "tournament": ("tournament_editors", "tournament_id")}


def helpers(db, kind, key):
    """Os ajudantes (linhas com id, username e email), pelo nick."""
    table, column = TABLES[kind]
    return db.execute(f"SELECT p.id, p.username, p.email FROM {table} e JOIN players p ON p.id = e.player_id "
                      f"WHERE e.{column} = ? ORDER BY p.username COLLATE NOCASE", (key,)).fetchall()


def is_helper(db, kind, key, player_id):
    table, column = TABLES[kind]
    return bool(player_id) and db.execute(f"SELECT 1 FROM {table} WHERE {column} = ? AND player_id = ?",
                                          (key, player_id)).fetchone() is not None


def responsible(db, kind, key, owner_id):
    """Os responsáveis: o dono (quem enviou a ISO / o organizador) e os ajudantes."""
    owner = db.execute("SELECT id, username, email FROM players WHERE id = ?", (owner_id,)).fetchall() if owner_id else []
    return owner + [p for p in helpers(db, kind, key) if p["id"] != owner_id]


def can_confirm(responsible_ids, author_id, player_id, alone_ok=False):
    """O jogador pode confirmar (ou recusar) a alteração feita por author_id?
    Só outro responsável. alone_ok (campeonato): se não sobrou nenhum outro
    responsável (os ajudantes saíram), o próprio autor confirma."""
    if player_id not in responsible_ids:
        return False
    if player_id != author_id:
        return True
    return alone_ok and not [other for other in responsible_ids if other != author_id]


def parse_ids(values):
    """Os ids do formulário (helper_ids), sem repetir e na ordem."""
    return list(dict.fromkeys(int(value) for value in values if str(value).isdigit()))


def check(db, kind, owner_id, player_ids, current_ids=()):
    """Os ajudantes pedidos valem? Quem entra agora (fora de current_ids) precisa
    ser amigo do dono. Devolve os ids pedidos (sem o dono, sem repetir) e as
    linhas (id, username, email) de quem entra."""
    wanted = [pid for pid in dict.fromkeys(player_ids) if pid != owner_id]
    if len(wanted) > MAX_HELPERS:
        raise TournamentError(f"No máximo {MAX_HELPERS} ajudantes.")
    new = []
    for pid in wanted:
        if pid in current_ids:
            continue
        player = db.execute("SELECT id, username, email FROM players WHERE id = ?", (pid,)).fetchone()
        if player is None or not owner_id or not are_friends(db, owner_id, pid):
            raise TournamentError(f"Só os amigos de quem {'enviou a ISO' if kind == 'iso' else 'criou o campeonato'} "
                                  "podem ajudar a editar.")
        new.append(player)
    return wanted, new


def set_helpers(db, kind, key, owner_id, player_ids, added_by=None):
    """Deixa os ajudantes iguais à lista. Quem entra agora precisa ser amigo do
    dono; quem já era ajudante continua valendo. Devolve (entraram, saíram),
    linhas com id, username e email, para os e-mails (depois do commit)."""
    table, column = TABLES[kind]
    current = {p["id"]: p for p in helpers(db, kind, key)}
    wanted, added = check(db, kind, owner_id, player_ids, current)
    for player in added:
        db.execute(f"INSERT INTO {table} ({column}, player_id, added_by, created_at) VALUES (?, ?, ?, ?)",
                   (key, player["id"], added_by, now()))
    removed = [player for pid, player in current.items() if pid not in wanted]
    for player in removed:
        db.execute(f"DELETE FROM {table} WHERE {column} = ? AND player_id = ?", (key, player["id"]))
    return added, removed
