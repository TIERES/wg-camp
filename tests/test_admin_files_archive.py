import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import create_app
from app.archive_org import ArchiveOrgError
from app.db import get_db, init_db, now

ISO_BYTES = b"iso de teste" * 100


class AdminFilesArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.downloads = root / "downloads"
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": str(root / "arena17.db"),
            "DOWNLOADS_DIR": str(self.downloads),
            "UPLOAD_TMP_DIR": str(root / "tmp"),
            "LIVE_DIR": str(root / "live"),
            "REPLAY_BACKUPS_DIR": str(root / "replay-backups"),
            "SERVE_DOWNLOADS_LOCALLY": True,
            "IA_ACCESS_KEY": "access",
            "IA_SECRET_KEY": "secret",
        })
        with self.app.app_context():
            init_db()
            db = get_db()
            db.execute(
                "INSERT INTO championships (id,slug,name,is_published,created_at,updated_at) VALUES (1,'copa-teste','Copa Teste',1,?,?)",
                (now(), now()),
            )
            db.execute(
                "INSERT INTO files (id,championship_id,display_name,stored_name,original_filename,file_type,file_size,is_published,created_at,updated_at) VALUES (1,1,'Copa','abc.bin','Copa Teste.bin','iso',?,1,?,?)",
                (len(ISO_BYTES), now(), now()),
            )
            db.commit()
        (self.downloads / "abc.bin").write_bytes(ISO_BYTES)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["csrf_token"] = "test-csrf-token"

    def tearDown(self):
        self.temp.cleanup()

    def _archive(self):
        return self.client.post("/admin/files/1/archive", data={"csrf_token": "test-csrf-token"})

    def _file(self):
        with self.app.app_context():
            return dict(get_db().execute("SELECT * FROM files WHERE id=1").fetchone())

    @mock.patch("app.admin.upload_file", return_value="https://archive.org/download/one-two-iso/Copa%20Teste.bin")
    @mock.patch("app.admin.list_item_files", return_value={"outro.bin": "123"})
    def test_uploads_under_original_name_and_stores_url(self, _list, upload):
        response = self._archive()
        self.assertEqual(response.status_code, 302)
        args, kwargs = upload.call_args
        self.assertEqual(args[0], "one-two-iso")
        self.assertEqual(kwargs["remote_name"], "Copa Teste.bin")
        entry = self._file()
        self.assertEqual(entry["archive_status"], "done")
        self.assertEqual(entry["archive_url"], "https://archive.org/download/one-two-iso/Copa%20Teste.bin")

        page = self.client.get("/admin/championships/1/files")
        self.assertIn(b"https://archive.org/download/one-two-iso/Copa%20Teste.bin", page.data)
        public = self.client.get("/")
        self.assertIn(b"pelo archive.org", public.data)

    @mock.patch("app.admin.upload_file")
    @mock.patch("app.admin.list_item_files")
    def test_same_md5_already_on_archive_is_linked_without_upload(self, list_files, upload):
        list_files.return_value = {"Nome Antigo.bin": hashlib.md5(ISO_BYTES).hexdigest()}
        self._archive()
        upload.assert_not_called()
        self.assertEqual(self._file()["archive_url"], "https://archive.org/download/one-two-iso/Nome%20Antigo.bin")

    @mock.patch("app.admin.upload_file", return_value="url")
    @mock.patch("app.admin.list_item_files", return_value={"Copa Teste.bin": "outro-md5"})
    def test_name_taken_by_different_file_gets_slug_prefix(self, _list, upload):
        self._archive()
        self.assertEqual(upload.call_args.kwargs["remote_name"], "copa-teste-Copa Teste.bin")

    @mock.patch("app.admin.upload_file", side_effect=ArchiveOrgError("archive.org retornou 403: negado"))
    @mock.patch("app.admin.list_item_files", return_value={})
    def test_failure_is_recorded(self, _list, _upload):
        self._archive()
        entry = self._file()
        self.assertEqual(entry["archive_status"], "error")
        self.assertIn("403", entry["archive_error"])
        self.assertIsNone(entry["archive_url"])
        page = self.client.get("/admin/championships/1/files")
        self.assertIn("403".encode(), page.data)

    @mock.patch("app.admin.upload_file")
    def test_in_progress_upload_is_not_started_twice(self, upload):
        with self.app.app_context():
            get_db().execute("UPDATE files SET archive_status='uploading', archive_updated_at=? WHERE id=1", (now(),))
            get_db().commit()
        self._archive()
        upload.assert_not_called()

    def test_without_keys_flashes_error(self):
        self.app.config["IA_ACCESS_KEY"] = ""
        response = self._archive()
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self._file()["archive_status"])


if __name__ == "__main__":
    unittest.main()
