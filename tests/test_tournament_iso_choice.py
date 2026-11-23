"""A ISO do campeonato escolhida na lista dos amistosos (iso_catalog.chosen_file +
iso.link): ao criar o campeonato ou em Gerenciar, em vez de enviar o arquivo. O
arquivo continua sendo da ISO de origem: nada do campeonato o apaga ou o manda
para o archive.org; com ajudantes, a troca espera a confirmação."""
import hashlib
import io
import unittest
from pathlib import Path

from app.db import get_db, now
from app.tournament import friends, iso, iso_catalog
from tests import test_tournament_edit as base  # o módulo, para o pytest não repetir os testes do TournamentEditTest

CSRF = base.CSRF
CATALOG_ISO = b"ISO do amistoso, ja enviada" * 64
UPLOADED_ISO = b"ISO enviada no campeonato" * 64


class TournamentIsoChoiceTest(unittest.TestCase):
    tearDown, client, query, mails, form, create = (
        base.TournamentEditTest.tearDown, base.TournamentEditTest.client, base.TournamentEditTest.query,
        base.TournamentEditTest.mails, base.TournamentEditTest.form, base.TournamentEditTest.create)

    def setUp(self):
        base.TournamentEditTest.setUp(self)  # owner (jog0), p1, p2 e p3
        self.app.config["SERVE_DOWNLOADS_LOCALLY"] = True
        with self.app.app_context():
            db = get_db()
            friends.request(db, self.owner, self.p1)
            friends.respond(db, self.p1, self.owner, True)
            db.commit()
        self.files = {}  # cadastro -> nome interno do arquivo (o padrão de storage.store_upload)
        self.catalog = self.add_catalog("WE 2002 Brasileirão 2026", self.games[0])

    def local(self, stored_name):
        return Path(self.app.config["DOWNLOADS_DIR"]) / stored_name

    def add_catalog(self, name, game, data=CATALOG_ISO, active=1, **columns):
        """Uma ISO enviada no menu Amistosos e liberada, com o arquivo neste servidor."""
        stored_name = hashlib.md5(name.encode()).hexdigest() + ".bin"
        folder = Path(self.app.config["DOWNLOADS_DIR"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / stored_name).write_bytes(data)
        values = {"name": name, "game_title_id": game, "stored_name": stored_name, "original_filename": name + ".bin",
                  "file_size": len(data), "md5": hashlib.md5(data + name.encode()).hexdigest(), "content_id": "ABCD1234:6C0",
                  "archive_name": f"isos/{name}.bin", "archive_status": "pending", "is_active": active,
                  "requested_by": self.p2, "approved_at": now(), "created_at": now(), "updated_at": now(), **columns}
        with self.app.app_context():
            db = get_db()
            catalog_id = db.execute(f"INSERT INTO catalog_isos ({', '.join(values)}) VALUES "
                                    f"({', '.join('?' * len(values))})", tuple(values.values())).lastrowid
            db.commit()
        self.files[catalog_id] = stored_name
        return catalog_id

    def iso_row(self, tournament_id):
        rows = self.query("SELECT * FROM tournament_isos WHERE tournament_id = ?", tournament_id)
        return rows[0] if rows else None

    def choose(self, player, slug, value):
        return self.client(player).post(f"/torneios/{slug}/iso/escolher", data={"csrf_token": CSRF, "iso": value},
                                        follow_redirects=True).get_data(as_text=True)

    def upload(self, player, slug, data=UPLOADED_ISO, name="Copa.bin"):
        return self.client(player).post(f"/torneios/{slug}/iso", data={
            "csrf_token": CSRF, "iso": (io.BytesIO(data), name)}, content_type="multipart/form-data",
            follow_redirects=True).get_data(as_text=True)

    def test_create_with_a_friendly_iso(self):
        page = self.client(self.owner).get("/torneios/novo").get_data(as_text=True)
        self.assertIn(f'data-value="c:{self.catalog}"', page)
        self.assertIn("ISO do campeonato (opcional)", page)
        response = self.client(self.owner).post("/torneios/novo", data=self.form(iso=f"c:{self.catalog}"))
        self.assertIn("/gerenciar#iso", response.headers["Location"])
        t = self.query("SELECT * FROM tournaments")[0]
        row = self.iso_row(t["id"])
        source = self.query("SELECT * FROM catalog_isos WHERE id = ?", self.catalog)[0]
        self.assertEqual((row["source"], row["stored_name"], row["original_filename"], row["md5"], row["content_id"]),
                         (f"c:{self.catalog}", self.files[self.catalog], "WE 2002 Brasileirão 2026.bin",
                          source["md5"], "ABCD1234:6C0"))
        self.assertEqual(row["uploaded_by"], self.owner)
        # Download da página do campeonato: o arquivo da ISO de origem.
        download = self.client(self.p3).get(f"/torneios/{t['slug']}/iso")
        self.assertEqual(download.data, CATALOG_ISO)
        download.close()
        manage = self.client(self.owner).get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True)
        self.assertIn("Escolhida da lista", manage)
        self.assertIn(f'href="/torneios/isos/{self.catalog}"', manage)
        self.assertNotIn("Enviar para o archive.org de novo", manage)
        flashed = self.client(self.owner).get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("WE 2002 Brasileirão 2026", flashed)
        # No convite do amistoso, a ISO aparece uma vez só (a do cadastro), não repetida pelo campeonato.
        with self.app.app_context():
            values = [o["value"] for o in iso_catalog.options(get_db(), self.p1)]
        self.assertIn(f"c:{self.catalog}", values)
        self.assertNotIn(f"t:{t['id']}", values)
        # Também sem o MD5 (ISO de Campeonato antiga, sem md5): vale a origem.
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE catalog_isos SET md5 = NULL")
            db.execute("UPDATE tournament_isos SET md5 = ''")
            db.commit()
            values = [o["value"] for o in iso_catalog.options(db, self.p1)]
        self.assertEqual(values.count(f"c:{self.catalog}"), 1)
        self.assertNotIn(f"t:{t['id']}", values)

    def test_create_without_iso_keeps_the_old_flow(self):
        response = self.client(self.owner).post("/torneios/novo", data=self.form())
        self.assertIn("/gerenciar#iso", response.headers["Location"])
        self.assertEqual(self.query("SELECT * FROM tournament_isos"), [])
        page = self.client(self.owner).get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("ou escolha uma já enviada", page)

    def test_create_refuses_other_game_hidden_or_not_ready_iso(self):
        other_game = self.add_catalog("WE 4", self.games[1])
        page = self.client(self.owner).post("/torneios/novo", data=self.form(iso=f"c:{other_game}"),
                                            follow_redirects=True).get_data(as_text=True)
        self.assertIn("escolha esse jogo base ou outra ISO", page)
        # O formulário volta com a ISO escolhida.
        self.assertIn(f'data-iso-preset="c:{other_game}"', page)
        inactive = self.add_catalog("Aguardando", self.games[0], active=0, approved_at=None)
        page = self.client(self.owner).post("/torneios/novo", data=self.form(iso=f"c:{inactive}"),
                                            follow_redirects=True).get_data(as_text=True)
        self.assertIn("ISO não encontrada", page)
        # Ainda indo para o archive.org (sem o download local deste servidor de teste): não dá ainda.
        self.app.config["SERVE_DOWNLOADS_LOCALLY"] = False
        listed = self.client(self.owner).get("/torneios/novo").get_data(as_text=True)
        self.assertNotIn(f'data-value="c:{self.catalog}"', listed)
        page = self.client(self.owner).post("/torneios/novo", data=self.form(iso=f"c:{self.catalog}"),
                                            follow_redirects=True).get_data(as_text=True)
        self.assertIn("ainda está indo para o archive.org", page)
        self.assertEqual(self.query("SELECT * FROM tournaments"), [])

    def test_manage_choose_and_upload_never_delete_the_friendly_file(self):
        t = self.create()
        self.upload(self.owner, t["slug"])
        uploaded = self.iso_row(t["id"])["stored_name"]
        page = self.client(self.owner).get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True)
        self.assertIn(f'data-value="c:{self.catalog}"', page)
        self.assertIn("Usar esta ISO", page)
        page = self.choose(self.owner, t["slug"], f"c:{self.catalog}")
        self.assertIn("a ISO do campeonato agora é", page)
        row = self.iso_row(t["id"])
        self.assertEqual((row["source"], row["stored_name"]), (f"c:{self.catalog}", self.files[self.catalog]))
        self.assertFalse(self.local(uploaded).exists())  # a enviada no campeonato era dele: sai
        # Enviar outra no lugar da escolhida não apaga o arquivo da ISO do amistoso.
        self.upload(self.owner, t["slug"], name="Copa v2.bin")
        row = self.iso_row(t["id"])
        self.assertIsNone(row["source"])
        self.assertEqual(row["original_filename"], "Copa v2.bin")
        self.assertTrue(self.local(self.files[self.catalog]).exists())
        # Outro campeonato pode escolher a ISO deste (a lista do convite também tem as de campeonato).
        other = self.create(name="Copa Dois")
        self.choose(self.owner, other["slug"], f"t:{t['id']}")
        self.assertEqual(self.iso_row(other["id"])["source"], f"t:{t['id']}")
        # A do próprio campeonato e a de outro jogo base não entram.
        self.assertIn("Esta já é a ISO do campeonato", self.choose(self.owner, t["slug"], f"t:{t['id']}"))
        other_game = self.add_catalog("WE 4", self.games[1])
        self.assertIn("escolha esse jogo base ou outra ISO", self.choose(self.owner, t["slug"], f"c:{other_game}"))
        self.assertEqual(self.client(self.p3).post(f"/torneios/{t['slug']}/iso/escolher",
                                                   data={"csrf_token": CSRF, "iso": f"c:{self.catalog}"}).status_code,
                         403)

    def test_with_helpers_the_choice_waits_for_confirmation(self):
        t = self.create()
        self.upload(self.owner, t["slug"])
        current = self.iso_row(t["id"])
        self.client(self.owner).post(f"/torneios/{t['slug']}/ajudantes",
                                     data={"csrf_token": CSRF, "helpers_sent": "1", "helper_ids": [str(self.p1)]})
        self.mails()
        page = self.choose(self.p1, t["slug"], f"c:{self.catalog}")
        self.assertIn("Alteração guardada", page)
        change, = self.query("SELECT * FROM tournament_changes")
        self.assertEqual((change["iso_source"], change["stored_name"], change["requested_by"]),
                         (f"c:{self.catalog}", self.files[self.catalog], self.p1))
        self.assertEqual(self.iso_row(t["id"])["stored_name"], current["stored_name"])
        sent = self.mails()
        self.assertIn("WE 2002 Brasileirão 2026 - WE 2002 Brasileirão 2026.bin", sent[-1][2])
        # O organizador confere o arquivo (vem da ISO de origem) e recusa: o arquivo do amistoso fica.
        owner = self.client(self.owner)
        check = owner.get(f"/torneios/{t['slug']}/alteracao/iso")
        self.assertEqual(check.data, CATALOG_ISO)
        check.close()
        manage = owner.get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True)
        self.assertNotIn("a ISO nova enviada é apagada", manage)
        owner.post(f"/torneios/{t['slug']}/alteracao/recusar", data={"csrf_token": CSRF})
        self.assertEqual(self.query("SELECT * FROM tournament_changes"), [])
        self.assertTrue(self.local(self.files[self.catalog]).exists())
        # De novo, e agora confirma: a escolhida entra e a enviada antes sai.
        self.choose(self.p1, t["slug"], f"c:{self.catalog}")
        owner.post(f"/torneios/{t['slug']}/alteracao/confirmar", data={"csrf_token": CSRF})
        row = self.iso_row(t["id"])
        self.assertEqual((row["source"], row["stored_name"], row["uploaded_by"]),
                         (f"c:{self.catalog}", self.files[self.catalog], self.p1))
        self.assertFalse(self.local(current["stored_name"]).exists())
        self.assertTrue(self.local(self.files[self.catalog]).exists())
        # A mesma que o campeonato já tem: nada a confirmar.
        self.assertIn("Ficou igual ao que já vale", self.choose(self.p1, t["slug"], f"c:{self.catalog}"))
        self.assertEqual(self.query("SELECT * FROM tournament_changes"), [])
        # Uma ISO enviada no lugar da escolhida pendente não apaga o arquivo do amistoso.
        other = self.add_catalog("Copa Sul", self.games[0])
        self.choose(self.p1, t["slug"], f"c:{other}")
        self.upload(self.p1, t["slug"], name="Copa v3.bin")
        change, = self.query("SELECT * FROM tournament_changes")
        self.assertEqual((change["iso_source"], change["original_filename"]), (None, "Copa v3.bin"))
        self.assertTrue(self.local(self.files[other]).exists())

    def test_archive_finish_updates_the_copies_and_copies_never_upload(self):
        self.app.config.update(IA_ACCESS_KEY="k", IA_SECRET_KEY="s")
        t = self.create()
        self.choose(self.owner, t["slug"], f"c:{self.catalog}")
        with self.app.app_context():
            db = get_db()
            # A cópia nunca vai para o archive.org por conta própria (nem pelo "heal").
            iso.start_archive(self.app, t["id"])
            row = iso.get(db, t["id"])
            self.assertEqual(row["archive_status"], "pending")
            db.execute("UPDATE tournament_isos SET archive_status = 'verifying', archive_updated_at = "
                       "'2026-01-01T00:00:00+00:00' WHERE tournament_id = ?", (t["id"],))
            db.commit()
            self.assertEqual(iso.heal(self.app, "tournament_isos", iso.get(db, t["id"]))["archive_status"],
                             "verifying")
            # A ISO de origem confirmada no archive.org: a cópia passa a baixar de lá.
            source = db.execute("SELECT * FROM catalog_isos WHERE id = ?", (self.catalog,)).fetchone()
            iso._finish(db, source, "catalog_isos", "isos/brasileirao.bin", True)
            row = iso.get(db, t["id"])
            self.assertEqual(row["archive_status"], "done")
            self.assertEqual(row["archive_url"], "https://archive.org/download/one-two-iso/isos/brasileirao.bin")
            self.assertIsNotNone(row["local_removed_at"])
        self.assertFalse(self.local(self.files[self.catalog]).exists())
        download = self.client(self.p3).get(f"/torneios/{t['slug']}/iso")
        self.assertEqual(download.status_code, 302)
        self.assertEqual(download.headers["Location"], "https://archive.org/download/one-two-iso/isos/brasileirao.bin")


if __name__ == "__main__":
    unittest.main()
