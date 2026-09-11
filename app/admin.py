import sqlite3
from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .db import get_db, now
from .arena17_import import import_championship
from .security import login_required, validate_csrf
from .storage import delete_stored_file, store_upload

bp = Blueprint("admin", __name__, url_prefix="/admin")


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
