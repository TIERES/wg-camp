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
from .db import get_db, now
from .arena17_import import import_championship
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


@bp.post("/logout")
@login_required
def logout():
    validate_csrf()
    session.clear()
    return redirect(url_for("admin.login"))


@bp.get("/")
@login_required
def dashboard():
    championships = get_db().execute("SELECT * FROM championships ORDER BY is_published DESC, start_date DESC, created_at DESC").fetchall()
    return render_template("admin/dashboard.html", championships=championships)


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
    except (ValueError, OSError, sqlite3.IntegrityError) as error:
        if stored:
            delete_stored_file(stored["stored_name"])
        flash(str(error), "error")
    else:
        flash("Arquivo enviado e registrado.", "success")
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
            url = upload_file(
                ISO_ARCHIVE_ITEM_IDENTIFIER,
                Path(app.config["DOWNLOADS_DIR"]) / entry["stored_name"],
                remote_name=remote_name,
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

    db.execute(
        "UPDATE files SET archive_status='uploading', archive_error=NULL, archive_updated_at=? WHERE id=?",
        (now(), file_id),
    )
    db.commit()
    app = current_app._get_current_object()
    args = (app, file_id, remote_name, action == "replace", remove_local)
    if app.config.get("TESTING"):
        _archive_iso(*args)
    else:
        threading.Thread(target=_archive_iso, args=args, daemon=True).start()
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
    db = get_db()
    replays = db.execute("SELECT * FROM live_sessions WHERE status='finished' ORDER BY started_at DESC").fetchall()
    stale = _stale_replays(db)
    stale_size = sum(r["bytes_received"] or 0 for r in stale)
    pending_backups = sorted(p.stem for p in _backups_dir().glob("*.json"))
    return render_template(
        "admin/replays.html",
        replays=replays,
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
    players = get_db().execute(
        "SELECT p.*, (SELECT COUNT(*) FROM memcards m WHERE m.player_id = p.id) AS cards "
        "FROM players p ORDER BY p.username COLLATE NOCASE"
    ).fetchall()
    return render_template("admin/players.html", players=players)


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
    games = db.execute(
        "SELECT g.*, (SELECT COUNT(*) FROM memcards m WHERE m.game_id = g.id) AS cards "
        "FROM games g ORDER BY g.name COLLATE NOCASE"
    ).fetchall()
    isos = db.execute(
        "SELECT f.display_name, f.original_filename, f.content_id, f.is_published, "
        "c.name AS championship_name, c.is_published AS championship_published "
        "FROM files f JOIN championships c ON c.id = f.championship_id "
        "WHERE f.file_type = 'iso' ORDER BY c.start_date DESC, c.created_at DESC, f.id DESC"
    ).fetchall()
    return render_template("admin/games.html", games=games, isos=isos, identifying=_identify_isos_lock.locked())


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
            return fill_championship_content_ids(get_db())
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
