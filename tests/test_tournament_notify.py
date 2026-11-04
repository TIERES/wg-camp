import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import get_db, init_db
from app.tournament import notify
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"


class TournamentNotifyTest(unittest.TestCase):
    """E-mails do torneio: início, placar reportado, contestação, fim dos jogos e encerramento automático."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
            db = get_db()
            self.players = [add_player(db, f"jog{i}") for i in range(3)]
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

    def test_flow(self):
        # jog0 organiza (sem jogar); jog1 x jog2 numa final de jogo único.
        owner, home_player, away_player = (self.client(p) for p in self.players)
        response = owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa Email", "starts_on": "2026-12-01", "rules": "Regras",
            "game_title_id": self.game, "format": "knockout", "max_players": "2", "entry_fee": "0",
            "ko_legs": "1", "final_legs": "1"})
        self.assertEqual(response.status_code, 302)
        slug = self.query("SELECT slug FROM tournaments")[0][0]
        for client, team in ((home_player, "Time Um"), (away_player, "Time Dois")):
            client.post(f"/torneios/{slug}/inscrever", data={"csrf_token": CSRF, "team_name": team})
        self.mails()

        owner.post(f"/torneios/{slug}/iniciar", data={"csrf_token": CSRF})
        started = self.mails()
        self.assertEqual(sorted(m[0] for m in started), ["jog1@x.com", "jog2@x.com"])
        self.assertIn("começou", started[0][1])
        self.assertIn("/torneios/meus-jogos", started[0][2])
        self.assertIn("Time Um (jog1) x Time Dois (jog2)", started[0][2].replace("Time Dois (jog2) x Time Um (jog1)",
                                                                                 "Time Um (jog1) x Time Dois (jog2)"))

        match = self.query("SELECT * FROM tournament_matches")[0]
        reporter = home_player if match["home_entry_id"] == self.query(
            "SELECT id FROM tournament_entries WHERE player_id = ?", self.players[1])[0][0] else away_player
        other_email = "jog2@x.com" if reporter is home_player else "jog1@x.com"
        reporter.post(f"/torneios/{slug}/jogos/{match['id']}/reportar",
                      data={"csrf_token": CSRF, "home_goals": "2", "away_goals": "1"})
        (reported,) = self.mails()
        self.assertEqual(reported[0], other_email)  # só quem precisa aprovar
        self.assertIn("2 x 1", reported[2])
        self.assertIn(f"/torneios/{slug}/jogos/{match['id']}", reported[2])

        disputer = away_player if reporter is home_player else home_player
        disputer.post(f"/torneios/{slug}/jogos/{match['id']}/contestar", data={
            "csrf_token": CSRF, "home_goals": "1", "away_goals": "1", "home_pens": "3", "away_pens": "4",
            "comment": "Foi empate e ganhei nos pênaltis"})
        disputed = self.mails()
        self.assertEqual(sorted(m[0] for m in disputed), ["jog0@x.com", "jog1@x.com", "jog2@x.com"])
        for _, _, body in disputed:
            self.assertIn("2 x 1", body)
            self.assertIn("1 x 1 (3 x 4 nos pênaltis)", body)
            self.assertIn("Foi empate", body)
        self.assertIn("/gerenciar#jogo-", next(b for to, _, b in disputed if to == "jog0@x.com"))

        # O organizador decide: era o último jogo -> aviso para encerrar (uma vez só).
        owner.post(f"/torneios/{slug}/partidas/{match['id']}", data={
            "csrf_token": CSRF, "home_goals": "1", "away_goals": "1", "home_pens": "3", "away_pens": "4"})
        (done,) = self.mails()
        self.assertEqual(done[0], "jog0@x.com")
        self.assertIn("encerre", done[1].lower())
        self.assertIn(f"/torneios/{slug}/gerenciar", done[2])
        self.assertIsNotNone(self.query("SELECT all_played_at FROM tournaments")[0][0])
        self.assertIn("encerra sozinho", owner.get(f"/torneios/{slug}/gerenciar").get_data(as_text=True))
        # Resultado apagado: não está mais tudo jogado; lançado de novo, avisa de novo.
        owner.post(f"/torneios/{slug}/partidas/{match['id']}", data={"csrf_token": CSRF, "acao": "apagar"})
        self.assertIsNone(self.query("SELECT all_played_at FROM tournaments")[0][0])
        owner.post(f"/torneios/{slug}/partidas/{match['id']}", data={
            "csrf_token": CSRF, "home_goals": "1", "away_goals": "1", "home_pens": "3", "away_pens": "4"})
        self.assertEqual([m[0] for m in self.mails()], ["jog0@x.com"])
        with self.app.test_request_context():
            notify.sweep(get_db())
        self.assertEqual(self.mails(), [])
        self.assertEqual(self.query("SELECT status FROM tournaments")[0][0], "running")

        # 24 h sem encerrar: encerra sozinho.
        with self.app.test_request_context():
            db = get_db()
            db.execute("UPDATE tournaments SET all_played_at = ?",
                       ((datetime.now(timezone.utc) - timedelta(hours=25)).replace(microsecond=0).isoformat(),))
            db.commit()
            notify.sweep(db)
        self.assertEqual(self.query("SELECT status FROM tournaments")[0][0], "finished")
        self.assertEqual(len(self.query("SELECT 1 FROM tournament_entries WHERE final_position IS NOT NULL")), 2)

    def test_stage_end_and_next_stage(self):
        # Pontos corridos + mata-mata com 3 jogadores (org organiza sem jogar), 2 classificados.
        with self.app.app_context():
            db = get_db()
            organizer = add_player(db, "org")
            db.commit()
        clients = [self.client(p) for p in self.players]
        owner = self.client(organizer)
        owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Liga Email", "starts_on": "2026-12-01", "rules": "Regras",
            "game_title_id": self.game, "format": "league_knockout", "max_players": "3", "entry_fee": "0",
            "legs": "1", "playoff_qualifiers": "2", "ko_legs": "1", "final_legs": "1"})
        slug = self.query("SELECT slug FROM tournaments")[0][0]
        for index, client in enumerate(clients):
            client.post(f"/torneios/{slug}/inscrever", data={"csrf_token": CSRF, "team_name": f"Time {index}"})
        owner.post(f"/torneios/{slug}/iniciar", data={"csrf_token": CSRF})
        self.assertEqual(self.query("SELECT status FROM tournaments")[0][0], "running")
        self.mails()

        matches = self.query("SELECT id FROM tournament_matches ORDER BY id")
        for match in matches:
            owner.post(f"/torneios/{slug}/partidas/{match['id']}", data={
                "csrf_token": CSRF, "home_goals": "1", "away_goals": "0"})
        (done,) = self.mails()  # só no último jogo, e não é o "encerre"
        self.assertEqual(done[0], "org@x.com")
        self.assertIn("gere a próxima fase", done[1])
        self.assertIn(f"/torneios/{slug}/gerenciar", done[2])
        self.assertIsNone(self.query("SELECT all_played_at FROM tournaments")[0][0])
        with self.app.test_request_context():
            notify.sweep(get_db())
        self.assertEqual(self.mails(), [])  # uma vez só

        owner.post(f"/torneios/{slug}/proxima-fase", data={"csrf_token": CSRF})
        started = self.mails()
        self.assertEqual(len(started), 2)  # só os 2 classificados
        self.assertTrue(all("Mata-mata" in subject for _, subject, _ in started))
        final = self.query("SELECT * FROM tournament_matches WHERE stage_id = "
                           "(SELECT MAX(id) FROM tournament_stages)")[0]
        for _, _, body in started:
            self.assertIn("Rodada", body)
        owner.post(f"/torneios/{slug}/partidas/{final['id']}", data={
            "csrf_token": CSRF, "home_goals": "2", "away_goals": "0"})
        (end,) = self.mails()
        self.assertIn("encerre", end[1].lower())

    def test_admin_email_gets_dispute_when_organizer_plays(self):
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO users (username, password_hash, created_at, updated_at) "
                       "VALUES ('adm', 'x', '2026-01-01', '2026-01-01')")
            db.commit()
        admin = self.app.test_client()
        with admin.session_transaction() as session:
            session["csrf_token"] = CSRF
            session["user_id"] = 1
        admin.post("/admin/torneios/email", data={"csrf_token": CSRF, "emails": "nao-e-email"})
        self.assertEqual(self.query("SELECT COUNT(*) FROM site_settings WHERE key = 'torneios_admin_email'")[0][0], 0)
        admin.post("/admin/torneios/email", data={"csrf_token": CSRF, "emails": "adm@wecamp.com; outro@wecamp.com"})
        self.assertIn('value="adm@wecamp.com, outro@wecamp.com"', admin.get("/admin/torneios/").get_data(as_text=True))

        # O organizador (jog0) joga contra jog1.
        owner, rival = self.client(self.players[0]), self.client(self.players[1])
        owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa Adm", "starts_on": "2026-12-01", "rules": "Regras",
            "game_title_id": self.game, "format": "knockout", "max_players": "2", "entry_fee": "0",
            "ko_legs": "1", "final_legs": "1"})
        slug = self.query("SELECT slug FROM tournaments")[0][0]
        for client, team in ((owner, "Time Um"), (rival, "Time Dois")):
            client.post(f"/torneios/{slug}/inscrever", data={"csrf_token": CSRF, "team_name": team})
        owner.post(f"/torneios/{slug}/iniciar", data={"csrf_token": CSRF})
        match_id = self.query("SELECT id FROM tournament_matches")[0][0]
        owner.post(f"/torneios/{slug}/jogos/{match_id}/reportar", data={"csrf_token": CSRF, "home_goals": "3",
                                                                        "away_goals": "0"})
        self.mails()
        rival.post(f"/torneios/{slug}/jogos/{match_id}/contestar", data={"csrf_token": CSRF, "comment": "Não foi isso"})
        sent = {to: (subject, body) for to, subject, body in self.mails()}
        self.assertEqual(sorted(sent), ["adm@wecamp.com", "jog0@x.com", "jog1@x.com", "outro@wecamp.com"])
        self.assertTrue(sent["adm@wecamp.com"][0].startswith("[Administração]"))
        self.assertIn("3 x 0", sent["adm@wecamp.com"][1])
        self.assertIn("administração do WE Camp decide", sent["jog1@x.com"][1])


if __name__ == "__main__":
    unittest.main()
