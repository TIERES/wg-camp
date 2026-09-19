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


class SpectateWatchTest(unittest.TestCase):
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

    def _ingest(self, session_id, body, sequence=0, end=False, api_key=None, owner=None):
        headers = {"X-Session-Id": session_id, "X-Sequence": str(sequence)}
        if end:
            headers["X-Session-End"] = "true"
        if api_key:
            headers["X-Api-Key"] = api_key
        if owner:
            headers["X-Owner-Name"] = owner
        return self.client.post("/spectate/ingest", data=body, headers=headers, content_type="application/octet-stream")

    def test_lookup_finds_live_session_by_room_name(self):
        self._ingest("111-222", build_header(game_name=b"Sala do Fulano"))

        found = self.client.get("/spectate/lookup?room=Sala+do+Fulano")
        self.assertEqual(found.status_code, 200)
        data = found.get_json()
        self.assertEqual(data["session_id"], "111-222")
        self.assertEqual(data["status"], "live")

        missing = self.client.get("/spectate/lookup?room=Sala+Inexistente")
        self.assertEqual(missing.status_code, 404)

    def test_lookup_prefers_live_over_finished_for_same_room_name(self):
        self._ingest("old-session", build_header(game_name=b"Sala Reused"), sequence=0, end=True)
        self._ingest("new-session", build_header(game_name=b"Sala Reused"))

        found = self.client.get("/spectate/lookup?room=Sala+Reused")
        self.assertEqual(found.get_json()["session_id"], "new-session")

    def test_lookup_with_owner_disambiguates_same_room_name(self):
        # Two different hosts both named their room "We2002.bin" at the same
        # time - without `owner`, lookup can only guess (most recent live);
        # with it, it must resolve to the exact host the spectator picked.
        self._ingest("session-a", build_header(game_name=b"We2002.bin"), owner="HostA")
        self._ingest("session-b", build_header(game_name=b"We2002.bin"), owner="HostB")

        found_a = self.client.get("/spectate/lookup?room=We2002.bin&owner=HostA")
        self.assertEqual(found_a.get_json()["session_id"], "session-a")

        found_b = self.client.get("/spectate/lookup?room=We2002.bin&owner=HostB")
        self.assertEqual(found_b.get_json()["session_id"], "session-b")

        missing_owner = self.client.get("/spectate/lookup?room=We2002.bin&owner=HostC")
        self.assertEqual(missing_owner.status_code, 404)

    def test_stream_returns_bytes_from_offset_and_status_headers(self):
        first_body = build_header() + frame_record()
        self._ingest("111-222", first_body)

        r1 = self.client.get("/spectate/stream/111-222?offset=0")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.data, first_body)
        self.assertEqual(r1.headers["X-Status"], "live")
        self.assertEqual(r1.headers["X-Next-Offset"], str(len(first_body)))

        second_body = frame_record(b"\x03\x04")
        self._ingest("111-222", second_body, sequence=1, end=True)

        r2 = self.client.get(f"/spectate/stream/111-222?offset={len(first_body)}")
        self.assertEqual(r2.data, second_body)
        self.assertEqual(r2.headers["X-Status"], "finished")

        r3 = self.client.get(f"/spectate/stream/111-222?offset={len(first_body) + len(second_body)}")
        self.assertEqual(r3.data, b"")
        self.assertEqual(r3.headers["X-Status"], "finished")

    def test_stream_unknown_session_is_404(self):
        response = self.client.get("/spectate/stream/does-not-exist?offset=0")
        self.assertEqual(response.status_code, 404)

    def test_lookup_and_stream_require_api_key_when_configured(self):
        self.app.config["SPECTATE_API_KEY"] = "s3cret"
        self._ingest("111-222", build_header(game_name=b"Sala Protegida"), api_key="s3cret")

        self.assertEqual(self.client.get("/spectate/lookup?room=Sala+Protegida").status_code, 401)
        self.assertEqual(
            self.client.get("/spectate/lookup?room=Sala+Protegida", headers={"X-Api-Key": "s3cret"}).status_code, 200
        )
        self.assertEqual(self.client.get("/spectate/stream/111-222?offset=0").status_code, 401)
        self.assertEqual(
            self.client.get("/spectate/stream/111-222?offset=0", headers={"X-Api-Key": "s3cret"}).status_code, 200
        )


if __name__ == "__main__":
    unittest.main()
