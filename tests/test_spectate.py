import struct
import tempfile
import unittest
from pathlib import Path

from app import create_app
from app.db import get_db


def build_header(app_name=b"n64emu", game_name=b"Winning Eleven 2002", playerno=1, numplayers=2):
    header = bytearray(400)
    header[0:4] = b"KRC1"
    header[4:4 + len(app_name)] = app_name
    header[132:132 + len(game_name)] = game_name
    header[260:264] = struct.pack("<i", 1234567890)
    header[264:268] = struct.pack("<i", playerno)
    header[268:272] = struct.pack("<i", numplayers)
    names = b"host\x00\x00guest\x00"
    header[272:272 + len(names)] = names
    return bytes(header)


def frame_record(payload=b"\x01\x02"):
    return b"\x12" + struct.pack("<h", len(payload)) + payload


class SpectateIngestTest(unittest.TestCase):
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
        })
        with self.app.app_context():
            from app.db import init_db
            init_db()
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_full_session_writes_finished_krec_and_db_row(self):
        first_body = build_header() + frame_record()
        r1 = self.client.post(
            "/spectate/ingest",
            data=first_body,
            headers={"X-Session-Id": "111-222", "X-Sequence": "0"},
            content_type="application/octet-stream",
        )
        self.assertEqual(r1.status_code, 204)
        self.assertTrue((self.live_dir / "111-222.krec.part").exists())

        second_body = frame_record(b"\x03\x04")
        r2 = self.client.post(
            "/spectate/ingest",
            data=second_body,
            headers={"X-Session-Id": "111-222", "X-Sequence": "1", "X-Session-End": "true"},
            content_type="application/octet-stream",
        )
        self.assertEqual(r2.status_code, 204)

        final_path = self.live_dir / "111-222.krec"
        self.assertTrue(final_path.exists())
        self.assertFalse((self.live_dir / "111-222.krec.part").exists())
        self.assertEqual(final_path.read_bytes(), first_body + second_body)

        with self.app.app_context():
            row = get_db().execute("SELECT * FROM live_sessions WHERE session_id = ?", ("111-222",)).fetchone()
        self.assertEqual(row["status"], "finished")
        self.assertEqual(row["game_name"], "Winning Eleven 2002")
        self.assertEqual(row["bytes_received"], len(first_body) + len(second_body))

    def test_rejects_bad_session_id(self):
        response = self.client.post(
            "/spectate/ingest",
            data=b"x",
            headers={"X-Session-Id": "not valid!", "X-Sequence": "0"},
        )
        self.assertEqual(response.status_code, 400)

    def test_rejects_missing_sequence(self):
        response = self.client.post(
            "/spectate/ingest",
            data=b"x",
            headers={"X-Session-Id": "111-222"},
        )
        self.assertEqual(response.status_code, 400)

    def test_requires_api_key_when_configured(self):
        self.app.config["SPECTATE_API_KEY"] = "s3cret"
        no_key = self.client.post(
            "/spectate/ingest",
            data=build_header(),
            headers={"X-Session-Id": "111-222", "X-Sequence": "0"},
        )
        self.assertEqual(no_key.status_code, 401)

        with_key = self.client.post(
            "/spectate/ingest",
            data=build_header(),
            headers={"X-Session-Id": "111-222", "X-Sequence": "0", "X-Api-Key": "s3cret"},
        )
        self.assertEqual(with_key.status_code, 204)


if __name__ == "__main__":
    unittest.main()
