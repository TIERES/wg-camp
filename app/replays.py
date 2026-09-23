import re
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from .db import get_db
from .spectate import reap_stale_live_sessions

bp = Blueprint("replays", __name__, url_prefix="/replays")

MIN_REPLAY_DURATION_SECONDS = 300
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 50

# Matches the session id kaillera-client assigns a replay ("<pid>-<unix-ts>",
# same format as spectate.py's live sessions) - used as a filename component
# below, so this whitelist also doubles as path-traversal protection.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Generous ceiling for a single N64 core savestate (retro_serialize() output).
MAX_STATE_BYTES = 32 * 1024 * 1024


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


def _finished_replay_or_404(session_id):
    if not SESSION_ID_RE.match(session_id):
        abort(400, "Session id inválido.")
    entry = get_db().execute(
        """SELECT * FROM live_sessions
           WHERE session_id = ? AND status = 'finished' AND duration_seconds >= ?""",
        (session_id, MIN_REPLAY_DURATION_SECONDS),
    ).fetchone()
    if not entry:
        abort(404)
    return entry


@bp.get("/<session_id>/download")
def download(session_id):
    entry = _finished_replay_or_404(session_id)

    download_name = _download_name(entry)

    if current_app.config["SERVE_DOWNLOADS_LOCALLY"]:
        return send_from_directory(current_app.config["LIVE_DIR"], entry["stored_name"], as_attachment=True, download_name=download_name)
    response = Response()
    response.headers["X-Accel-Redirect"] = f"/_protected_replays/{entry['stored_name']}"
    response.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    return response


def _retryconnect_states_dir():
    path = Path(current_app.config["LIVE_DIR"]) / "retryconnect_states"
    path.mkdir(parents=True, exist_ok=True)
    return path


@bp.post("/<session_id>/state")
def upload_state(session_id):
    """retry-connect: host uploads a savestate taken at the exact frame it
    stopped fast-forwarding a group replay (see kaillera-client's
    kcore/kaillera_retryconnect.cpp). Fast-forward during retry-connect is
    host-only and purely local - different machines/cores can't be trusted to
    reach the identical frame from replaying the same recorded input at
    whatever speed their own hardware allows, so nobody else tries to
    reproduce it by fast-forwarding themselves. Instead, the moment the host
    stops, everyone else loads this exact state.

    Body: 4-byte little-endian frame index the state was taken at, followed
    by the raw retro_serialize() bytes. Only the latest state matters for a
    given replay session, so a new upload just overwrites the last one -
    nothing here needs cleaning up by callers.
    """
    _finished_replay_or_404(session_id)

    if request.content_length and request.content_length > MAX_STATE_BYTES:
        abort(413)
    body = request.get_data(cache=False)
    if len(body) > MAX_STATE_BYTES:
        abort(413)
    if len(body) < 4:
        abort(400, "Corpo do state save inválido.")

    (_retryconnect_states_dir() / f"{session_id}.state").write_bytes(body)
    return ("", 204)


@bp.get("/<session_id>/state")
def download_state(session_id):
    _finished_replay_or_404(session_id)

    state_name = f"{session_id}.state"
    if not (_retryconnect_states_dir() / state_name).exists():
        abort(404)

    if current_app.config["SERVE_DOWNLOADS_LOCALLY"]:
        return send_from_directory(str(_retryconnect_states_dir()), state_name, as_attachment=True, download_name=state_name)
    response = Response()
    response.headers["X-Accel-Redirect"] = f"/_protected_replay_states/{state_name}"
    response.headers["Content-Disposition"] = f'attachment; filename="{state_name}"'
    return response
