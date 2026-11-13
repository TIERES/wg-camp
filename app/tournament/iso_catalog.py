"""ISOs para os amistosos: as dos torneios e as do cadastro da administração.

catalog_isos (cadastro da administração do WE Camp): nome, jogo base, imagem
opcional e o arquivo - uma ISO já enviada num Campeonato (files.id) ou enviada
no próprio cadastro (mesmo fluxo do archive.org das ISOs de torneio - iso.py).

Um jogador que não encontra a ISO pode enviá-la (request_upload): ela vai
para o archive.org e fica inativa até a administração liberar (approve),
confirmando nome, jogo base e a imagem.

Quem enviou escolhe amigos para ajudar a editar (editors.py): ele e os
ajudantes são os responsáveis pela ISO. Um responsável pode editá-la depois
(request_edit): nome, jogo base, imagem e um arquivo novo. Enquanto ela aguarda
a liberação, a alteração entra direto (a administração confere tudo ao
liberar). Já liberada, vira uma alteração pendente (catalog_iso_revisions): os
amistosos continuam com a versão atual até OUTRO responsável confirmar
(confirm_edit - o e-mail leva o antes e o depois), a administração aprovar ou,
sem resposta, AUTO_APPROVE_HOURS depois (apply_due, chamada pelas páginas de
torneio).

Cada ISO liberada tem a sua página (tournaments.iso_page), para compartilhar:
imagens, informações e o download para quem está logado.

No convite do amistoso a escolha é opcional e vira o valor "t:<torneio>" ou
"c:<cadastro>"; o jogo do amistoso (Ranking por jogo) é o da ISO.
"""
from datetime import datetime, timedelta, timezone

from .. import images
from ..db import now
from ..storage import delete_stored_file, human_size, store_upload
from . import editors, iso
from .store import TournamentError, are_friends

ARCHIVE_FOLDER = "isos"
AUTO_APPROVE_HOURS = 24
REVISIONS = "catalog_iso_revisions"

# A alteração pendente de cada ISO (se houver), com o prefixo rev_.
_REVISION_COLUMNS = (
    "r.id AS rev_id, r.name AS rev_name, r.game_title_id AS rev_game_id, rg.name AS rev_game_name, "
    "r.original_filename AS rev_filename, r.file_size AS rev_file_size, r.stored_name AS rev_stored_name, "
    "r.archive_status AS rev_archive_status, r.local_removed_at AS rev_local_removed_at, "
    "r.approved_at AS rev_approved_at, r.submitted_at AS rev_submitted_at, "
    "r.image_change AS rev_image_change, r.image AS rev_image, r.requested_by AS rev_requested_by, "
    "rvp.username AS rev_requester_name, r.confirmed_by AS rev_confirmed_by")
_REVISION_JOIN = ("LEFT JOIN catalog_iso_revisions r ON r.catalog_iso_id = c.id "
                  "LEFT JOIN game_titles rg ON rg.id = r.game_title_id "
                  "LEFT JOIN players rvp ON rvp.id = r.requested_by")
# "A imagem não muda" nas contas do que mudou (None = sem imagem).
_KEEP = object()


class NothingChanged(TournamentError):
    """Editar sem mudar nada (quem chama pode ter mudado só os ajudantes)."""


def get(db, catalog_id):
    return db.execute(
        "SELECT c.*, g.name AS game_name, g.code AS game_code, f.original_filename AS file_filename, "
        "f.display_name AS file_display_name, f.stored_name AS file_stored_name, f.archive_url AS file_archive_url, "
        "f.local_removed_at AS file_local_removed_at, f.file_size AS file_file_size, "
        "rp.username AS requester_name, rp.email AS requester_email, "
        f"{_REVISION_COLUMNS} "
        "FROM catalog_isos c JOIN game_titles g ON g.id = c.game_title_id LEFT JOIN files f ON f.id = c.file_id "
        f"LEFT JOIN players rp ON rp.id = c.requested_by {_REVISION_JOIN} WHERE c.id = ?",
        (catalog_id,)).fetchone()


def all_rows(db):
    return db.execute(
        "SELECT c.*, g.name AS game_name, g.code AS game_code, f.original_filename AS file_filename, "
        f"f.display_name AS file_display_name, rp.username AS requester_name, {_REVISION_COLUMNS} FROM catalog_isos c "
        "JOIN game_titles g ON g.id = c.game_title_id LEFT JOIN files f ON f.id = c.file_id "
        f"LEFT JOIN players rp ON rp.id = c.requested_by {_REVISION_JOIN} "
        "ORDER BY c.approved_at IS NOT NULL, c.is_active DESC, c.name COLLATE NOCASE").fetchall()


def mine(db, player_id):
    """ISOs de que o jogador é responsável: as que enviou e as que ajuda a
    editar (as aguardando liberação primeiro)."""
    return db.execute(
        f"SELECT c.*, g.name AS game_name, g.code AS game_code, rp.username AS requester_name, {_REVISION_COLUMNS} "
        "FROM catalog_isos c JOIN game_titles g ON g.id = c.game_title_id "
        f"LEFT JOIN players rp ON rp.id = c.requested_by {_REVISION_JOIN} WHERE c.requested_by = ? "
        "OR c.id IN (SELECT catalog_iso_id FROM catalog_iso_editors WHERE player_id = ?) "
        "ORDER BY c.approved_at IS NOT NULL, c.id DESC", (player_id, player_id)).fetchall()


def revision(db, catalog_id):
    """A alteração pendente da ISO (None se não houver)."""
    return db.execute(
        "SELECT r.*, g.name AS game_name, p.username AS requester_name, p.email AS requester_email, "
        "cp.username AS confirmer_name FROM catalog_iso_revisions r "
        "JOIN game_titles g ON g.id = r.game_title_id LEFT JOIN players p ON p.id = r.requested_by "
        "LEFT JOIN players cp ON cp.id = r.confirmed_by WHERE r.catalog_iso_id = ?", (catalog_id,)).fetchone()


def responsible(db, row):
    """Os responsáveis pela ISO: quem enviou e os ajudantes (id, username, email)."""
    return editors.responsible(db, "iso", row["id"], row["requested_by"])


def can_edit(db, row, player_id):
    """Quem enviou a ISO ou um ajudante dela."""
    return bool(player_id) and (row["requested_by"] == player_id or editors.is_helper(db, "iso", row["id"], player_id))


def can_confirm(db, row, player_id):
    """O jogador pode confirmar (ou recusar) a alteração pendente? Só outro
    responsável - nunca quem a fez (editors.can_confirm)."""
    if not row["rev_id"] or row["rev_approved_at"]:
        return False
    return editors.can_confirm([p["id"] for p in responsible(db, row)], row["rev_requested_by"], player_id)


def pending_count(db):
    """Para o selo no menu da administração: ISOs enviadas por jogadores
    esperando a liberação + alterações esperando aprovação."""
    return db.execute("SELECT (SELECT COUNT(*) FROM catalog_isos WHERE approved_at IS NULL) + "
                      "(SELECT COUNT(*) FROM catalog_iso_revisions WHERE approved_at IS NULL)").fetchone()[0]


def auto_approve_at(submitted_at):
    """Quando a alteração enviada em submitted_at é aprovada sozinha (ISO UTC)."""
    return (datetime.fromisoformat(submitted_at) + timedelta(hours=AUTO_APPROVE_HOURS)).isoformat()


def busy(rev):
    """O arquivo novo da alteração ainda está indo para o archive.org (ou sendo
    conferido lá): ela só é aplicada depois."""
    return bool(rev and rev["stored_name"] and not rev["local_removed_at"]
                and rev["archive_status"] in ("uploading", "verifying"))


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
    _check_game(db, game_id)
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
    # O arquivo é copiado antes de escrever no banco: copiar centenas de MB com a
    # transação aberta segurava a trava de escrita do SQLite, e a thread do
    # archive.org de outra ISO falhava com "database is locked" (ficava em
    # "conferindo" para sempre).
    stored = _store(upload) if upload is not None else None
    try:
        stamp = now()
        if catalog_id is None:
            catalog_id = db.execute(
                "INSERT INTO catalog_isos (name, game_title_id, is_active, approved_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (name, game_id, int(active), stamp, stamp, stamp)).lastrowid
        else:
            db.execute("UPDATE catalog_isos SET name = ?, game_title_id = ?, is_active = ?, updated_at = ? "
                       "WHERE id = ?", (name, game_id, int(active), stamp, catalog_id))
        if stored is not None:
            db.execute(
                "UPDATE catalog_isos SET file_id = NULL, stored_name = ?, original_filename = ?, file_size = ?, "
                "md5 = ?, content_id = ?, archive_name = ?, archive_status = 'pending', archive_url = NULL, "
                "archive_error = NULL, archive_updated_at = NULL, archive_progress = NULL, local_removed_at = NULL "
                "WHERE id = ?",
                (stored["stored_name"], stored["original_filename"], stored["file_size"], stored["md5"],
                 iso.content_id_of(stored), _archive_name(db, stored), catalog_id))
        elif file_id:
            db.execute(
                "UPDATE catalog_isos SET file_id = ?, stored_name = NULL, original_filename = NULL, file_size = NULL, "
                "md5 = NULL, archive_name = NULL, archive_status = NULL, archive_url = NULL, archive_error = NULL, "
                "archive_updated_at = NULL, local_removed_at = NULL, "
                "content_id = (SELECT content_id FROM files WHERE id = ?) WHERE id = ?", (file_id, file_id, catalog_id))
    except BaseException:
        _drop_local(stored)
        raise
    if stored is not None or file_id:
        _drop_local(previous)
    return catalog_id


def _check_game(db, game_id):
    if not db.execute("SELECT 1 FROM game_titles WHERE id = ? AND is_active = 1", (game_id,)).fetchone():
        raise TournamentError("Escolha o jogo base da ISO.")


def _store(upload):
    """Guarda o arquivo enviado em DOWNLOADS_DIR (storage.store_upload)."""
    iso.check_upload(upload)
    try:
        stored = store_upload(upload)
    except ValueError as error:
        raise TournamentError(str(error)) from None
    if stored["file_size"] == 0:
        delete_stored_file(stored["stored_name"])
        raise TournamentError("O arquivo enviado está vazio.")
    return stored


def _archive_name(db, stored):
    """Nome no item do archive.org: isos/<arquivo>. Se outra ISO do cadastro (ou
    uma alteração pendente) já usa esse nome com outro conteúdo, vai numa
    subpasta (isos/<n>/<arquivo>), porque o envio substituiria o arquivo dela
    lá. O nome do arquivo baixado continua o mesmo."""
    filename_, md5 = stored["original_filename"], stored["md5"]

    def taken(name):
        return db.execute(
            "SELECT 1 FROM catalog_isos WHERE archive_name = ? AND IFNULL(md5, '') != ? UNION ALL "
            "SELECT 1 FROM catalog_iso_revisions WHERE archive_name = ? AND IFNULL(md5, '') != ? LIMIT 1",
            (name, md5, name, md5)).fetchone() is not None

    name, number = f"{ARCHIVE_FOLDER}/{filename_}", 1
    while taken(name):
        number += 1
        name = f"{ARCHIVE_FOLDER}/{number}/{filename_}"
    return name


def request_upload(db, player_id, name, game_id, upload, image=None):
    """ISO enviada por um jogador: entra inativa, esperando a administração
    liberar. `image`: a imagem já gravada (images.save), opcional. Devolve o id
    (commit e envio para o archive.org com quem chama)."""
    if upload is None:
        raise TournamentError("Escolha o arquivo da ISO.")
    catalog_id = save(db, None, name, game_id, upload=upload, active=False)
    db.execute("UPDATE catalog_isos SET requested_by = ?, approved_at = NULL, image = ? WHERE id = ?",
               (player_id, image, catalog_id))
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


# --- Edição pelos responsáveis (quem enviou a ISO e os ajudantes) ----------------

def _changes(row, name, game_id, game_name, new_file, image=_KEEP):
    """O que muda em relação à versão atual: [{"field", "label", "before",
    "after"}] - o "antes e depois" dos e-mails (notify.change_lines) e das
    páginas. Na imagem, before/after são os nomes dos arquivos ("" = sem imagem)."""
    changes = []
    if name != row["name"]:
        changes.append({"field": "name", "label": "Nome", "before": row["name"], "after": name})
    if game_id != row["game_title_id"]:
        changes.append({"field": "game", "label": "Jogo base", "before": row["game_name"], "after": game_name})
    if new_file is not None:
        size = f" ({human_size(new_file['file_size'])})" if new_file["file_size"] else ""
        changes.append({"field": "file", "label": "Arquivo", "before": filename(row) or "-",
                        "after": f"{new_file['original_filename']}{size}"})
    if image is not _KEEP and (image or "") != (row["image"] or ""):
        changes.append({"field": "image", "label": "Imagem", "before": row["image"] or "", "after": image or ""})
    return changes


def pending_changes(row):
    """As mudanças da alteração pendente de uma ISO (linha com as colunas rev_*)."""
    if not row["rev_id"]:
        return []
    new_file = ({"original_filename": row["rev_filename"], "file_size": row["rev_file_size"]}
                if row["rev_stored_name"] else None)
    return _changes(row, row["rev_name"], row["rev_game_id"], row["rev_game_name"], new_file,
                    row["rev_image"] if row["rev_image_change"] else _KEEP)


def _drop_image(name):
    """Apaga uma imagem (ou o cartão do link compartilhado) que saiu de uso."""
    if name:
        images.delete("torneio", name)


def request_edit(db, player_id, catalog_id, name, game_id, upload=None, image=None, remove_image=False):
    """Um responsável (quem enviou ou um ajudante) altera o nome, o jogo base, a
    imagem e/ou envia um arquivo novo. `image`: a imagem nova já gravada
    (images.save); `remove_image`: fica sem imagem.

    Aguardando liberação: altera direto (mode "direct"). Já liberada: grava a
    alteração pendente (mode "revision"), que substitui uma anterior e reinicia
    o prazo da aprovação automática; um arquivo igual ao atual não conta como
    mudança, e sem imagem nova a da pendente continua valendo. Voltando tudo ao
    que já é a versão atual, a pendente é descartada (mode "cancelled").
    Devolve {"mode", "changes", "uploaded"} - o commit e o envio do arquivo para
    o archive.org ficam com quem chama, que também apaga `image` se der erro."""
    row = get(db, catalog_id)
    if row is None or not can_edit(db, row, player_id):
        raise TournamentError("ISO não encontrada.")
    name = (name or "").strip()
    if not name:
        raise TournamentError("Informe o nome da ISO.")
    _check_game(db, game_id)
    game_name = db.execute("SELECT name FROM game_titles WHERE id = ?", (game_id,)).fetchone()[0]
    nothing = "Nada foi alterado: mude o nome, o jogo base, a imagem ou envie um arquivo novo."

    if not row["approved_at"]:
        target = image if image else (None if remove_image and row["image"] else _KEEP)
        changes = _changes(row, name, game_id, game_name, None, target)
        if upload is None and not changes:
            raise NothingChanged(nothing)
        save(db, catalog_id, name, game_id, upload=upload, active=False)
        if target is not _KEEP:
            db.execute("UPDATE catalog_isos SET image = ?, share_image = NULL WHERE id = ?", (target, catalog_id))
        new_file = _current_file(db, catalog_id) if upload is not None else None
        if target is not _KEEP:
            _drop_image(row["image"])
            _drop_image(row["share_image"])
        return {"mode": "direct", "changes": _changes(row, name, game_id, game_name, new_file, target),
                "uploaded": upload is not None}

    rev = revision(db, catalog_id)
    if upload is not None and rev is not None and iso.uploading(rev):
        raise TournamentError("O arquivo novo enviado antes ainda está indo para o archive.org. Tente de novo em alguns "
                              "minutos.")
    # A imagem da alteração: a nova, tirar a atual, ou a que a pendente já tinha.
    if image:
        image_change, new_image = 1, image
    elif remove_image:
        image_change, new_image = (1 if row["image"] else 0), None
    elif rev is not None:
        image_change, new_image = rev["image_change"], rev["image"]
    else:
        image_change, new_image = 0, None
    stored = _store(upload) if upload is not None else None
    try:
        # file: "new" (o enviado agora), "keep" (o da pendente, se houver) ou "drop" (volta ao atual).
        file = "keep"
        if stored is not None:
            if row["md5"] and stored["md5"] == row["md5"]:
                file = "drop"
            elif not (rev is not None and rev["stored_name"] and stored["md5"] == rev["md5"]):
                file = "new"
            if file != "new":
                _drop_local(stored)
        new_file = stored if file == "new" else (rev if file == "keep" and rev is not None and rev["stored_name"]
                                                 else None)
        changes = _changes(row, name, game_id, game_name, new_file, new_image if image_change else _KEEP)
        if not changes:
            if rev is None:
                raise NothingChanged(nothing)
            _delete_revision(db, rev)
            return {"mode": "cancelled", "changes": [], "uploaded": False}
        stamp = now()
        if rev is None:
            db.execute(
                "INSERT INTO catalog_iso_revisions (catalog_iso_id, requested_by, name, game_title_id, image_change, "
                "image, submitted_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (catalog_id, player_id, name, game_id, image_change, new_image, stamp, stamp, stamp))
        else:
            db.execute("UPDATE catalog_iso_revisions SET requested_by = ?, name = ?, game_title_id = ?, "
                       "image_change = ?, image = ?, approved_at = NULL, confirmed_by = NULL, submitted_at = ?, "
                       "updated_at = ? WHERE id = ?",
                       (player_id, name, game_id, image_change, new_image, stamp, stamp, rev["id"]))
        if file == "new":
            db.execute(
                "UPDATE catalog_iso_revisions SET stored_name = ?, original_filename = ?, file_size = ?, md5 = ?, "
                "content_id = ?, archive_name = ?, archive_status = 'pending', archive_url = NULL, "
                "archive_error = NULL, archive_updated_at = NULL, archive_progress = NULL, local_removed_at = NULL "
                "WHERE catalog_iso_id = ?",
                (stored["stored_name"], stored["original_filename"], stored["file_size"], stored["md5"],
                 iso.content_id_of(stored), _archive_name(db, stored), catalog_id))
        elif file == "drop" and rev is not None and rev["stored_name"]:
            db.execute(
                "UPDATE catalog_iso_revisions SET stored_name = NULL, original_filename = NULL, file_size = NULL, "
                "md5 = NULL, content_id = NULL, archive_name = NULL, archive_status = NULL, archive_url = NULL, "
                "archive_error = NULL, archive_updated_at = NULL, archive_progress = NULL, local_removed_at = NULL "
                "WHERE id = ?", (rev["id"],))
    except BaseException:
        if stored is not None and file == "new":
            _drop_local(stored)
        raise
    if rev is not None and rev["stored_name"] and file in ("new", "drop"):
        _drop_local(rev)
    if rev is not None and rev["image_change"] and rev["image"] != new_image:
        _drop_image(rev["image"])  # a imagem da pendente anterior saiu
    return {"mode": "revision", "changes": changes, "uploaded": file == "new"}


def _current_file(db, catalog_id):
    return db.execute("SELECT original_filename, file_size FROM catalog_isos WHERE id = ?", (catalog_id,)).fetchone()


def _delete_revision(db, rev):
    db.execute("DELETE FROM catalog_iso_revisions WHERE id = ?", (rev["id"],))
    _drop_local(rev)
    if rev["image_change"]:
        _drop_image(rev["image"])


def cancel_edit(db, player_id, catalog_id):
    """Quem fez a alteração pendente desiste dela."""
    rev = revision(db, catalog_id)
    if rev is None or rev["requested_by"] != player_id:
        raise TournamentError("Esta ISO não tem alteração sua esperando aprovação.")
    if iso.uploading(rev):
        raise TournamentError("O arquivo novo ainda está indo para o archive.org. Tente de novo quando terminar.")
    _delete_revision(db, rev)


def _pending_for(db, player_id, catalog_id):
    """(ISO, alteração) se o jogador pode confirmar ou recusar a pendente."""
    row = get(db, catalog_id)
    if row is None or not can_edit(db, row, player_id):
        raise TournamentError("ISO não encontrada.")
    rev = revision(db, catalog_id)
    if rev is None or rev["approved_at"]:
        raise TournamentError("Esta ISO não tem alteração esperando confirmação.")
    if not can_confirm(db, row, player_id):
        raise TournamentError("A alteração precisa ser confirmada por outro responsável pela ISO, não por quem a fez.")
    return row, rev


def confirm_edit(db, player_id, catalog_id):
    """Outro responsável confirma a alteração pendente: vale como a aprovação da
    administração - aplica agora ou, se o arquivo novo ainda está indo para o
    archive.org, assim que terminar (apply_due). Devolve (aplicou?, alteração)."""
    _, rev = _pending_for(db, player_id, catalog_id)
    db.execute("UPDATE catalog_iso_revisions SET approved_at = ?, confirmed_by = ?, updated_at = ? WHERE id = ?",
               (now(), player_id, now(), rev["id"]))
    rev = revision(db, catalog_id)
    if busy(rev):
        return False, rev
    _apply(db, rev)
    return True, rev


def refuse_edit(db, player_id, catalog_id):
    """Outro responsável recusa a alteração pendente: a ISO continua como está.
    Devolve a alteração (para o e-mail de quem a fez)."""
    _, rev = _pending_for(db, player_id, catalog_id)
    if iso.uploading(rev):
        raise TournamentError("O arquivo novo ainda está indo para o archive.org. Tente de novo quando terminar.")
    _delete_revision(db, rev)
    return rev


def approve_edit(db, catalog_id):
    """A administração aprova a alteração pendente: aplica agora ou, se o arquivo
    novo ainda está indo para o archive.org, assim que terminar (apply_due).
    Devolve (aplicou?, alteração)."""
    rev = revision(db, catalog_id)
    if rev is None:
        raise TournamentError("Esta ISO não tem alteração esperando aprovação.")
    db.execute("UPDATE catalog_iso_revisions SET approved_at = ?, updated_at = ? WHERE id = ?",
               (now(), now(), rev["id"]))
    if busy(rev):
        return False, rev
    _apply(db, rev)
    return True, rev


def reject_edit(db, catalog_id):
    """A administração recusa a alteração pendente: a ISO continua como está.
    Devolve a alteração (para o e-mail de quem a fez)."""
    rev = revision(db, catalog_id)
    if rev is None:
        raise TournamentError("Esta ISO não tem alteração esperando aprovação.")
    if iso.uploading(rev):
        raise TournamentError("O arquivo novo ainda está indo para o archive.org. Tente de novo quando terminar.")
    _delete_revision(db, rev)
    return rev


def _apply(db, rev):
    """Troca a versão atual pela da alteração e apaga a alteração."""
    row = get(db, rev["catalog_iso_id"])
    db.execute("UPDATE catalog_isos SET name = ?, game_title_id = ?, updated_at = ? WHERE id = ?",
               (rev["name"], rev["game_title_id"], now(), rev["catalog_iso_id"]))
    if rev["image_change"]:
        # O cartão do link compartilhado era da imagem antiga (a página da ISO monta o novo).
        db.execute("UPDATE catalog_isos SET image = ?, share_image = NULL WHERE id = ?",
                   (rev["image"], rev["catalog_iso_id"]))
    if rev["stored_name"]:
        db.execute(
            "UPDATE catalog_isos SET file_id = NULL, stored_name = ?, original_filename = ?, file_size = ?, md5 = ?, "
            "content_id = ?, archive_name = ?, archive_status = ?, archive_url = ?, archive_error = ?, "
            "archive_updated_at = ?, archive_progress = ?, local_removed_at = ? WHERE id = ?",
            (rev["stored_name"], rev["original_filename"], rev["file_size"], rev["md5"], rev["content_id"],
             rev["archive_name"], rev["archive_status"], rev["archive_url"], rev["archive_error"],
             rev["archive_updated_at"], rev["archive_progress"], rev["local_removed_at"], rev["catalog_iso_id"]))
    db.execute("DELETE FROM catalog_iso_revisions WHERE id = ?", (rev["id"],))
    if rev["stored_name"]:
        _drop_local(row)
    if rev["image_change"]:
        _drop_image(row["image"])
        _drop_image(row["share_image"])


def apply_due(db, app, at=None):
    """Aplica as alterações confirmadas por outro responsável ou aprovadas pela
    administração e as sem resposta há AUTO_APPROVE_HOURS - as com arquivo novo,
    só depois que ele terminou de ir para o archive.org (um envio parado é
    recomeçado: iso.heal). Faz o commit de cada uma e devolve [(ISO como ficou,
    aprovada sozinha?, a alteração)] para os e-mails."""
    at = at or datetime.now(timezone.utc)
    limit = (at - timedelta(hours=AUTO_APPROVE_HOURS)).replace(microsecond=0).isoformat()
    applied = []
    for due in db.execute("SELECT * FROM catalog_iso_revisions WHERE approved_at IS NOT NULL OR submitted_at <= ? "
                          "ORDER BY id", (limit,)).fetchall():
        healed = iso.heal(app, REVISIONS, due)
        if healed is None or busy(healed):
            continue
        rev = revision(db, due["catalog_iso_id"])  # com os nomes de quem fez e de quem confirmou
        _apply(db, rev)
        db.commit()
        applied.append((get(db, rev["catalog_iso_id"]), rev["approved_at"] is None, rev))
    return applied


def _drop_local(previous):
    """Apaga a cópia deste servidor: de uma linha do banco (se ainda está aqui)
    ou do arquivo recém-guardado (o dict do store_upload)."""
    if not previous or not previous["stored_name"]:
        return
    if not isinstance(previous, dict) and previous["local_removed_at"]:
        return
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
