import struct
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import create_app
from app.db import get_db


def build_header(app_name=b"n64emu", game_name=b"Winning Eleven 2002", player_slots=(b"host", b"guest")):
    header = bytearray(400)
    header[0:4] = b"KRC1"
    header[4:4 + len(app_name)] = app_name
    header[132:132 + len(game_name)] = game_name
    header[260:264] = struct.pack("<i", 1234567890)
    header[264:268] = struct.pack("<i", 1)
    header[268:272] = struct.pack("<i", 2)
    for index, name in enumerate(player_slots):
        offset = 272 + index * 32
        header[offset:offset + len(name)] = name
    return bytes(header)


def frame_record(payload=b"\x01\x02"):
    return b"\x12" + struct.pack("<h", len(payload)) + payload


class ReplaysTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.live_dir = root / "live"
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": str(root / "arena17.db"),
            "DOWNLOADS_DIR": str(root / "downloads"),
            "UPLOAD_TMP_DIR": str(root / "tmp"),
            "LIVE_DIR": str(self.live_dir),
            "SPECTATE_API_KEY": "",
            "SERVE_DOWNLOADS_LOCALLY": True,
        })
        with self.app.app_context():
            from app.db import init_db
            init_db()
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def _finish_session(self, session_id, started_ago_seconds):
        self.client.post(
            "/spectate/ingest",
            data=build_header(),
            headers={"X-Session-Id": session_id, "X-Sequence": "0"},
            content_type="application/octet-stream",
        )
        with self.app.app_context():
            started_at = (datetime.now(timezone.utc) - timedelta(seconds=started_ago_seconds)).replace(microsecond=0).isoformat()
            get_db().execute("UPDATE live_sessions SET started_at = ? WHERE session_id = ?", (started_at, session_id))
            get_db().commit()
        self.client.post(
            "/spectate/ingest",
            data=frame_record(),
            headers={"X-Session-Id": session_id, "X-Sequence": "1", "X-Session-End": "true"},
            content_type="application/octet-stream",
        )

    def test_long_session_appears_in_replay_list_with_duration(self):
        self._finish_session("111-222", started_ago_seconds=310)

        response = self.client.get("/replays/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"host, guest", response.data)
        self.assertIn(b"Winning Eleven 2002", response.data)
        self.assertIn(b"5m", response.data)

        with self.app.app_context():
            row = get_db().execute("SELECT duration_seconds FROM live_sessions WHERE session_id = ?", ("111-222",)).fetchone()
        self.assertGreaterEqual(row["duration_seconds"], 300)

    def test_short_session_is_excluded_from_list_and_download(self):
        self._finish_session("333-444", started_ago_seconds=10)

        response = self.client.get("/replays/")
        self.assertNotIn(b"333-444", response.data)

        download = self.client.get("/replays/333-444/download")
        self.assertEqual(download.status_code, 404)

    def test_download_serves_the_krec_file(self):
        self._finish_session("555-666", started_ago_seconds=400)

        download = self.client.get("/replays/555-666/download")
        self.assertEqual(download.status_code, 200)
        self.assertTrue((self.live_dir / "555-666.krec").exists())

    def test_unknown_session_download_is_404(self):
        response = self.client.get("/replays/does-not-exist/download")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
