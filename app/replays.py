from flask import Blueprint, Response, abort, current_app, render_template, send_from_directory
from werkzeug.utils import secure_filename

from .db import get_db

bp = Blueprint("replays", __name__, url_prefix="/replays")

MIN_REPLAY_DURATION_SECONDS = 300


@bp.get("/")
def index():
    rows = get_db().execute(
        """SELECT * FROM live_sessions
           WHERE status = 'finished' AND duration_seconds >= ?
           ORDER BY started_at DESC""",
        (MIN_REPLAY_DURATION_SECONDS,),
    ).fetchall()
    return render_template("public/replays.html", replays=rows)


@bp.get("/<session_id>/download")
def download(session_id):
    entry = get_db().execute(
        """SELECT * FROM live_sessions
           WHERE session_id = ? AND status = 'finished' AND duration_seconds >= ?""",
        (session_id, MIN_REPLAY_DURATION_SECONDS),
    ).fetchone()
    if not entry:
        abort(404)

    label = secure_filename(f"{entry['game_name']}_{entry['player_names']}") or entry["session_id"]
    download_name = f"{label}.krec"

    if current_app.config["SERVE_DOWNLOADS_LOCALLY"]:
        return send_from_directory(current_app.config["LIVE_DIR"], entry["stored_name"], as_attachment=True, download_name=download_name)
    response = Response()
    response.headers["X-Accel-Redirect"] = f"/_protected_replays/{entry['stored_name']}"
    response.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    return response
