import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.archive_org import _ProgressReader
from app.db import get_db, init_db, now
from app.tournament import friends, iso_catalog
from app.tournament.store import TournamentError
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"
ISO = b"PS1 ISO do amistoso" * 64


class FriendlyIsoTest(unittest.TestCase):
    """ISO do amistoso: cadastro da administração (upload ou ISO de Campeonato),
    ISOs dos torneios, escolha no convite, download e pedido de cadastro."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.app.config["SERVE_DOWNLOADS_LOCALLY"] = True
        with self.app.app_context():
            init_db()
            db = get_db()
            self.players = [add_player(db, f"p{i}") for i in range(3)]
            friends.request(db, self.players[0], self.players[1])
            friends.respond(db, self.players[1], self.players[0], True)
            db.execute("INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('adm', 'x', ?, ?)",
                       (now(), now()))
            self.game = db.execute("SELECT id FROM game_titles WHERE code = 'we2002'").fetchone()[0]
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def client(self, player=None, admin=False):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            if player:
                session["player_id"] = player
            if admin:
                session["user_id"] = 1
        return client

    def query(self, sql, *args):
        with self.app.app_context():
            return get_db().execute(sql, args).fetchall()

    def mails(self):
        box = self.app.extensions.setdefault("mail_outbox", [])
        sent = [(m["To"], m["Subject"], m.get_content()) for m in box]
        box.clear()
        return sent

    def stored_file(self, name, data=ISO):
        folder = Path(self.app.config["DOWNLOADS_DIR"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)

    def invite(self, iso_value):
        p0, p1, _ = self.players
        self.client(p0).post("/torneios/amistosos/novo", data={"csrf_token": CSRF, "amigo": p1, "iso": iso_value})
        return self.query("SELECT * FROM friendly_matches ORDER BY id DESC")[0]

    def test_admin_uploads_iso_and_friendly_invite_has_download(self):
        admin = self.client(admin=True)
        response = admin.post("/admin/torneios/isos/nova", data={
            "csrf_token": CSRF, "name": "WE 2002 Brasileirão", "game_title_id": self.game, "is_active": "on",
            "iso": (io.BytesIO(ISO), "WE2002 BR.bin")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        row = self.query("SELECT * FROM catalog_isos")[0]
        self.assertEqual((row["name"], row["original_filename"], row["archive_name"]),
                         ("WE 2002 Brasileirão", "WE2002 BR.bin", "isos/WE2002 BR.bin"))
        self.assertIn("WE 2002 Brasileirão", admin.get("/admin/torneios/isos").get_data(as_text=True))

        p0, p1, p2 = self.players
        page = self.client(p0).get("/torneios/amistosos").get_data(as_text=True)
        self.assertIn(f'data-value="c:{row["id"]}"', page)
        self.assertIn("Não encontrou a ISO?", page)
        self.mails()
        friendly = self.invite(f"c:{row['id']}")
        self.assertEqual((friendly["iso_catalog_id"], friendly["game_title_id"]), (row["id"], self.game))
        (to, _, body), = self.mails()
        self.assertIn("p1@x.com", to)
        self.assertIn("ISO: WE 2002 Brasileirão (WE2002 BR.bin)", body)
        shown = self.client(p1).get(f"/torneios/amistosos/{friendly['id']}").get_data(as_text=True)
        self.assertIn("Download ISO", shown)
        self.assertIn("Winning Eleven 2002", shown)
        download = self.client(p1).get(f"/torneios/amistosos/{friendly['id']}/iso")
        self.assertEqual(download.data, ISO)
        download.close()
        self.assertEqual(self.client(p2).get(f"/torneios/amistosos/{friendly['id']}/iso").status_code, 404)

    def test_admin_iso_from_championship_file(self):
        with self.app.app_context():
            db = get_db()
            champ = db.execute("INSERT INTO championships (slug, name, created_at, updated_at) "
                               "VALUES ('copa', 'Copa Antiga', ?, ?)", (now(), now())).lastrowid
            file_id = db.execute(
                "INSERT INTO files (championship_id, display_name, stored_name, original_filename, file_type, file_size, "
                "created_at, updated_at) VALUES (?, 'ISO da Copa', 'copa.bin', 'Copa.bin', 'iso', ?, ?, ?)",
                (champ, len(ISO), now(), now())).lastrowid
            db.commit()
        self.stored_file("copa.bin")
        admin = self.client(admin=True)
        self.assertIn("Copa Antiga: ISO da Copa (Copa.bin)", admin.get("/admin/torneios/isos/nova").get_data(as_text=True))
        admin.post("/admin/torneios/isos/nova", data={"csrf_token": CSRF, "name": "Copa", "game_title_id": self.game,
                                                       "file_id": file_id, "is_active": "on"})
        row = self.query("SELECT * FROM catalog_isos")[0]
        self.assertEqual((row["file_id"], row["stored_name"]), (file_id, None))
        self.assertEqual(admin.get(f"/admin/files/{file_id}/archive-status").get_json(),
                         {"state": "local", "configured": False})
        friendly = self.invite(f"c:{row['id']}")
        download = self.client(self.players[0]).get(f"/torneios/amistosos/{friendly['id']}/iso")
        self.assertEqual(download.data, ISO)
        download.close()

    def test_admin_needs_name_game_and_file(self):
        admin = self.client(admin=True)
        page = admin.post("/admin/torneios/isos/nova", data={"csrf_token": CSRF, "name": "Sem arquivo",
                                                              "game_title_id": self.game}).get_data(as_text=True)
        self.assertIn("Envie o arquivo da ISO", page)
        self.assertEqual(self.query("SELECT COUNT(*) FROM catalog_isos")[0][0], 0)
        self.assertEqual(self.client(self.players[0]).get("/admin/torneios/isos/nova").status_code, 302)

    def test_tournament_iso_is_offered_only_to_who_can_see_the_tournament(self):
        p0, _, p2 = self.players
        with self.app.app_context():
            db = get_db()
            tid = db.execute(
                "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                "settings_json, friends_only, created_at, updated_at) VALUES ('copa-amigos', 'Copa dos Amigos', ?, ?, "
                "'league', 'registration', 4, '{\"legs\": 1}', 1, ?, ?)", (self.game, p2, now(), now())).lastrowid
            db.execute("INSERT INTO tournament_isos (tournament_id, stored_name, original_filename, file_size, md5, "
                       "archive_name, created_at, updated_at) VALUES (?, 'amigos.bin', 'Amigos.bin', ?, 'x', 'a', ?, ?)",
                       (tid, len(ISO), now(), now()))
            db.commit()
            self.assertNotIn(f"t:{tid}", [o["value"] for o in iso_catalog.options(db, p0)])
            with self.assertRaises(TournamentError):
                iso_catalog.resolve(db, p0, f"t:{tid}")
            friends.request(db, p0, p2)
            friends.respond(db, p2, p0, True)
            db.commit()
            self.assertIn(f"t:{tid}", [o["value"] for o in iso_catalog.options(db, p0)])
        self.stored_file("amigos.bin")
        self.assertEqual(self.client(p2).get("/torneios/copa-amigos/iso/status").get_json()["state"], "local")
        self.assertEqual(self.client(p0).get("/torneios/copa-amigos/iso/status").status_code, 403)
        friendly = self.invite(f"t:{tid}")
        self.assertEqual((friendly["iso_tournament_id"], friendly["game_title_id"]), (tid, self.game))
        self.assertIn("Copa dos Amigos", self.client(p0).get(f"/torneios/amistosos/{friendly['id']}").get_data(as_text=True))
        download = self.client(p0).get(f"/torneios/amistosos/{friendly['id']}/iso")
        self.assertEqual(download.data, ISO)
        download.close()

    def test_friendly_without_iso(self):
        friendly = self.invite("")
        self.assertEqual((friendly["iso_catalog_id"], friendly["iso_tournament_id"], friendly["game_title_id"]),
                         (None, None, None))
        shown = self.client(self.players[1]).get(f"/torneios/amistosos/{friendly['id']}").get_data(as_text=True)
        self.assertNotIn("Download ISO", shown)

    def player_upload(self, player, name="WE 2002 Copa 2026", data=ISO):
        return self.client(player).post("/torneios/amistosos/isos/enviar", data={
            "csrf_token": CSRF, "name": name, "game_title_id": self.game, "iso": (io.BytesIO(data), "Copa 2026.bin")},
            content_type="multipart/form-data")

    def test_player_uploads_iso_and_admin_releases_it(self):
        p0, p1, _ = self.players
        self.client(admin=True).post("/admin/torneios/email", data={"csrf_token": CSRF, "emails": "adm@wecamp.com"})
        self.mails()
        response = self.player_upload(p0)
        self.assertTrue(response.headers["Location"].endswith("/torneios/amistosos#minhas-isos"))
        row = self.query("SELECT * FROM catalog_isos")[0]
        self.assertEqual((row["requested_by"], row["approved_at"], row["is_active"], row["original_filename"]),
                         (p0, None, 0, "Copa 2026.bin"))
        (to, subject, body), = self.mails()
        self.assertIn("adm@wecamp.com", to)
        self.assertIn("p0 enviou uma ISO para liberar", subject)
        self.assertIn("Jogo base: Winning Eleven 2002", body)
        # Aguardando liberação: não aparece no convite, aparece para quem enviou.
        page = self.client(p0).get("/torneios/amistosos").get_data(as_text=True)
        self.assertNotIn(f'data-value="c:{row["id"]}"', page)
        self.assertIn("Aguardando liberação", page)
        self.assertEqual(self.client(p0).get(f"/torneios/isos/{row['id']}/status").get_json(),
                         {"state": "local", "configured": False})
        self.assertEqual(self.client(p1).get(f"/torneios/isos/{row['id']}/status").status_code, 404)
        admin = self.client(admin=True)
        self.assertIn("Aguardando liberação (1)", admin.get("/admin/torneios/isos").get_data(as_text=True))
        edit = admin.get(f"/admin/torneios/isos/{row['id']}").get_data(as_text=True)
        self.assertIn("Liberar ISO", edit)
        # "Salvar sem liberar" mantém inativa.
        admin.post(f"/admin/torneios/isos/{row['id']}", data={"csrf_token": CSRF, "name": "Copa 2026",
                                                               "game_title_id": self.game, "is_active": "on"})
        self.assertEqual(self.query("SELECT is_active, approved_at FROM catalog_isos")[0][:], (0, None))
        self.mails()
        admin.post(f"/admin/torneios/isos/{row['id']}", data={"csrf_token": CSRF, "name": "WE 2002 Copa Oficial",
                                                               "game_title_id": self.game, "is_active": "on",
                                                               "liberar": "1"})
        row = self.query("SELECT * FROM catalog_isos")[0]
        self.assertEqual((row["name"], row["is_active"]), ("WE 2002 Copa Oficial", 1))
        self.assertIsNotNone(row["approved_at"])
        (to, subject, _), = self.mails()
        self.assertIn("p0@x.com", to)
        self.assertIn("foi liberada", subject)
        self.assertIn(f'data-value="c:{row["id"]}"', self.client(p1).get("/torneios/amistosos").get_data(as_text=True))

    def test_admin_rejects_player_iso(self):
        p0 = self.players[0]
        self.player_upload(p0)
        row = self.query("SELECT * FROM catalog_isos")[0]
        stored = Path(self.app.config["DOWNLOADS_DIR"]) / row["stored_name"]
        self.assertTrue(stored.exists())
        self.mails()
        self.client(admin=True).post(f"/admin/torneios/isos/{row['id']}/recusar",
                                     data={"csrf_token": CSRF, "motivo": "ISO corrompida"})
        self.assertEqual(self.query("SELECT COUNT(*) FROM catalog_isos")[0][0], 0)
        self.assertFalse(stored.exists())
        (to, subject, body), = self.mails()
        self.assertIn("não foi liberada", subject)
        self.assertIn("Motivo: ISO corrompida", body)

    def test_player_upload_needs_name_game_and_file(self):
        p0 = self.players[0]
        self.client(p0).post("/torneios/amistosos/isos/enviar", data={"csrf_token": CSRF, "name": "X",
                                                                       "game_title_id": self.game})
        self.assertEqual(self.query("SELECT COUNT(*) FROM catalog_isos")[0][0], 0)
        self.player_upload(p0, name="")
        self.assertEqual(self.query("SELECT COUNT(*) FROM catalog_isos")[0][0], 0)

    @mock.patch("app.tournament.iso.list_item_files", return_value={})
    @mock.patch("app.tournament.iso.upload_file")
    def test_archive_progress_is_recorded(self, upload_file, _list):
        self.app.config.update(IA_ACCESS_KEY="k", IA_SECRET_KEY="s", ARCHIVE_VERIFY_INTERVAL=0,
                               ARCHIVE_VERIFY_TIMEOUT=0)
        seen = []
        database = self.app.config["DATABASE"]

        def fake_upload(*args, progress=None, **kwargs):
            progress(256, 1024)
            other = sqlite3.connect(database)
            try:
                seen.append(other.execute("SELECT archive_status, archive_progress FROM catalog_isos").fetchone())
            finally:
                other.close()
            progress(1024, 1024)
            return "https://archive.org/x"

        upload_file.side_effect = fake_upload
        self.player_upload(self.players[0])
        self.assertEqual(seen, [("uploading", 25)])
        row = self.query("SELECT archive_status, archive_progress FROM catalog_isos")[0]
        # O archive.org ainda não lista o MD5 (mock): fica com erro, mas o envio chegou a 100%.
        self.assertEqual(tuple(row), ("error", 100))
        state = self.client(self.players[0]).get("/torneios/isos/1/status").get_json()
        self.assertEqual(state["state"], "error")


class ProgressReaderTest(unittest.TestCase):
    def test_reports_bytes_read(self):
        calls = []
        reader = _ProgressReader(io.BytesIO(b"x" * 10), 10, lambda sent, total: calls.append((sent, total)),
                                 interval=3600)
        while reader.read(4):
            pass
        self.assertEqual(calls[0], (4, 10))  # o primeiro bloco sempre avisa
        self.assertEqual(calls[-1], (10, 10))


if __name__ == "__main__":
    unittest.main()
