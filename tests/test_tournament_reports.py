import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import get_db, init_db, now
from app.tournament import formats, reports, store
from app.tournament.store import TournamentError
from app.wallet import balance
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"


class ReportsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.ctx = self.app.app_context()
        self.ctx.push()
        init_db()
        self.db = get_db()
        self.game = self.db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES ('WE', ?, ?)",
                                    (now(), now())).lastrowid
        self.players = [add_player(self.db, f"p{i}") for i in range(5)]
        self.db.execute("INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('adm', 'x', ?, ?)",
                        (now(), now()))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.ctx.pop()
        self.temp.cleanup()

    def tournament(self, fmt="league", players=2, organizer=0, **config):
        cfg = formats.normalize(fmt, {k: str(v) for k, v in config.items()})
        tid = self.db.execute(
            "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
            "settings_json, created_at, updated_at) VALUES (?, 'T', ?, ?, ?, 'registration', ?, ?, ?, ?)",
            (f"t-{random.random()}", self.game, self.players[organizer], fmt, players, json.dumps(cfg), now(),
             now())).lastrowid
        for i, pid in enumerate(self.players[:players]):
            store.enroll(self.db, self.t(tid), pid, f"Time {i}")
        store.start(self.db, self.t(tid), random.Random(2))
        self.db.commit()
        return tid

    def t(self, tid):
        return store.get_tournament(self.db, tid)

    def sides(self, match_id):
        m = self.db.execute("SELECT * FROM tournament_matches WHERE id = ?", (match_id,)).fetchone()
        player = lambda entry: self.db.execute("SELECT player_id FROM tournament_entries WHERE id = ?",
                                               (entry,)).fetchone()[0]
        return m, player(m["home_entry_id"]), player(m["away_entry_id"])

    def status(self, match_id):
        return self.db.execute("SELECT status FROM tournament_matches WHERE id = ?", (match_id,)).fetchone()[0]

    def test_report_then_confirm_pays_and_counts(self):
        tid = self.tournament(legs=1)
        m, home, away = self.sides(self.db.execute("SELECT id FROM tournament_matches").fetchone()[0])
        with self.assertRaises(TournamentError):
            reports.report(self.db, self.t(tid), m["id"], self.players[4], (1, 0, None, None))  # não joga
        reports.report(self.db, self.t(tid), m["id"], home, (2, 1, None, None), "jogão")
        self.db.commit()
        self.assertEqual(self.status(m["id"]), "reported")
        self.assertEqual(balance(self.db, home), 100000)  # nada pago antes de confirmar
        table = store.group_tables(self.db, store.stages_of(self.db, tid)[0]["id"])[0][1]
        self.assertEqual(sum(row["played"] for row in table), 0)
        with self.assertRaises(TournamentError):
            reports.confirm(self.db, self.t(tid), m["id"], home)  # quem reportou não confirma
        with self.assertRaises(TournamentError):
            reports.report(self.db, self.t(tid), m["id"], away, (0, 0, None, None))  # adversário não reporta por cima
        with self.assertRaises(TournamentError):
            reports.report(self.db, self.t(tid), m["id"], home, (3, 1, None, None))  # quem reportou não muda
        # O organizador libera um novo reporte (descarta o enviado).
        reports.clear(self.db, self.t(tid), m["id"], user_id=1)
        self.assertEqual(self.status(m["id"]), "scheduled")
        reports.report(self.db, self.t(tid), m["id"], home, (3, 1, None, None))
        reports.confirm(self.db, self.t(tid), m["id"], away)
        self.db.commit()
        m2 = self.db.execute("SELECT * FROM tournament_matches WHERE id = ?", (m["id"],)).fetchone()
        self.assertEqual((m2["status"], m2["home_goals"], m2["away_goals"]), ("confirmed", 3, 1))
        self.assertEqual(balance(self.db, home), 100050)
        self.assertEqual([h["action"] for h in reports.history(self.db, m["id"])],
                         ["report", "clear", "report", "confirm"])
        with self.assertRaises(TournamentError):
            reports.report(self.db, self.t(tid), m["id"], home, (0, 0, None, None))

    def test_dispute_and_resolve(self):
        tid = self.tournament(legs=1)
        m, home, away = self.sides(self.db.execute("SELECT id FROM tournament_matches").fetchone()[0])
        reports.report(self.db, self.t(tid), m["id"], away, (0, 3, None, None))
        with self.assertRaises(TournamentError):
            reports.dispute(self.db, self.t(tid), m["id"], home, "")  # precisa de motivo
        reports.dispute(self.db, self.t(tid), m["id"], home, "Foi 1 x 1, ele saiu antes", (1, 1, None, None))
        self.assertEqual(self.status(m["id"]), "disputed")
        with self.assertRaises(TournamentError):
            reports.report(self.db, self.t(tid), m["id"], away, (0, 3, None, None))
        reports.resolve(self.db, self.t(tid), m["id"], (1, 1, None, None), user_id=1, comment="Visto no replay")
        self.db.commit()
        self.assertEqual(self.status(m["id"]), "confirmed")
        actions = [h["action"] for h in reports.history(self.db, m["id"])]
        self.assertEqual(actions, ["report", "dispute", "resolve"])
        reports.clear(self.db, self.t(tid), m["id"], user_id=1)
        self.assertEqual(self.status(m["id"]), "scheduled")
        self.assertEqual(balance(self.db, home), 100000)

    def test_auto_confirm_after_deadline(self):
        tid = self.tournament(legs=1)
        m, home, away = self.sides(self.db.execute("SELECT id FROM tournament_matches").fetchone()[0])
        reports.report(self.db, self.t(tid), m["id"], home, (2, 2, None, None))
        self.db.commit()
        self.assertEqual(reports.auto_confirm(self.db), 0)
        self.assertEqual(reports.auto_confirm(self.db, datetime.now(timezone.utc) + timedelta(hours=25)), 1)
        self.assertEqual(self.status(m["id"]), "confirmed")
        self.assertEqual(reports.history(self.db, m["id"])[-1]["action"], "auto_confirm")
        self.assertEqual(balance(self.db, away), 100025)

    def test_knockout_report_checks_pens_without_side_effects(self):
        tid = self.tournament("knockout", players=2, ko_legs=1, final_legs=1)
        m, home, away = self.sides(self.db.execute("SELECT id FROM tournament_matches").fetchone()[0])
        with self.assertRaises(TournamentError):
            reports.report(self.db, self.t(tid), m["id"], home, (1, 1, None, None))
        self.db.rollback()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM wallet_entries WHERE kind != 'bonus_inicial'")
                         .fetchone()[0], 0)
        self.assertEqual(self.status(m["id"]), "scheduled")
        reports.report(self.db, self.t(tid), m["id"], home, (1, 1, 4, 2))
        self.assertIsNone(self.db.execute("SELECT winner_entry_id FROM tournament_ties").fetchone()[0])
        reports.confirm(self.db, self.t(tid), m["id"], away)
        self.assertEqual(self.db.execute("SELECT winner_entry_id FROM tournament_ties").fetchone()[0],
                         m["home_entry_id"])

    def test_auto_confirm_that_no_longer_fits_becomes_dispute(self):
        tid = self.tournament(legs=1)
        m, home, away = self.sides(self.db.execute("SELECT id FROM tournament_matches").fetchone()[0])
        reports.report(self.db, self.t(tid), m["id"], home, (1, 0, None, None))
        self.db.execute("UPDATE tournament_stages SET status = 'done'")
        self.db.commit()
        reports.auto_confirm(self.db, datetime.now(timezone.utc) + timedelta(days=2))
        self.assertEqual(self.status(m["id"]), "disputed")

    # --- web --------------------------------------------------------------

    def client(self, player=None, admin=False):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            if player:
                session["player_id"] = player
            if admin:
                session["user_id"] = 1
        return client

    def test_web_flow_and_organizer_conflict(self):
        tid = self.tournament(legs=1)  # o organizador (p0) joga
        slug = self.t(tid)["slug"]
        m, home, away = self.sides(self.db.execute("SELECT id FROM tournament_matches").fetchone()[0])
        home_client, away_client = self.client(home), self.client(away)
        self.assertIn("Meus jogos".encode(), home_client.get(f"/torneios/{slug}").data)
        self.assertIn("Reportar o placar".encode(), home_client.get(f"/torneios/{slug}/jogos/{m['id']}").data)
        self.assertEqual(self.app.test_client().get(f"/torneios/{slug}/jogos/{m['id']}").status_code, 200)

        # O organizador não decide o próprio jogo.
        organizer = self.client(self.players[0])
        organizer.post(f"/torneios/{slug}/partidas/{m['id']}", data={
            "csrf_token": CSRF, "home_goals": "5", "away_goals": "0", "acao": "salvar"})
        self.assertEqual(self.status(m["id"]), "scheduled")

        home_client.post(f"/torneios/{slug}/jogos/{m['id']}/reportar", data={
            "csrf_token": CSRF, "home_goals": "2", "away_goals": "0", "comment": "fácil"})
        self.assertEqual(self.status(m["id"]), "reported")
        page = away_client.get(f"/torneios/{slug}/jogos/{m['id']}").data.decode()
        self.assertIn("Confirmar este placar", page)
        self.assertIn("fácil", page)
        self.assertIn("Confirmar partida".encode(), away_client.get("/torneios/meus-jogos").data)
        self.assertIn("todo-badge", away_client.get("/torneios/").data.decode())  # badge no nick e em Meus jogos
        away_client.post(f"/torneios/{slug}/jogos/{m['id']}/contestar", data={
            "csrf_token": CSRF, "home_goals": "", "away_goals": "", "comment": "Não foi isso"})
        self.assertEqual(self.status(m["id"]), "disputed")
        self.assertIn("Placares para acompanhar".encode(), self.client(admin=True).get(f"/torneios/{slug}/gerenciar").data)
        self.client(admin=True).post(f"/torneios/{slug}/partidas/{m['id']}", data={
            "csrf_token": CSRF, "home_goals": "1", "away_goals": "1", "acao": "salvar", "comment": "replay",
            "voltar": "jogo"})
        self.assertEqual(self.status(m["id"]), "confirmed")
        history = self.client().get(f"/torneios/{slug}/jogos/{m['id']}").data.decode()
        self.assertIn("adm (administração)", history)
        self.assertNotIn("todo-badge", away_client.get("/torneios/").data.decode())


    def test_confirm_from_my_matches(self):
        tid = self.tournament(legs=1)
        slug = self.t(tid)["slug"]
        m, home, away = self.sides(self.db.execute("SELECT id FROM tournament_matches").fetchone()[0])
        reports.report(self.db, self.t(tid), m["id"], home, (3, 1, None, None))
        self.db.commit()
        home_page = self.client(home).get("/torneios/meus-jogos").get_data(as_text=True)
        self.assertNotIn("Confirmar partida", home_page)  # quem reportou não confirma
        away_client = self.client(away)
        page = away_client.get("/torneios/meus-jogos").get_data(as_text=True)
        self.assertIn("mm-confirm", page)
        self.assertIn("Confirmação automática em", page)
        self.assertIn(f"/torneios/{slug}/jogos/{m['id']}/confirmar", page)
        reply = away_client.post(f"/torneios/{slug}/jogos/{m['id']}/confirmar",
                                 data={"csrf_token": CSRF, "voltar": "meus-jogos"})
        self.assertTrue(reply.headers["Location"].endswith("/torneios/meus-jogos"))
        self.assertEqual(self.status(m["id"]), "confirmed")
        self.assertNotIn("Confirmar partida", away_client.get("/torneios/meus-jogos").get_data(as_text=True))

    def test_reporter_accepts_disputed_score(self):
        tid = self.tournament(legs=1)
        m, home, away = self.sides(self.db.execute("SELECT id FROM tournament_matches").fetchone()[0])
        reports.report(self.db, self.t(tid), m["id"], home, (5, 1, None, None))
        reports.dispute(self.db, self.t(tid), m["id"], away, "Usuario cadastrou errado", (4, 1, None, None))
        self.db.commit()
        with self.assertRaises(TournamentError):
            reports.accept_dispute(self.db, self.t(tid), m["id"], away)  # quem contestou não aceita o próprio
        with self.assertRaises(TournamentError):
            reports.accept_dispute(self.db, self.t(tid), m["id"], self.players[4])  # não joga
        reports.accept_dispute(self.db, self.t(tid), m["id"], home)
        self.db.commit()
        m2 = self.db.execute("SELECT * FROM tournament_matches WHERE id = ?", (m["id"],)).fetchone()
        self.assertEqual((m2["status"], m2["home_goals"], m2["away_goals"]), ("confirmed", 4, 1))
        self.assertEqual([h["action"] for h in reports.history(self.db, m["id"])], ["report", "dispute", "confirm"])

    def test_dispute_without_score_stays_with_organizer(self):
        tid = self.tournament(legs=1)
        m, home, away = self.sides(self.db.execute("SELECT id FROM tournament_matches").fetchone()[0])
        reports.report(self.db, self.t(tid), m["id"], home, (2, 0, None, None))
        reports.dispute(self.db, self.t(tid), m["id"], away, "Não jogamos ainda")
        with self.assertRaises(TournamentError):
            reports.accept_dispute(self.db, self.t(tid), m["id"], home)
        self.assertEqual(self.status(m["id"]), "disputed")

if __name__ == "__main__":
    unittest.main()
