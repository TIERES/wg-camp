import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_replays import build_header, frame_record

from app import create_app
from app.db import get_db, init_db, now


class AdminReplaysTest(unittest.TestCase):
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
            "REPLAY_BACKUPS_DIR": str(root / "replay-backups"),
            "SPECTATE_API_KEY": "",
            "SERVE_DOWNLOADS_LOCALLY": True,
            "IA_ACCESS_KEY": "",
            "IA_SECRET_KEY": "",
        })
        with self.app.app_context():
            init_db()
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def _login(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["csrf_token"] = "test-csrf-token"

    def _finish_session(self, session_id, started_ago_days=0, started_ago_seconds=400):
        self.client.post(
            "/spectate/ingest",
            data=build_header(),
            headers={"X-Session-Id": session_id, "X-Sequence": "0"},
            content_type="application/octet-stream",
        )
        with self.app.app_context():
            started_at = (
                datetime.now(timezone.utc) - timedelta(days=started_ago_days, seconds=started_ago_seconds)
            ).replace(microsecond=0).isoformat()
            get_db().execute("UPDATE live_sessions SET started_at = ? WHERE session_id = ?", (started_at, session_id))
            get_db().commit()
        self.client.post(
            "/spectate/ingest",
            data=frame_record(),
            headers={"X-Session-Id": session_id, "X-Sequence": "1", "X-Session-End": "true"},
            content_type="application/octet-stream",
        )

    def test_replays_list_requires_login(self):
        response = self.client.get("/admin/replays")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_replays_list_renders_with_replays_and_pending_backup(self):
        self._finish_session("111-222", started_ago_days=90)
        self._finish_session("recent-1", started_ago_days=1)
        self._login()
        self.client.post("/admin/replays/cleanup/generate", data={"csrf_token": "test-csrf-token"})

        response = self.client.get("/admin/replays")
        self.assertEqual(response.status_code, 200)
        self.assertIn("host, guest".encode("utf-8"), response.data)
        self.assertIn(b"Backup pendente", response.data)

    def test_replays_list_renders_with_no_replays(self):
        self._login()
        response = self.client.get("/admin/replays")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Nenhum replay finalizado ainda".encode("utf-8"), response.data)

    def test_edit_form_renders(self):
        self._finish_session("111-222")
        self._login()
        response = self.client.get("/admin/replays/111-222/edit")
        self.assertEqual(response.status_code, 200)
        self.assertIn("host, guest".encode("utf-8"), response.data)

    def test_edit_replaces_player_names(self):
        self._finish_session("111-222")
        self._login()

        response = self.client.post(
            "/admin/replays/111-222/edit",
            data={"csrf_token": "test-csrf-token", "player_names": "Novo Nome, Outro Nome"},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            row = get_db().execute("SELECT player_names FROM live_sessions WHERE session_id=?", ("111-222",)).fetchone()
        self.assertEqual(row["player_names"], "Novo Nome, Outro Nome")

    def test_delete_removes_row_and_file(self):
        self._finish_session("333-444")
        self._login()
        self.assertTrue((self.live_dir / "333-444.krec").exists())

        response = self.client.post(
            "/admin/replays/333-444/delete",
            data={"csrf_token": "test-csrf-token", "confirm": "delete"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse((self.live_dir / "333-444.krec").exists())
        with self.app.app_context():
            row = get_db().execute("SELECT 1 FROM live_sessions WHERE session_id=?", ("333-444",)).fetchone()
        self.assertIsNone(row)

    def test_delete_without_confirm_checkbox_does_nothing(self):
        self._finish_session("555-666")
        self._login()

        self.client.post("/admin/replays/555-666/delete", data={"csrf_token": "test-csrf-token"})
        self.assertTrue((self.live_dir / "555-666.krec").exists())
        with self.app.app_context():
            row = get_db().execute("SELECT 1 FROM live_sessions WHERE session_id=?", ("555-666",)).fetchone()
        self.assertIsNotNone(row)

    def test_cleanup_only_selects_replays_older_than_retention_window(self):
        self._finish_session("old-1", started_ago_days=90)
        self._finish_session("recent-1", started_ago_days=5)
        self._login()

        response = self.client.post("/admin/replays/cleanup/generate", data={"csrf_token": "test-csrf-token"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        # Old one is gone from the DB path taken during status page render is
        # not yet - generate doesn't delete anything on its own.
        with self.app.app_context():
            rows = get_db().execute("SELECT session_id FROM live_sessions").fetchall()
        session_ids = {r["session_id"] for r in rows}
        self.assertIn("old-1", session_ids)
        self.assertIn("recent-1", session_ids)

        backups_dir = Path(self.app.config["REPLAY_BACKUPS_DIR"])
        manifests = list(backups_dir.glob("*.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text())
        self.assertEqual(manifest["session_ids"], ["old-1"])
        self.assertFalse(manifest["deleted_from_server"])

        zip_path = backups_dir / f"{manifest['label']}.zip"
        self.assertTrue(zip_path.exists())
        with zipfile.ZipFile(zip_path) as zf:
            self.assertEqual(zf.namelist(), ["old-1.krec"])

    def test_cleanup_generate_is_noop_when_nothing_is_stale(self):
        self._finish_session("recent-1", started_ago_days=5)
        self._login()

        response = self.client.post("/admin/replays/cleanup/generate", data={"csrf_token": "test-csrf-token"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        backups_dir = Path(self.app.config["REPLAY_BACKUPS_DIR"])
        self.assertEqual(list(backups_dir.glob("*.json")), [])

    def test_cleanup_full_flow_download_then_confirm_delete_then_discard(self):
        self._finish_session("old-1", started_ago_days=90)
        self._login()
        self.client.post("/admin/replays/cleanup/generate", data={"csrf_token": "test-csrf-token"})

        backups_dir = Path(self.app.config["REPLAY_BACKUPS_DIR"])
        label = next(backups_dir.glob("*.json")).stem

        status = self.client.get(f"/admin/replays/cleanup/{label}")
        self.assertEqual(status.status_code, 200)

        with self.client.get(f"/admin/replays/cleanup/{label}/download") as download:
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.mimetype, "application/zip")
            download.get_data()  # fully consume so Werkzeug releases the file handle (matters on Windows)

        # Confirming delete removes the replay from the server but keeps
        # the backup around (it's now the only remaining copy).
        confirm = self.client.post(
            f"/admin/replays/cleanup/{label}/confirm-delete",
            data={"csrf_token": "test-csrf-token", "confirm": "delete"},
        )
        self.assertEqual(confirm.status_code, 302)
        self.assertFalse((self.live_dir / "old-1.krec").exists())
        with self.app.app_context():
            row = get_db().execute("SELECT 1 FROM live_sessions WHERE session_id=?", ("old-1",)).fetchone()
        self.assertIsNone(row)
        self.assertTrue((backups_dir / f"{label}.zip").exists())

        # A second confirm-delete is a no-op (already deleted), not an error.
        second_confirm = self.client.post(
            f"/admin/replays/cleanup/{label}/confirm-delete",
            data={"csrf_token": "test-csrf-token", "confirm": "delete"},
        )
        self.assertEqual(second_confirm.status_code, 302)

        discard = self.client.post(
            f"/admin/replays/cleanup/{label}/discard",
            data={"csrf_token": "test-csrf-token", "confirm": "delete"},
        )
        self.assertEqual(discard.status_code, 302)
        self.assertFalse((backups_dir / f"{label}.zip").exists())
        self.assertFalse((backups_dir / f"{label}.json").exists())

    def test_cleanup_discard_without_confirm_keeps_backup(self):
        self._finish_session("old-1", started_ago_days=90)
        self._login()
        self.client.post("/admin/replays/cleanup/generate", data={"csrf_token": "test-csrf-token"})
        backups_dir = Path(self.app.config["REPLAY_BACKUPS_DIR"])
        label = next(backups_dir.glob("*.json")).stem

        self.client.post(f"/admin/replays/cleanup/{label}/discard", data={"csrf_token": "test-csrf-token"})
        self.assertTrue((backups_dir / f"{label}.zip").exists())

    def test_archive_upload_without_keys_flashes_error_instead_of_crashing(self):
        self._finish_session("old-1", started_ago_days=90)
        self._login()
        self.client.post("/admin/replays/cleanup/generate", data={"csrf_token": "test-csrf-token"})
        backups_dir = Path(self.app.config["REPLAY_BACKUPS_DIR"])
        label = next(backups_dir.glob("*.json")).stem

        response = self.client.post(
            f"/admin/replays/cleanup/{label}/archive",
            data={"csrf_token": "test-csrf-token"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Falha ao enviar".encode("utf-8"), response.data)

    def test_admin_download_works_for_short_replays_unlike_public_route(self):
        # started_ago_days pushes started_at back, but ended_at is always
        # "real now" (see _finish_session) - so duration_seconds naturally
        # comes out large. Force it short explicitly to isolate what this
        # test actually checks: the admin route ignoring the 5-minute
        # public minimum, independent of how old the replay is.
        self._finish_session("short-1", started_ago_days=90)
        with self.app.app_context():
            get_db().execute("UPDATE live_sessions SET duration_seconds=10 WHERE session_id=?", ("short-1",))
            get_db().commit()
        self._login()

        public = self.client.get("/replays/short-1/download")
        self.assertEqual(public.status_code, 404)

        with self.client.get("/admin/replays/short-1/download") as admin_download:
            self.assertEqual(admin_download.status_code, 200)
            admin_download.get_data()

    def test_backup_label_path_traversal_is_rejected(self):
        self._login()
        response = self.client.get("/admin/replays/cleanup/..%2f..%2f..%2fetc%2fpasswd")
        self.assertIn(response.status_code, (400, 404))


if __name__ == "__main__":
    unittest.main()
