import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.archive_org import ArchiveOrgError
from app.db import get_db, init_db, now
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"
ISO = b"PS1 ISO de teste" * 64
ISO_MD5 = hashlib.md5(ISO).hexdigest()
ARCHIVE_URL = "https://archive.org/download/one-two-iso/torneios/copa-iso-1/Copa%20Teste.bin"


class TournamentIsoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.app.config.update(ARCHIVE_VERIFY_INTERVAL=0, ARCHIVE_VERIFY_TIMEOUT=0)
        with self.app.app_context():
            init_db()
            db = get_db()
            self.owner, self.other = add_player(db, "dono"), add_player(db, "outro")
            self.game = db.execute("SELECT id FROM game_titles WHERE code = 'we2002'").fetchone()[0]
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def client(self, player=None):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            if player:
                session["player_id"] = player
        return client

    def query(self, sql, *args):
        with self.app.app_context():
            return get_db().execute(sql, args).fetchall()

    def create(self, iso=ISO, filename="Copa Teste.bin", **extra):
        """Cria o torneio e, como o organizador faz depois, envia a ISO em Gerenciar."""
        data = {"csrf_token": CSRF, "name": "Copa ISO", "game_title_id": self.game, "format": "knockout",
                "max_players": "4", "entry_fee": "0", "prize_split": "60,30,10", "ko_legs": "1", "final_legs": "1",
                "team_mode": "free", **extra}
        response = self.client(self.owner).post("/torneios/novo", data=data)
        if response.status_code != 302 or iso is None:
            return response
        self.assertIn("/gerenciar#iso", response.headers["Location"])
        slug = self.query("SELECT slug FROM tournaments")[0]["slug"]
        return self.client(self.owner).post(f"/torneios/{slug}/iso", data={"csrf_token": CSRF, "iso": (io.BytesIO(iso), filename)},
                                            content_type="multipart/form-data")

    def iso_row(self):
        rows = self.query("SELECT * FROM tournament_isos")
        return rows[0] if rows else None

    def local_path(self, row):
        return Path(self.app.config["DOWNLOADS_DIR"]) / row["stored_name"]

    def with_archive_keys(self):
        self.app.config.update(IA_ACCESS_KEY="k", IA_SECRET_KEY="s")

    def test_create_with_dates_and_iso_without_archive_keys(self):
        response = self.create(starts_on="2026-11-07", ends_on="2026-11-28")
        self.assertEqual(response.status_code, 302)
        tournament = self.query("SELECT * FROM tournaments")[0]
        self.assertEqual((tournament["starts_on"], tournament["ends_on"]), ("2026-11-07", "2026-11-28"))
        row = self.iso_row()
        self.assertEqual((row["original_filename"], row["md5"], row["file_size"]), ("Copa Teste.bin", ISO_MD5, len(ISO)))
        self.assertEqual(row["archive_status"], "pending")
        self.assertEqual(row["archive_name"], f"torneios/{tournament['slug']}/Copa Teste.bin")
        self.assertEqual(self.local_path(row).read_bytes(), ISO)

        page = self.client(self.other).get(f"/torneios/{tournament['slug']}").get_data(as_text=True)
        self.assertIn("Início 07/11/2026 · Fim 28/11/2026", page)
        self.assertIn("sendo enviada para o archive.org", page)
        self.assertIn("07/11/2026 a 28/11/2026", self.client().get("/torneios/").get_data(as_text=True))

        # Teste local sem proxy: o download sai deste servidor.
        self.app.config["SERVE_DOWNLOADS_LOCALLY"] = True
        page = self.client(self.other).get(f"/torneios/{tournament['slug']}").get_data(as_text=True)
        self.assertIn("Baixar Copa Teste.bin", page)
        download = self.client(self.other).get(f"/torneios/{tournament['slug']}/iso")
        self.assertEqual(download.data, ISO)
        download.close()

    def test_download_needs_login(self):
        self.create()
        slug = self.query("SELECT slug FROM tournaments")[0]["slug"]
        self.assertIn("Entre para baixar a ISO", self.client().get(f"/torneios/{slug}").get_data(as_text=True))
        response = self.client().get(f"/torneios/{slug}/iso")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/conta/entrar", response.headers["Location"])

    def test_invalid_dates_and_extension_are_refused(self):
        response = self.create(starts_on="2026-11-28", ends_on="2026-11-07")
        self.assertIn("O fim provável não pode ser antes do início.", response.get_data(as_text=True))
        self.assertEqual(self.query("SELECT COUNT(*) FROM tournaments")[0][0], 0)
        self.create(iso=None)
        owner = self.client(self.owner)
        response = owner.post("/torneios/copa-iso-1/iso", data={"csrf_token": CSRF, "iso": (io.BytesIO(ISO), "copa.exe")},
                              content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("A ISO precisa ser", response.get_data(as_text=True))
        self.assertIsNone(self.iso_row())
        self.assertEqual(list(Path(self.app.config["DOWNLOADS_DIR"]).iterdir()), [])

    def test_create_form_has_no_iso_field(self):
        page = self.client(self.owner).get("/torneios/novo").get_data(as_text=True)
        self.assertNotIn('name="iso"', page)
        self.assertIn("enviada depois de criar, em Gerenciar", page)

    def test_too_big_is_refused_before_reading(self):
        self.app.config["TOURNAMENT_ISO_MAX_BYTES"] = 0
        response = self.create(iso=b"x" * (1024 ** 2 + 10))
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.iso_row())

    @mock.patch("app.tournament.iso.upload_file")
    @mock.patch("app.tournament.iso.list_item_files")
    def test_archive_upload_verify_and_remove_local(self, listing, upload):
        self.with_archive_keys()
        listing.side_effect = [{}, {"torneios/copa-iso-1/Copa Teste.bin": {"md5": ISO_MD5}}]
        self.create()
        row = self.iso_row()
        upload.assert_called_once()
        self.assertEqual(upload.call_args.kwargs["remote_name"], "torneios/copa-iso-1/Copa Teste.bin")
        self.assertEqual((row["archive_status"], row["archive_url"]), ("done", ARCHIVE_URL))
        self.assertIsNotNone(row["local_removed_at"])
        self.assertFalse(self.local_path(row).exists())
        download = self.client(self.other).get("/torneios/copa-iso-1/iso")
        self.assertEqual((download.status_code, download.headers["Location"]), (302, ARCHIVE_URL))

    @mock.patch("app.tournament.iso.upload_file")
    @mock.patch("app.tournament.iso.list_item_files")
    def test_same_md5_already_on_archive_is_not_sent_again(self, listing, upload):
        self.with_archive_keys()
        listing.return_value = {"WE2002 Copa.bin": {"md5": ISO_MD5}}
        self.create()
        upload.assert_not_called()
        row = self.iso_row()
        self.assertEqual(row["archive_url"], "https://archive.org/download/one-two-iso/WE2002%20Copa.bin")
        self.assertFalse(self.local_path(row).exists())

    @mock.patch("app.tournament.iso.upload_file", side_effect=ArchiveOrgError("archive.org retornou 503"))
    @mock.patch("app.tournament.iso.list_item_files", return_value={})
    def test_archive_error_keeps_local_copy_and_can_retry(self, listing, upload):
        self.with_archive_keys()
        self.create()
        row = self.iso_row()
        self.assertEqual((row["archive_status"], row["archive_error"]), ("error", "archive.org retornou 503"))
        self.assertTrue(self.local_path(row).exists())
        manage = self.client(self.owner).get("/torneios/copa-iso-1/gerenciar").get_data(as_text=True)
        self.assertIn("Enviar para o archive.org de novo", manage)

        upload.side_effect = None
        listing.side_effect = [{}, {"torneios/copa-iso-1/Copa Teste.bin": {"md5": ISO_MD5}}]
        self.client(self.owner).post("/torneios/copa-iso-1/iso/archive-org", data={"csrf_token": CSRF})
        self.assertEqual(self.iso_row()["archive_status"], "done")
        self.assertFalse(self.local_path(row).exists())

    @mock.patch("app.tournament.iso.upload_file")
    @mock.patch("app.tournament.iso.list_item_files", return_value={})
    def test_not_listed_in_time_keeps_local_copy(self, listing, upload):
        self.with_archive_keys()
        self.create()
        row = self.iso_row()
        self.assertEqual(row["archive_status"], "error")
        self.assertIn("ainda não lista", row["archive_error"])
        self.assertTrue(self.local_path(row).exists())

    def test_manage_upload_replaces_iso_and_edits_dates(self):
        self.create(iso=None)
        self.assertIsNone(self.iso_row())
        owner = self.client(self.owner)
        owner.post("/torneios/copa-iso-1/iso", data={"csrf_token": CSRF, "iso": (io.BytesIO(ISO), "Copa Teste.bin")},
                   content_type="multipart/form-data")
        first = self.iso_row()
        owner.post("/torneios/copa-iso-1/iso", data={"csrf_token": CSRF, "iso": (io.BytesIO(b"nova"), "Copa 2.iso")},
                   content_type="multipart/form-data")
        second = self.iso_row()
        self.assertEqual(second["original_filename"], "Copa 2.iso")
        self.assertFalse(self.local_path(first).exists())
        self.assertTrue(self.local_path(second).exists())

        forbidden = self.client(self.other).post("/torneios/copa-iso-1/iso", data={
            "csrf_token": CSRF, "iso": (io.BytesIO(ISO), "x.bin")}, content_type="multipart/form-data")
        self.assertEqual(forbidden.status_code, 403)

        owner.post("/torneios/copa-iso-1/editar", data={"csrf_token": CSRF, "name": "Copa ISO", "rules": "",
                                                       "starts_on": "2026-12-01", "ends_on": ""})
        tournament = self.query("SELECT starts_on, ends_on FROM tournaments")[0]
        self.assertEqual((tournament["starts_on"], tournament["ends_on"]), ("2026-12-01", None))


if __name__ == "__main__":
    unittest.main()
