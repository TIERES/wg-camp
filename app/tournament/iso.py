"""ISO do torneio: o organizador envia, o servidor guarda a cópia em
DOWNLOADS_DIR e uma thread manda para o archive.org. Quando o archive.org
lista o arquivo com o mesmo MD5, a cópia local é apagada e o download (só
para quem está logado) passa a redirecionar para lá.

Vai para o mesmo item das ISOs dos campeonatos (app/admin.py), numa pasta
por torneio. As ISOs do cadastro da administração (catalog_isos -
iso_catalog.py) usam o mesmo envio: as funções recebem a tabela. Se o item já tem um arquivo com o mesmo MD5 (a ISO de um
campeonato ou de outro torneio), nada é enviado: o torneio aponta para ele.
"""
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..archive_org import ArchiveOrgError, download_url, list_item_files, upload_file
from ..db import get_db, now
from ..storage import clean_original_filename, delete_stored_file, store_upload
from .store import TournamentError

ARCHIVE_ITEM = "one-two-iso"
EXTENSIONS = {"iso", "bin", "chd", "zip", "7z", "rar"}
# Um envio parado em 'uploading' há mais que isso (servidor reiniciado no
# meio) não impede mais um novo envio.
STALE = timedelta(hours=3)
# Tabela com a ISO -> coluna que identifica a linha.
TABLES = {"tournament_isos": "tournament_id", "catalog_isos": "id"}


def get(db, tournament_id):
    return db.execute("SELECT * FROM tournament_isos WHERE tournament_id = ?", (tournament_id,)).fetchone()


def check_upload(upload):
    """Erro (TournamentError) se o arquivo não pode ser a ISO do torneio."""
    name = clean_original_filename(upload.filename)
    extension = name.rsplit(".", 1)[1].lower() if "." in name else ""
    if extension not in EXTENSIONS:
        raise TournamentError("A ISO precisa ser " + ", ".join(f".{e}" for e in sorted(EXTENSIONS)) + ".")


def check_size(request, max_bytes):
    """O tamanho vem do Content-Length do formulário inteiro: dá para
    recusar antes de ler o corpo (o navegador já confere antes de enviar)."""
    if request.content_length and request.content_length > max_bytes + 1024 ** 2:
        raise TournamentError(f"A ISO pode ter no máximo {max_bytes // 1024 ** 2} MB.")


def downloadable(row, app):
    """Já dá para baixar: está no archive.org (ou foi enviada e está sendo
    confirmada) ou, em teste local sem proxy, direto deste servidor."""
    return bool(row and (row["archive_url"] or (not row["local_removed_at"] and app.config["SERVE_DOWNLOADS_LOCALLY"])))


def status(row, app):
    """Situação do envio para o archive.org, para a barra de progresso
    (static/js/archive-progress.js): state = sending | verifying | done |
    error | local (só neste servidor, sem envio em andamento) | none."""
    if row is None or not row["stored_name"]:
        return {"state": "none"}
    if row["local_removed_at"] or row["archive_status"] == "done":
        return {"state": "done", "progress": 100, "url": row["archive_url"]}
    if uploading(row):
        return {"state": "sending", "progress": row["archive_progress"] or 0}
    if row["archive_status"] == "verifying":
        return {"state": "verifying", "progress": 100}
    if row["archive_status"] == "error":
        return {"state": "error", "error": row["archive_error"] or "Falha ao enviar para o archive.org."}
    return {"state": "local", "configured": archive_configured(app)}


def uploading(row):
    if row is None or row["archive_status"] != "uploading" or not row["archive_updated_at"]:
        return False
    return datetime.now(timezone.utc) - datetime.fromisoformat(row["archive_updated_at"]) < STALE


def save(db, tournament, upload, player_id):
    """Guarda a ISO enviada (substitui a anterior) e deixa pronta para o
    archive.org. O commit fica com quem chama."""
    previous = get(db, tournament["id"])
    if uploading(previous):
        raise TournamentError("A ISO anterior ainda está sendo enviada para o archive.org. Tente de novo em alguns minutos.")
    try:
        stored = store_upload(upload)
    except ValueError as error:
        raise TournamentError(str(error))
    if stored["file_size"] == 0:
        delete_stored_file(stored["stored_name"])
        raise TournamentError("O arquivo enviado está vazio.")
    db.execute(
        "INSERT OR REPLACE INTO tournament_isos (tournament_id, stored_name, original_filename, file_size, md5, "
        "archive_name, archive_status, uploaded_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (tournament["id"], stored["stored_name"], stored["original_filename"], stored["file_size"], stored["md5"],
         f"torneios/{tournament['slug']}/{stored['original_filename']}", player_id, now(), now()))
    if previous and not previous["local_removed_at"]:
        delete_stored_file(previous["stored_name"])


def archive_configured(app):
    return bool(app.config.get("IA_ACCESS_KEY") and app.config.get("IA_SECRET_KEY"))


def _row(db, table, key):
    return db.execute(f"SELECT * FROM {table} WHERE {TABLES[table]} = ?", (key,)).fetchone()


def start_archive(app, key, table="tournament_isos"):
    """Manda a ISO para o archive.org em segundo plano (na hora, em testes).
    Sem as chaves (ambiente local), a ISO fica só neste servidor."""
    row = _row(get_db(), table, key)
    if row is None or not row["stored_name"] or row["local_removed_at"] or not archive_configured(app):
        return
    _set(get_db(), row, table, archive_status="uploading", archive_error=None, archive_progress=0)
    get_db().commit()
    if app.config.get("TESTING"):
        _archive(app, key, row["stored_name"], table)
    else:
        threading.Thread(target=_archive, args=(app, key, row["stored_name"], table), daemon=True).start()


def _set(db, row, table="tournament_isos", **values):
    """Atualiza a linha só se ela ainda é a desta ISO (o organizador pode ter
    enviado outra no meio do caminho)."""
    values["archive_updated_at"] = values["updated_at"] = now()
    columns = ", ".join(f"{key} = ?" for key in values)
    db.execute(f"UPDATE {table} SET {columns} WHERE {TABLES[table]} = ? AND stored_name = ?",
               (*values.values(), row[TABLES[table]], row["stored_name"]))


def _wait_for_md5(app, remote_name, md5):
    """O archive.org responde ao PUT antes de o arquivo aparecer na lista do
    item (uma tarefa na fila processa); pode levar alguns minutos."""
    deadline = time.monotonic() + app.config.get("ARCHIVE_VERIFY_TIMEOUT", 45 * 60)
    interval = app.config.get("ARCHIVE_VERIFY_INTERVAL", 30)
    while True:
        try:
            remote = list_item_files(ARCHIVE_ITEM).get(remote_name)
        except ArchiveOrgError:
            remote = None
        if remote and remote["md5"] == md5:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _archive(app, key, stored_name, table="tournament_isos"):
    with app.app_context():
        db = get_db()
        row = db.execute(f"SELECT * FROM {table} WHERE {TABLES[table]} = ? AND stored_name = ?",
                         (key, stored_name)).fetchone()
        if row is None:
            return
        try:
            existing = list_item_files(ARCHIVE_ITEM)
            remote_name = next((name for name, info in existing.items() if info["md5"] == row["md5"]), None)
            if remote_name is None:
                remote_name = row["archive_name"]

                def progress(sent, total):
                    _set(db, row, table, archive_progress=min(100, 100 * sent // max(total, 1)))
                    db.commit()

                upload_file(ARCHIVE_ITEM, Path(app.config["DOWNLOADS_DIR"]) / stored_name, remote_name=remote_name,
                            access_key=app.config["IA_ACCESS_KEY"], secret_key=app.config["IA_SECRET_KEY"],
                            progress=progress)
                _set(db, row, table, archive_status="verifying", archive_name=remote_name,
                     archive_url=download_url(ARCHIVE_ITEM, remote_name))
                db.commit()
                if not _wait_for_md5(app, remote_name, row["md5"]):
                    _set(db, row, table, archive_status="error",
                         archive_error="Enviada, mas o archive.org ainda não lista o arquivo com o mesmo MD5. A cópia "
                                       "deste servidor foi mantida; tente enviar para o archive.org de novo mais tarde.")
                    db.commit()
                    return
        except (ArchiveOrgError, OSError) as error:
            _set(db, row, table, archive_status="error", archive_error=str(error))
            db.commit()
            return
        _set(db, row, table, archive_status="done", archive_name=remote_name, archive_url=download_url(ARCHIVE_ITEM, remote_name),
             archive_error=None, local_removed_at=now())
        db.commit()
        try:
            delete_stored_file(stored_name)
        except (ValueError, OSError):
            pass
