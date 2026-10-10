import hashlib
import tempfile
import unittest
import zlib
from io import BytesIO
from pathlib import Path
from unittest import mock

from app import create_app
from app.archive_org import ArchiveOrgError
from app.db import get_db, init_db, now

ISO_BYTES = b"iso de teste" * 100
ISO_MD5 = hashlib.md5(ISO_BYTES).hexdigest()


def remote(md5):
    return {"md5": md5, "size": 474431328, "mtime": "1786825765"}


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
            "ARCHIVE_VERIFY_INTERVAL": 0,
            "ARCHIVE_VERIFY_TIMEOUT": 0,
        })
        with self.app.app_context():
            init_db()
            db = get_db()
            db.execute(
                "INSERT INTO championships (id,slug,name,is_published,created_at,updated_at) VALUES (1,'copa-teste','Copa Teste',1,?,?)",
                (now(), now()),
            )
            db.execute(
                "INSERT INTO files (id,championship_id,display_name,stored_name,original_filename,file_type,file_size,is_published,created_at,updated_at) VALUES (1,1,'Copa','0123456789abcdef0123456789abcdef.bin','Copa Teste.bin','iso',?,1,?,?)",
                (len(ISO_BYTES), now(), now()),
            )
            db.commit()
        (self.downloads / "0123456789abcdef0123456789abcdef.bin").write_bytes(ISO_BYTES)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["csrf_token"] = "test-csrf-token"

    def tearDown(self):
        self.temp.cleanup()

    def _archive(self, **form):
        return self.client.post("/admin/files/1/archive", data={"csrf_token": "test-csrf-token", **form})

    def _file(self):
        with self.app.app_context():
            return dict(get_db().execute("SELECT * FROM files WHERE id=1").fetchone())

    @mock.patch("app.admin.upload_file", return_value="https://archive.org/download/one-two-iso/Copa%20Teste.bin")
    @mock.patch("app.admin.list_item_files", return_value={"outro.bin": remote("123")})
    def test_uploads_under_original_name_and_stores_url(self, _list, upload):
        response = self._archive()
        self.assertEqual(response.status_code, 302)
        args, kwargs = upload.call_args
        self.assertEqual(args[0], "one-two-iso")
        self.assertEqual(kwargs["remote_name"], "Copa Teste.bin")
        entry = self._file()
        self.assertEqual(entry["archive_status"], "done")
        self.assertEqual(entry["archive_url"], "https://archive.org/download/one-two-iso/Copa%20Teste.bin")
        self.assertEqual(entry["md5"], ISO_MD5)

        page = self.client.get("/admin/championships/1/files")
        self.assertIn(b"https://archive.org/download/one-two-iso/Copa%20Teste.bin", page.data)
        public = self.client.get("/")
        self.assertIn(b"pelo archive.org", public.data)

    @mock.patch("app.admin.upload_file")
    @mock.patch("app.admin.list_item_files", return_value={"Copa Teste.bin": remote(ISO_MD5)})
    def test_same_name_same_md5_is_linked_without_upload(self, _list, upload):
        self._archive()
        upload.assert_not_called()
        entry = self._file()
        self.assertEqual(entry["archive_status"], "done")
        self.assertEqual(entry["archive_url"], "https://archive.org/download/one-two-iso/Copa%20Teste.bin")

    @mock.patch("app.admin.upload_file")
    @mock.patch("app.admin.list_item_files", return_value={"Nome Antigo.bin": remote(ISO_MD5)})
    def test_same_md5_under_other_name_is_linked_without_upload(self, _list, upload):
        self._archive()
        upload.assert_not_called()
        self.assertEqual(self._file()["archive_url"], "https://archive.org/download/one-two-iso/Nome%20Antigo.bin")

    @mock.patch("app.admin.delete_file")
    @mock.patch("app.admin.upload_file")
    @mock.patch("app.admin.list_item_files", return_value={"Copa Teste.bin": remote("md5-antigo")})
    def test_same_name_different_md5_asks_before_replacing(self, _list, upload, delete):
        response = self._archive()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Substituir ISO no archive.org", response.data)
        self.assertIn(b"md5-antigo", response.data)
        self.assertIn(ISO_MD5.encode(), response.data)
        upload.assert_not_called()
        delete.assert_not_called()
        self.assertIsNone(self._file()["archive_status"])

    @mock.patch("app.admin.delete_file")
    @mock.patch("app.admin.upload_file", return_value="https://archive.org/download/one-two-iso/Copa%20Teste.bin")
    @mock.patch("app.admin.list_item_files", return_value={"Copa Teste.bin": remote("md5-antigo")})
    def test_confirmed_replace_deletes_old_then_uploads_same_name(self, _list, upload, delete):
        calls = []
        delete.side_effect = lambda *a, **k: calls.append("delete")
        upload.side_effect = lambda *a, **k: calls.append("upload") or "https://archive.org/download/one-two-iso/Copa%20Teste.bin"
        response = self._archive(replace="yes")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(calls, ["delete", "upload"])
        self.assertEqual(delete.call_args.args[:2], ("one-two-iso", "Copa Teste.bin"))
        self.assertEqual(upload.call_args.kwargs["remote_name"], "Copa Teste.bin")
        self.assertEqual(self._file()["archive_status"], "done")

    @mock.patch("app.admin.delete_file")
    @mock.patch("app.admin.upload_file", side_effect=ArchiveOrgError("archive.org retornou 500: falhou"))
    @mock.patch("app.admin.list_item_files", return_value={"Copa Teste.bin": remote("md5-antigo")})
    def test_replace_upload_failure_warns_old_copy_may_be_gone(self, _list, _upload, _delete):
        self._archive(replace="yes")
        entry = self._file()
        self.assertEqual(entry["archive_status"], "error")
        self.assertIn("antiga pode já ter sido excluída", entry["archive_error"])

    @mock.patch("app.admin.list_item_files", side_effect=ArchiveOrgError("timeout"))
    def test_metadata_failure_does_not_start_upload(self, _list):
        response = self._archive()
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self._file()["archive_status"])

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


    @mock.patch("app.admin.upload_file", return_value="https://archive.org/download/one-two-iso/Copa%20Teste.bin")
    @mock.patch("app.admin.list_item_files")
    def test_migration_removes_local_copy_once_md5_is_confirmed(self, list_files, _upload):
        # Comparison before upload, then the first poll doesn't list it yet, the second does.
        list_files.side_effect = [{}, {}, {"Copa Teste.bin": remote(ISO_MD5)}]
        self.app.config["ARCHIVE_VERIFY_TIMEOUT"] = 60
        self._archive(remove_local="yes")
        entry = self._file()
        self.assertEqual(entry["archive_status"], "done")
        self.assertIsNotNone(entry["local_removed_at"])
        self.assertIsNone(entry["archive_error"])
        self.assertFalse((self.downloads / "0123456789abcdef0123456789abcdef.bin").exists())

        download = self.client.get("/download/1")
        self.assertEqual(download.status_code, 302)
        self.assertEqual(download.headers["Location"], "https://archive.org/download/one-two-iso/Copa%20Teste.bin")
        page = self.client.get("/admin/championships/1/files")
        self.assertIn("Migrada".encode(), page.data)

    @mock.patch("app.admin.upload_file", return_value="url")
    @mock.patch("app.admin.list_item_files")
    def test_migration_keeps_local_copy_when_md5_never_matches(self, list_files, _upload):
        list_files.side_effect = [{}, {"Copa Teste.bin": remote("md5-corrompido")}]
        self._archive(remove_local="yes")
        entry = self._file()
        self.assertEqual(entry["archive_status"], "done")
        self.assertIsNone(entry["local_removed_at"])
        self.assertIn("foi mantida", entry["archive_error"])
        self.assertTrue((self.downloads / "0123456789abcdef0123456789abcdef.bin").exists())

    @mock.patch("app.admin.upload_file")
    @mock.patch("app.admin.list_item_files", return_value={"Copa Teste.bin": remote(ISO_MD5)})
    def test_already_on_archive_with_same_md5_removes_local_right_away(self, _list, upload):
        self._archive(remove_local="yes")
        upload.assert_not_called()
        self.assertIsNotNone(self._file()["local_removed_at"])
        self.assertFalse((self.downloads / "0123456789abcdef0123456789abcdef.bin").exists())

    @mock.patch("app.admin.upload_file", return_value="url")
    @mock.patch("app.admin.list_item_files", return_value={})
    def test_without_remove_local_the_copy_stays(self, _list, _upload):
        self._archive()
        self.assertIsNone(self._file()["local_removed_at"])
        self.assertTrue((self.downloads / "0123456789abcdef0123456789abcdef.bin").exists())

    def test_iso_upload_is_identified_for_memory_cards(self):
        with self.app.app_context():
            get_db().execute("INSERT INTO championships (id,slug,name,is_published,created_at,updated_at) VALUES (2,'copa-2','Copa 2',1,?,?)", (now(), now()))
            get_db().commit()
        data = b"outra iso" * 50
        response = self.client.post("/admin/championships/2/files", data={
            "csrf_token": "test-csrf-token", "file_type": "iso", "is_published": "on", "file": (BytesIO(data), "Copa 2.bin"),
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            entry = get_db().execute("SELECT * FROM files WHERE championship_id=2").fetchone()
        self.assertEqual(entry["content_id"], f"{zlib.crc32(data):08X}:{len(data):X}")

    def test_iso_upload_keeps_spaces_and_accents_in_the_name(self):
        for cid, name, expected in ((10, "Winning Eleven 2002 Edição BR.bin", "Winning Eleven 2002 Edição BR.bin"),
                                    (11, "C:\\Jogos\\WE: 2002?  final .bin", "WE 2002 final .bin")):
            with self.app.app_context():
                get_db().execute("INSERT INTO championships (id,slug,name,is_published,created_at,updated_at) VALUES (?,?,?,1,?,?)",
                                 (cid, f"copa-{cid}", f"Copa {cid}", now(), now()))
                get_db().commit()
            response = self.client.post(f"/admin/championships/{cid}/files", data={
                "csrf_token": "test-csrf-token", "file_type": "iso", "is_published": "on", "file": (BytesIO(b"iso"), name),
            })
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                entry = get_db().execute("SELECT * FROM files WHERE championship_id=?", (cid,)).fetchone()
            self.assertEqual(entry["original_filename"], expected)
            self.assertEqual(entry["display_name"], expected)

    def test_download_header_carries_the_utf8_name(self):
        from app.storage import attachment_header
        header = attachment_header("Winning Eleven 2002 Edição BR.bin")
        self.assertEqual(header, "attachment; filename=\"Winning Eleven 2002 Edicao BR.bin\"; "
                                 "filename*=UTF-8''Winning%20Eleven%202002%20Edi%C3%A7%C3%A3o%20BR.bin")
        header.encode("latin-1")  # HTTP headers must be latin-1

    @mock.patch("app.archive_org.list_item_files", return_value={
        "WE Legends 2005.chd": {"md5": "x", "crc32": "f8131957", "size": 474431328, "mtime": None}})
    def test_identify_isos_from_local_copy_and_archive_org(self, list_files):
        with self.app.app_context():
            db = get_db()
            for cid, name, stored, url in ((2, "Legends", "1" * 32 + ".chd", "https://archive.org/download/one-two-iso/WE%20Legends%202005.chd"),
                                           (3, "Compactada", "2" * 32 + ".7z", None)):
                db.execute("INSERT INTO championships (id,slug,name,is_published,created_at,updated_at) VALUES (?,?,?,1,?,?)", (cid, f"c{cid}", name, now(), now()))
                db.execute("INSERT INTO files (id,championship_id,display_name,stored_name,original_filename,file_type,file_size,is_published,created_at,updated_at,archive_url,local_removed_at) "
                           "VALUES (?,?,?,?,?,'iso',1,1,?,?,?,?)", (cid, cid, name, stored, name, now(), now(), url, now() if url else None))
            db.commit()
        response = self.client.post("/admin/jogos/identificar-isos", data={"csrf_token": "test-csrf-token"})
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            ids = {row["id"]: row["content_id"] for row in get_db().execute("SELECT id, content_id FROM files")}
        self.assertEqual(ids[1], f"{zlib.crc32(ISO_BYTES):08X}:{len(ISO_BYTES):X}")  # cópia local
        self.assertEqual(ids[2], "F8131957:1C473F60")  # migrada: crc32 do archive.org
        self.assertIsNone(ids[3])  # .7z: o RetroArch lê o conteúdo descompactado
        list_files.assert_called_once_with("one-two-iso")
        page = self.client.get("/admin/jogos").get_data(as_text=True)
        self.assertIn("F8131957:1C473F60", page)
        self.assertIn("não identificada", page)

    @mock.patch("app.admin.list_item_files")
    def test_migrated_file_cannot_be_archived_again(self, list_files):
        with self.app.app_context():
            get_db().execute("UPDATE files SET local_removed_at=?, archive_url='u' WHERE id=1", (now(),))
            get_db().commit()
        self._archive(remove_local="yes")
        list_files.assert_not_called()


if __name__ == "__main__":
    unittest.main()
