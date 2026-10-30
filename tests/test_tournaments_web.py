import re
import tempfile
import unittest
from pathlib import Path

from app.db import get_db, init_db, now
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"


class TournamentsWebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
            db = get_db()
            self.players = [add_player(db, f"jog{i}") for i in range(5)]
            db.execute("INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('adm', 'x', ?, ?)",
                       (now(), now()))
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

    def test_full_flow(self):
        admin = self.client(admin=True)
        self.assertEqual(admin.get("/admin/torneios/").status_code, 200)
        admin.post("/admin/torneios/jogos", data={"csrf_token": CSRF, "name": "Winning Eleven 2002",
                                                  "platform": "PS1", "is_active": "on"})
        admin.post("/admin/torneios/ajustes", data={
            "csrf_token": CSRF, "torneios_bonus_inicial": "1.000,00", "torneios_inscricao_max": "50,00",
            "torneios_vitoria": "1,00", "torneios_empate": "0,25", "torneios_derrota": "0",
            "torneios_abertos_por_jogador": "3"})
        game_id = self.query("SELECT id FROM game_titles")[0]["id"]

        owner = self.client(self.players[0])
        self.assertEqual(owner.get("/torneios/novo").status_code, 200)
        too_expensive = owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa Teste", "game_title_id": game_id, "format": "knockout",
            "max_players": "4", "entry_fee": "60,00", "prize_split": "60,30,10", "ko_legs": "1", "final_legs": "1"})
        self.assertIn("no máximo".encode(), too_expensive.data)
        response = owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa Teste", "game_title_id": game_id, "format": "world_cup",
            "max_players": "4", "entry_fee": "10,00", "prize_split": "60,30,10", "groups": "2",
            "qualifiers": "2", "group_legs": "1", "ko_legs": "1", "final_legs": "1", "third_place": "on"})
        self.assertEqual(response.status_code, 302)
        slug = self.query("SELECT slug FROM tournaments")[0]["slug"]
        self.assertTrue(slug.startswith("copa-teste-"))

        anon = self.app.test_client()
        page = anon.get(f"/torneios/{slug}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Entre para se inscrever".encode(), page.data)
        self.assertEqual(anon.get("/torneios/").status_code, 200)

        for pid in self.players[:4]:
            self.client(pid).post(f"/torneios/{slug}/inscrever", data={"csrf_token": CSRF, "team_name": f"Time {pid}"})
        self.assertEqual(len(self.query("SELECT * FROM tournament_entries")), 4)
        self.assertEqual(self.query("SELECT wallet_cents FROM players WHERE id = ?", self.players[1])[0][0], 99000)

        # Só o organizador (ou a administração) gerencia.
        self.assertEqual(self.client(self.players[1]).get(f"/torneios/{slug}/gerenciar").status_code, 403)
        self.assertEqual(self.client(self.players[1]).post(f"/torneios/{slug}/iniciar",
                                                            data={"csrf_token": CSRF}).status_code, 403)
        owner.post(f"/torneios/{slug}/iniciar", data={"csrf_token": CSRF})
        self.assertEqual(self.query("SELECT status FROM tournaments")[0][0], "running")

        def play_all():
            for _ in range(20):
                pending = self.query("SELECT m.id FROM tournament_matches m JOIN tournament_stages s "
                                     "ON s.id = m.stage_id WHERE m.status != 'confirmed' AND s.status = 'running'")
                if not pending:
                    return
                for row in pending:
                    owner.post(f"/torneios/{slug}/partidas/{row['id']}", data={
                        "csrf_token": CSRF, "home_goals": "2", "away_goals": "1", "acao": "salvar"})

        play_all()
        manage = owner.get(f"/torneios/{slug}/gerenciar")
        self.assertIn("Gerar próxima fase".encode(), manage.data)
        owner.post(f"/torneios/{slug}/proxima-fase", data={"csrf_token": CSRF})
        play_all()
        for tab in ("geral", "tabela", "jogos"):
            self.assertEqual(anon.get(f"/torneios/{slug}?aba={tab}").status_code, 200)
        bracket = anon.get(f"/torneios/{slug}?aba=tabela").data.decode()
        self.assertIn("Final", bracket)
        self.assertIn("Disputa de 3º lugar", bracket)
        owner.post(f"/torneios/{slug}/encerrar", data={"csrf_token": CSRF})
        self.assertEqual(self.query("SELECT status FROM tournaments")[0][0], "finished")
        prizes = [r[0] for r in self.query("SELECT amount_cents FROM wallet_entries WHERE kind = 'premio' ORDER BY ref_key")]
        self.assertEqual(prizes, [2400, 1200, 400])
        self.assertIn("1º".encode(), anon.get(f"/torneios/{slug}?aba=geral").data)

        wallet = self.client(self.players[1]).get("/torneios/carteira")
        self.assertEqual(wallet.status_code, 200)
        self.assertIn(b"W$", wallet.data)
        total = self.query("SELECT SUM(wallet_cents), (SELECT SUM(amount_cents) FROM wallet_entries) FROM players")[0]
        self.assertEqual(total[0], total[1])

    def test_admin_wallet_adjust_and_nav_balance(self):
        admin = self.client(admin=True)
        admin.post("/admin/torneios/carteira", data={"csrf_token": CSRF, "username": "jog1", "amount": "5,50",
                                                     "direction": "credito", "note": "Teste"})
        admin.post("/admin/torneios/carteira", data={"csrf_token": CSRF, "username": "jog1", "amount": "5,50",
                                                     "direction": "credito", "note": "Teste"})
        self.assertEqual(self.query("SELECT wallet_cents FROM players WHERE id = ?", self.players[1])[0][0], 101100)
        page = self.client(self.players[1]).get("/conta/").data.decode()
        self.assertIn("W$1.011,00", page)

    def test_player_open_tournament_limit(self):
        with self.app.app_context():
            db = get_db()
            game = db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES ('X', ?, ?)",
                              (now(), now())).lastrowid
            db.execute("INSERT INTO site_settings (key, value, updated_at) VALUES ('torneios_abertos_por_jogador', '1', ?)",
                       (now(),))
            db.commit()
        owner = self.client(self.players[2])
        data = {"csrf_token": CSRF, "name": "Liga", "game_title_id": game, "format": "league", "max_players": "4",
                "entry_fee": "0", "prize_split": "60,30,10", "legs": "1"}
        self.assertEqual(owner.post("/torneios/novo", data=data).status_code, 302)
        second = owner.post("/torneios/novo", data=data)
        self.assertEqual(second.status_code, 200)
        self.assertIn("limite".encode(), second.data)

    def test_cancel_requires_confirmation_and_refunds(self):
        with self.app.app_context():
            db = get_db()
            game = db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES ('X', ?, ?)",
                              (now(), now())).lastrowid
            db.commit()
        owner = self.client(self.players[0])
        owner.post("/torneios/novo", data={"csrf_token": CSRF, "name": "Liga", "game_title_id": game,
                                           "format": "league", "max_players": "4", "entry_fee": "20",
                                           "prize_split": "60,30,10", "legs": "1"})
        slug = self.query("SELECT slug FROM tournaments")[0]["slug"]
        self.client(self.players[3]).post(f"/torneios/{slug}/inscrever", data={"csrf_token": CSRF, "team_name": "Real Suciedad"})
        owner.post(f"/torneios/{slug}/cancelar", data={"csrf_token": CSRF, "confirmar": "nao"})
        self.assertEqual(self.query("SELECT status FROM tournaments")[0][0], "registration")
        owner.post(f"/torneios/{slug}/cancelar", data={"csrf_token": CSRF, "confirmar": "CANCELAR"})
        self.assertEqual(self.query("SELECT status FROM tournaments")[0][0], "cancelled")
        self.assertEqual(self.query("SELECT wallet_cents FROM players WHERE id = ?", self.players[3])[0][0], 100000)


if __name__ == "__main__":
    unittest.main()
