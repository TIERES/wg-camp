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

    def test_free_mode_enroll_finds_existing_team(self):
        with self.app.app_context():
            db = get_db()
            game = db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES ('WE', ?, ?)",
                              (now(), now())).lastrowid
            db.execute("INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                       "settings_json, team_mode, created_at, updated_at) VALUES ('livre', 'Livre', ?, ?, 'league', "
                       "'registration', 4, '{\"legs\": 1}', 'free', ?, ?)", (game, self.players[0], now(), now()))
            db.commit()
        player = self.client(self.players[1])
        page = player.get("/torneios/livre").data.decode()
        self.assertIn('list="free-team-list"', page)
        self.assertIn('<option value="Brasil">', page)
        player.post("/torneios/livre/inscrever", data={"csrf_token": CSRF, "team_name": "brasil"})
        entry = self.query("SELECT e.team_name, t.flag FROM tournament_entries e JOIN teams t ON t.id = e.team_id")
        self.assertEqual([tuple(r) for r in entry], [("Brasil", "br.svg")])
        self.assertIn("/static/teams/", player.get("/torneios/livre?aba=geral").data.decode())

    def test_user_menu_my_tournaments_and_status(self):
        from app.tournament import store
        with self.app.app_context():
            db = get_db()
            game = db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES ('WE', ?, ?)",
                              (now(), now())).lastrowid
            for slug, name in (("minha", "Copa Minha"), ("outra", "Copa Outra")):
                db.execute("INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                           "settings_json, created_at, updated_at) VALUES (?, ?, ?, ?, 'league', 'registration', 4, "
                           "'{\"legs\": 1}', ?, ?)", (slug, name, game, self.players[0], now(), now()))
            store.enroll(db, db.execute("SELECT * FROM tournaments WHERE slug = 'minha'").fetchone(), self.players[1], "Time Um")
            db.commit()
        anon = self.client()
        self.assertEqual(anon.get("/torneios/meus-campeonatos").status_code, 302)
        self.assertEqual(anon.get("/torneios/status").status_code, 302)
        self.assertNotIn('class="user-menu"', anon.get("/").data.decode())

        player = self.client(self.players[1])
        home = player.get("/").data.decode()
        self.assertIn('class="user-name">jog1<', home)
        for label in ("Meus campeonatos", "Status", "Configurações", "Sair"):
            self.assertIn(label, home)
        mine = player.get("/torneios/meus-campeonatos").data.decode()
        self.assertIn('t-name">Copa Minha', mine)
        self.assertNotIn("Copa Outra", mine)  # nem nas sugestões
        self.assertNotIn('t-name">Copa Minha', player.get("/torneios/meus-campeonatos?status=finished").data.decode())
        self.assertIn('t-name">Copa Minha', player.get("/torneios/meus-campeonatos?nome=minha&organizador=jog0").data.decode())
        self.assertNotIn('t-name">Copa Minha', player.get("/torneios/meus-campeonatos?organizador=jog3").data.decode())
        status = player.get("/torneios/status")
        self.assertEqual(status.status_code, 200)
        self.assertIn("ainda não tem partidas confirmadas", status.data.decode())

    def test_full_flow(self):
        admin = self.client(admin=True)
        admin_page = admin.get("/admin/torneios/")
        self.assertEqual(admin_page.status_code, 200)
        self.assertNotIn("Cadastrar jogo".encode(), admin_page.data)
        admin.post("/admin/torneios/ajustes", data={
            "csrf_token": CSRF, "torneios_bonus_inicial": "1.000,00", "torneios_inscricao_max": "50,00",
            "torneios_vitoria": "1,00", "torneios_empate": "0,25", "torneios_derrota": "0",
            "torneios_abertos_por_jogador": "3"})
        game_id = self.query("SELECT id FROM game_titles WHERE code = 'we2002'")[0]["id"]

        owner = self.client(self.players[0])
        new_page = owner.get("/torneios/novo").get_data(as_text=True)
        self.assertEqual(new_page.count('name="game_title_id"'), 5)  # os 5 Winning Eleven do PS1
        self.assertIn("images/games/we3fv.svg", new_page)
        too_expensive = owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa Teste", "starts_on": "2026-12-01", "rules": "Regras", "game_title_id": game_id, "format": "knockout",
            "max_players": "4", "entry_fee": "60,00", "prize_split": "60,30,10", "ko_legs": "1", "final_legs": "1"})
        self.assertIn("no máximo".encode(), too_expensive.data)
        response = owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa Teste", "starts_on": "2026-12-01", "rules": "Regras", "game_title_id": game_id, "format": "world_cup",
            "max_players": "4", "entry_fee": "10,00", "prize_split": "60,30,10", "groups": "2",
            "qualifiers": "2", "group_legs": "1", "ko_legs": "1", "final_legs": "1", "third_place": "on"})
        self.assertEqual(response.status_code, 302)
        slug = self.query("SELECT slug FROM tournaments")[0]["slug"]
        self.assertTrue(slug.startswith("copa-teste-"))

        anon = self.app.test_client()
        page = anon.get(f"/torneios/{slug}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Entre para se inscrever".encode(), page.data)
        self.assertIn(b'class="t-header-cover" src="/static/images/games/we2002.svg"', page.data)
        listing = anon.get(f"/torneios/?jogo={game_id}").get_data(as_text=True)
        self.assertIn('class="t-cover" src="/static/images/games/we2002.svg"', listing)
        self.assertIn('game-chip active', listing)
        # Filtros por nome e organizador: trecho, sem acento nem maiúsculas.
        self.assertIn("Copa Teste", anon.get("/torneios/?nome=cópa tes").get_data(as_text=True))
        self.assertIn("Copa Teste", anon.get("/torneios/?organizador=JOG0").get_data(as_text=True))
        self.assertIn("Nenhum torneio encontrado", anon.get("/torneios/?nome=copa&organizador=jog1").get_data(as_text=True))
        self.assertIn("&#34;Copa Teste&#34;", anon.get("/torneios/").get_data(as_text=True))

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
                    # O organizador joga este torneio: quem lança é a administração.
                    admin.post(f"/torneios/{slug}/partidas/{row['id']}", data={
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

    def test_seed_games_keeps_existing_and_disables_others(self):
        from app.tournament.games import GAMES, seed_games
        with self.app.app_context():
            db = get_db()
            # Banco de antes da lista fixa: o WE 2002 cadastrado à mão e outro jogo.
            old = db.execute("SELECT id FROM game_titles WHERE code = 'we2002'").fetchone()[0]
            db.execute("UPDATE game_titles SET code = NULL, name = 'winning eleven 2002' WHERE id = ?", (old,))
            db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES ('PES 6', ?, ?)", (now(), now()))
            seed_games(db)
            seed_games(db)
            rows = db.execute("SELECT id, code, name, is_active FROM game_titles ORDER BY year, name").fetchall()
        active = [row for row in rows if row["is_active"]]
        self.assertEqual([row["code"] for row in active], [code for code, _, _ in GAMES])
        self.assertEqual(next(row["id"] for row in active if row["code"] == "we2002"), old)
        self.assertFalse(next(row for row in rows if row["name"] == "PES 6")["is_active"])

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
        data = {"csrf_token": CSRF, "name": "Liga", "starts_on": "2026-12-01", "rules": "Regras", "game_title_id": game, "format": "league", "max_players": "4",
                "entry_fee": "0", "prize_split": "60,30,10", "legs": "1"}
        self.assertEqual(owner.post("/torneios/novo", data=data).status_code, 302)
        second = owner.post("/torneios/novo", data=data)
        self.assertEqual(second.status_code, 200)
        self.assertIn("limite".encode(), second.data)

    def test_create_requires_fields(self):
        game = self.query("SELECT id FROM game_titles WHERE code = 'we2002'")[0]["id"]
        owner = self.client(self.players[0])
        page = owner.get("/torneios/novo").get_data(as_text=True)
        self.assertIn("Jogo base", page)
        self.assertIn('name="starts_on" required', page)
        self.assertIn('maxlength="5000" required', page)
        data = {"csrf_token": CSRF, "name": "Liga", "starts_on": "2026-12-01", "rules": "Regras", "game_title_id": game,
                "format": "league", "max_players": "4", "entry_fee": "0", "legs": "1"}
        for key, value, message in (("starts_on", "", "Informe o início provável."),
                                    ("rules", "   ", "Escreva o regulamento / observações."),
                                    ("game_title_id", "", "Escolha o jogo base."),
                                    ("max_players", "1", "pelo menos 2 vagas"),
                                    ("entry_fee", "10,00", "Escolha a divisão do prêmio.")):
            refused = owner.post("/torneios/novo", data={**data, key: value})
            self.assertIn(message, refused.get_data(as_text=True), key)
        self.assertEqual(self.query("SELECT COUNT(*) FROM tournaments")[0][0], 0)
        # Sem inscrição a divisão do prêmio não é pedida.
        self.assertEqual(owner.post("/torneios/novo", data=data).status_code, 302)
        self.assertEqual(self.query("SELECT prize_split FROM tournaments")[0][0], "60,30,10")

    def test_cancel_requires_confirmation_and_refunds(self):
        with self.app.app_context():
            db = get_db()
            game = db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES ('X', ?, ?)",
                              (now(), now())).lastrowid
            db.commit()
        owner = self.client(self.players[0])
        owner.post("/torneios/novo", data={"csrf_token": CSRF, "name": "Liga", "starts_on": "2026-12-01", "rules": "Regras", "game_title_id": game,
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
