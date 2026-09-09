from flask import Blueprint, Response, abort, current_app, render_template, send_from_directory

from .db import get_db

bp = Blueprint("public", __name__)


@bp.get("/")
def index():
    db = get_db()
    championships = db.execute("SELECT * FROM championships WHERE is_published = 1 ORDER BY start_date DESC, created_at DESC").fetchall()
    files_by_championship = {}
    if championships:
        placeholders = ",".join("?" for _ in championships)
        entries = db.execute(f"SELECT * FROM files WHERE is_published = 1 AND championship_id IN ({placeholders}) ORDER BY id DESC", tuple(item["id"] for item in championships)).fetchall()
        for entry in entries:
            files_by_championship.setdefault(entry["championship_id"], []).append(entry)
    return render_template("public/index.html", championships=championships, files_by_championship=files_by_championship)


@bp.get("/download/<int:file_id>")
def download(file_id):
    """Autoriza o arquivo; em produção o Nginx faz a transmissão via X-Accel."""
    entry = get_db().execute(
        """SELECT files.* FROM files JOIN championships ON championships.id = files.championship_id
           WHERE files.id = ? AND files.is_published = 1 AND championships.is_published = 1""",
        (file_id,),
    ).fetchone()
    if not entry:
        abort(404)
    if current_app.config["SERVE_DOWNLOADS_LOCALLY"]:
        return send_from_directory(current_app.config["DOWNLOADS_DIR"], entry["stored_name"], as_attachment=True, download_name=entry["original_filename"])
    response = Response()
    response.headers["X-Accel-Redirect"] = f"/_protected_downloads/{entry['stored_name']}"
    response.headers["Content-Disposition"] = f'attachment; filename="{entry["original_filename"]}"'
    return response
