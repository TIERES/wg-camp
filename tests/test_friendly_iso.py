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

    def test_options_newest_registered_first(self):
        """A escolha da ISO mostra as cadastradas mais recentemente primeiro, do
        cadastro (pela liberação) e dos torneios juntas."""
        p0, _, _ = self.players
        with self.app.app_context():
            db = get_db()
            def catalog(name, created, approved=None):
                return db.execute(
                    "INSERT INTO catalog_isos (name, game_title_id, stored_name, original_filename, is_active, "
                    "approved_at, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                    (name, self.game, name + ".bin", name + ".bin", approved, created, created)).lastrowid
            old = catalog("Antiga", "2026-01-01T00:00:00+00:00")
            late = catalog("Liberada agora", "2026-01-02T00:00:00+00:00", "2026-03-01T00:00:00+00:00")
            tid = db.execute(
                "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                "settings_json, created_at, updated_at) VALUES ('copa', 'Copa', ?, ?, 'league', 'registration', 4, "
                "'{\"legs\": 1}', ?, ?)", (self.game, p0, now(), now())).lastrowid
            db.execute("INSERT INTO tournament_isos (tournament_id, stored_name, original_filename, file_size, md5, "
                       "archive_name, created_at, updated_at) VALUES (?, 'copa.bin', 'Copa.bin', 1, 'x', 'a', ?, ?)",
                       (tid, "2026-02-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"))
            db.commit()
            self.assertEqual([o["value"] for o in iso_catalog.options(db, p0)], [f"c:{late}", f"t:{tid}", f"c:{old}"])
        page = self.client(p0).get("/torneios/amistosos").get_data(as_text=True)
        self.assertLess(page.index(f'data-value="c:{late}" data-order="0"'), page.index(f'data-value="c:{old}" data-order="2"'))
        # Sem "Remover ISO": a escolhida sai clicando nela de novo. "Enviar ISO" abre a janela do envio.
        self.assertNotIn("Remover ISO", page)
        self.assertEqual(page.count("data-iso-upload"), 3)  # 2 botões + o seletor no script
        self.assertIn('id="iso-upload"', page)
        self.assertIn("A ISO vai para a administração conferir o nome e o jogo base", page)
        self.assertNotIn('<details class="iso-request"', page)

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


class PlayerIsoEditTest(unittest.TestCase):
    """Quem enviou a ISO edita nome, jogo base e arquivo: direto enquanto ela
    aguarda a liberação; já liberada, a alteração espera a aprovação da
    administração (ou 24 h). Também: o selo do menu da administração e o envio
    para o archive.org que ficou parado em "conferindo"."""

    # Os helpers do FriendlyIsoTest, sem herdar (e repetir) os testes dele.
    tearDown, client, query, mails = (FriendlyIsoTest.tearDown, FriendlyIsoTest.client, FriendlyIsoTest.query,
                                      FriendlyIsoTest.mails)
    invite, player_upload = FriendlyIsoTest.invite, FriendlyIsoTest.player_upload

    def setUp(self):
        FriendlyIsoTest.setUp(self)
        self.client(admin=True).post("/admin/torneios/email", data={"csrf_token": CSRF, "emails": "adm@wecamp.com"})
        with self.app.app_context():
            self.game2 = get_db().execute("SELECT id FROM game_titles WHERE code != 'we2002' AND is_active = 1 "
                                          "ORDER BY id").fetchone()[0]

    def edit(self, player, catalog_id, name, game=None, data=None, filename="Copa 2026 v2.bin"):
        form = {"csrf_token": CSRF, "name": name, "game_title_id": game or self.game}
        if data is not None:
            form["iso"] = (io.BytesIO(data), filename)
        return self.client(player).post(f"/torneios/amistosos/isos/{catalog_id}/editar", data=form,
                                        content_type="multipart/form-data")

    def released(self):
        """ISO enviada pelo p0 e já liberada pela administração."""
        self.player_upload(self.players[0])
        row = self.query("SELECT * FROM catalog_isos")[0]
        self.client(admin=True).post(f"/admin/torneios/isos/{row['id']}", data={
            "csrf_token": CSRF, "name": row["name"], "game_title_id": self.game, "is_active": "on", "liberar": "1"})
        self.mails()
        return self.query("SELECT * FROM catalog_isos")[0]

    def test_pending_iso_edit_applies_directly(self):
        p0 = self.players[0]
        self.player_upload(p0)
        row = self.query("SELECT * FROM catalog_isos")[0]
        self.mails()
        response = self.edit(p0, row["id"], "Copa 2026 Oficial", self.game2)
        self.assertTrue(response.headers["Location"].endswith("#minhas-isos"))
        after = self.query("SELECT * FROM catalog_isos")[0]
        self.assertEqual((after["name"], after["game_title_id"], after["approved_at"], after["is_active"]),
                         ("Copa 2026 Oficial", self.game2, None, 0))
        self.assertEqual(self.query("SELECT COUNT(*) FROM catalog_iso_revisions")[0][0], 0)
        (to, subject, body), = self.mails()
        self.assertIn("adm@wecamp.com", to)
        self.assertIn("que aguarda liberação", subject)
        self.assertIn("Nome: WE 2002 Copa 2026 → Copa 2026 Oficial", body)
        # Nada mudou: recusa.
        self.edit(p0, row["id"], "Copa 2026 Oficial", self.game2)
        self.assertEqual(self.mails(), [])

    def test_released_iso_edit_waits_for_admin_approval(self):
        p0, p1, _ = self.players
        row = self.released()
        old_local = Path(self.app.config["DOWNLOADS_DIR"]) / row["stored_name"]
        new_data = b"ISO nova versao" * 64
        self.edit(p0, row["id"], "Copa 2026 v2", data=new_data)
        # A ISO continua como estava até a aprovação; a alteração fica guardada.
        current = self.query("SELECT * FROM catalog_isos")[0]
        self.assertEqual((current["name"], current["stored_name"]), (row["name"], row["stored_name"]))
        rev = self.query("SELECT * FROM catalog_iso_revisions")[0]
        self.assertEqual((rev["name"], rev["original_filename"], rev["approved_at"]),
                         ("Copa 2026 v2", "Copa 2026 v2.bin", None))
        self.assertEqual(rev["archive_name"], "isos/Copa 2026 v2.bin")
        (to, subject, body), = self.mails()
        self.assertIn("adm@wecamp.com", to)
        self.assertIn("aprove a alteração", subject)
        self.assertIn("Arquivo: Copa 2026.bin → Copa 2026 v2.bin", body)
        self.assertIn("a alteração é aprovada automaticamente", body)
        page = self.client(p0).get("/torneios/amistosos").get_data(as_text=True)
        self.assertIn("Alteração em análise", page)
        self.assertIn(f'data-dialog="cancelar-alteracao-{row["id"]}"', page)

        # Administração: selo no menu (nome e "ISOs para amistosos") e a alteração na página da ISO.
        admin = self.client(admin=True)
        listing = admin.get("/admin/torneios/isos").get_data(as_text=True)
        self.assertEqual(listing.count('<span class="todo-badge"'), 2)
        self.assertIn("Alterações aguardando aprovação (1)", listing)
        edit_page = admin.get(f"/admin/torneios/isos/{row['id']}").get_data(as_text=True)
        self.assertIn("Aprovar alteração", edit_page)
        download = admin.get(f"/admin/torneios/isos/{row['id']}/alteracao/arquivo")
        self.assertEqual(download.data, new_data)
        download.close()
        # Antes da aprovação, o amistoso ainda baixa a versão atual.
        friendly = self.invite(f"c:{row['id']}")
        download = self.client(p1).get(f"/torneios/amistosos/{friendly['id']}/iso")
        self.assertEqual(download.data, ISO)
        download.close()

        admin.post(f"/admin/torneios/isos/{row['id']}/alteracao/aprovar", data={"csrf_token": CSRF})
        after = self.query("SELECT * FROM catalog_isos")[0]
        self.assertEqual((after["name"], after["original_filename"], after["stored_name"]),
                         ("Copa 2026 v2", "Copa 2026 v2.bin", rev["stored_name"]))
        self.assertIsNotNone(after["approved_at"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM catalog_iso_revisions")[0][0], 0)
        self.assertFalse(old_local.exists())
        sent = self.mails()
        self.assertTrue(any("p0@x.com" in to and "foi aprovada" in subject for to, subject, _ in sent))
        self.assertNotIn('<span class="todo-badge"', admin.get("/admin/torneios/isos").get_data(as_text=True))
        download = self.client(p1).get(f"/torneios/amistosos/{friendly['id']}/iso")
        self.assertEqual(download.data, new_data)
        download.close()

    def test_approval_waits_for_the_new_file_upload(self):
        """Aprovada com o arquivo novo ainda indo para o archive.org: aplica quando o envio termina."""
        p0 = self.players[0]
        row = self.released()
        self.edit(p0, row["id"], "Copa 2026 v3", data=b"v3" * 512)
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE catalog_iso_revisions SET archive_status = 'uploading', archive_updated_at = ?", (now(),))
            db.commit()
        self.client(admin=True).post(f"/admin/torneios/isos/{row['id']}/alteracao/aprovar", data={"csrf_token": CSRF})
        self.assertEqual(self.query("SELECT name FROM catalog_isos")[0][0], row["name"])
        self.assertIsNotNone(self.query("SELECT approved_at FROM catalog_iso_revisions")[0][0])
        with self.app.app_context():
            db = get_db()
            self.assertEqual(iso_catalog.apply_due(db, self.app), [])
            db.execute("UPDATE catalog_iso_revisions SET archive_status = 'done', local_removed_at = ?, "
                       "archive_url = 'https://archive.org/v3'", (now(),))
            db.commit()
            (applied, auto), = iso_catalog.apply_due(db, self.app)
            self.assertFalse(auto)
        after = self.query("SELECT name, archive_url, archive_status FROM catalog_isos")[0]
        self.assertEqual(tuple(after), ("Copa 2026 v3", "https://archive.org/v3", "done"))

    def test_released_iso_edit_is_auto_approved_after_24h(self):
        from datetime import datetime, timedelta, timezone
        from app.tournament import notify
        p0 = self.players[0]
        row = self.released()
        self.edit(p0, row["id"], "Copa 2026 Renomeada", self.game2)
        self.mails()
        with self.app.test_request_context():  # os e-mails montam links (url_for)
            db = get_db()
            self.assertEqual(iso_catalog.apply_due(db, self.app), [])  # ainda no prazo
            later = datetime.now(timezone.utc) + timedelta(hours=iso_catalog.AUTO_APPROVE_HOURS, minutes=1)
            (applied, auto), = iso_catalog.apply_due(db, self.app, at=later)
            self.assertTrue(auto)
            notify.iso_edit_approved(db, applied, auto=auto)
        after = self.query("SELECT name, game_title_id FROM catalog_isos")[0]
        self.assertEqual(tuple(after), ("Copa 2026 Renomeada", self.game2))
        sent = self.mails()
        self.assertEqual(len(sent), 2)
        self.assertTrue(any("p0@x.com" in to and "aprovada automaticamente" in body for to, _, body in sent))
        self.assertTrue(any("adm@wecamp.com" in to and "aprovada automaticamente" in subject for to, subject, _ in sent))

    def test_admin_rejects_edit_and_player_can_cancel(self):
        p0 = self.players[0]
        row = self.released()
        self.edit(p0, row["id"], "Outro nome", data=b"ISO recusada" * 64)
        rev = self.query("SELECT * FROM catalog_iso_revisions")[0]
        new_local = Path(self.app.config["DOWNLOADS_DIR"]) / rev["stored_name"]
        self.assertTrue(new_local.exists())
        self.mails()
        self.client(admin=True).post(f"/admin/torneios/isos/{row['id']}/alteracao/recusar",
                                     data={"csrf_token": CSRF, "motivo": "Arquivo errado"})
        self.assertEqual(self.query("SELECT COUNT(*) FROM catalog_iso_revisions")[0][0], 0)
        self.assertFalse(new_local.exists())
        self.assertEqual(self.query("SELECT name FROM catalog_isos")[0][0], row["name"])
        (to, subject, body), = self.mails()
        self.assertIn("não foi aprovada", subject)
        self.assertIn("Motivo: Arquivo errado", body)
        # O jogador desiste de uma alteração.
        self.edit(p0, row["id"], "Mais um nome")
        self.client(p0).post(f"/torneios/amistosos/isos/{row['id']}/cancelar-alteracao", data={"csrf_token": CSRF})
        self.assertEqual(self.query("SELECT COUNT(*) FROM catalog_iso_revisions")[0][0], 0)

    def test_edit_rules(self):
        p0, p1, _ = self.players
        row = self.released()
        # Só quem enviou edita.
        self.edit(p1, row["id"], "Invasor")
        self.assertEqual(self.query("SELECT COUNT(*) FROM catalog_iso_revisions")[0][0], 0)
        # O mesmo arquivo de novo, sem mudar o nome: nada foi alterado (e a cópia nova é apagada).
        self.edit(p0, row["id"], row["name"], data=ISO)
        self.assertEqual(self.query("SELECT COUNT(*) FROM catalog_iso_revisions")[0][0], 0)
        self.assertEqual(len(list(Path(self.app.config["DOWNLOADS_DIR"]).iterdir())), 1)
        # Editar de novo substitui a pendente; voltar tudo ao atual descarta.
        self.edit(p0, row["id"], "Nome A")
        self.edit(p0, row["id"], "Nome B")
        self.assertEqual([r[0] for r in self.query("SELECT name FROM catalog_iso_revisions")], ["Nome B"])
        self.edit(p0, row["id"], row["name"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM catalog_iso_revisions")[0][0], 0)

    def test_new_file_with_a_taken_name_goes_to_a_subfolder(self):
        """Outro arquivo com o mesmo nome não pode substituir o de outra ISO no archive.org."""
        self.player_upload(self.players[0])
        self.player_upload(self.players[0], data=b"outro conteudo" * 64)
        names = [r[0] for r in self.query("SELECT archive_name FROM catalog_isos ORDER BY id")]
        self.assertEqual(names, ["isos/Copa 2026.bin", "isos/2/Copa 2026.bin"])

    @mock.patch("app.tournament.iso.list_item_files")
    def test_stuck_verifying_is_resumed(self, list_files):
        """A thread da conferência morreu (servidor reiniciado, banco travado): a
        consulta da barra de progresso recomeça a conferência e termina."""
        self.app.config.update(IA_ACCESS_KEY="k", IA_SECRET_KEY="s", ARCHIVE_VERIFY_INTERVAL=0,
                               ARCHIVE_VERIFY_TIMEOUT=0)
        list_files.return_value = {}
        self.player_upload(self.players[0])  # sem o MD5 no archive.org (mock): erro
        row = self.query("SELECT * FROM catalog_isos")[0]
        local = Path(self.app.config["DOWNLOADS_DIR"]) / row["stored_name"]
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE catalog_isos SET archive_status = 'verifying', archive_name = 'isos/Copa 2026.bin', "
                       "archive_url = 'https://archive.org/x', archive_updated_at = '2026-11-11T18:40:00+00:00', "
                       "archive_error = NULL, local_removed_at = NULL")
            db.commit()
        list_files.return_value = {"isos/Copa 2026.bin": {"md5": row["md5"], "crc32": None, "size": 1, "mtime": 1}}
        state = self.client(self.players[0]).get(f"/torneios/isos/{row['id']}/status").get_json()
        self.assertEqual(state["state"], "done")
        after = self.query("SELECT archive_status, local_removed_at FROM catalog_isos")[0]
        self.assertEqual(after["archive_status"], "done")
        self.assertIsNotNone(after["local_removed_at"])
        self.assertFalse(local.exists())
        # Recente (a thread pode estar viva): não mexe.
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE catalog_isos SET archive_status = 'verifying', archive_updated_at = ?, "
                       "local_removed_at = NULL", (now(),))
            db.commit()
        self.assertEqual(self.client(self.players[0]).get(f"/torneios/isos/{row['id']}/status").get_json()["state"],
                         "verifying")

    @mock.patch("app.tournament.iso.list_item_files", return_value={})
    @mock.patch("app.tournament.iso.upload_file")
    def test_archive_thread_error_never_leaves_iso_stuck(self, _upload, _list):
        """Um erro inesperado no fim do envio ("database is locked") deixa a ISO
        com erro (dá para enviar de novo), não parada em "conferindo"."""
        self.app.config.update(IA_ACCESS_KEY="k", IA_SECRET_KEY="s")
        with mock.patch("app.tournament.iso._wait_for_md5",
                        side_effect=sqlite3.OperationalError("database is locked")):
            self.player_upload(self.players[0])
        row = self.query("SELECT archive_status, archive_error FROM catalog_isos")[0]
        self.assertEqual(row["archive_status"], "error")
        self.assertIn("database is locked", row["archive_error"])


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
