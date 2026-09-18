import os
import re
import secrets
from pathlib import Path

from flask import Blueprint, abort, current_app, request

from .db import get_db, now

bp = Blueprint("spectate", __name__, url_prefix="/spectate")

# Matches the session id the kaillera-client DLL generates ("<pid>-<unix-ts>").
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

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


def _ensure_session_row(db, session_id, stored_name):
    db.execute(
        """INSERT INTO live_sessions (session_id, status, stored_name, bytes_received, started_at, updated_at)
           VALUES (?, 'live', ?, 0, ?, ?)
           ON CONFLICT(session_id) DO NOTHING""",
        (session_id, stored_name, now(), now()),
    )


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

    if request.content_length and request.content_length > MAX_BATCH_BYTES:
        abort(413)
    body = request.get_data(cache=False)
    if len(body) > MAX_BATCH_BYTES:
        abort(413)

    live_dir = _live_dir()
    stored_name = f"{session_id}.krec.part"
    file_path = live_dir / stored_name

    db = get_db()
    _ensure_session_row(db, session_id, stored_name)

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
        final_path = live_dir / f"{session_id}.krec"
        try:
            os.replace(file_path, final_path)
            final_name = final_path.name
        except OSError:
            final_name = stored_name
        db.execute(
            "UPDATE live_sessions SET status='finished', stored_name=?, ended_at=?, updated_at=? WHERE session_id=?",
            (final_name, now(), now(), session_id),
        )

    db.commit()
    return ("", 204)
