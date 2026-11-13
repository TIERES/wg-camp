import hashlib
import json
import re
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for
from werkzeug.security import check_password_hash

from .archive_org import ArchiveOrgError, delete_file, download_url, list_item_files, upload_file, upload_zip
from .db import get_db, now, set_setting
from .arena17_import import import_championship
from .public import SETTING_MANUAL_UPDATE_CARDS, manual_update_cards_visible
from .replays import _download_name
from .security import login_required, validate_csrf
from .storage import delete_stored_file, store_upload
from .timeutil import format_local

bp = Blueprint("admin", __name__, url_prefix="/admin")

# A replay stays downloadable/editable forever, but the "clean up old
# replays" button only ever offers to remove ones older than this - a
# retention window, not a one-shot purge.
REPLAY_RETENTION_DAYS = 60
STORED_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.krec$")
BACKUP_LABEL_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_a_[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def slugify(value):
    import re
    from unicodedata import normalize
    value = normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def valid_date(value):
    if not value:
        return None
    datetime.strptime(value, "%Y-%m-%d")
    return value


@bp.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        validate_csrf()
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (request.form.get("username", ""),)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["user_id"] = user["id"]
            session["csrf_token"] = __import__("secrets").token_urlsafe(32)
            db = get_db()
            db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now(), user["id"]))
            db.commit()
            return redirect(url_for("admin.dashboard"))
        flash("Usuário ou senha inválidos.", "error")
    return render_template("admin/login.html")


@bp.app_context_processor
def inject_admin():
    """Nome do administrador logado, para o menu do cabeçalho (base.html)."""
    if "user_id" not in session:
        return {}
    row = get_db().execute("SELECT username FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    return {"admin_name": row["username"] if row else "Administração"}


@bp.post("/logout")
@login_required
def logout():
    validate_csrf()
    session.clear()
    return redirect(url_for("admin.login"))


@bp.get("/")
@login_required
def dashboard():
    from .listing import cap, like
    filters = {"name": request.args.get("nome", "").strip(), "status": request.args.get("status", "")}
    sql, params = "SELECT * FROM championships WHERE 1 = 1", []
    if filters["name"]:
        sql += " AND (name LIKE ? ESCAPE '\\' OR league_name LIKE ? ESCAPE '\\')"
        params += [like(filters["name"])] * 2
    if filters["status"] in ("publicado", "oculto"):
        sql += " AND is_published = ?"
        params.append(1 if filters["status"] == "publicado" else 0)
    championships, total = cap(get_db().execute(
        sql + " ORDER BY is_published DESC, start_date DESC, created_at DESC", params).fetchall())
    return render_template("admin/dashboard.html", championships=championships, total=total, filters=filters,
                           filtered=any(filters.values()),
                           mostrar_atualizacao_manual=manual_update_cards_visible())


@bp.post("/pagina-inicial/atualizacao-manual")
@login_required
def manual_update_cards():
    """Mostra ou oculta, na página inicial, os cards "Atualização light" e
    "kailleraclient.dll" (public.SETTING_MANUAL_UPDATE_CARDS)."""
    validate_csrf()
    show = request.form.get("mostrar") == "1"
    set_setting(SETTING_MANUAL_UPDATE_CARDS, "1" if show else "0")
    flash("Cards de atualização manual " + ("visíveis" if show else "ocultos") + " na página inicial.", "success")
    return redirect(url_for("admin.dashboard"))


@bp.route("/championships/new", methods=("GET", "POST"))
@login_required
def championship_new():
    if request.method == "POST":
        return save_championship()
    return render_template("admin/championship_form.html", championship=None)


@bp.post("/championships/import-arena17")
@login_required
def championship_import_arena17():
    validate_csrf()
    try:
        data = import_championship(request.form.get("arena17_url", "").strip())
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(data)


@bp.route("/championships/<int:championship_id>/edit", methods=("GET", "POST"))
@login_required
def championship_edit(championship_id):
    championship = get_db().execute("SELECT * FROM championships WHERE id = ?", (championship_id,)).fetchone()
    if not championship:
        return ("Não encontrado", 404)
    if request.method == "POST":
        return save_championship(championship)
    return render_template("admin/championship_form.html", championship=championship)


@bp.post("/championships/<int:championship_id>/delete")
@login_required
def championship_delete(championship_id):
    validate_csrf()
    db = get_db()
    championship = db.execute("SELECT * FROM championships WHERE id=?", (championship_id,)).fetchone()
    if not championship:
        return ("Não encontrado", 404)
    count = db.execute("SELECT COUNT(*) FROM files WHERE championship_id=?", (championship_id,)).fetchone()[0]
    if request.form.get("confirm") != "delete":
        flash("Para excluir, marque a confirmação.", "error")
    elif count:
        flash("Remova os arquivos do campeonato antes de excluí-lo.", "error")
    else:
        db.execute("DELETE FROM championships WHERE id=?", (championship_id,))
        db.commit()
        flash("Campeonato removido.", "success")
        return redirect(url_for("admin.dashboard"))
    return redirect(url_for("admin.championship_edit", championship_id=championship_id))


def save_championship(existing=None):
    validate_csrf()
    name = request.form.get("name", "").strip()
    if not name:
        flash("O nome é obrigatório.", "error")
        return redirect(request.url)
    try:
        start, end = valid_date(request.form.get("start_date")), valid_date(request.form.get("end_date"))
    except ValueError:
        flash("Use datas válidas.", "error")
        return redirect(request.url)
    db = get_db()
    published = int("is_published" in request.form)
    timestamp = now()
    slug = slugify(request.form.get("slug", "") or name)
    try:
        with db:
            values = (slug, name, request.form.get("league_name", "").strip() or None, request.form.get("description", "").strip(), request.form.get("arena17_url", "").strip() or None, start, end, published, timestamp)
            if existing:
                db.execute("UPDATE championships SET slug=?, name=?, league_name=?, description=?, arena17_url=?, start_date=?, end_date=?, is_published=?, updated_at=? WHERE id=?", values + (existing["id"],))
            else:
                db.execute("INSERT INTO championships (slug,name,league_name,description,arena17_url,start_date,end_date,is_published,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", values + (timestamp,))
    except sqlite3.IntegrityError:
        flash("Slug já utilizado.", "error")
        return redirect(request.url)
    flash("Campeonato salvo.", "success")
    return redirect(url_for("admin.dashboard"))


@bp.get("/championships/<int:championship_id>/files")
@login_required
def files(championship_id):
    db = get_db()
    championship = db.execute("SELECT * FROM championships WHERE id=?", (championship_id,)).fetchone()
    if not championship:
        return ("Não encontrado", 404)
    entries = db.execute("SELECT * FROM files WHERE championship_id=? ORDER BY id DESC", (championship_id,)).fetchall()
    return render_template(
        "admin/files.html",
        championship=championship,
        files=entries,
        archive_busy={entry["id"] for entry in entries if _archive_upload_in_progress(entry)},
        archive_configured=bool(current_app.config.get("IA_ACCESS_KEY") and current_app.config.get("IA_SECRET_KEY")),
    )


@bp.post("/championships/<int:championship_id>/files")
@login_required
def file_upload(championship_id):
    validate_csrf()
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Selecione um arquivo.", "error")
        return redirect(url_for("admin.files", championship_id=championship_id))
    stored = None
    db = get_db()
    try:
        if request.form.get("file_type") not in {"iso", "rom", "patch", "update", "other"}:
            raise ValueError("Tipo de arquivo inválido.")
        if db.execute("SELECT 1 FROM files WHERE championship_id=?", (championship_id,)).fetchone():
            raise ValueError("Cada campeonato pode ter apenas um arquivo. Edite ou remova o arquivo atual antes de enviar outro.")
        stored = store_upload(upload)
        display_name = request.form.get("display_name", "").strip() or stored["original_filename"]
        # Identifies the ISO for the Memory Card online (players' account page).
        from .memcards import content_id_from, is_game_file
        content_id = content_id_from(stored["crc32"], stored["file_size"]) if is_game_file(stored["stored_name"]) else None
        db.execute("INSERT INTO files (championship_id,display_name,description,stored_name,original_filename,file_type,file_size,sha256,md5,content_id,is_published,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (championship_id, display_name, request.form.get("description", "").strip(), stored["stored_name"], stored["original_filename"], request.form.get("file_type", "other"), stored["file_size"], stored["sha256"], stored["md5"], content_id, int("is_published" in request.form), now(), now()))
        db.commit()
        file_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    except (ValueError, OSError, sqlite3.IntegrityError) as error:
        if stored:
            delete_stored_file(stored["stored_name"])
        flash(str(error), "error")
    else:
        # ISO (ou arquivo de jogo): sempre para o archive.org - só ele guarda arquivos grandes.
        message = "Arquivo enviado e registrado."
        if request.form.get("file_type") == "iso" or is_game_file(stored["stored_name"]):
            message += " " + _auto_archive_iso(db, file_id)
        flash(message, "success")
        return redirect(url_for("admin.dashboard"))
    return redirect(url_for("admin.files", championship_id=championship_id))


@bp.route("/files/<int:file_id>/edit", methods=("GET", "POST"))
@login_required
def file_edit(file_id):
    db = get_db()
    entry = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not entry:
        return ("Não encontrado", 404)
    if request.method == "POST":
        validate_csrf()
        file_type = request.form.get("file_type")
        if file_type not in {"iso", "rom", "patch", "update", "other"}:
            flash("Tipo de arquivo inválido.", "error")
        else:
            name = request.form.get("display_name", "").strip()
            if not name:
                flash("O nome exibido é obrigatório.", "error")
            else:
                db.execute("UPDATE files SET display_name=?, description=?, file_type=?, is_published=?, updated_at=? WHERE id=?", (name, request.form.get("description", "").strip(), file_type, int("is_published" in request.form), now(), file_id))
                db.commit()
                flash("Informações do arquivo atualizadas.", "success")
                return redirect(url_for("admin.files", championship_id=entry["championship_id"]))
    return render_template("admin/file_form.html", file=entry)


@bp.post("/files/<int:file_id>/delete")
@login_required
def file_delete(file_id):
    validate_csrf()
    entry = get_db().execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not entry:
        return ("Não encontrado", 404)
    if request.form.get("confirm") != "delete":
        flash("Para excluir, marque a confirmação.", "error")
        return redirect(url_for("admin.files", championship_id=entry["championship_id"]))
    delete_stored_file(entry["stored_name"])
    db = get_db()
    db.execute("DELETE FROM files WHERE id=?", (file_id,))
    db.commit()
    flash("Arquivo removido permanentemente.", "success")
    return redirect(url_for("admin.files", championship_id=entry["championship_id"]))


# Championship ISOs go into the existing "one-two-iso" item (created by hand
# on archive.org, alongside the RetroArch zips), under their original file
# name. Each file's MD5 is kept in files.md5 (archive.org lists an md5 per
# file, not a sha256) so the item's copy can be compared with ours:
#   - same name, same md5      -> already there, just link it;
#   - same name, different md5 -> the ISO was updated: ask, then delete the
#                                 old copy on archive.org and upload ours;
#   - same md5 under another name (uploaded by hand) -> link that one;
#   - otherwise                -> upload under the original name.
# The upload runs on a background thread - a ~470 MB PUT would hold one of
# gunicorn's two threads for minutes - and its progress lives in the
# files.archive_* columns. A restart mid-upload leaves status 'uploading'
# behind, so that status only blocks a new attempt for ISO_ARCHIVE_STALE.
ISO_ARCHIVE_ITEM_IDENTIFIER = "one-two-iso"
ISO_ARCHIVE_STALE = timedelta(hours=3)


def _file_md5(path):
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_md5(db, entry):
    """files.md5, computed (and saved) on first use for files uploaded
    before the column existed."""
    if entry["md5"]:
        return entry["md5"]
    md5 = _file_md5(Path(current_app.config["DOWNLOADS_DIR"]) / entry["stored_name"])
    db.execute("UPDATE files SET md5=? WHERE id=?", (md5, entry["id"]))
    db.commit()
    return md5


def _archive_plan(entry, md5, existing):
    """Returns (action, remote_name) with action one of "link", "replace",
    "upload" - see the comment above."""
    name = entry["original_filename"]
    if name in existing:
        return ("link" if existing[name]["md5"] == md5 else "replace"), name
    same = next((other for other, info in existing.items() if info["md5"] == md5), None)
    if same:
        return "link", same
    return "upload", name


def _remove_local_copy(db, entry, reason):
    """Deletes this server's copy of an ISO already confirmed on
    archive.org - from then on public.download redirects to archive_url."""
    delete_stored_file(entry["stored_name"])
    db.execute("UPDATE files SET local_removed_at=?, archive_error=? WHERE id=?", (now(), reason, entry["id"]))


def _wait_for_archive_md5(app, remote_name, md5):
    """Polls the item's file list until `remote_name` shows up with our md5.
    archive.org answers the PUT before the file reaches its listing (it's
    processed by a queued task), so this can take a few minutes."""
    deadline = time.monotonic() + app.config.get("ARCHIVE_VERIFY_TIMEOUT", 45 * 60)
    interval = app.config.get("ARCHIVE_VERIFY_INTERVAL", 30)
    while True:
        try:
            remote = list_item_files(ISO_ARCHIVE_ITEM_IDENTIFIER).get(remote_name)
        except ArchiveOrgError:
            remote = None
        if remote and remote["md5"] == md5:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _archive_iso(app, file_id, remote_name, replace, remove_local):
    with app.app_context():
        db = get_db()
        entry = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        keys = {"access_key": app.config["IA_ACCESS_KEY"], "secret_key": app.config["IA_SECRET_KEY"]}
        try:
            if replace:
                delete_file(ISO_ARCHIVE_ITEM_IDENTIFIER, remote_name, **keys)

            def progress(sent, total):
                # Barra de progresso (static/js/archive-progress.js); renova archive_updated_at.
                db.execute("UPDATE files SET archive_progress=?, archive_updated_at=? WHERE id=?",
                           (min(100, 100 * sent // max(total, 1)), now(), file_id))
                db.commit()

            url = upload_file(
                ISO_ARCHIVE_ITEM_IDENTIFIER,
                Path(app.config["DOWNLOADS_DIR"]) / entry["stored_name"],
                remote_name=remote_name,
                progress=progress,
                **keys,
            )
        except (ArchiveOrgError, OSError) as error:
            message = str(error)
            if replace:
                message += " (a ISO antiga pode já ter sido excluída do archive.org - envie novamente)"
            db.execute(
                "UPDATE files SET archive_status='error', archive_error=?, archive_updated_at=? WHERE id=?",
                (message, now(), file_id),
            )
            db.commit()
            return

        if not remove_local:
            db.execute(
                "UPDATE files SET archive_status='done', archive_url=?, archive_error=NULL, archive_updated_at=? WHERE id=?",
                (url, now(), file_id),
            )
            db.commit()
            return

        db.execute(
            "UPDATE files SET archive_status='verifying', archive_url=?, archive_error=NULL, archive_updated_at=? WHERE id=?",
            (url, now(), file_id),
        )
        db.commit()
        if _wait_for_archive_md5(app, remote_name, entry["md5"]):
            try:
                _remove_local_copy(db, entry, None)
            except (ValueError, OSError) as error:
                db.execute("UPDATE files SET archive_error=? WHERE id=?", (f"Confirmada no archive.org, mas a cópia local não pôde ser removida: {error}", file_id))
        else:
            db.execute(
                "UPDATE files SET archive_error=? WHERE id=?",
                ("Enviada, mas o archive.org ainda não lista o arquivo com o mesmo MD5 - a cópia deste servidor foi mantida. Clique em enviar de novo mais tarde para conferir e remover.", file_id),
            )
        db.execute("UPDATE files SET archive_status='done', archive_updated_at=? WHERE id=?", (now(), file_id))
        db.commit()


def _archive_upload_in_progress(entry):
    if entry["archive_status"] not in ("uploading", "verifying") or not entry["archive_updated_at"]:
        return False
    return datetime.now(timezone.utc) - datetime.fromisoformat(entry["archive_updated_at"]) < ISO_ARCHIVE_STALE


@bp.get("/files/<int:file_id>/archive-status")
@login_required
def file_archive_status(file_id):
    """Situação do envio para o archive.org (barra de progresso - static/js/archive-progress.js)."""
    entry = get_db().execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not entry:
        return ("Não encontrado", 404)
    if entry["local_removed_at"] or (entry["archive_status"] == "done" and entry["archive_url"]):
        state = {"state": "done", "progress": 100, "url": entry["archive_url"]}
    elif entry["archive_status"] == "uploading" and _archive_upload_in_progress(entry):
        state = {"state": "sending", "progress": entry["archive_progress"] or 0}
    elif entry["archive_status"] == "verifying" and _archive_upload_in_progress(entry):
        state = {"state": "verifying", "progress": 100}
    elif entry["archive_status"] == "error":
        state = {"state": "error", "error": entry["archive_error"] or "Falha ao enviar para o archive.org."}
    else:
        state = {"state": "local", "configured": bool(current_app.config.get("IA_ACCESS_KEY")
                                                      and current_app.config.get("IA_SECRET_KEY"))}
    return jsonify(state)


def _start_archive_thread(db, file_id, remote_name, replace, remove_local):
    db.execute(
        "UPDATE files SET archive_status='uploading', archive_error=NULL, archive_progress=0, archive_updated_at=? "
        "WHERE id=?",
        (now(), file_id),
    )
    db.commit()
    app = current_app._get_current_object()
    args = (app, file_id, remote_name, replace, remove_local)
    if app.config.get("TESTING"):
        _archive_iso(*args)
    else:
        threading.Thread(target=_archive_iso, args=args, daemon=True).start()


def _auto_archive_iso(db, file_id):
    """ISO enviada na administração: vai sempre para o archive.org (arquivos
    grandes ficam só lá), como as ISOs do portal e dos amistosos
    (tournament/iso.py: start_archive). Mesmo plano do botão "Enviar para o
    archive.org", sempre removendo a cópia deste servidor depois de confirmada
    (mesmo MD5). Já lá com o mesmo conteúdo: só liga. Mesmo nome com conteúdo
    diferente no archive.org: não substitui sozinho - fica para o botão, que
    pede confirmação. Retorna a mensagem para o administrador."""
    entry = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not (current_app.config.get("IA_ACCESS_KEY") and current_app.config.get("IA_SECRET_KEY")):
        return "As chaves do archive.org não estão configuradas: a ISO ficou só neste servidor."
    try:
        md5 = _local_md5(db, entry)
        existing = list_item_files(ISO_ARCHIVE_ITEM_IDENTIFIER)
    except (ArchiveOrgError, OSError) as error:
        return (f"Não foi possível falar com o archive.org ({error}): a ISO ficou neste servidor. "
                "Use \"Enviar para o archive.org\" em Arquivos para tentar de novo.")
    action, remote_name = _archive_plan(entry, md5, existing)
    if action == "link":
        url = download_url(ISO_ARCHIVE_ITEM_IDENTIFIER, remote_name)
        db.execute("UPDATE files SET archive_status='done', archive_url=?, archive_error=NULL, archive_updated_at=? "
                   "WHERE id=?", (url, now(), file_id))
        _remove_local_copy(db, entry, None)
        db.commit()
        return f"Esta ISO já estava no archive.org, com o mesmo conteúdo ({url}); a cópia deste servidor foi removida."
    if action == "replace":
        return (f"O archive.org já tem uma ISO \"{remote_name}\" com conteúdo diferente: ela não foi substituída "
                "sozinha. Abra Arquivos e use \"Enviar para o archive.org\" para confirmar a troca.")
    _start_archive_thread(db, file_id, remote_name, False, True)
    return ("Envio para o archive.org iniciado; depois de confirmada lá (mesmo MD5), a cópia deste servidor é "
            "removida e o download passa a vir do archive.org.")


@bp.post("/files/<int:file_id>/archive")
@login_required
def file_archive(file_id):
    validate_csrf()
    db = get_db()
    entry = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not entry:
        return ("Não encontrado", 404)
    back = redirect(url_for("admin.files", championship_id=entry["championship_id"]))
    if entry["local_removed_at"]:
        flash("Esta ISO já foi migrada para o archive.org e não existe mais neste servidor.", "error")
        return back
    if not (current_app.config.get("IA_ACCESS_KEY") and current_app.config.get("IA_SECRET_KEY")):
        flash("Chaves do archive.org não configuradas.", "error")
        return back
    if _archive_upload_in_progress(entry):
        flash("O envio para o archive.org já está em andamento.", "error")
        return back
    remove_local = request.form.get("remove_local") == "yes"

    try:
        md5 = _local_md5(db, entry)
        existing = list_item_files(ISO_ARCHIVE_ITEM_IDENTIFIER)
    except (ArchiveOrgError, OSError) as error:
        flash(f"Não foi possível comparar com o archive.org: {error}", "error")
        return back
    action, remote_name = _archive_plan(entry, md5, existing)

    if action == "link":
        url = download_url(ISO_ARCHIVE_ITEM_IDENTIFIER, remote_name)
        db.execute(
            "UPDATE files SET archive_status='done', archive_url=?, archive_error=NULL, archive_updated_at=? WHERE id=?",
            (url, now(), file_id),
        )
        if remove_local:
            # The listing already shows this exact md5 - nothing to wait for.
            _remove_local_copy(db, entry, None)
        db.commit()
        if remove_local:
            flash(f"Esta ISO já estava no archive.org, com o mesmo conteúdo: {url}. A cópia deste servidor foi removida.", "success")
        else:
            flash(f"Esta ISO já está no archive.org, com o mesmo conteúdo: {url}", "success")
        return back

    if action == "replace" and request.form.get("replace") != "yes":
        mtime = existing[remote_name]["mtime"]
        return render_template(
            "admin/file_archive_confirm.html",
            remote_date=datetime.fromtimestamp(int(mtime), timezone.utc).isoformat() if mtime else None,
            file=entry,
            remote_name=remote_name,
            remote=existing[remote_name],
            local_md5=md5,
            item=ISO_ARCHIVE_ITEM_IDENTIFIER,
            remove_local=remove_local,
        )

    _start_archive_thread(db, file_id, remote_name, action == "replace", remove_local)
    if action == "replace":
        flash("Substituição iniciada: a ISO antiga será excluída do archive.org e a nova enviada. Atualize esta página em alguns minutos para ver o link.", "success")
    else:
        flash("Envio para o archive.org iniciado. Atualize esta página em alguns minutos para ver o link.", "success")
    return back


###############################################################################
# Replays - edit metadata, delete individually, and a retention-window
# "backup then remove" cleanup for anything older than REPLAY_RETENTION_DAYS.
###############################################################################

def _live_dir():
    path = Path(current_app.config["LIVE_DIR"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _backups_dir():
    path = Path(current_app.config["REPLAY_BACKUPS_DIR"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _delete_replay_file(stored_name):
    if not STORED_NAME_RE.fullmatch(stored_name):
        raise ValueError("Nome interno de replay inválido.")
    (_live_dir() / stored_name).unlink(missing_ok=True)


def _retention_cutoff():
    return (datetime.now(timezone.utc) - timedelta(days=REPLAY_RETENTION_DAYS)).replace(microsecond=0).isoformat()


def _stale_replays(db):
    return db.execute(
        "SELECT * FROM live_sessions WHERE status='finished' AND started_at < ? ORDER BY started_at",
        (_retention_cutoff(),),
    ).fetchall()


def _date_range_label(rows):
    oldest = format_local(rows[0]["started_at"], "%Y-%m-%d")
    newest = format_local(rows[-1]["started_at"], "%Y-%m-%d")
    return f"{oldest}_a_{newest}"


def _backup_paths(label):
    if not BACKUP_LABEL_RE.fullmatch(label):
        abort(404)
    backups = _backups_dir()
    return backups / f"{label}.zip", backups / f"{label}.json"


def _load_backup_manifest(label):
    zip_path, manifest_path = _backup_paths(label)
    if not zip_path.exists() or not manifest_path.exists():
        abort(404, "Backup não encontrado - pode já ter sido excluído ou descartado.")
    return zip_path, json.loads(manifest_path.read_text(encoding="utf-8"))


@bp.get("/replays")
@login_required
def replays_list():
    from .listing import cap, like, local_day
    db = get_db()
    filters = {"player": request.args.get("jogador", "").strip(), "game": request.args.get("jogo", "").strip(),
               "date_from": request.args.get("de", ""), "date_to": request.args.get("ate", "")}
    sql, params = "SELECT * FROM live_sessions WHERE status='finished'", []
    if filters["player"]:
        sql += " AND player_names LIKE ? ESCAPE '\\'"
        params.append(like(filters["player"]))
    if filters["game"]:
        sql += " AND game_name LIKE ? ESCAPE '\\'"
        params.append(like(filters["game"]))
    start, end = local_day(filters["date_from"]), local_day(filters["date_to"], end=True)
    if start:
        sql += " AND started_at >= ?"
        params.append(start)
    if end:
        sql += " AND started_at < ?"
        params.append(end)
    replays, total = cap(db.execute(sql + " ORDER BY started_at DESC", params).fetchall())
    stale = _stale_replays(db)
    stale_size = sum(r["bytes_received"] or 0 for r in stale)
    pending_backups = sorted(p.stem for p in _backups_dir().glob("*.json"))
    return render_template(
        "admin/replays.html",
        replays=replays,
        total=total,
        filters=filters,
        filtered=any(filters.values()),
        stale_count=len(stale),
        stale_size=stale_size,
        retention_days=REPLAY_RETENTION_DAYS,
        pending_backups=pending_backups,
    )


@bp.route("/replays/<session_id>/edit", methods=("GET", "POST"))
@login_required
def replay_edit(session_id):
    db = get_db()
    replay = db.execute("SELECT * FROM live_sessions WHERE session_id=?", (session_id,)).fetchone()
    if not replay:
        return ("Não encontrado", 404)
    if request.method == "POST":
        validate_csrf()
        player_names = request.form.get("player_names", "").strip()[:255]
        db.execute("UPDATE live_sessions SET player_names=?, updated_at=? WHERE session_id=?", (player_names, now(), session_id))
        db.commit()
        flash("Replay atualizado.", "success")
        return redirect(url_for("admin.replays_list"))
    return render_template("admin/replay_edit.html", replay=replay)


@bp.get("/replays/<session_id>/download")
@login_required
def replay_download(session_id):
    """Same as replays.download() but without the public 5-minute minimum
    - admin needs to be able to pull any replay, including short ones,
    e.g. to review before editing/deleting it."""
    entry = get_db().execute("SELECT * FROM live_sessions WHERE session_id=? AND status='finished'", (session_id,)).fetchone()
    if not entry:
        abort(404)
    download_name = _download_name(entry)
    if current_app.config["SERVE_DOWNLOADS_LOCALLY"]:
        return send_from_directory(current_app.config["LIVE_DIR"], entry["stored_name"], as_attachment=True, download_name=download_name)
    response = Response()
    response.headers["X-Accel-Redirect"] = f"/_protected_replays/{entry['stored_name']}"
    response.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    return response


@bp.post("/replays/bulk-delete")
@login_required
def replays_bulk_delete():
    """Deletes one or more replays selected via checkboxes in the replays
    list. No server-side confirmation checkbox by design - the "tem
    certeza?" prompt is a client-side JS dialog (see replays.html), since
    this is an authenticated admin-only action, not a public-facing one."""
    validate_csrf()
    session_ids = request.form.getlist("session_ids")
    if not session_ids:
        flash("Nenhum replay selecionado.", "error")
        return redirect(url_for("admin.replays_list"))

    db = get_db()
    placeholders = ",".join("?" * len(session_ids))
    rows = db.execute(f"SELECT session_id, stored_name FROM live_sessions WHERE session_id IN ({placeholders})", session_ids).fetchall()
    for row in rows:
        _delete_replay_file(row["stored_name"])
    db.executemany("DELETE FROM live_sessions WHERE session_id=?", [(row["session_id"],) for row in rows])
    db.commit()
    flash(f"{len(rows)} replay(s) excluído(s).", "success")
    return redirect(url_for("admin.replays_list"))


@bp.post("/replays/cleanup/generate")
@login_required
def replays_cleanup_generate():
    """Zips every replay older than the retention window to a durable file
    under REPLAY_BACKUPS_DIR - nothing is deleted here. The admin reviews
    the result (downloads it and/or sends it to archive.org, both
    repeatable) and only the separate confirm-delete step below actually
    removes anything, so a failed/interrupted backup can never lead to
    data loss."""
    validate_csrf()
    db = get_db()
    stale = _stale_replays(db)
    if not stale:
        flash(f"Nenhum replay com mais de {REPLAY_RETENTION_DAYS} dias para limpar.", "error")
        return redirect(url_for("admin.replays_list"))

    label = _date_range_label(stale)
    zip_path, manifest_path = _backup_paths(label)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in stale:
            src = _live_dir() / row["stored_name"]
            if src.exists():
                zf.write(src, arcname=row["stored_name"])
    manifest = {
        "label": label,
        "created_at": now(),
        "session_ids": [row["session_id"] for row in stale],
        "stored_names": [row["stored_name"] for row in stale],
        "deleted_from_server": False,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    flash(f"Backup {label}.zip gerado com {len(stale)} replay(s). Baixe e/ou envie para o archive.org antes de confirmar a exclusão.", "success")
    return redirect(url_for("admin.replays_cleanup_status", label=label))


@bp.get("/replays/cleanup/<label>")
@login_required
def replays_cleanup_status(label):
    zip_path, manifest = _load_backup_manifest(label)
    return render_template(
        "admin/replays_cleanup.html",
        label=label,
        manifest=manifest,
        zip_size=zip_path.stat().st_size,
        archive_configured=bool(current_app.config.get("IA_ACCESS_KEY") and current_app.config.get("IA_SECRET_KEY")),
    )


@bp.get("/replays/cleanup/<label>/download")
@login_required
def replays_cleanup_download(label):
    zip_path, _manifest = _load_backup_manifest(label)
    return send_file(zip_path, as_attachment=True, download_name=f"{label}.zip", mimetype="application/zip")



# One fixed item holds every backup as a separate dated .zip file, instead
# of a new item per cleanup run - browsable as a single collection at
# archive.org/details/<this>. Title/description are item-level metadata
# (archive.org has no per-file description via this API), so they stay
# generic; each zip's filename (the date range) is what tells batches
# apart within the item's file listing.
ARCHIVE_ORG_ITEM_IDENTIFIER = "wecamp-replays"


@bp.post("/replays/cleanup/<label>/archive")
@login_required
def replays_cleanup_archive(label):
    validate_csrf()
    zip_path, manifest = _load_backup_manifest(label)
    try:
        url = upload_zip(
            identifier=ARCHIVE_ORG_ITEM_IDENTIFIER,
            file_path=zip_path,
            access_key=current_app.config["IA_ACCESS_KEY"],
            secret_key=current_app.config["IA_SECRET_KEY"],
            collection=current_app.config["IA_COLLECTION"],
            title="WE Camp - Backups de Replays",
            description="Backups periódicos de replays gravados na comunidade WE Camp (Winning Eleven 2002), um .zip por limpeza - o nome de cada arquivo é o intervalo de datas que ele cobre.",
        )
    except ArchiveOrgError as error:
        flash(f"Falha ao enviar para o archive.org: {error}", "error")
    else:
        flash(f"Enviado para o archive.org: {url}", "success")
    return redirect(url_for("admin.replays_cleanup_status", label=label))


@bp.post("/replays/cleanup/<label>/confirm-delete")
@login_required
def replays_cleanup_confirm_delete(label):
    validate_csrf()
    zip_path, manifest = _load_backup_manifest(label)
    if manifest.get("deleted_from_server"):
        flash("Esses replays já foram removidos do servidor.", "error")
        return redirect(url_for("admin.replays_cleanup_status", label=label))
    if request.form.get("confirm") != "delete":
        flash("Para excluir, marque a confirmação.", "error")
        return redirect(url_for("admin.replays_cleanup_status", label=label))

    db = get_db()
    for stored_name in manifest["stored_names"]:
        _delete_replay_file(stored_name)
    db.executemany("DELETE FROM live_sessions WHERE session_id=?", [(sid,) for sid in manifest["session_ids"]])
    db.commit()

    # Keep the manifest (flagged) and the zip itself - it's now the only
    # remaining copy of what was just removed from the server. Only
    # "discard" below deletes it, once it's safely downloaded/archived.
    manifest["deleted_from_server"] = True
    _backup_paths(label)[1].write_text(json.dumps(manifest), encoding="utf-8")
    flash(f"{len(manifest['session_ids'])} replay(s) removido(s) do servidor. O backup {label}.zip continua disponível para download até você descartá-lo.", "success")
    return redirect(url_for("admin.replays_cleanup_status", label=label))


@bp.post("/replays/cleanup/<label>/discard")
@login_required
def replays_cleanup_discard(label):
    validate_csrf()
    zip_path, manifest = _load_backup_manifest(label)
    if request.form.get("confirm") != "delete":
        flash("Para descartar, marque a confirmação.", "error")
        return redirect(url_for("admin.replays_cleanup_status", label=label))
    zip_path.unlink(missing_ok=True)
    _backup_paths(label)[1].unlink(missing_ok=True)
    if manifest.get("deleted_from_server"):
        flash(f"Backup {label}.zip descartado. Esses replays já não existem mais em lugar nenhum neste servidor.", "success")
    else:
        flash(f"Backup {label}.zip descartado. Os replays continuam no servidor.", "success")
    return redirect(url_for("admin.replays_list"))


# ---------------------------------------------------------------------------
# Jogadores e jogos do Memory Card online (app/players.py, app/memcards.py)
# ---------------------------------------------------------------------------

@bp.get("/jogadores")
@login_required
def players_list():
    from .accounts import UNVERIFIED_TTL
    from .listing import cap, like
    filters = {"name": request.args.get("nome", "").strip(), "status": request.args.get("confirmado", "")}
    sql, params = ("SELECT p.*, (SELECT COUNT(*) FROM memcards m WHERE m.player_id = p.id) AS cards, "
                   "(SELECT MAX(t.created_at) FROM player_tokens t WHERE t.player_id = p.id AND t.kind = 'verify') "
                   "AS last_verify FROM players p WHERE 1 = 1"), []
    if filters["name"]:
        sql += " AND (p.username LIKE ? ESCAPE '\\' OR p.email LIKE ? ESCAPE '\\')"
        params += [like(filters["name"])] * 2
    if filters["status"] == "sim":
        sql += " AND p.email_verified_at IS NOT NULL"
    elif filters["status"] == "nao":
        sql += " AND p.email_verified_at IS NULL"
    players, total = cap(get_db().execute(sql + " ORDER BY p.username COLLATE NOCASE", params).fetchall())
    # Cadastro não confirmado: até quando o nick e o e-mail ficam reservados (48 h depois do
    # cadastro ou do último link de confirmação - accounts.already_registered); None = já livres.
    reserved = {}
    for player in players:
        if player["email_verified_at"]:
            continue
        last = max(filter(None, (player["created_at"], player["last_verify"])))
        until = datetime.fromisoformat(last) + UNVERIFIED_TTL
        reserved[player["id"]] = until.isoformat() if until > datetime.now(timezone.utc) else None
    return render_template("admin/players.html", players=players, total=total, filters=filters,
                           filtered=any(filters.values()), reserved=reserved,
                           reserve_hours=int(UNVERIFIED_TTL.total_seconds() // 3600))


def _player_back():
    """Volta para a lista de jogadores como estava (filtros e quantos mostrar)."""
    target = request.form.get("voltar", "")
    return redirect(target if target.startswith("/admin/jogadores") else url_for("admin.players_list"))


def _player_or_404(db, player_id):
    player = db.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    if player is None:
        abort(404)
    return player


@bp.post("/jogadores/<int:player_id>/editar")
@login_required
def player_edit(player_id):
    """Corrige o e-mail de um jogador (digitado errado no cadastro) e/ou define uma
    nova senha para ele.
      - e-mail novo, ainda não confirmado: o link de confirmação vai para o e-mail novo;
      - já confirmado: ele recebe um aviso no e-mail (o novo, se mudou) dizendo o que
        a administração trocou - nunca a senha, que a administração passa para ele;
      - senha nova: as conexões do RetroArch TIERES (tokens da API) caem, como na troca
        de senha pelo próprio jogador, e os links de "Esqueci minha senha" deixam de valer."""
    from werkzeug.security import generate_password_hash
    from .accounts import (already_registered, email_error, normalize_email, password_error,
                           purge_stale_unverified, revoke_api_tokens)
    from .mailer import MailError, send_mail
    from .players import _send_verification
    validate_csrf()
    db = get_db()
    player = _player_or_404(db, player_id)
    email = normalize_email(request.form.get("email")) or player["email"]
    password = request.form.get("password", "")
    new_email = email != (player["email"] or "").lower()  # contas antigas podem ter maiúsculas no e-mail
    error = None
    if not new_email and not password:
        error = "Nada foi alterado: mude o e-mail ou digite uma nova senha."
    if not error and new_email:
        error = email_error(email)
        if not error and already_registered(db, "email", email, except_id=player_id):
            error = "Este e-mail já é de outra conta."
    if not error and password:
        error = password_error(password)
    if error:
        flash(f"{player['username']}: {error}", "error")
        return _player_back()
    stamp = now()
    if new_email:
        purge_stale_unverified(db, "", email)  # um cadastro pendente vencido com esse e-mail sai, como no cadastro
        db.execute("UPDATE players SET email = ?, updated_at = ? WHERE id = ?", (email, stamp, player_id))
        # Os links de confirmação e de nova senha que foram para o e-mail antigo deixam de valer.
        db.execute("UPDATE player_tokens SET used_at = ? WHERE player_id = ? AND kind IN ('verify', 'reset') "
                   "AND used_at IS NULL", (stamp, player_id))
    if password:
        db.execute("UPDATE players SET password_hash = ?, updated_at = ? WHERE id = ?",
                   (generate_password_hash(password), stamp, player_id))
        db.execute("UPDATE player_tokens SET used_at = ? WHERE player_id = ? AND kind = 'reset' AND used_at IS NULL",
                   (stamp, player_id))
        revoke_api_tokens(db, player_id)
    db.commit()
    player = _player_or_404(db, player_id)
    done = ([f"e-mail corrigido para {email}"] if new_email else []) + (["nova senha definida"] if password else [])
    summary = f"{player['username']}: {' e '.join(done)}."
    if password and player["email_verified_at"]:
        summary += " Os PCs conectados precisam entrar de novo no Kaillera; passe a senha nova para o jogador."
    if not player["email_verified_at"]:
        if new_email:
            try:
                _send_verification(db, player)
            except MailError:
                flash(summary + " Não foi possível enviar a confirmação agora: use \"Reenviar confirmação\" em "
                                "alguns minutos.", "error")
                return _player_back()
            summary += " O link de confirmação foi enviado para o e-mail novo."
        flash(summary, "success")
        return _player_back()
    changed = []
    if new_email:
        changed.append("o e-mail da sua conta passou a ser este endereço. É por ele que você recupera o acesso "
                       "(\"Esqueci minha senha\") e recebe os avisos dos campeonatos e amistosos")
    if password:
        changed.append("a sua senha foi trocada. Use a senha nova que a administração passou para você, no site e "
                       "no Kaillera (os PCs que estavam conectados precisam entrar de novo)")
    try:
        send_mail(player["email"], "Sua conta no WE Camp foi atualizada", (
            f"Olá, {player['username']}!\n\nA administração do WE Camp atualizou a sua conta:\n\n"
            + "\n".join(f"  - {item[0].upper()}{item[1:]}." for item in changed)
            + "\n\nSe você não pediu essa mudança, fale com a administração do WE Camp.\n"))
    except MailError:
        pass  # send_mail já registrou no log; a mudança já vale
    flash(summary, "success")
    return _player_back()


@bp.post("/jogadores/<int:player_id>/reenviar-confirmacao")
@login_required
def player_resend(player_id):
    """Reenvia o link de confirmação de um cadastro ainda não confirmado. O nick e
    o e-mail ficam reservados por mais 48 h a partir daqui (accounts._STALE)."""
    from .accounts import recently_sent
    from .mailer import MailError
    from .players import _send_verification
    validate_csrf()
    db = get_db()
    player = _player_or_404(db, player_id)
    if player["email_verified_at"]:
        flash(f"{player['username']} já confirmou o cadastro.", "error")
    elif recently_sent(db, player_id, "verify"):
        flash(f"Uma confirmação foi enviada para {player['username']} há menos de 2 minutos. Espere um pouco antes "
              "de reenviar.", "error")
    else:
        try:
            _send_verification(db, player)
        except MailError:
            flash("Não foi possível enviar o e-mail agora. Tente novamente em alguns minutos.", "error")
        else:
            flash(f"Confirmação reenviada para {player['email']}. O link vale por 48 horas, e o nick "
                  f"{player['username']} fica reservado até lá.", "success")
    return _player_back()


@bp.post("/jogadores/<int:player_id>/excluir")
@login_required
def player_delete(player_id):
    """Exclui um cadastro que nunca foi confirmado, para liberar o nick e o e-mail
    na hora (sem esperar as 48 h). Conta confirmada não é excluída por aqui. Quem
    não confirmou nunca entrou no site: não há campeonato, carteira nem Memory
    Card dele (o que houver de cadastro sai junto - ON DELETE CASCADE)."""
    from . import images
    validate_csrf()
    db = get_db()
    player = _player_or_404(db, player_id)
    if player["email_verified_at"]:
        flash(f"{player['username']} já confirmou o cadastro: só dá para excluir cadastro não confirmado.", "error")
        return _player_back()
    try:
        db.execute("DELETE FROM players WHERE id = ? AND email_verified_at IS NULL", (player_id,))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        flash(f"O cadastro de {player['username']} tem registros ligados a ele e não pode ser excluído.", "error")
        return _player_back()
    images.delete("avatar", player["avatar"])
    flash(f"Cadastro de {player['username']} excluído: o nick e o e-mail {player['email']} estão livres para um "
          "novo cadastro.", "success")
    return _player_back()


@bp.route("/jogos", methods=("GET", "POST"))
@login_required
def games_list():
    from .memcards import normalize_content_id

    db = get_db()
    if request.method == "POST":
        validate_csrf()
        game_id = request.form.get("game_id", type=int)
        name = request.form.get("name", "").strip()[:128]
        if not name:
            flash("Informe o nome do jogo.", "error")
        elif game_id:
            db.execute("UPDATE games SET name = ?, updated_at = ? WHERE id = ?", (name, now(), game_id))
            db.commit()
            flash("Jogo renomeado.", "success")
        else:
            content_id = normalize_content_id(request.form.get("content_id"))
            if not content_id:
                flash("Identificador inválido. Use CRC32:TAMANHO em hexadecimal, como no log kaillera_sync.log "
                      "(ou o comando flask register-game).", "error")
            else:
                try:
                    db.execute("INSERT INTO games (content_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                               (content_id, name, now(), now()))
                    db.commit()
                    flash("Jogo cadastrado.", "success")
                except sqlite3.IntegrityError:
                    flash("Este identificador já está cadastrado.", "error")
        return redirect(url_for("admin.games_list"))
    from .listing import cap, like
    search = request.args.get("busca", "").strip()
    pattern = like(search)
    games = db.execute(
        "SELECT g.*, (SELECT COUNT(*) FROM memcards m WHERE m.game_id = g.id AND m.player_id IS NOT NULL) AS cards, "
        "(SELECT m.current_version FROM memcards m WHERE m.game_id = g.id AND m.player_id IS NULL AND m.iso_slot = 1) AS iso_version, "
        "(SELECT m.current_version FROM memcards m WHERE m.game_id = g.id AND m.player_id IS NULL AND m.iso_slot = 2) AS iso2_version "
        "FROM games g WHERE g.name LIKE ? ESCAPE '\\' OR g.content_id LIKE ? ESCAPE '\\' ORDER BY g.name COLLATE NOCASE",
        (pattern, pattern)
    ).fetchall()
    isos = db.execute(
        "SELECT f.display_name, f.original_filename, f.content_id, f.is_published, "
        "c.name AS championship_name, c.is_published AS championship_published "
        "FROM files f JOIN championships c ON c.id = f.championship_id "
        "WHERE f.file_type = 'iso' AND (c.name LIKE ? ESCAPE '\\' OR f.display_name LIKE ? ESCAPE '\\' "
        "OR f.original_filename LIKE ? ESCAPE '\\' OR IFNULL(f.content_id, '') LIKE ? ESCAPE '\\') "
        "ORDER BY c.start_date DESC, c.created_at DESC, f.id DESC", (pattern,) * 4
    ).fetchall()
    games, games_total = cap(games)
    isos, isos_total = cap(isos)
    return render_template("admin/games.html", games=games, isos=isos, games_total=games_total, isos_total=isos_total,
                           search=search, identifying=_identify_isos_lock.locked())


# "M. Card da ISO": os cartões do jogo, sem dono, que todos usam numa sala com
# essa opção - um no slot 1 e outro no slot 2 (ex.: a Master League, uma
# partida por vez, salva pelo host no fim de cada uma). Aqui o administrador
# vê o histórico, baixa, restaura e envia um cartão de cada slot - para um
# jogo já cadastrado ou para uma ISO de campeonato identificada que ainda não
# teve partida (o jogo é criado no envio).

def _iso_card_game(content_id, create=False):
    from .memcards import get_or_create_game, normalize_content_id

    content_id = normalize_content_id(content_id)
    if not content_id:
        abort(404)
    db = get_db()
    game = db.execute("SELECT * FROM games WHERE content_id = ?", (content_id,)).fetchone()
    if game:
        return game
    iso = db.execute(
        "SELECT original_filename FROM files WHERE file_type = 'iso' AND content_id = ? COLLATE NOCASE "
        "ORDER BY is_published DESC, id DESC LIMIT 1", (content_id,)
    ).fetchone()
    if not iso:
        abort(404)
    if not create:
        return {"id": None, "content_id": content_id, "name": iso["original_filename"]}
    return get_or_create_game(db, content_id, iso["original_filename"])


def _iso_slot(value):
    from .memcards import ISO_CARD_SLOTS

    try:
        slot = int(value or 1)
    except (TypeError, ValueError):
        abort(404)
    if slot not in ISO_CARD_SLOTS:
        abort(404)
    return slot


def _iso_card(game, slot=1):
    if not game["id"]:
        return None
    return get_db().execute(
        "SELECT m.*, g.name AS game_name, g.content_id FROM memcards m JOIN games g ON g.id = m.game_id "
        "WHERE m.player_id IS NULL AND m.game_id = ? AND m.iso_slot = ?", (game["id"], slot)
    ).fetchone()


@bp.get("/jogos/<content_id>/memory-card")
@login_required
def iso_card(content_id):
    from .memcards import ISO_CARD_SLOTS, iso_lock

    from .listing import cap, like
    game = _iso_card_game(content_id)
    note = request.args.get("origem", "").strip()
    slots = []
    for slot in ISO_CARD_SLOTS:
        card = _iso_card(game, slot)
        versions = get_db().execute(
            "SELECT * FROM memcard_versions WHERE memcard_id = ? AND IFNULL(note, '') LIKE ? ESCAPE '\\' "
            "ORDER BY version DESC", (card["id"], like(note))
        ).fetchall() if card else []
        versions, total = cap(versions)
        slots.append({"slot": slot, "card": card, "versions": versions, "total": total})
    lock = iso_lock(get_db(), game["id"]) if game["id"] else None
    return render_template("admin/iso_memcard.html", game=game, slots=slots, lock=lock, note=note)


@bp.post("/jogos/<content_id>/memory-card/liberar")
@login_required
def iso_card_unlock(content_id):
    validate_csrf()
    game = _iso_card_game(content_id)
    if game["id"]:
        db = get_db()
        db.execute("DELETE FROM iso_card_locks WHERE game_id = ?", (game["id"],))
        db.commit()
        flash("M. Card da ISO liberado: uma nova partida já pode usá-lo.", "success")
    return redirect(url_for("admin.iso_card", content_id=game["content_id"]))


@bp.post("/jogos/<content_id>/memory-card/enviar")
@login_required
def iso_card_upload(content_id):
    from .memcards import add_version, card_error, get_or_create_iso_memcard

    validate_csrf()
    slot = _iso_slot(request.form.get("slot"))
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Escolha o arquivo do Memory Card.", "error")
        return redirect(url_for("admin.iso_card", content_id=content_id))
    data = upload.read(256 * 1024 + 1)
    error = card_error(data)
    if error:
        flash(error, "error")
        return redirect(url_for("admin.iso_card", content_id=content_id))
    db = get_db()
    game = _iso_card_game(content_id, create=True)
    card = get_or_create_iso_memcard(db, game["id"], slot)
    version = add_version(db, card["id"], data, "upload", f"Enviado pelo admin: {upload.filename[:100]}")
    db.commit()
    flash(f"M. Card da ISO de {game['name']}, slot {slot}, atualizado (versão {version}).", "success")
    return redirect(url_for("admin.iso_card", content_id=game["content_id"]))


@bp.get("/jogos/<content_id>/memory-card/baixar")
@login_required
def iso_card_download(content_id):
    from .memcards import card_path
    from .players import card_zip_response

    slot = _iso_slot(request.args.get("slot"))
    card = _iso_card(_iso_card_game(content_id), slot)
    if not card:
        abort(404)
    db = get_db()
    version = db.execute(
        "SELECT * FROM memcard_versions WHERE memcard_id = ? AND version = ?",
        (card["id"], request.args.get("versao", type=int) or card["current_version"]),
    ).fetchone()
    if not version or not card_path(version["sha256"]).exists():
        abort(404)
    if slot == 1:
        return card_zip_response(db, card, version, "ISO")
    # O slot 2 do pacote TIERES é o pcsx-card2.mcd (pcsx_rearmed_memcard2 = "shared").
    return card_zip_response(db, card, version, f"ISO-slot{slot}", f"saves/PCSX-ReARMed/pcsx-card{slot}.mcd")


@bp.post("/jogos/<content_id>/memory-card/restaurar")
@login_required
def iso_card_restore(content_id):
    from .memcards import add_version, card_path

    validate_csrf()
    slot = _iso_slot(request.form.get("slot"))
    card = _iso_card(_iso_card_game(content_id), slot)
    if not card:
        abort(404)
    db = get_db()
    version = db.execute(
        "SELECT * FROM memcard_versions WHERE memcard_id = ? AND version = ?",
        (card["id"], request.form.get("version", type=int)),
    ).fetchone()
    if not version or not card_path(version["sha256"]).exists():
        abort(404)
    if version["version"] == card["current_version"]:
        flash("Esta já é a versão atual.", "error")
    else:
        new_version = add_version(db, card["id"], card_path(version["sha256"]).read_bytes(), "restore",
                                  f"Restaurada a versão {version['version']}")
        db.commit()
        flash(f"Slot {slot}: versão {version['version']} restaurada como versão {new_version}.", "success")
    return redirect(url_for("admin.iso_card", content_id=card["content_id"]))


# Championship ISOs uploaded before files.content_id existed: CRC32 of the
# local copy (a few seconds per ISO - hence the thread) or, for the ones
# migrated to archive.org, the crc32 its file list has.
_identify_isos_lock = threading.Lock()


def _identify_isos(app):
    if not _identify_isos_lock.acquire(blocking=False):
        return None
    try:
        with app.app_context():
            from .memcards import fill_championship_content_ids
            from .tournament.iso import fill_content_ids
            found, missing = fill_championship_content_ids(get_db())
            more, still = fill_content_ids(get_db(), app)
            return found + more, missing + still
    except Exception:
        app.logger.exception("Falha ao identificar as ISOs dos campeonatos")
        return None
    finally:
        _identify_isos_lock.release()


@bp.post("/jogos/identificar-isos")
@login_required
def games_identify_isos():
    validate_csrf()
    app = current_app._get_current_object()
    if app.config.get("TESTING"):
        result = _identify_isos(app)
        if result:
            found, missing = result
            flash(f"{found} ISO(s) identificada(s)." + (f" Sem identificação: {', '.join(missing)}." if missing else ""),
                  "success" if not missing else "error")
    elif _identify_isos_lock.locked():
        flash("A identificação das ISOs já está em andamento.", "error")
    else:
        threading.Thread(target=_identify_isos, args=(app,), daemon=True).start()
        flash("Identificação das ISOs iniciada. Atualize esta página em alguns segundos.", "success")
    return redirect(url_for("admin.games_list"))
