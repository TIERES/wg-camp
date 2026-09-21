import json
import re
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for
from werkzeug.security import check_password_hash

from .archive_org import ArchiveOrgError, upload_zip
from .db import get_db, now
from .arena17_import import import_championship
from .replays import _download_name
from .security import login_required, validate_csrf
from .storage import delete_stored_file, store_upload

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
    return render_template("admin/files.html", championship=championship, files=entries)


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
        db.execute("INSERT INTO files (championship_id,display_name,description,stored_name,original_filename,file_type,file_size,sha256,is_published,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (championship_id, display_name, request.form.get("description", "").strip(), stored["stored_name"], stored["original_filename"], request.form.get("file_type", "other"), stored["file_size"], stored["sha256"], int("is_published" in request.form), now(), now()))
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
    oldest = rows[0]["started_at"][:10]
    newest = rows[-1]["started_at"][:10]
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


@bp.post("/replays/<session_id>/delete")
@login_required
def replay_delete(session_id):
    validate_csrf()
    db = get_db()
    replay = db.execute("SELECT * FROM live_sessions WHERE session_id=?", (session_id,)).fetchone()
    if not replay:
        return ("Não encontrado", 404)
    if request.form.get("confirm") != "delete":
        flash("Para excluir, marque a confirmação.", "error")
        return redirect(url_for("admin.replays_list"))
    _delete_replay_file(replay["stored_name"])
    db.execute("DELETE FROM live_sessions WHERE session_id=?", (session_id,))
    db.commit()
    flash("Replay excluído.", "success")
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


@bp.post("/replays/cleanup/<label>/archive")
@login_required
def replays_cleanup_archive(label):
    validate_csrf()
    zip_path, manifest = _load_backup_manifest(label)
    identifier = f"wecamp-replays-{label}"
    try:
        url = upload_zip(
            identifier=identifier,
            file_path=zip_path,
            access_key=current_app.config["IA_ACCESS_KEY"],
            secret_key=current_app.config["IA_SECRET_KEY"],
            collection=current_app.config["IA_COLLECTION"],
            title=f"WE Camp - Replays {label.replace('_a_', ' a ')}",
            description=f"Backup de {len(manifest['session_ids'])} replay(s) de partidas gravadas na WE Camp entre {label.replace('_a_', ' e ')}.",
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
