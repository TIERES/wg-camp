from flask import Blueprint, Response, abort, current_app, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from .db import get_db
from .spectate import reap_stale_live_sessions

bp = Blueprint("replays", __name__, url_prefix="/replays")

MIN_REPLAY_DURATION_SECONDS = 300
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 50


def _download_name(entry):
    label = secure_filename(f"{entry['game_name']}_{entry['player_names']}") or entry["session_id"]
    return f"{label}.krec"


@bp.get("/")
def index():
    db = get_db()
    reap_stale_live_sessions(db)
    rows = db.execute(
        """SELECT * FROM live_sessions
           WHERE status = 'finished' AND duration_seconds >= ?
           ORDER BY started_at DESC""",
        (MIN_REPLAY_DURATION_SECONDS,),
    ).fetchall()
    return render_template("public/replays.html", replays=rows)


@bp.get("/list.txt")
def list_txt():
    """Machine-readable replay list for the kaillera-client DLL ("Replays
    Online" in the Playback screen), which speaks plain HTTP with no JSON
    library - a tab-separated line per replay is far simpler to parse in C
    than a JSON array. One line per replay, newest first:

        session_id\tstarted_at (DD-MM-YYYY HH:MM)\tgame_name\tplayer_names\tduration_seconds\tdownload_name\tbytes_received

    Tabs/newlines in free-text fields (game_name/player_names, both
    attacker-controlled via the recording header) are stripped so a row
    always has exactly 7 tab-separated fields.
    """
    try:
        limit = int(request.args.get("limit", DEFAULT_LIST_LIMIT))
    except ValueError:
        limit = DEFAULT_LIST_LIMIT
    limit = max(1, min(limit, MAX_LIST_LIMIT))

    db = get_db()
    reap_stale_live_sessions(db)
    rows = db.execute(
        """SELECT * FROM live_sessions
           WHERE status = 'finished' AND duration_seconds >= ?
           ORDER BY started_at DESC
           LIMIT ?""",
        (MIN_REPLAY_DURATION_SECONDS, limit),
    ).fetchall()

    def clean(value):
        return (value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")

    lines = []
    for entry in rows:
        started_at = entry["started_at"] or ""
        when = f"{started_at[8:10]}-{started_at[5:7]}-{started_at[0:4]} {started_at[11:16]}" if len(started_at) >= 16 else started_at
        lines.append("\t".join([
            entry["session_id"],
            when,
            clean(entry["game_name"]),
            clean(entry["player_names"]),
            str(entry["duration_seconds"] or 0),
            _download_name(entry),
            str(entry["bytes_received"] or 0),
        ]))

    return Response("\n".join(lines) + ("\n" if lines else ""), mimetype="text/plain")


@bp.get("/<session_id>/download")
def download(session_id):
    entry = get_db().execute(
        """SELECT * FROM live_sessions
           WHERE session_id = ? AND status = 'finished' AND duration_seconds >= ?""",
        (session_id, MIN_REPLAY_DURATION_SECONDS),
    ).fetchone()
    if not entry:
        abort(404)

    download_name = _download_name(entry)

    if current_app.config["SERVE_DOWNLOADS_LOCALLY"]:
        return send_from_directory(current_app.config["LIVE_DIR"], entry["stored_name"], as_attachment=True, download_name=download_name)
    response = Response()
    response.headers["X-Accel-Redirect"] = f"/_protected_replays/{entry['stored_name']}"
    response.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    return response
