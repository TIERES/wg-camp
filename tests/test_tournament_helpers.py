"""Ajudantes do campeonato (app/tournament/editors.py) e a alteração que espera
confirmação (app/tournament/changes.py): o organizador escolhe amigos para
ajudar a editar os dados, a imagem e a ISO; com ajudantes, o que um responsável
muda só entra depois que OUTRO responsável confirma - o e-mail leva o antes e
o depois. Sem ajudantes, tudo continua entrando direto."""
import io
import unittest
from pathlib import Path

from app.db import get_db
from app.tournament import friends
from tests import test_tournament_edit as base  # o módulo, para o pytest não repetir os testes do TournamentEditTest

CSRF = base.CSRF
ISO = b"ISO do campeonato" * 64
NEW_ISO = b"ISO nova do campeonato" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"imagem do campeonato" * 20


class TournamentHelpersTest(unittest.TestCase):
    tearDown, client, query, mails, wallet, form, create, enroll, entries = (
        base.TournamentEditTest.tearDown, base.TournamentEditTest.client, base.TournamentEditTest.query,
        base.TournamentEditTest.mails, base.TournamentEditTest.wallet, base.TournamentEditTest.form,
        base.TournamentEditTest.create, base.TournamentEditTest.enroll, base.TournamentEditTest.entries)

    def setUp(self):
        base.TournamentEditTest.setUp(self)  # owner (jog0), p1, p2 e p3
        with self.app.app_context():
            db = get_db()
            friends.request(db, self.owner, self.p1)
            friends.respond(db, self.p1, self.owner, True)
            db.commit()
        self.app.config["SERVE_DOWNLOADS_LOCALLY"] = True

    def admin(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            session["user_id"] = 1
        return client

    def with_helper(self, **changes):
        """Campeonato do owner com o p1 de ajudante."""
        t = self.create(**changes)
        self.client(self.owner).post(f"/torneios/{t['slug']}/ajudantes",
                                     data={"csrf_token": CSRF, "helpers_sent": "1", "helper_ids": [str(self.p1)]})
        self.mails()
        return t

    def edit(self, player, slug, **changes):
        return self.client(player).post(f"/torneios/{slug}/editar", data=self.form(**changes),
                                        follow_redirects=True).get_data(as_text=True)

    def act(self, client, slug, action, **extra):
        return client.post(f"/torneios/{slug}/alteracao/{action}", data={"csrf_token": CSRF, **extra},
                           follow_redirects=True).get_data(as_text=True)

    def upload_iso(self, player, slug, data, name):
        return self.client(player).post(f"/torneios/{slug}/iso", data={
            "csrf_token": CSRF, "iso": (io.BytesIO(data), name)}, content_type="multipart/form-data",
            follow_redirects=True).get_data(as_text=True)

    def tournament(self, tournament_id):
        return self.query("SELECT * FROM tournaments WHERE id = ?", tournament_id)[0]

    def changes(self):
        return self.query("SELECT * FROM tournament_changes")

    def local(self, stored_name):
        return Path(self.app.config["DOWNLOADS_DIR"]) / stored_name

    def test_organizer_chooses_friends_and_the_helper_gets_a_reduced_manage_page(self):
        t = self.create()
        owner = self.client(self.owner)
        # Só amigos de quem criou.
        page = owner.post(f"/torneios/{t['slug']}/ajudantes", data={"csrf_token": CSRF, "helper_ids": [str(self.p2)]},
                          follow_redirects=True).get_data(as_text=True)
        self.assertIn("Só os amigos de quem criou o campeonato podem ajudar a editar.", page)
        self.assertEqual(self.query("SELECT COUNT(*) FROM tournament_editors")[0][0], 0)
        self.assertEqual(self.client(self.p1).get(f"/torneios/{t['slug']}/gerenciar").status_code, 403)
        page = owner.post(f"/torneios/{t['slug']}/ajudantes", data={"csrf_token": CSRF, "helper_ids": [str(self.p1)]},
                          follow_redirects=True).get_data(as_text=True)
        self.assertIn("Ajudantes atualizados.", page)
        (to, subject, body), = self.mails()
        self.assertIn("jog1@x.com", to)
        self.assertIn('adicionou você como ajudante do campeonato "Copa Teste"', subject)
        self.assertIn(f"/torneios/{t['slug']}/gerenciar", body)
        # O ajudante: os dados, a imagem e a ISO - sem sortear, cancelar, convidar nem mexer nos ajudantes.
        helper = self.client(self.p1)
        page = helper.get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True)
        for shown in ("Dados do campeonato", "Imagem do campeonato", "ISO do campeonato", "Responsáveis por este campeonato"):
            self.assertIn(shown, page)
        for hidden in ("Cancelar campeonato", "Fechar inscrições e sortear", "Convidar jogadores", "Salvar ajudantes"):
            self.assertNotIn(hidden, page)
        for action in ("iniciar", "cancelar", "ajudantes", "convidar"):
            self.assertEqual(helper.post(f"/torneios/{t['slug']}/{action}", data={"csrf_token": CSRF}).status_code, 403)
        self.assertEqual(self.client(self.p2).get(f"/torneios/{t['slug']}/gerenciar").status_code, 403)
        # Aparece em "Meus campeonatos" do ajudante, com o botão para editar.
        mine = helper.get("/torneios/meus-campeonatos").get_data(as_text=True)
        self.assertIn("você ajuda a editar", mine)
        self.assertIn("Editar dados e ISO", helper.get(f"/torneios/{t['slug']}").get_data(as_text=True))
        # Tirar o ajudante avisa e fecha o acesso.
        owner.post(f"/torneios/{t['slug']}/ajudantes", data={"csrf_token": CSRF, "helpers_sent": "1"})
        (to, subject, _), = self.mails()
        self.assertIn("Você não é mais ajudante do campeonato", subject)
        self.assertEqual(helper.get(f"/torneios/{t['slug']}/gerenciar").status_code, 403)

    def test_change_waits_for_another_responsible_and_applies_on_confirmation(self):
        t = self.with_helper()
        self.enroll(self.p2, t["slug"], self.teams[0])
        paid = self.wallet(self.p2)
        self.mails()
        # O organizador muda o nome e o valor da inscrição: fica guardado, nada muda ainda.
        page = self.edit(self.owner, t["slug"], name="Copa Nova", entry_fee="W$ 20,00")
        self.assertIn("Alteração guardada: ela entra quando outro responsável confirmar. jog1 recebeu o e-mail", page)
        self.assertIn("Alteração esperando confirmação", page)
        row = self.tournament(t["id"])
        self.assertEqual((row["name"], row["entry_fee_cents"]), ("Copa Teste", 1000))
        self.assertEqual(len(self.entries(t["id"])), 1)
        (to, subject, body), = self.mails()
        self.assertIn("jog1@x.com", to)
        self.assertIn("Confirme a alteração de jog0 no campeonato Copa Teste", subject)
        self.assertIn("Nome: Copa Teste → Copa Nova", body)
        self.assertIn("Inscrição: W$10,00 → W$20,00", body)
        self.assertIn("1 inscrito(s) saem do campeonato e recebem de volta o valor pago", body)
        self.assertIn(f"/torneios/{t['slug']}/gerenciar#alteracao", body)
        # Os cards dos campeonatos mostram o selo: "em análise" para quem fez, "confirme" para o outro.
        self.assertIn("Alteração em análise", self.client(self.owner).get("/torneios/meus-campeonatos").get_data(as_text=True))
        helper_list = self.client(self.p1).get("/torneios/meus-campeonatos").get_data(as_text=True)
        self.assertIn("Confirme a alteração", helper_list)
        self.assertIn(f'href="/torneios/{t["slug"]}/gerenciar#alteracao"', helper_list)
        self.assertNotIn("Confirme a alteração", self.client(self.p2).get("/torneios/").get_data(as_text=True))
        # O formulário já abre com a alteração; o sorteio espera a confirmação.
        owner = self.client(self.owner)
        manage = owner.get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True)
        self.assertIn('value="Copa Nova"', manage)
        self.assertIn("Cancelar alteração", manage)
        self.assertNotIn("Confirmar alteração", manage)
        started = owner.post(f"/torneios/{t['slug']}/iniciar", data={"csrf_token": CSRF}, follow_redirects=True)
        self.assertIn("Há uma alteração dos dados do campeonato esperando confirmação", started.get_data(as_text=True))
        # Quem fez não confirma a própria alteração; quem não é responsável não chega nem perto.
        self.assertIn("precisa ser confirmada por outro responsável", self.act(owner, t["slug"], "confirmar"))
        self.assertEqual(self.client(self.p2).post(f"/torneios/{t['slug']}/alteracao/confirmar",
                                                   data={"csrf_token": CSRF}).status_code, 403)
        self.assertEqual(self.tournament(t["id"])["name"], "Copa Teste")
        # O ajudante vê o antes e o depois e confirma: aí sim vale, com os avisos aos inscritos.
        helper = self.client(self.p1)
        manage = helper.get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True)
        self.assertIn("Confirmar alteração", manage)
        self.assertIn("1 inscrito(s) saem do campeonato", manage)
        self.assertIn("Alteração confirmada: o campeonato já está atualizado. 1 inscrito(s) avisado(s) por e-mail.",
                      self.act(helper, t["slug"], "confirmar"))
        row = self.tournament(t["id"])
        self.assertEqual((row["name"], row["entry_fee_cents"]), ("Copa Nova", 2000))
        self.assertEqual(self.entries(t["id"]), {})
        self.assertEqual(self.wallet(self.p2), paid + 1000)
        self.assertEqual(self.changes(), [])
        sent = {to: (subject, body) for to, subject, body in self.mails()}
        self.assertEqual(sorted(sent), ["jog0@x.com", "jog2@x.com"])
        self.assertIn("sua inscrição foi cancelada", sent["jog2@x.com"][0])
        self.assertIn("Alteração confirmada no campeonato Copa Nova", sent["jog0@x.com"][0])
        self.assertIn("jog1 confirmou a sua alteração", sent["jog0@x.com"][1])

    def test_helper_iso_and_image_wait_for_confirmation(self):
        t = self.create()
        # Sem ajudantes: a ISO entra direto, como sempre.
        self.upload_iso(self.owner, t["slug"], ISO, "Copa.bin")
        current = self.query("SELECT * FROM tournament_isos")[0]
        self.assertEqual(self.changes(), [])
        self.client(self.owner).post(f"/torneios/{t['slug']}/ajudantes",
                                     data={"csrf_token": CSRF, "helper_ids": [str(self.p1)]})
        self.mails()
        # O ajudante envia a ISO nova e a imagem: ficam guardadas.
        page = self.upload_iso(self.p1, t["slug"], NEW_ISO, "Copa v2.bin")
        self.assertIn("Alteração guardada", page)
        self.client(self.p1).post(f"/torneios/{t['slug']}/imagem", data={
            "csrf_token": CSRF, "image": (io.BytesIO(PNG), "arte.png")}, content_type="multipart/form-data")
        change, = self.changes()
        self.assertEqual((change["requested_by"], change["original_filename"], change["image_change"]),
                         (self.p1, "Copa v2.bin", 1))
        self.assertEqual(self.query("SELECT stored_name FROM tournament_isos")[0][0], current["stored_name"])
        self.assertIsNone(self.tournament(t["id"])["image"])
        self.assertTrue(self.local(current["stored_name"]).exists() and self.local(change["stored_name"]).exists())
        sent = self.mails()
        self.assertEqual({to for to, _, _ in sent}, {"jog0@x.com"})
        self.assertIn("ISO: Copa.bin (1.1 KB) → Copa v2.bin (1.4 KB)", sent[-1][2])
        self.assertIn(f"/imagens/torneio/{change['image']}", sent[-1][2])
        # Antes de confirmar, o download da página continua com a ISO atual; o organizador confere a nova.
        download = self.client(self.p2).get(f"/torneios/{t['slug']}/iso")
        self.assertEqual(download.data, ISO)
        download.close()
        owner = self.client(self.owner)
        check = owner.get(f"/torneios/{t['slug']}/alteracao/iso")
        self.assertEqual(check.data, NEW_ISO)
        check.close()
        self.assertEqual(self.client(self.p2).get(f"/torneios/{t['slug']}/alteracao/iso").status_code, 403)
        self.assertIn("Alteração confirmada", self.act(owner, t["slug"], "confirmar"))
        after = self.query("SELECT * FROM tournament_isos")[0]
        self.assertEqual((after["stored_name"], after["original_filename"], after["uploaded_by"]),
                         (change["stored_name"], "Copa v2.bin", self.p1))
        self.assertEqual(self.tournament(t["id"])["image"], change["image"])
        self.assertFalse(self.local(current["stored_name"]).exists())
        download = self.client(self.p2).get(f"/torneios/{t['slug']}/iso")
        self.assertEqual(download.data, NEW_ISO)
        download.close()

    def test_refuse_and_cancel_discard_the_stored_files(self):
        t = self.with_helper()
        self.upload_iso(self.p1, t["slug"], NEW_ISO, "Copa v2.bin")
        stored = self.local(self.changes()[0]["stored_name"])
        self.assertTrue(stored.exists())
        self.mails()
        # Só quem fez cancela; o outro responsável recusa (com o motivo no e-mail).
        owner, helper = self.client(self.owner), self.client(self.p1)
        self.assertIn("Só quem fez a alteração pode cancelá-la", self.act(owner, t["slug"], "cancelar"))
        self.assertIn("Alteração recusada", self.act(owner, t["slug"], "recusar", motivo="ISO errada"))
        self.assertEqual(self.changes(), [])
        self.assertFalse(stored.exists())
        (to, subject, body), = self.mails()
        self.assertIn("jog1@x.com", to)
        self.assertIn("Alteração recusada no campeonato Copa Teste", subject)
        self.assertIn("jog0 recusou a sua alteração", body)
        self.assertIn("Motivo: ISO errada", body)
        self.edit(self.p1, t["slug"], name="Copa do Ajudante")
        self.mails()
        self.assertIn("Alteração cancelada", self.act(helper, t["slug"], "cancelar"))
        self.assertEqual((self.changes(), self.tournament(t["id"])["name"]), ([], "Copa Teste"))
        (to, subject, _), = self.mails()
        self.assertIn("jog0@x.com", to)
        self.assertIn("Alteração cancelada no campeonato", subject)
        # Salvar o que já vale não deixa alteração pendente.
        self.assertIn("não há o que confirmar", self.edit(self.p1, t["slug"]))
        self.assertEqual(self.changes(), [])

    def test_last_responsible_left_confirms_the_own_change(self):
        t = self.with_helper()
        self.edit(self.owner, t["slug"], name="Copa Sozinha")
        owner = self.client(self.owner)
        owner.post(f"/torneios/{t['slug']}/ajudantes", data={"csrf_token": CSRF, "helpers_sent": "1"})
        page = owner.get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True)
        self.assertIn("você mesmo pode confirmar a alteração", page)
        self.assertIn("Alteração confirmada", self.act(owner, t["slug"], "confirmar"))
        self.assertEqual(self.tournament(t["id"])["name"], "Copa Sozinha")
        # Sem ajudantes, volta a entrar direto.
        self.assertIn("Campeonato atualizado.", self.edit(self.owner, t["slug"], name="Copa Direta"))
        self.assertEqual((self.tournament(t["id"])["name"], self.changes()), ("Copa Direta", []))

    def test_administration_edits_directly_and_can_confirm(self):
        t = self.with_helper()
        admin = self.admin()
        page = admin.post(f"/torneios/{t['slug']}/editar", data=self.form(name="Copa da Administração"),
                          follow_redirects=True).get_data(as_text=True)
        self.assertIn("Campeonato atualizado.", page)
        self.assertEqual((self.tournament(t["id"])["name"], self.changes()), ("Copa da Administração", []))
        self.edit(self.p1, t["slug"], name="Copa do Ajudante")
        self.mails()
        self.assertIn("Alteração confirmada", self.act(admin, t["slug"], "confirmar"))
        self.assertEqual(self.tournament(t["id"])["name"], "Copa do Ajudante")
        sent = self.mails()
        self.assertEqual(sorted(to for to, _, _ in sent), ["jog0@x.com", "jog1@x.com"])
        self.assertIn("A administração do WE Camp confirmou", sent[0][2])

    def test_after_the_start_the_short_form_also_waits_for_confirmation(self):
        t = self.with_helper(entry_fee="W$ 0,00")
        self.enroll(self.p2, t["slug"], self.teams[0])
        self.enroll(self.p3, t["slug"], self.teams[1])
        self.client(self.owner).post(f"/torneios/{t['slug']}/iniciar", data={"csrf_token": CSRF})
        self.assertEqual(self.tournament(t["id"])["status"], "running")
        self.mails()
        page = self.client(self.p1).post(f"/torneios/{t['slug']}/editar", data={
            "csrf_token": CSRF, "name": "Copa em Andamento", "starts_on": "2026-12-01", "ends_on": "2026-12-20",
            "rules": "Regras novas\ncom duas linhas"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Alteração guardada", page)
        self.assertEqual(self.tournament(t["id"])["name"], "Copa Teste")
        (to, _, body), = self.mails()
        self.assertIn("jog0@x.com", to)
        self.assertIn("Nome: Copa Teste → Copa em Andamento", body)
        self.assertIn("Regulamento:\n    Antes:\n      Regras\n    Depois:\n      Regras novas\n      com duas linhas", body)
        owner = self.client(self.owner)
        self.assertIn('value="Copa em Andamento"', owner.get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True))
        self.act(owner, t["slug"], "confirmar")
        row = self.tournament(t["id"])
        self.assertEqual((row["name"], row["rules"]), ("Copa em Andamento", "Regras novas\ncom duas linhas"))

    def test_helpers_chosen_when_creating(self):
        self.client(self.owner).post("/torneios/novo", data=self.form(helpers_sent="1", helper_ids=[str(self.p1)]))
        t = self.query("SELECT * FROM tournaments")[0]
        self.assertEqual([r["player_id"] for r in self.query("SELECT * FROM tournament_editors")], [self.p1])
        self.assertTrue(any("jog1@x.com" in to and "adicionou você como ajudante do campeonato" in subject
                            for to, subject, _ in self.mails()))
        # Quem não é amigo não entra: o campeonato nem é criado.
        page = self.client(self.owner).post("/torneios/novo", data=self.form(
            name="Outra Copa", helper_ids=[str(self.p2)])).get_data(as_text=True)
        self.assertIn("Só os amigos de quem criou o campeonato podem ajudar a editar.", page)
        self.assertEqual(self.query("SELECT COUNT(*) FROM tournaments")[0][0], 1)
        # Privado ("Somente para amigos"): o ajudante continua vendo, mesmo desfeita a amizade.
        self.client(self.owner).post(f"/torneios/{t['slug']}/ajudantes", data={"csrf_token": CSRF, "helpers_sent": "1"})
        self.client(self.owner).post(f"/torneios/{t['slug']}/editar", data=self.form(friends_only="on"))
        self.client(self.owner).post(f"/torneios/{t['slug']}/ajudantes",
                                     data={"csrf_token": CSRF, "helper_ids": [str(self.p1)]})
        with self.app.app_context():
            db = get_db()
            friends.remove(db, self.owner, self.p1)
            db.commit()
        self.assertEqual(self.client(self.p1).get(f"/torneios/{t['slug']}/gerenciar").status_code, 200)
        self.assertEqual(self.client(self.p2).get(f"/torneios/{t['slug']}/gerenciar").status_code, 404)


if __name__ == "__main__":
    unittest.main()
