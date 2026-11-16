"""Aviso de campeonato liberado (tournament/announce.py): no sorteio, cada
inscrito recebe o e-mail em card e vê a janela em qualquer página do site até
clicar em "Entendi" (ou "Ver o campeonato")."""
import tempfile
import unittest
from pathlib import Path

from app.db import get_db, init_db, now
from app.tournament import store
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"


class TournamentNoticeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.app.config["PUBLIC_URL"] = "https://we.example"
        with self.app.app_context():
            init_db()
            db = get_db()
            self.players = [add_player(db, f"jog{i}") for i in range(5)]
            game = db.execute("SELECT id FROM game_titles WHERE code = 'we2002'").fetchone()["id"]
            tid = db.execute(
                "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                "entry_fee_cents, prize_split, settings_json, starts_on, ends_on, image, share_image, created_at, "
                "updated_at) VALUES ('copa-teste', 'Copa Teste', ?, ?, 'league', 'registration', 4, 1000, '60,30,10', "
                "'{\"legs\": 1}', '2026-11-15', '2026-11-30', 'camp.webp', 'camp.jpg', ?, ?)",
                (game, self.players[0], now(), now())).lastrowid
            tournament = store.get_tournament(db, tid)
            for n, pid in enumerate(self.players[:4]):
                store.enroll(db, tournament, pid, team_name=f"Time {n}")
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def client(self, player):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            session["player_id"] = player
        return client

    def test_card_email_and_popup_until_understood(self):
        organizer, player, outsider = self.players[0], self.players[1], self.players[4]
        # Antes do sorteio: nada.
        self.assertNotIn('id="aviso-campeonato"', self.client(player).get("/torneios/").data.decode())
        self.client(organizer).post("/torneios/copa-teste/iniciar", data={"csrf_token": CSRF})
        mails = [m for m in self.app.extensions["mail_outbox"] if "está valendo" in m["Subject"]]
        self.assertEqual(len(mails), 4)  # um para cada inscrito
        mail = mails[0]
        self.assertEqual(mail["Subject"], "🏆 Copa Teste: está valendo!")
        text = mail.get_body(("plain",)).get_content()
        html = mail.get_body(("html",)).get_content()
        for content in (text, html):
            self.assertIn("Conta WE Camp", content)
            self.assertIn("W$40,00", content)  # 4 x W$10
            self.assertIn("Seus jogos na primeira fase", content)
            self.assertIn("não é preciso responder", content)
        # No card padrão (email/_base.html): logo + "Campeonatos / Winning Eleven" e o rodapé "WE Camp".
        self.assertIn("images/logo/wecamp-192.png", html)
        self.assertIn(">Winning Eleven</span>", html)
        self.assertIn(">WE Camp</a>", html)
        # O card: a imagem de compartilhamento (JPEG) e a capa do jogo base em PNG, com o endereço do site.
        self.assertIn('src="https://we.example/imagens/torneio/camp.jpg"', html)
        self.assertIn('src="https://we.example/static/images/games/email/we2002.png"', html)
        self.assertIn('href="https://we.example/torneios/copa-teste"', html)
        self.assertIn("🥇 1º W$24,00", html)
        # A janela em qualquer página, só para os inscritos.
        page = self.client(player).get("/ao-vivo").data.decode()
        self.assertIn('id="aviso-campeonato"', page)
        self.assertIn("🏆 Copa Teste: está valendo!", page)
        self.assertIn('src="/static/images/games/email/we2002.png"', page)
        self.assertNotIn('id="aviso-campeonato"', self.client(outsider).get("/ao-vivo").data.decode())
        # Uma janela de cada vez: a da Conta WE Camp espera.
        self.assertNotIn('id="conta-wecamp-popup"', page)
        # "Entendi": volta para a mesma página e a janela não aparece mais.
        client = self.client(player)
        reply = client.post("/torneios/copa-teste/aviso-entendi", data={"csrf_token": CSRF, "voltar": "/ao-vivo?x=1"})
        self.assertEqual(reply.headers["Location"], "/ao-vivo?x=1")
        self.assertNotIn('id="aviso-campeonato"', client.get("/torneios/").data.decode())
        # "Ver o campeonato" também marca como visto; endereço de fora não é seguido.
        other = self.client(self.players[2])
        reply = other.post("/torneios/copa-teste/aviso-entendi", data={"csrf_token": CSRF, "voltar": "https://mal.example/"})
        self.assertEqual(reply.headers["Location"], "/torneios/copa-teste")
        self.assertNotIn('id="aviso-campeonato"', other.get("/").data.decode())
        # Os outros inscritos continuam vendo.
        self.assertIn('id="aviso-campeonato"', self.client(self.players[3]).get("/").data.decode())


    def test_group_stage_email_shows_the_group_not_the_rounds(self):
        """Campeonato com fase de grupos: o e-mail mostra o grupo do jogador (bandeira/escudo,
        time e jogador), não os jogos de cada rodada."""
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE tournaments SET format = 'world_cup', "
                       "settings_json = '{\"groups\": 2, \"group_legs\": 1, \"qualifiers\": 2, \"ko_legs\": 1, "
                       "\"final_legs\": 1, \"third_place\": false}'")
            for n, flag in enumerate(("br.svg", "ar.svg", "de.svg", "fr.svg")):
                team = db.execute("SELECT id, name FROM teams WHERE flag = ?", (flag,)).fetchone()
                db.execute("UPDATE tournament_entries SET team_id = ?, team_name = ? WHERE player_id = ?",
                           (team["id"], team["name"], self.players[n]))
            db.commit()
        self.client(self.players[0]).post("/torneios/copa-teste/iniciar", data={"csrf_token": CSRF})
        mails = {m["To"]: m for m in self.app.extensions["mail_outbox"] if "está valendo" in m["Subject"]}
        mail = mails["jog1@x.com"]
        html = mail.get_body(("html",)).get_content()
        text = mail.get_body(("plain",)).get_content()
        self.assertRegex(html, r"⚽ Seu grupo: Grupo [AB]")
        self.assertIn("Os 2 primeiros do grupo vão para o mata-mata.", html)
        self.assertIn('src="https://we.example/static/images/email/flags/ar.png"', html)  # a bandeira de jog1 (Argentina)
        self.assertIn("(você)", html)
        self.assertEqual(html.count('<img src="https://we.example/static/images/email/flags/'), 2)  # 2 por grupo
        self.assertNotIn("Rodada 1", html)
        self.assertNotIn("Seus jogos na primeira fase", html)
        self.assertRegex(text, r"Seu grupo: Grupo [AB]\n")
        self.assertIn("Argentina - jog1 (você)", text)
        self.assertNotIn("Rodada", text)


if __name__ == "__main__":
    unittest.main()
