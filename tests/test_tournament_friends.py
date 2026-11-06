import tempfile
import unittest
from pathlib import Path

from app.db import get_db, init_db
from app.tournament import friends
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"


class FriendsAndInvitesTest(unittest.TestCase):
    """Amigos (pedido, aceite, recusa, desfazer) e convites para torneios."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
            db = get_db()
            self.players = [add_player(db, f"jog{i}") for i in range(4)]
            self.game = db.execute("SELECT id FROM game_titles WHERE is_active = 1 ORDER BY id LIMIT 1").fetchone()[0]
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def client(self, player):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            session["player_id"] = player
        return client

    def query(self, sql, *args):
        with self.app.app_context():
            return get_db().execute(sql, args).fetchall()

    def mails(self):
        box = self.app.extensions.setdefault("mail_outbox", [])
        sent = [(m["To"], m["Subject"], m.get_content()) for m in box]
        box.clear()
        return sent

    def relation(self, a, b):
        with self.app.app_context():
            return friends.relation(get_db(), a, b)

    def create(self, owner, name, invite_ids=()):
        owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": name, "starts_on": "2026-12-01", "rules": "Regras",
            "game_title_id": self.game, "format": "knockout", "max_players": "2", "entry_fee": "0",
            "ko_legs": "1", "final_legs": "1", "invite_ids": [str(i) for i in invite_ids]})
        return self.query("SELECT * FROM tournaments WHERE name = ?", name)[0]

    def test_player_card_popup_has_add_friend(self):
        j0, j1, _, _ = self.players
        c0 = self.client(j0)
        card = c0.get("/torneios/jogadores/jog1/resumo", headers={"Referer": "http://localhost/torneios/ranking"}).data.decode()
        self.assertIn("Adicionar amigo", card)
        self.assertIn('name="voltar" value="/torneios/ranking"', card)
        self.assertNotIn("Adicionar amigo", c0.get("/torneios/jogadores/jog0/resumo").data.decode())  # o próprio
        response = c0.post("/torneios/amigos/adicionar", data={"csrf_token": CSRF, "nome": "jog1",
                                                               "voltar": "/torneios/ranking"})
        self.assertTrue(response.headers["Location"].endswith("/torneios/ranking"))
        self.assertIn("Pedido de amizade enviado", c0.get("/torneios/jogadores/jog1/resumo").data.decode())
        self.assertIn("Aceitar amizade", self.client(j1).get("/torneios/jogadores/jog0/resumo").data.decode())

    def test_friend_request_accept_decline_remove(self):
        j0, j1, j2, _ = self.players
        c0, c1, c2 = self.client(j0), self.client(j1), self.client(j2)
        self.assertIn("Adicionar amigo", c0.get("/torneios/jogadores/jog1").data.decode())
        self.assertNotIn("Adicionar amigo", c0.get("/torneios/jogadores/jog0").data.decode())  # a própria página

        c0.post("/torneios/amigos/adicionar", data={"csrf_token": CSRF, "nome": "JOG1", "voltar": "/torneios/jogadores/jog1"})
        self.assertEqual(self.relation(j0, j1), "sent")
        self.assertEqual(self.relation(j1, j0), "received")
        mail = self.mails()
        self.assertEqual(mail[0][0], "jog1@x.com")
        self.assertIn("/torneios/amigos", mail[0][2])
        self.assertIn("Pedido de amizade enviado", c0.get("/torneios/jogadores/jog1").data.decode())
        page = c1.get("/torneios/amigos").data.decode()
        self.assertIn("Pedidos de amizade (1)", page)
        self.assertIn('title="Pedidos de amizade">1<', page)  # contador no menu
        self.assertIn("Aceitar amizade", c1.get("/torneios/jogadores/jog0").data.decode())

        # pedir de novo não duplica; pedir para si mesmo não pode
        c0.post("/torneios/amigos/adicionar", data={"csrf_token": CSRF, "nome": "jog1"})
        c0.post("/torneios/amigos/adicionar", data={"csrf_token": CSRF, "nome": "jog0"})
        self.assertEqual(len(self.query("SELECT * FROM friendships")), 1)

        c1.post(f"/torneios/amigos/{j0}/aceitar", data={"csrf_token": CSRF})
        self.assertEqual(self.relation(j0, j1), "friends")
        self.assertEqual(self.mails()[0][0], "jog0@x.com")  # aviso do aceite
        self.assertIn("Amigos ✓", c0.get("/torneios/jogadores/jog1").data.decode())
        self.assertIn("Meus amigos (1)", c1.get("/torneios/amigos").data.decode())

        # quem recebeu um pedido e pede de volta aceita na hora; recusar apaga o pedido
        c2.post("/torneios/amigos/adicionar", data={"csrf_token": CSRF, "nome": "jog0"})
        c2.post("/torneios/amigos/adicionar", data={"csrf_token": CSRF, "nome": "jog3"})
        c0.post("/torneios/amigos/adicionar", data={"csrf_token": CSRF, "nome": "jog2"})
        self.assertEqual(self.relation(j0, j2), "friends")
        self.client(self.players[3]).post(f"/torneios/amigos/{j2}/recusar", data={"csrf_token": CSRF})
        self.assertIsNone(self.relation(j2, self.players[3]))

        c1.post(f"/torneios/amigos/{j0}/remover", data={"csrf_token": CSRF})
        self.assertIsNone(self.relation(j0, j1))
        self.assertEqual(self.client(j0).get("/torneios/amigos/x").status_code, 404)

    def test_invites_on_create_and_manage(self):
        j0, j1, j2, j3 = self.players
        c0, c1 = self.client(j0), self.client(j1)
        c0.post("/torneios/amigos/adicionar", data={"csrf_token": CSRF, "nome": "jog1"})
        c1.post(f"/torneios/amigos/{j0}/aceitar", data={"csrf_token": CSRF})
        self.mails()

        # Criar: amigos aparecem para convite; jog3 não é amigo nem jogou com jog0, então é ignorado.
        page = c0.get("/torneios/novo").data.decode()
        self.assertIn('name="invite_ids" value="%d"' % j1, page)
        self.assertNotIn('name="invite_ids" value="%d"' % j3, page)
        first = self.create(c0, "Copa Um", [j1, j3])
        invites = self.query("SELECT player_id, invited_by FROM tournament_invites")
        self.assertEqual([tuple(r) for r in invites], [(j1, j0)])
        mail = self.mails()
        self.assertEqual([m[0] for m in mail], ["jog1@x.com"])
        self.assertIn("Copa Um", mail[0][1])
        self.assertIn(f"/torneios/{first['slug']}", mail[0][2])

        mine = c1.get("/torneios/meus-campeonatos").data.decode()
        self.assertIn("Convites para campeonatos (1)", mine)
        self.assertIn('title="Convites para campeonatos">1<', mine)
        self.assertIn("convidou você para este campeonato", c1.get(f"/torneios/{first['slug']}").data.decode())
        c1.post(f"/torneios/{first['slug']}/convite/recusar", data={"csrf_token": CSRF})
        self.assertNotIn("Convites para campeonatos", c1.get("/torneios/meus-campeonatos").data.decode())
        # quem recusou pode ser convidado de novo (quantas vezes o organizador quiser)
        manage = c0.get(f"/torneios/{first['slug']}/gerenciar").data.decode()
        self.assertIn("(recusou)", manage)
        self.assertIn('name="invite_ids" value="%d"' % j1, manage)
        self.mails()
        for _ in range(2):
            c0.post(f"/torneios/{first['slug']}/convidar", data={"csrf_token": CSRF, "invite_ids": [str(j1)]})
            self.assertEqual([m[0] for m in self.mails()], ["jog1@x.com"])
            self.assertIn("Convites para campeonatos (1)", c1.get("/torneios/meus-campeonatos").data.decode())
            c1.post(f"/torneios/{first['slug']}/convite/recusar", data={"csrf_token": CSRF})
        self.assertEqual(len(self.query("SELECT id FROM tournament_invites WHERE player_id = ?", j1)), 1)

        # jog2 joga a Copa Um; na Copa Dois ele aparece como jogador de um torneio de jog0.
        self.client(j2).post(f"/torneios/{first['slug']}/inscrever", data={"csrf_token": CSRF, "team_name": "Time"})
        second = self.create(c0, "Copa Dois")
        manage = c0.get(f"/torneios/{second['slug']}/gerenciar").data.decode()
        self.assertIn('id="convidar"', manage)
        self.assertIn(f'<option value="{j2}">Copa Um (1)</option>', manage)
        c0.post(f"/torneios/{second['slug']}/convidar", data={"csrf_token": CSRF, "invite_ids": [str(j1), str(j2)]})
        self.assertEqual(sorted(m[0] for m in self.mails()), ["jog1@x.com", "jog2@x.com"])
        # repetir não reenvia; quem não organiza não convida
        c0.post(f"/torneios/{second['slug']}/convidar", data={"csrf_token": CSRF, "invite_ids": [str(j1)]})
        self.assertEqual(self.mails(), [])
        self.assertEqual(c1.post(f"/torneios/{second['slug']}/convidar",
                                 data={"csrf_token": CSRF, "invite_ids": [str(j2)]}).status_code, 403)
        self.assertIn("(convidado)", c0.get(f"/torneios/{second['slug']}/gerenciar").data.decode())

        # inscrito, o convite some da lista dele
        self.client(j2).post(f"/torneios/{second['slug']}/inscrever", data={"csrf_token": CSRF, "team_name": "Time"})
        self.assertNotIn("Convites para campeonatos", self.client(j2).get("/torneios/meus-campeonatos").data.decode())
        manage = c0.get(f"/torneios/{second['slug']}/gerenciar").data.decode()
        self.assertIn("(inscrito)", manage)
        # convidado (laranja) e inscrito (verde) aparecem marcados e travados, fora do formulário
        self.assertIn('invite-option is-pending', manage)
        self.assertIn('invite-option is-enrolled', manage)
        self.assertIn('<input type="checkbox" value="%d" checked data-locked' % j1, manage)
        self.assertIn('<input type="checkbox" value="%d" checked data-locked' % j2, manage)
        self.assertNotIn('name="invite_ids" value="%d"' % j2, manage)
        self.assertIn("data-invite-submit", manage)

        # o organizador remove o inscrito: o convite some junto e dá para convidar de novo
        entry = self.query("SELECT id FROM tournament_entries WHERE tournament_id = ? AND player_id = ?", second["id"], j2)[0][0]
        c0.post(f"/torneios/{second['slug']}/inscritos/{entry}/remover", data={"csrf_token": CSRF})
        self.assertEqual(self.query("SELECT id FROM tournament_invites WHERE tournament_id = ? AND player_id = ?", second["id"], j2), [])
        manage = c0.get(f"/torneios/{second['slug']}/gerenciar").data.decode()
        self.assertIn('name="invite_ids" value="%d"' % j2, manage)
        self.assertNotIn("Convites para campeonatos", self.client(j2).get("/torneios/meus-campeonatos").data.decode())
        c0.post(f"/torneios/{second['slug']}/convidar", data={"csrf_token": CSRF, "invite_ids": [str(j2)]})
        self.assertEqual([m[0] for m in self.mails()], ["jog2@x.com"])
        self.assertIn("Convites para campeonatos (1)", self.client(j2).get("/torneios/meus-campeonatos").data.decode())


    def test_friends_only_tournament(self):
        j0, j1, j2, j3 = self.players
        c0, c1, c2 = self.client(j0), self.client(j1), self.client(j2)
        c0.post("/torneios/amigos/adicionar", data={"csrf_token": CSRF, "nome": "jog1"})
        c1.post(f"/torneios/amigos/{j0}/aceitar", data={"csrf_token": CSRF})
        # jog2 jogou um torneio do jog0, mas não é amigo: no torneio de amigos não pode ser convidado.
        old = self.create(c0, "Copa Velha")
        c2.post(f"/torneios/{old['slug']}/inscrever", data={"csrf_token": CSRF, "team_name": "Time 2"})
        self.mails()
        c0.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa Amigos", "starts_on": "2026-12-01", "rules": "Regras",
            "game_title_id": self.game, "format": "knockout", "max_players": "2", "entry_fee": "0",
            "ko_legs": "1", "final_legs": "1", "friends_only": "on", "invite_ids": [str(j1), str(j2)]})
        t = self.query("SELECT * FROM tournaments WHERE name = 'Copa Amigos'")[0]
        self.assertEqual(t["friends_only"], 1)
        self.assertEqual([r[0] for r in self.query("SELECT player_id FROM tournament_invites WHERE tournament_id = ?",
                                                  t["id"])], [j1])
        slug = t["slug"]
        # Fora da lista pública e de Meus campeonatos para quem não é amigo; página dá 404.
        self.assertNotIn("Copa Amigos", c2.get("/torneios/").data.decode())
        self.assertNotIn("Copa Amigos", c2.get("/torneios/meus-campeonatos?status=todos").data.decode())
        self.assertNotIn("Copa Amigos", self.app.test_client().get("/torneios/").data.decode())
        self.assertEqual(c2.get(f"/torneios/{slug}").status_code, 404)
        c2.post(f"/torneios/{slug}/inscrever", data={"csrf_token": CSRF, "team_name": "Time 2"})
        self.assertFalse(self.query("SELECT 1 FROM tournament_entries WHERE tournament_id = ?", t["id"]))
        # O amigo vê em Meus campeonatos (sem estar inscrito), na lista pública e se inscreve.
        self.assertIn('t-name">Copa Amigos', c1.get("/torneios/meus-campeonatos").data.decode())
        self.assertIn("Somente amigos", c1.get("/torneios/").data.decode())
        self.assertEqual(c1.get(f"/torneios/{slug}").status_code, 200)
        c1.post(f"/torneios/{slug}/inscrever", data={"csrf_token": CSRF, "team_name": "Time 1"})
        self.assertEqual(len(self.query("SELECT 1 FROM tournament_entries WHERE tournament_id = ?", t["id"])), 1)
        # Desfeita a amizade, quem já está inscrito continua vendo.
        c0.post(f"/torneios/amigos/{j1}/remover", data={"csrf_token": CSRF})
        self.assertEqual(c1.get(f"/torneios/{slug}").status_code, 200)
        # Em Gerenciar o organizador tira a marca e o torneio volta a ser público.
        c0.post(f"/torneios/{slug}/editar", data={"csrf_token": CSRF, "name": "Copa Amigos", "rules": "Regras",
                                                   "starts_on": "2026-12-01"})
        self.assertIn("Copa Amigos", c2.get("/torneios/").data.decode())
        self.assertEqual(c2.get(f"/torneios/{slug}").status_code, 200)


if __name__ == "__main__":
    unittest.main()
