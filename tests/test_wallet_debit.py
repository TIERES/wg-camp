"""Débito por derrota: valor negativo no ajuste, desconto só até W$0,00 e os avisos de saldo zerado."""
import tempfile
import unittest
from pathlib import Path

from app.db import get_db, init_db, now
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"


class WalletDebitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
            db = get_db()
            self.players = [add_player(db, f"jog{i}") for i in range(3)]
            db.execute("INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('adm', 'x', ?, ?)",
                       (now(), now()))
            db.execute("INSERT OR REPLACE INTO site_settings (key, value, updated_at) VALUES "
                       "('torneios_admin_email', 'adm@x.com', ?)", (now(),))
            self.game = db.execute("SELECT id FROM game_titles WHERE is_active = 1 ORDER BY id LIMIT 1").fetchone()[0]
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

    def save_settings(self, derrota):
        return self.client(admin=True).post("/admin/torneios/ajustes", data={
            "csrf_token": CSRF, "torneios_bonus_inicial": "W$ 1.000,00", "torneios_inscricao_max": "W$ 50,00",
            "torneios_vitoria": "W$ 0,50", "torneios_empate": "W$ 0,25", "torneios_derrota": derrota,
            "torneios_abertos_por_jogador": "3", "torneios_confirmacao_horas": "24"})

    def balance(self, player):
        return self.query("SELECT wallet_cents FROM players WHERE id = ?", player)[0][0]

    def play_and_lose(self, name):
        """jog0 organiza um mata-mata de 2 (jog1 x jog2) e lança uma vitória de jog1; devolve o slug."""
        owner = self.client(self.players[0])
        owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": name, "starts_on": "2026-12-01", "rules": "Regras",
            "game_title_id": self.game, "format": "knockout", "max_players": "2", "entry_fee": "0",
            "ko_legs": "1", "final_legs": "1"})
        slug = self.query("SELECT slug FROM tournaments WHERE name = ?", name)[0][0]
        for player, team in ((self.players[1], "Time Um"), (self.players[2], "Time Dois")):
            self.client(player).post(f"/torneios/{slug}/inscrever", data={"csrf_token": CSRF, "team_name": team})
        owner.post(f"/torneios/{slug}/iniciar", data={"csrf_token": CSRF})
        match = self.query("SELECT m.* FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
                           "JOIN tournaments t ON t.id = s.tournament_id WHERE t.slug = ?", slug)[0]
        home_player = self.query("SELECT player_id FROM tournament_entries WHERE id = ?", match["home_entry_id"])[0][0]
        winner_home = home_player == self.players[1]
        owner.post(f"/torneios/{slug}/partidas/{match['id']}", data={
            "csrf_token": CSRF, "home_goals": "2" if winner_home else "0", "away_goals": "0" if winner_home else "2",
            "acao": "salvar"})
        return slug

    def test_negative_setting_is_saved_and_shown(self):
        self.save_settings("-W$ 0,60")
        self.assertEqual(self.query("SELECT value FROM site_settings WHERE key = 'torneios_derrota'")[0][0], "-60")
        page = self.client(admin=True).get("/admin/torneios/").data.decode()
        self.assertIn("Débito por derrota", page)
        self.assertIn('data-wcash-signed', page)
        self.assertIn('value="-W$0,60"', page)
        # os outros valores continuam sem aceitar negativo
        self.client(admin=True).post("/admin/torneios/ajustes", data={
            "csrf_token": CSRF, "torneios_bonus_inicial": "-W$ 1,00", "torneios_inscricao_max": "W$ 50,00",
            "torneios_vitoria": "W$ 0,50", "torneios_empate": "W$ 0,25", "torneios_derrota": "W$ 0,00",
            "torneios_abertos_por_jogador": "3", "torneios_confirmacao_horas": "24"})
        self.assertEqual(self.query("SELECT value FROM site_settings WHERE key = 'torneios_derrota'")[0][0], "-60")
        wallet = self.client(self.players[1]).get("/torneios/carteira").data.decode()
        self.assertIn("derrota desconta W$0,60 (só até zerar o saldo)", wallet)

    def test_debit_stops_at_zero_and_warns_once(self):
        self.save_settings("-W$ 0,60")
        with self.app.app_context():
            db = get_db()
            # jog2 (vai perder) com só W$0,50: o débito de W$0,60 para em W$0,00
            db.execute("UPDATE players SET wallet_cents = 50, wallet_started_at = ? WHERE id = ?",
                       (now(), self.players[2]))
            db.commit()
        self.mails()
        self.play_and_lose("Copa Débito")
        self.assertEqual(self.balance(self.players[2]), 0)
        debit = self.query("SELECT amount_cents FROM wallet_entries WHERE player_id = ? AND kind = 'derrota'",
                           self.players[2])
        self.assertEqual([r[0] for r in debit], [-50])
        self.mails()  # e-mails do torneio
        # a próxima página de torneio avisa o jogador e a administração, uma vez só
        self.client().get("/torneios/")
        sent = self.mails()
        self.assertEqual(sorted(m[0] for m in sent), ["adm@x.com", "jog2@x.com"])
        player_mail = next(m for m in sent if m[0] == "jog2@x.com")
        self.assertIn("estará creditando um novo valor", player_mail[2])
        admin_mail = next(m for m in sent if m[0] == "adm@x.com")
        self.assertIn("Jogador sem saldo W$: jog2", admin_mail[1])
        self.client().get("/torneios/")
        self.assertEqual(self.mails(), [])
        wallet = self.client(self.players[2]).get("/torneios/carteira").data.decode()
        self.assertIn("estará creditando um novo valor", wallet)

        # sem saldo, nova derrota não desconta nada nem avisa de novo
        self.play_and_lose("Copa Débito 2")
        self.assertEqual(self.balance(self.players[2]), 0)
        self.assertEqual(len(self.query("SELECT id FROM wallet_entries WHERE player_id = ? AND kind = 'derrota'",
                                        self.players[2])), 1)
        self.mails()
        self.client().get("/torneios/")
        self.assertEqual(self.mails(), [])
        # o vencedor recebe normalmente
        self.assertGreater(self.balance(self.players[1]), 0)

    def test_partial_debit_does_not_warn(self):
        self.save_settings("-W$ 0,60")
        self.play_and_lose("Copa Parcial")
        self.assertEqual(self.balance(self.players[2]), 100000 - 60)
        self.mails()
        self.client().get("/torneios/")
        self.assertEqual(self.mails(), [])


if __name__ == "__main__":
    unittest.main()
