"""ISO do torneio: o organizador envia, o servidor guarda a cópia em
DOWNLOADS_DIR e uma thread manda para o archive.org. Quando o archive.org
lista o arquivo com o mesmo MD5, a cópia local é apagada e o download (só
para quem está logado) passa a redirecionar para lá.

Vai para o mesmo item das ISOs dos campeonatos (app/admin.py), numa pasta
por torneio. As ISOs do cadastro da administração (catalog_isos -
iso_catalog.py) usam o mesmo envio: as funções recebem a tabela. Se o item já tem um arquivo com o mesmo MD5 (a ISO de um
campeonato ou de outro torneio), nada é enviado: o torneio aponta para ele.
"""
import sqlite3
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
# meio) não impede mais um novo envio - e heal() envia de novo.
STALE = timedelta(hours=3)
# Tabela com a ISO -> coluna que identifica a linha. catalog_iso_revisions guarda
# o arquivo novo de uma alteração esperando aprovação (iso_catalog.request_edit).
TABLES = {"tournament_isos": "tournament_id", "catalog_isos": "id", "catalog_iso_revisions": "id"}
# As threads do envio esperam até isto por uma escrita no banco travada por outra
# requisição (o padrão do sqlite3 é 5 s). Sem isso, um "database is locked" no fim
# da conferência matava a thread e a ISO ficava em "conferindo" para sempre.
THREAD_BUSY_TIMEOUT_MS = 120_000


def get(db, tournament_id):
    return db.execute("SELECT * FROM tournament_isos WHERE tournament_id = ?", (tournament_id,)).fetchone()


def check_upload(upload):
    """Erro (TournamentError) se o arquivo não pode ser a ISO do torneio."""
    name = clean_original_filename(upload.filename)
    extension = name.rsplit(".", 1)[1].lower() if "." in name else ""
    if extension not in EXTENSIONS:
        raise TournamentError("A ISO precisa ser " + ", ".join(f".{e}" for e in sorted(EXTENSIONS)) + ".")


def check_size(request, max_bytes, extra=0):
    """O tamanho vem do Content-Length do formulário inteiro: dá para
    recusar antes de ler o corpo (o navegador já confere antes de enviar).
    extra: o que mais vai no mesmo formulário (a imagem da ISO dos amistosos)."""
    if request.content_length and request.content_length > max_bytes + extra + 1024 ** 2:
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


def content_id_of(stored):
    """CRC32:TAMANHO de uma ISO recém-enviada (storage.store_upload) - o
    content_id que o RetroArch TIERES calcula do arquivo que o jogador abre;
    None para .zip/.7z/.rar (o jogo é o que está dentro)."""
    from ..memcards import content_id_from, is_game_file
    if not is_game_file(stored["original_filename"]):
        return None
    return content_id_from(stored["crc32"], stored["file_size"])


def fill_content_ids(db, app):
    """ISOs de campeonato e de amistoso ainda sem content_id: pela cópia deste
    servidor ou, nas que já estão no archive.org, pelo crc32 e tamanho que ele
    lista (o md5 confere que é o mesmo arquivo). Devolve (identificadas,
    nomes das que não deu)."""
    from ..memcards import content_id_for_path, content_id_from, is_game_file
    listing = None
    found, missing = 0, []
    rows = [("tournament_isos", "tournament_id", r) for r in db.execute(
        "SELECT * FROM tournament_isos WHERE content_id IS NULL").fetchall()]
    rows += [("catalog_isos", "id", r) for r in db.execute(
        "SELECT * FROM catalog_isos WHERE content_id IS NULL").fetchall()]
    for table, key, row in rows:
        content_id = None
        if table == "catalog_isos" and row["file_id"]:
            linked = db.execute("SELECT content_id FROM files WHERE id = ?", (row["file_id"],)).fetchone()
            content_id = linked["content_id"] if linked else None
        elif row["original_filename"] and is_game_file(row["original_filename"]):
            local = Path(app.config["DOWNLOADS_DIR"]) / (row["stored_name"] or "")
            if row["stored_name"] and not row["local_removed_at"] and local.is_file():
                content_id = content_id_for_path(local)
            elif row["archive_name"]:
                if listing is None:
                    try:
                        listing = list_item_files(ARCHIVE_ITEM)
                    except ArchiveOrgError:
                        listing = {}
                info = listing.get(row["archive_name"])
                if info and info.get("crc32") and info.get("size") and info.get("md5") == row["md5"]:
                    content_id = content_id_from(int(info["crc32"], 16), info["size"])
        if content_id:
            db.execute(f"UPDATE {table} SET content_id = ? WHERE {key} = ?", (content_id, row[key]))
            db.commit()
            found += 1
        elif not (table == "catalog_isos" and not row["file_id"] and not row["original_filename"]):
            missing.append(row["original_filename"] or "ISO %s" % row[key])
    return found, missing


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
        "content_id, archive_name, archive_status, uploaded_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (tournament["id"], stored["stored_name"], stored["original_filename"], stored["file_size"], stored["md5"],
         content_id_of(stored), f"torneios/{tournament['slug']}/{stored['original_filename']}", player_id, now(),
         now()))
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
    _spawn(app, _archive, key, row["stored_name"], table)


def _spawn(app, target, *args):
    """Roda o envio/conferência numa thread (na hora, em testes)."""
    if app.config.get("TESTING"):
        target(app, *args)
    else:
        threading.Thread(target=target, args=(app, *args), daemon=True).start()


def verify_stale_after(app):
    """Conferência em 'verifying' há mais que isto: a thread que esperava o
    archive.org listar o arquivo já teria terminado (com done ou error), então
    ela morreu (servidor reiniciado no meio, erro do banco)."""
    return timedelta(seconds=app.config.get("ARCHIVE_VERIFY_TIMEOUT", 45 * 60)) + timedelta(minutes=5)


def heal(app, table, row):
    """Envio parado porque a thread morreu: 'verifying' há mais que o prazo da
    conferência volta a conferir (o arquivo normalmente já está no archive.org);
    'uploading' sem progresso há mais que STALE é enviado de novo (o _archive vê
    antes se o archive.org já tem o arquivo). Chamada pelas consultas da situação
    do envio (barras de progresso). Devolve a linha como ficou."""
    if row is None or not row["stored_name"] or row["local_removed_at"] or not archive_configured(app):
        return row
    current, stamp = row["archive_status"], row["archive_updated_at"]
    if current not in ("verifying", "uploading") or not stamp:
        return row
    age = datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
    if not ((current == "verifying" and age > verify_stale_after(app)) or (current == "uploading" and age > STALE)):
        return row
    db, key = get_db(), row[TABLES[table]]
    # Só uma requisição recomeça: as barras de progresso consultam ao mesmo tempo.
    try:
        claimed = db.execute(
            f"UPDATE {table} SET archive_updated_at = ? WHERE {TABLES[table]} = ? AND stored_name = ? "
            "AND archive_status = ? AND archive_updated_at = ?",
            (now(), key, row["stored_name"], current, stamp)).rowcount
        db.commit()
    except sqlite3.OperationalError:  # banco travado agora: a próxima consulta tenta de novo
        db.rollback()
        return row
    if claimed:
        app.logger.warning("archive.org: envio parado em '%s' (%s %s) desde %s - recomeçando", current, table, key, stamp)
        if current == "verifying":
            _spawn(app, _resume_verify, key, row["stored_name"], table)
        else:
            start_archive(app, key, table)
    return _row(db, table, key)


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


def _thread_db():
    """Conexão da thread do envio: espera a trava de outra escrita em vez de falhar."""
    db = get_db()
    db.execute(f"PRAGMA busy_timeout = {THREAD_BUSY_TIMEOUT_MS}")
    return db


def _fail(app, db, row, table, error):
    """Erro inesperado na thread (banco travado além do prazo...): registra no log
    e, se der, deixa a ISO com erro em vez de parada em 'uploading'/'verifying'."""
    app.logger.exception("archive.org: falha no envio da ISO (%s %s)", table, row[TABLES[table]])
    try:
        db.rollback()
        _set(db, row, table, archive_status="error", archive_error=f"Falha no envio para o archive.org: {error}")
        db.commit()
    except sqlite3.Error:
        pass


def _finish(db, row, table, remote_name, verified):
    """Fim da conferência: com o mesmo MD5 no archive.org, a cópia deste servidor
    é apagada e o download passa a ir para lá; sem, fica com erro (e a cópia)."""
    if not verified:
        _set(db, row, table, archive_status="error",
             archive_error="Enviada, mas o archive.org ainda não lista o arquivo com o mesmo MD5. A cópia "
                           "deste servidor foi mantida; tente enviar para o archive.org de novo mais tarde.")
        db.commit()
        return
    _set(db, row, table, archive_status="done", archive_name=remote_name,
         archive_url=download_url(ARCHIVE_ITEM, remote_name), archive_error=None, local_removed_at=now())
    db.commit()
    try:
        delete_stored_file(row["stored_name"])
    except (ValueError, OSError):
        pass


def _archive(app, key, stored_name, table="tournament_isos"):
    with app.app_context():
        db = _thread_db()
        row = db.execute(f"SELECT * FROM {table} WHERE {TABLES[table]} = ? AND stored_name = ?",
                         (key, stored_name)).fetchone()
        if row is None:
            return
        try:
            existing = list_item_files(ARCHIVE_ITEM)
            remote_name = next((name for name, info in existing.items() if info["md5"] == row["md5"]), None)
            verified = True
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
                verified = _wait_for_md5(app, remote_name, row["md5"])
            _finish(db, row, table, remote_name, verified)
        except (ArchiveOrgError, OSError) as error:
            _set(db, row, table, archive_status="error", archive_error=str(error))
            db.commit()
        except Exception as error:  # noqa: BLE001 - a thread nunca morre deixando a ISO parada
            _fail(app, db, row, table, error)


def _resume_verify(app, key, stored_name, table):
    """Recomeça a conferência de uma ISO parada em 'verifying' (heal)."""
    with app.app_context():
        db = _thread_db()
        row = db.execute(f"SELECT * FROM {table} WHERE {TABLES[table]} = ? AND stored_name = ?",
                         (key, stored_name)).fetchone()
        if row is None or row["archive_status"] != "verifying" or not row["archive_name"]:
            return
        try:
            _finish(db, row, table, row["archive_name"], _wait_for_md5(app, row["archive_name"], row["md5"]))
        except Exception as error:  # noqa: BLE001
            _fail(app, db, row, table, error)
