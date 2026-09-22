import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request

from .db import get_db, now

bp = Blueprint("spectate", __name__, url_prefix="/spectate")

# Matches the session id the kaillera-client DLL generates ("<pid>-<unix-ts>").
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# A single GET /stream response never returns more than this many bytes, so a
# spectator far behind live still gets bounded, steadily-progressing chunks
# instead of one huge read.
MAX_STREAM_CHUNK_BYTES = 1 * 1024 * 1024

# Layout of the 400-byte KRC1-style header the client sends once, on the
# first batch (X-Sequence: 0) of every session - byte-identical to the local
# .krec file header written by kailleraclient.cpp's _gameCallback.
HEADER_SIZE = 400
_APP_NAME = slice(4, 132)
_GAME_NAME = slice(132, 260)
_TIMESTAMP = slice(260, 264)
_PLAYERNO = slice(264, 268)
_NUMPLAYERS = slice(268, 272)
_PLAYER_NAMES = slice(272, 400)

MAX_BATCH_BYTES = 2 * 1024 * 1024  # generous ceiling for a ~300ms batch of controller frames

# A hosting client normally updates its session every ~300ms (see
# N02_STREAM_BATCH_MS in n02_stream.cpp) for as long as it's connected, and
# always sends X-Session-End on a graceful shutdown/game-end. No update in
# this long means the host vanished without that signal - a crash, a force
# quit, or a dead network - not a normal lag spike. Reap it so the bytes
# already on disk don't stay stranded as an undownloadable .krec.part
# forever (see kaillera-client issue: a Kaillera-server connection drop mid
# game silently orphaned the second half of a match this way).
STALE_LIVE_TIMEOUT_SECONDS = 300


def _live_dir():
    path = Path(current_app.config["LIVE_DIR"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _check_api_key():
    expected = current_app.config.get("SPECTATE_API_KEY") or ""
    if not expected:
        # No key configured: allow through (matches this project's existing
        # convention for ARENA17_SECRET_KEY - documented as required in
        # production, not hard-enforced here). Set ARENA17_SPECTATE_KEY to
        # require callers to send a matching X-Api-Key header.
        return
    provided = request.headers.get("X-Api-Key", "")
    if not secrets.compare_digest(provided, expected):
        abort(401)


def _decode_cstr(chunk: bytes) -> str:
    return chunk.split(b"\x00", 1)[0].decode("latin-1", "replace")


def _parse_header(body: bytes):
    if len(body) < HEADER_SIZE or body[0:4] not in (b"KRC0", b"KRC1"):
        return None
    player_names = [
        _decode_cstr(body[_PLAYER_NAMES][i * 32:(i + 1) * 32])
        for i in range(4)
    ]
    return {
        "app_name": _decode_cstr(body[_APP_NAME]),
        "game_name": _decode_cstr(body[_GAME_NAME]),
        "playerno": int.from_bytes(body[_PLAYERNO], "little", signed=True),
        "numplayers": int.from_bytes(body[_NUMPLAYERS], "little", signed=True),
        "player_names": ", ".join(name for name in player_names if name),
    }


def _ensure_session_row(db, session_id, stored_name, owner_name):
    db.execute(
        """INSERT INTO live_sessions (session_id, owner_name, status, stored_name, bytes_received, started_at, updated_at)
           VALUES (?, ?, 'live', ?, 0, ?, ?)
           ON CONFLICT(session_id) DO NOTHING""",
        (session_id, owner_name, stored_name, now(), now()),
    )


def _finalize_session(db, session_id, stored_name, ended_at):
    """Renames <session_id>.krec.part to its final .krec name and marks the
    live_sessions row finished. `ended_at` is when the last usable byte
    arrived - the client's own timestamp on a graceful X-Session-End, or the
    session's last ingest update for one the reaper below gives up on -
    never "now", which would count a host's downtime as part of the replay.
    """
    final_path = _live_dir() / f"{session_id}.krec"
    final_name = stored_name
    try:
        os.replace(_live_dir() / stored_name, final_path)
        final_name = final_path.name
    except OSError:
        pass  # .part never showed up (sequence 0 never arrived) - nothing to rename

    row = db.execute("SELECT started_at FROM live_sessions WHERE session_id = ?", (session_id,)).fetchone()
    duration_seconds = 0
    if row:
        duration_seconds = max(0, int((datetime.fromisoformat(ended_at) - datetime.fromisoformat(row["started_at"])).total_seconds()))
    db.execute(
        "UPDATE live_sessions SET status='finished', stored_name=?, ended_at=?, duration_seconds=?, updated_at=? WHERE session_id=?",
        (final_name, ended_at, duration_seconds, ended_at, session_id),
    )


def reap_stale_live_sessions(db, exclude_session_id=None):
    """Finalizes any 'live' session abandoned for STALE_LIVE_TIMEOUT_SECONDS.

    Cheap to call on every request that touches live_sessions (ingest, the
    replay list/page): the query is a no-op in the common case where nothing
    is stale. `exclude_session_id` lets ingest() skip the session it's about
    to update itself, so a host whose own update just crossed the staleness
    threshold (e.g. it was retrying a stuck connection) isn't reaped out
    from under the batch that's arriving right now.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_LIVE_TIMEOUT_SECONDS)).replace(microsecond=0).isoformat()
    query = "SELECT session_id, stored_name, updated_at FROM live_sessions WHERE status = 'live' AND updated_at < ?"
    params = [cutoff]
    if exclude_session_id:
        query += " AND session_id != ?"
        params.append(exclude_session_id)

    stale = db.execute(query, params).fetchall()
    for row in stale:
        _finalize_session(db, row["session_id"], row["stored_name"], row["updated_at"])
    if stale:
        db.commit()


@bp.post("/ingest")
def ingest():
    """Receives one live-spectate batch from a hosting kaillera-client.

    See common/n02_stream.h (kaillera-client repo) for the wire contract:
    headers X-Session-Id / X-Sequence / X-Session-End, body is a straight
    concatenation of .krec-format records, with the 400-byte session header
    prefixed on sequence 0. Batches for a given session always arrive in
    order from a single sender thread, so we just append bytes as they come.
    """
    _check_api_key()

    session_id = request.headers.get("X-Session-Id", "")
    if not SESSION_ID_RE.match(session_id):
        abort(400, "Session id inválido.")

    try:
        sequence = int(request.headers.get("X-Sequence", "-1"))
    except ValueError:
        abort(400, "X-Sequence inválido.")
    if sequence < 0:
        abort(400, "X-Sequence ausente.")

    session_end = request.headers.get("X-Session-End", "").strip().lower() == "true"
    owner_name = request.headers.get("X-Owner-Name", "")[:64]

    if request.content_length and request.content_length > MAX_BATCH_BYTES:
        abort(413)
    body = request.get_data(cache=False)
    if len(body) > MAX_BATCH_BYTES:
        abort(413)

    live_dir = _live_dir()
    stored_name = f"{session_id}.krec.part"
    file_path = live_dir / stored_name

    db = get_db()
    reap_stale_live_sessions(db, exclude_session_id=session_id)
    _ensure_session_row(db, session_id, stored_name, owner_name)

    if sequence == 0:
        with file_path.open("wb") as f:
            f.write(body)
        header = _parse_header(body)
        if header:
            db.execute(
                """UPDATE live_sessions SET app_name=?, game_name=?, player_names=?, updated_at=?
                   WHERE session_id=?""",
                (header["app_name"], header["game_name"], header["player_names"], now(), session_id),
            )
    else:
        # A missing sequence 0 (e.g. server restarted mid-session) just means
        # we lose the header/metadata for this session - still append so we
        # don't lose the frame data itself.
        with file_path.open("ab") as f:
            f.write(body)

    db.execute(
        "UPDATE live_sessions SET bytes_received = bytes_received + ?, updated_at = ? WHERE session_id = ?",
        (len(body), now(), session_id),
    )

    if session_end:
        _finalize_session(db, session_id, stored_name, now())

    db.commit()
    return ("", 204)


@bp.get("/lookup")
def lookup():
    """Finds the live (or just-finished) session for a Kaillera room name.

    In kaillera-server mode, the "game name" the client sends with a session
    is the room name the host typed when creating the game (see
    kaillera_ui.cpp's GAME / kailleraclient.cpp's _gameCallback), so a
    spectator picking "Watch" on a room in the lobby can look it up here by
    that same name. Returns the most recently started match for that name,
    preferring one that's still live over an already-finished one.

    Room names aren't unique - two different hosts can create rooms with the
    same name at the same time. When the caller also passes `owner` (the
    "owner" column the lobby list already shows next to the room name), it
    narrows the match to that specific host instead of guessing from the
    name alone, since a Kaillera user can only host one room at a time.
    """
    _check_api_key()

    room = request.args.get("room", "").strip()
    if not room:
        abort(400, "Parâmetro 'room' ausente.")
    owner = request.args.get("owner", "").strip()

    if owner:
        query = """SELECT session_id, status, app_name, game_name, player_names, bytes_received
                   FROM live_sessions
                   WHERE game_name = ? AND owner_name = ?
                   ORDER BY (status = 'live') DESC, started_at DESC
                   LIMIT 1"""
        params = (room, owner)
    else:
        query = """SELECT session_id, status, app_name, game_name, player_names, bytes_received
                   FROM live_sessions
                   WHERE game_name = ?
                   ORDER BY (status = 'live') DESC, started_at DESC
                   LIMIT 1"""
        params = (room,)

    row = get_db().execute(query, params).fetchone()
    if row is None:
        abort(404, "Nenhuma transmissão encontrada para essa sala.")

    return jsonify(dict(row))


@bp.get("/stream/<session_id>")
def stream(session_id):
    """Serves back the bytes of a live (or finished) session from an offset.

    A spectator polls this repeatedly with an ever-increasing `offset`
    (starting at 0) to fast-forward-replay the same record stream the host
    is recording locally - each response's body is a straight slice of the
    session's .krec/.krec.part file, so concatenating bodies in order
    reproduces the same bytes described in n02_stream.h. X-Status tells the
    caller whether to expect more data later ("live") or stop asking
    ("finished"); X-Next-Offset is the offset to request next.
    """
    _check_api_key()

    if not SESSION_ID_RE.match(session_id):
        abort(400, "Session id inválido.")

    try:
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        abort(400, "Offset inválido.")
    if offset < 0:
        abort(400, "Offset inválido.")

    row = get_db().execute(
        "SELECT status, stored_name, bytes_received FROM live_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        abort(404, "Sessão desconhecida.")

    # Never read past bytes_received: it's only bumped in ingest() *after*
    # that batch's write() has returned, so it's the one number guaranteed to
    # never be ahead of what's actually, fully landed on disk. Reading the
    # file's raw current length instead would race a concurrent ingest()
    # append on the gunicorn worker's other thread - a spectator polling the
    # live edge could catch the file mid-write and get a torn/truncated tail
    # record, desyncing their .krec parser (seen in practice: kaillera-client
    # reported "unrecognized record type" and dropped the stream a few
    # minutes into watching a live match).
    want = max(0, min(MAX_STREAM_CHUNK_BYTES, row["bytes_received"] - offset))

    file_path = _live_dir() / row["stored_name"]
    chunk = b""
    if want > 0:
        try:
            with file_path.open("rb") as f:
                f.seek(offset)
                chunk = f.read(want)
        except FileNotFoundError:
            pass  # host hasn't sent its first batch yet - report status, no bytes

    response = current_app.response_class(chunk, mimetype="application/octet-stream")
    response.headers["X-Status"] = row["status"]
    response.headers["X-Next-Offset"] = str(offset + len(chunk))
    return response
