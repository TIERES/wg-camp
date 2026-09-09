from flask import Blueprint, render_template

from .db import get_db

bp = Blueprint("public", __name__)


@bp.get("/")
def index():
    db = get_db()
    championship = db.execute(
        "SELECT * FROM championships WHERE is_current = 1 AND is_published = 1"
    ).fetchone()
    files = []
    if championship:
        files = db.execute(
            "SELECT * FROM files WHERE championship_id = ? AND is_published = 1 ORDER BY id DESC",
            (championship["id"],),
        ).fetchall()
    return render_template("public/index.html", championship=championship, files=files)
