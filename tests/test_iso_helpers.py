"""ISO dos amistosos com ajudantes (app/tournament/editors.py): quem enviou
escolhe amigos para ajudar a editar; a imagem vai no envio e na edição; numa
ISO já liberada, a alteração de um responsável entra quando OUTRO responsável
confirma (o e-mail leva o antes e o depois), sem depender da administração nem
do prazo. E a página da ISO, com o link para compartilhar e o download só para
quem está logado."""
import io
import unittest
from pathlib import Path

from app.db import get_db, now
from app.tournament import iso_catalog, notify
from tests import test_friendly_iso as base  # o módulo, para o pytest não repetir os testes do FriendlyIsoTest

CSRF, ISO = base.CSRF, base.ISO

PNG = b"\x89PNG\r\n\x1a\n" + b"imagem da iso" * 20
PNG2 = b"\x89PNG\r\n\x1a\n" + b"outra imagem" * 20
JPG = b"\xff\xd8\xff" + b"cartao do link" * 20
NEW_ISO = b"ISO nova versao" * 64


class IsoHelpersTest(unittest.TestCase):
    # Os helpers do FriendlyIsoTest, sem herdar (e repetir) os testes dele.
    tearDown, client, query, mails = (base.FriendlyIsoTest.tearDown, base.FriendlyIsoTest.client,
                                      base.FriendlyIsoTest.query, base.FriendlyIsoTest.mails)

    def setUp(self):
        base.FriendlyIsoTest.setUp(self)  # p0 e p1 amigos; p2 não é amigo de ninguém
        self.client(admin=True).post("/admin/torneios/email", data={"csrf_token": CSRF, "emails": "adm@wecamp.com"})
        self.p0, self.p1, self.p2 = self.players
        self.mails()

    def upload(self, player, helpers=(), image=None, name="WE 2002 Copa 2026"):
        data = {"csrf_token": CSRF, "name": name, "game_title_id": self.game, "helpers_sent": "1",
                "helper_ids": [str(h) for h in helpers], "iso": (io.BytesIO(ISO), "Copa 2026.bin")}
        if image:
            data["image"] = (io.BytesIO(image), "capa.png")
        return self.client(player).post("/torneios/amistosos/isos/enviar", data=data,
                                        content_type="multipart/form-data", follow_redirects=True)

    def edit(self, player, catalog_id, name, data=None, image=None, helpers=None, remove_image=False):
        form = {"csrf_token": CSRF, "name": name, "game_title_id": self.game}
        if data is not None:
            form["iso"] = (io.BytesIO(data), "Copa 2026 v2.bin")
        if image is not None:
            form["image"] = (io.BytesIO(image), "capa2.png")
        if helpers is not None:
            form.update(helpers_sent="1", helper_ids=[str(h) for h in helpers])
        if remove_image:
            form["remove_image"] = "on"
        return self.client(player).post(f"/torneios/amistosos/isos/{catalog_id}/editar", data=form,
                                        content_type="multipart/form-data", follow_redirects=True)

    def released(self, helpers=(), image=PNG):
        """ISO enviada pelo p0 (com os ajudantes) e já liberada pela administração."""
        self.upload(self.p0, helpers, image)
        row = self.iso()
        self.client(admin=True).post(f"/admin/torneios/isos/{row['id']}", data={
            "csrf_token": CSRF, "name": row["name"], "game_title_id": self.game, "is_active": "on", "liberar": "1"})
        self.mails()
        return self.iso()

    def iso(self):
        return self.query("SELECT * FROM catalog_isos ORDER BY id DESC")[0]

    def revisions(self):
        return self.query("SELECT * FROM catalog_iso_revisions")

    def image_file(self, name):
        return Path(self.app.config["TOURNAMENT_IMAGES_DIR"]) / name

    def act(self, player, catalog_id, action, **extra):
        return self.client(player).post(f"/torneios/isos/{catalog_id}/alteracao/{action}",
                                        data={"csrf_token": CSRF, **extra}, follow_redirects=True)

    def test_upload_with_image_and_helpers(self):
        page = self.upload(self.p0, [self.p1], PNG).get_data(as_text=True)
        self.assertIn("ISO recebida!", page)
        row = self.iso()
        self.assertTrue(self.image_file(row["image"]).exists())
        self.assertEqual([r["player_id"] for r in self.query("SELECT * FROM catalog_iso_editors")], [self.p1])
        sent = self.mails()
        admin = next(body for to, subject, body in sent if "adm@wecamp.com" in to)
        self.assertIn("Imagem: http", admin)
        self.assertIn("Ajudantes: p1", admin)
        self.assertTrue(any("p1@x.com" in to and "adicionou você como ajudante da ISO" in subject
                            for to, subject, _ in sent))
        # O ajudante vê a ISO em "Minhas ISOs"; quem não é responsável, não.
        helper_page = self.client(self.p1).get("/torneios/amistosos").get_data(as_text=True)
        self.assertIn("Você ajuda a editar", helper_page)
        self.assertIn("enviada por p0", helper_page)
        self.assertNotIn("Minhas ISOs", self.client(self.p2).get("/torneios/amistosos").get_data(as_text=True))

    def test_only_friends_can_be_helpers_and_nothing_is_stored(self):
        page = self.upload(self.p0, [self.p2], PNG).get_data(as_text=True)
        self.assertIn("Só os amigos de quem enviou a ISO podem ajudar a editar.", page)
        self.assertEqual(self.query("SELECT COUNT(*) FROM catalog_isos")[0][0], 0)
        self.assertEqual(list(Path(self.app.config["DOWNLOADS_DIR"]).iterdir()), [])
        images = Path(self.app.config["TOURNAMENT_IMAGES_DIR"])
        self.assertEqual(list(images.iterdir()) if images.exists() else [], [])

    def test_helper_edit_is_confirmed_by_the_owner(self):
        row = self.released([self.p1])
        old_image, old_local = self.image_file(row["image"]), Path(self.app.config["DOWNLOADS_DIR"]) / row["stored_name"]
        page = self.edit(self.p1, row["id"], "Copa 2026 v2", data=NEW_ISO, image=PNG2).get_data(as_text=True)
        self.assertIn("p0 recebeu o e-mail com o antes e o depois", page)
        # A ISO continua como estava; a alteração fica guardada, com a imagem nova.
        self.assertEqual(self.iso()["name"], row["name"])
        rev, = self.revisions()
        self.assertEqual((rev["requested_by"], rev["name"], rev["image_change"], rev["approved_at"]),
                         (self.p1, "Copa 2026 v2", 1, None))
        self.assertTrue(self.image_file(rev["image"]).exists())
        sent = self.mails()
        self.assertEqual(sorted(to for to, _, _ in sent), ["adm@wecamp.com", "p0@x.com"])
        to, subject, body = next(m for m in sent if "p0@x.com" in m[0])
        self.assertIn("Confirme a alteração de p1 na ISO WE 2002 Copa 2026", subject)
        self.assertIn("Nome: WE 2002 Copa 2026 → Copa 2026 v2", body)
        self.assertIn("Arquivo: Copa 2026.bin → Copa 2026 v2.bin", body)
        self.assertIn(f"/imagens/torneio/{row['image']}", body)   # a imagem de antes...
        self.assertIn(f"/imagens/torneio/{rev['image']}", body)   # ...e a de depois
        self.assertIn(f"/torneios/isos/{row['id']}/alteracao", body)
        self.assertIn("também receberam o pedido", next(b for t, _, b in sent if "adm@wecamp.com" in t))

        # Quem fez a alteração não confirma a própria; quem não é responsável, também não.
        self.assertIn("precisa ser confirmada por outro responsável",
                      self.act(self.p1, row["id"], "confirmar").get_data(as_text=True))
        self.assertIn("ISO não encontrada", self.act(self.p2, row["id"], "confirmar").get_data(as_text=True))
        self.assertEqual(len(self.revisions()), 1)
        # O dono vê o pedido em Minhas ISOs e na página da ISO (o link do e-mail pede o login).
        self.assertIn("Confirme a alteração", self.client(self.p0).get("/torneios/amistosos").get_data(as_text=True))
        link = self.client().get(f"/torneios/isos/{row['id']}/alteracao")
        self.assertIn("/conta/entrar", link.headers["Location"])
        iso_page = self.client(self.p0).get(f"/torneios/isos/{row['id']}").get_data(as_text=True)
        self.assertIn("Confirmar alteração", iso_page)
        self.assertIn("Copa 2026 v2.bin", iso_page)
        self.assertNotIn("Confirmar alteração", self.client(self.p1).get(f"/torneios/isos/{row['id']}").get_data(as_text=True))
        download = self.client(self.p0).get(f"/torneios/isos/{row['id']}/alteracao/arquivo")
        self.assertEqual(download.data, NEW_ISO)
        download.close()

        # O dono confirma: entra na hora, sem a administração e sem o prazo.
        self.assertIn("Alteração confirmada", self.act(self.p0, row["id"], "confirmar").get_data(as_text=True))
        after = self.iso()
        self.assertEqual((after["name"], after["original_filename"], after["image"], after["share_image"]),
                         ("Copa 2026 v2", "Copa 2026 v2.bin", rev["image"], None))
        self.assertEqual(self.revisions(), [])
        self.assertFalse(old_image.exists())
        self.assertFalse(old_local.exists())
        (to, subject, body), = self.mails()  # só quem fez a alteração é avisado
        self.assertIn("p1@x.com", to)
        self.assertIn("foi aprovada", subject)
        self.assertIn("p0 confirmou a sua alteração", body)

    def test_owner_edit_is_confirmed_by_a_helper(self):
        row = self.released([self.p1])
        self.edit(self.p0, row["id"], "Copa do Dono")
        sent = self.mails()
        self.assertTrue(any("p1@x.com" in to and "Confirme a alteração de p0" in subject for to, subject, _ in sent))
        self.assertIn("Alteração em análise", self.client(self.p0).get("/torneios/amistosos").get_data(as_text=True))
        self.assertIn("Você alterou esta ISO", self.client(self.p0).get(f"/torneios/isos/{row['id']}").get_data(as_text=True))
        self.assertIn("precisa ser confirmada por outro responsável",
                      self.act(self.p0, row["id"], "confirmar").get_data(as_text=True))
        self.act(self.p1, row["id"], "confirmar")
        self.assertEqual(self.iso()["name"], "Copa do Dono")
        (to, _, body), = self.mails()
        self.assertIn("p0@x.com", to)
        self.assertIn("p1 confirmou a sua alteração", body)

    def test_refuse_and_cancel(self):
        row = self.released([self.p1])
        self.edit(self.p1, row["id"], "Nome recusado", data=NEW_ISO, image=PNG2)
        rev, = self.revisions()
        new_local, new_image = Path(self.app.config["DOWNLOADS_DIR"]) / rev["stored_name"], self.image_file(rev["image"])
        self.assertTrue(new_local.exists() and new_image.exists())
        self.mails()
        page = self.act(self.p0, row["id"], "recusar", motivo="Arquivo errado").get_data(as_text=True)
        self.assertIn("Alteração recusada", page)
        self.assertEqual(self.revisions(), [])
        self.assertFalse(new_local.exists() or new_image.exists())
        self.assertEqual((self.iso()["name"], self.iso()["image"]), (row["name"], row["image"]))
        (to, subject, body), = self.mails()
        self.assertIn("p1@x.com", to)
        self.assertIn("não foi aprovada", subject)
        self.assertIn("p0 recusou a alteração que você pediu", body)
        self.assertIn("Motivo: Arquivo errado", body)
        # Só quem fez a alteração a cancela.
        self.edit(self.p0, row["id"], "Nome do dono")
        self.client(self.p1).post(f"/torneios/amistosos/isos/{row['id']}/cancelar-alteracao", data={"csrf_token": CSRF})
        self.assertEqual(len(self.revisions()), 1)
        self.client(self.p0).post(f"/torneios/amistosos/isos/{row['id']}/cancelar-alteracao", data={"csrf_token": CSRF})
        self.assertEqual(self.revisions(), [])

    def test_confirmation_waits_for_the_new_file(self):
        """Confirmada com o arquivo novo ainda indo para o archive.org: entra quando o envio termina."""
        row = self.released([self.p1])
        self.edit(self.p1, row["id"], "Copa v3", data=NEW_ISO)
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE catalog_iso_revisions SET archive_status = 'uploading', archive_updated_at = ?", (now(),))
            db.commit()
        self.mails()
        self.assertIn("ainda está indo para o archive.org", self.act(self.p0, row["id"], "confirmar").get_data(as_text=True))
        rev, = self.revisions()
        self.assertEqual((self.iso()["name"], rev["confirmed_by"]), (row["name"], self.p0))
        self.assertIsNotNone(rev["approved_at"])
        self.assertEqual(self.mails(), [])
        with self.app.test_request_context():
            db = get_db()
            self.assertEqual(iso_catalog.apply_due(db, self.app), [])
            db.execute("UPDATE catalog_iso_revisions SET archive_status = 'done', local_removed_at = ?, "
                       "archive_url = 'https://archive.org/v3'", (now(),))
            db.commit()
            (applied, auto, done), = iso_catalog.apply_due(db, self.app)
            self.assertFalse(auto)
            notify.iso_edit_approved(db, applied, auto=auto, rev=done)
        self.assertEqual(self.iso()["name"], "Copa v3")
        (to, _, body), = self.mails()
        self.assertIn("p1@x.com", to)
        self.assertIn("p0 confirmou a sua alteração", body)

    def test_without_helpers_it_still_needs_the_administration_or_the_deadline(self):
        row = self.released()
        page = self.edit(self.p0, row["id"], "Copa sozinho").get_data(as_text=True)
        self.assertIn("Alteração enviada para a administração do WE Camp aprovar", page)
        self.assertEqual([to for to, _, _ in self.mails()], ["adm@wecamp.com"])
        self.assertIn("precisa ser confirmada por outro responsável",
                      self.act(self.p0, row["id"], "confirmar").get_data(as_text=True))
        self.assertEqual((self.iso()["name"], len(self.revisions())), (row["name"], 1))

    def test_only_the_owner_chooses_the_helpers(self):
        row = self.released([self.p1])
        # O ajudante manda a lista vazia junto com uma edição: os ajudantes não mudam.
        self.edit(self.p1, row["id"], "Tentativa", helpers=[])
        self.assertEqual(len(self.query("SELECT 1 FROM catalog_iso_editors")), 1)
        self.client(self.p1).post(f"/torneios/amistosos/isos/{row['id']}/cancelar-alteracao", data={"csrf_token": CSRF})
        self.mails()
        # O dono tira o ajudante sem mudar mais nada: vale, e o ajudante é avisado.
        page = self.edit(self.p0, row["id"], row["name"], helpers=[]).get_data(as_text=True)
        self.assertIn("Ajudantes da ISO atualizados.", page)
        self.assertEqual(self.query("SELECT COUNT(*) FROM catalog_iso_editors")[0][0], 0)
        (to, subject, _), = self.mails()
        self.assertIn("p1@x.com", to)
        self.assertIn("Você não é mais ajudante da ISO", subject)
        self.assertIn("ISO não encontrada", self.edit(self.p1, row["id"], "Sem acesso").get_data(as_text=True))
        self.assertEqual(self.revisions(), [])

    def test_image_of_an_iso_waiting_for_release_changes_directly(self):
        self.upload(self.p0)
        row = self.iso()
        self.assertIsNone(row["image"])
        self.edit(self.p0, row["id"], row["name"], image=PNG)
        first = self.iso()["image"]
        self.assertTrue(self.image_file(first).exists())
        self.assertEqual(self.revisions(), [])
        self.edit(self.p0, row["id"], row["name"], image=PNG2)
        second = self.iso()["image"]
        self.assertFalse(self.image_file(first).exists())  # a trocada é apagada
        self.edit(self.p0, row["id"], row["name"], remove_image=True)
        self.assertIsNone(self.iso()["image"])
        self.assertFalse(self.image_file(second).exists())

    def test_image_change_of_a_released_iso_can_be_dropped(self):
        row = self.released([self.p1])
        self.edit(self.p1, row["id"], row["name"], image=PNG2)
        rev, = self.revisions()
        pending = self.image_file(rev["image"])
        # Sem imagem nova, a da alteração pendente continua valendo.
        self.edit(self.p1, row["id"], "Outro nome")
        self.assertEqual(self.revisions()[0]["image"], rev["image"])
        # "Tirar a imagem": a alteração passa a ser ficar sem imagem, e a pendente é apagada.
        self.edit(self.p1, row["id"], "Outro nome", remove_image=True)
        rev, = self.revisions()
        self.assertEqual((rev["image_change"], rev["image"]), (1, None))
        self.assertFalse(pending.exists())
        self.act(self.p0, row["id"], "confirmar")
        self.assertEqual((self.iso()["name"], self.iso()["image"]), ("Outro nome", None))
        self.assertFalse(self.image_file(row["image"]).exists())

    def test_iso_page_and_download_only_for_logged_players(self):
        self.upload(self.p0, [self.p1], PNG)
        row = self.iso()
        url = f"/torneios/isos/{row['id']}"
        # Aguardando liberação: só os responsáveis e a administração veem.
        self.assertEqual(self.client().get(url).status_code, 404)
        self.assertEqual(self.client(self.p2).get(url).status_code, 404)
        self.assertIn("aguarda a liberação", self.client(self.p1).get(url).get_data(as_text=True))
        self.assertEqual(self.client(admin=True).get(url).status_code, 200)
        self.client(admin=True).post(f"/admin/torneios/isos/{row['id']}", data={
            "csrf_token": CSRF, "name": row["name"], "game_title_id": self.game, "is_active": "on", "liberar": "1"})
        sent = self.mails()
        self.assertTrue(any("p0@x.com" in to and f"/torneios/isos/{row['id']}" in body for to, _, body in sent))
        # Liberada: a página é pública, com a prévia do link; o download pede o login.
        page = self.client().get(url).get_data(as_text=True)
        self.assertIn('<meta property="og:title" content="WE 2002 Copa 2026">', page)
        self.assertIn(f"/imagens/torneio/{row['image']}", page)
        self.assertIn("Entre para baixar a ISO", page)
        self.assertNotIn("Baixar a ISO</a>", page)
        self.assertIn("p0", page)
        anonymous = self.client().get(url + "/baixar")
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/conta/entrar", anonymous.headers["Location"])
        logged = self.client(self.p2)
        self.assertIn("Baixar a ISO", logged.get(url).get_data(as_text=True))
        download = logged.get(url + "/baixar")
        self.assertEqual(download.data, ISO)
        download.close()
        # O convite de amistoso já abre com a ISO da página escolhida.
        self.assertIn(f'href="/torneios/amistosos?iso=c:{row["id"]}"',
                      self.client(self.p0).get(url).get_data(as_text=True))
        # Desativada pela administração: some para quem não é responsável.
        self.client(admin=True).post(f"/admin/torneios/isos/{row['id']}", data={
            "csrf_token": CSRF, "name": row["name"], "game_title_id": self.game})
        self.assertEqual(self.client().get(url).status_code, 404)
        self.assertEqual(self.client(self.p2).get(url + "/baixar").status_code, 404)
        self.assertEqual(self.client(self.p0).get(url).status_code, 200)

    def test_share_card_only_from_a_responsible(self):
        row = self.released([self.p1])
        url = f"/torneios/isos/{row['id']}/imagem-compartilhar"

        def send(player, origin):
            return self.client(player).post(url, data={"csrf_token": CSRF, "origem": origin,
                                                      "image": (io.BytesIO(JPG), "compartilhar.jpg")},
                                            content_type="multipart/form-data")

        self.assertEqual(send(self.p2, row["image"]).status_code, 404)
        self.assertEqual(send(self.p0, "outra.png").status_code, 409)  # o cartão é de uma imagem que já saiu
        self.assertEqual(send(self.p1, row["image"]).status_code, 204)
        card = self.iso()["share_image"]
        self.assertTrue(card.endswith(".jpg"))
        page = self.client().get(f"/torneios/isos/{row['id']}").get_data(as_text=True)
        self.assertIn(f"/imagens/torneio/{card}", page)
        self.assertIn('<meta property="og:image:width" content="1200">', page)
        # Imagem nova confirmada: o cartão antigo sai (o navegador de um responsável monta outro).
        self.edit(self.p1, row["id"], row["name"], image=PNG2)
        self.act(self.p0, row["id"], "confirmar")
        self.assertIsNone(self.iso()["share_image"])
        self.assertFalse(self.image_file(card).exists())


if __name__ == "__main__":
    unittest.main()
