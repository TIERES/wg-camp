"""ISOs para os amistosos: as dos torneios e as do cadastro da administração.

catalog_isos (cadastro da administração do WE Camp): nome, jogo base, imagem
opcional e o arquivo - uma ISO já enviada num Campeonato (files.id) ou enviada
no próprio cadastro (mesmo fluxo do archive.org das ISOs de torneio - iso.py).

Um jogador que não encontra a ISO pode enviá-la (request_upload): ela vai
para o archive.org e fica inativa até a administração liberar (approve),
confirmando nome, jogo base e a imagem.

No convite do amistoso a escolha é opcional e vira o valor "t:<torneio>" ou
"c:<cadastro>"; o jogo do amistoso (Ranking por jogo) é o da ISO.
"""
from ..db import now
from ..storage import delete_stored_file, store_upload
from . import iso
from .store import TournamentError, are_friends

ARCHIVE_FOLDER = "isos"


def get(db, catalog_id):
    return db.execute(
        "SELECT c.*, g.name AS game_name, g.code AS game_code, f.original_filename AS file_filename, "
        "f.display_name AS file_display_name, f.stored_name AS file_stored_name, f.archive_url AS file_archive_url, "
        "f.local_removed_at AS file_local_removed_at, rp.username AS requester_name, rp.email AS requester_email "
        "FROM catalog_isos c JOIN game_titles g ON g.id = c.game_title_id LEFT JOIN files f ON f.id = c.file_id "
        "LEFT JOIN players rp ON rp.id = c.requested_by WHERE c.id = ?",
        (catalog_id,)).fetchone()


def all_rows(db):
    return db.execute(
        "SELECT c.*, g.name AS game_name, g.code AS game_code, f.original_filename AS file_filename, "
        "f.display_name AS file_display_name, rp.username AS requester_name FROM catalog_isos c "
        "JOIN game_titles g ON g.id = c.game_title_id LEFT JOIN files f ON f.id = c.file_id "
        "LEFT JOIN players rp ON rp.id = c.requested_by "
        "ORDER BY c.approved_at IS NOT NULL, c.is_active DESC, c.name COLLATE NOCASE").fetchall()


def requested_by(db, player_id):
    """ISOs que o jogador enviou (as aguardando liberação primeiro)."""
    return db.execute(
        "SELECT c.*, g.name AS game_name, g.code AS game_code FROM catalog_isos c "
        "JOIN game_titles g ON g.id = c.game_title_id WHERE c.requested_by = ? "
        "ORDER BY c.approved_at IS NOT NULL, c.id DESC", (player_id,)).fetchall()


def championship_files(db):
    """ISOs já enviadas nos Campeonatos (para o admin escolher no cadastro)."""
    return db.execute(
        "SELECT f.id, f.display_name, f.original_filename, c.name AS championship_name FROM files f "
        "JOIN championships c ON c.id = f.championship_id WHERE f.file_type = 'iso' "
        "ORDER BY c.start_date DESC, c.created_at DESC, f.id DESC").fetchall()


def filename(row):
    return row["original_filename"] or row["file_filename"] or ""


def save(db, catalog_id, name, game_id, file_id=None, upload=None, active=True):
    """Cria (catalog_id None) ou altera o cadastro; devolve o id. Com `upload`,
    a ISO enviada substitui o arquivo anterior (o commit e o envio para o
    archive.org ficam com quem chama). Sem upload nem ISO de Campeonato, mantém
    o arquivo que já tinha."""
    name = (name or "").strip()
    if not name:
        raise TournamentError("Informe o nome da ISO.")
    if not db.execute("SELECT 1 FROM game_titles WHERE id = ? AND is_active = 1", (game_id,)).fetchone():
        raise TournamentError("Escolha o jogo base da ISO.")
    if file_id and not db.execute("SELECT 1 FROM files WHERE id = ? AND file_type = 'iso'", (file_id,)).fetchone():
        raise TournamentError("ISO do Campeonato não encontrada.")
    previous = get(db, catalog_id) if catalog_id else None
    if catalog_id and previous is None:
        raise TournamentError("ISO não encontrada.")
    if upload is None and not file_id and not (previous and (previous["stored_name"] or previous["file_id"])):
        raise TournamentError("Envie o arquivo da ISO ou escolha uma ISO já enviada num Campeonato.")
    if upload is not None and iso.uploading(previous):
        raise TournamentError("A ISO anterior ainda está sendo enviada para o archive.org. Tente de novo em alguns "
                              "minutos.")
    stamp = now()
    if catalog_id is None:
        catalog_id = db.execute(
            "INSERT INTO catalog_isos (name, game_title_id, is_active, approved_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (name, game_id, int(active), stamp, stamp, stamp)).lastrowid
    else:
        db.execute("UPDATE catalog_isos SET name = ?, game_title_id = ?, is_active = ?, updated_at = ? WHERE id = ?",
                   (name, game_id, int(active), stamp, catalog_id))
    if upload is not None:
        iso.check_upload(upload)
        try:
            stored = store_upload(upload)
        except ValueError as error:
            raise TournamentError(str(error)) from None
        if stored["file_size"] == 0:
            delete_stored_file(stored["stored_name"])
            raise TournamentError("O arquivo enviado está vazio.")
        db.execute(
            "UPDATE catalog_isos SET file_id = NULL, stored_name = ?, original_filename = ?, file_size = ?, md5 = ?, "
            "content_id = ?, archive_name = ?, archive_status = 'pending', archive_url = NULL, archive_error = NULL, "
            "archive_updated_at = NULL, local_removed_at = NULL WHERE id = ?",
            (stored["stored_name"], stored["original_filename"], stored["file_size"], stored["md5"],
             iso.content_id_of(stored), f"{ARCHIVE_FOLDER}/{stored['original_filename']}", catalog_id))
        _drop_local(previous)
    elif file_id:
        db.execute(
            "UPDATE catalog_isos SET file_id = ?, stored_name = NULL, original_filename = NULL, file_size = NULL, "
            "md5 = NULL, archive_name = NULL, archive_status = NULL, archive_url = NULL, archive_error = NULL, "
            "archive_updated_at = NULL, local_removed_at = NULL, "
            "content_id = (SELECT content_id FROM files WHERE id = ?) WHERE id = ?", (file_id, file_id, catalog_id))
        _drop_local(previous)
    return catalog_id


def request_upload(db, player_id, name, game_id, upload):
    """ISO enviada por um jogador: entra inativa, esperando a administração
    liberar. Devolve o id (commit e envio para o archive.org com quem chama)."""
    if upload is None:
        raise TournamentError("Escolha o arquivo da ISO.")
    catalog_id = save(db, None, name, game_id, upload=upload, active=False)
    db.execute("UPDATE catalog_isos SET requested_by = ?, approved_at = NULL WHERE id = ?", (player_id, catalog_id))
    return catalog_id


def approve(db, catalog_id):
    """Libera a ISO enviada por um jogador (ela passa a aparecer no convite)."""
    db.execute("UPDATE catalog_isos SET approved_at = ?, is_active = 1, updated_at = ? WHERE id = ? "
               "AND approved_at IS NULL", (now(), now(), catalog_id))


def reject(db, catalog_id):
    """Recusa a ISO enviada por um jogador: apaga o cadastro e a cópia deste
    servidor (o que já foi para o archive.org fica lá). Devolve a linha."""
    row = get(db, catalog_id)
    if row is None or row["approved_at"]:
        raise TournamentError("Esta ISO não está aguardando liberação.")
    if iso.uploading(row):
        raise TournamentError("A ISO ainda está sendo enviada para o archive.org. Tente de novo quando terminar.")
    db.execute("UPDATE friendly_matches SET iso_catalog_id = NULL WHERE iso_catalog_id = ?", (catalog_id,))
    db.execute("DELETE FROM catalog_isos WHERE id = ?", (catalog_id,))
    _drop_local(row)
    return row


def _drop_local(previous):
    if previous and previous["stored_name"] and not previous["local_removed_at"]:
        try:
            delete_stored_file(previous["stored_name"])
        except (ValueError, OSError):
            pass


def _visible(db, me, tournament):
    """Torneio "Somente para amigos": só quem pode vê-lo vê a ISO dele."""
    if not tournament["friends_only"] or tournament["organizer_id"] == me:
        return True
    if are_friends(db, me, tournament["organizer_id"]):
        return True
    return db.execute("SELECT 1 FROM tournament_entries WHERE tournament_id = ? AND player_id = ?",
                      (tournament["id"], me)).fetchone() is not None


def options(db, me):
    """As ISOs que o jogador pode escolher no convite (do cadastro e dos
    torneios), as cadastradas mais recentemente primeiro: a do cadastro conta
    da liberação (ou do envio, sem liberação), a do torneio, do envio."""
    result = [{
        "value": f"c:{row['id']}", "kind": "catalog", "title": row["name"], "game": row["game_name"],
        "game_code": row["game_code"], "image": row["image"], "filename": filename(row),
        "registered_at": row["approved_at"] or row["created_at"] or "",
    } for row in all_rows(db) if row["is_active"] and (row["stored_name"] or row["file_id"])]
    rows = db.execute(
        "SELECT t.id, t.name, t.image, t.friends_only, t.organizer_id, g.name AS game_name, g.code AS game_code, "
        "i.original_filename, i.created_at FROM tournament_isos i JOIN tournaments t ON t.id = i.tournament_id "
        "JOIN game_titles g ON g.id = t.game_title_id WHERE t.status != 'cancelled' ORDER BY t.id DESC").fetchall()
    result += [{
        "value": f"t:{row['id']}", "kind": "tournament", "title": row["name"], "game": row["game_name"],
        "game_code": row["game_code"], "image": row["image"], "filename": row["original_filename"],
        "registered_at": row["created_at"] or "",
    } for row in rows if _visible(db, me, row)]
    result.sort(key=lambda o: o["registered_at"], reverse=True)  # sort estável: empate fica na ordem acima
    return result


def resolve(db, me, value):
    """Valor escolhido no convite -> (iso_tournament_id, iso_catalog_id, game_title_id)."""
    value = (value or "").strip()
    if not value:
        return None, None, None
    kind, _, raw = value.partition(":")
    if not raw.isdigit():
        raise TournamentError("ISO inválida.")
    key = int(raw)
    if kind == "t":
        row = db.execute("SELECT t.* FROM tournament_isos i JOIN tournaments t ON t.id = i.tournament_id "
                         "WHERE t.id = ? AND t.status != 'cancelled'", (key,)).fetchone()
        if row is None or not _visible(db, me, row):
            raise TournamentError("ISO não encontrada.")
        return key, None, row["game_title_id"]
    if kind == "c":
        row = get(db, key)
        if row is None or not row["is_active"] or not (row["stored_name"] or row["file_id"]):
            raise TournamentError("ISO não encontrada.")
        return None, key, row["game_title_id"]
    raise TournamentError("ISO inválida.")
