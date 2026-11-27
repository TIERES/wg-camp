"""Reabrir as inscrições (store.reopen_registration): campeonato iniciado sem
nenhum jogo disputado volta para inscrições abertas - entra quem faltou e o
sorteio é feito de novo ao iniciar."""
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.db import get_db, init_db, now
from app.tournament import formats, store
from app.tournament.store import TournamentError
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"


class ReopenStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.ctx = self.app.app_context()
        self.ctx.push()
        init_db()
        self.db = get_db()
        self.game = self.db.execute("SELECT id FROM game_titles ORDER BY id LIMIT 1").fetchone()[0]
        self.players = [add_player(self.db, f"p{i}") for i in range(12)]
        self.rng = random.Random(7)

    def tearDown(self):
        self.db.close()
        self.ctx.pop()
        self.temp.cleanup()

    def tournament(self, players, fmt="knockout", **config):
        cfg = formats.normalize(fmt, {k: str(v) for k, v in config.items()})
        tid = self.db.execute(
            "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
            "settings_json, created_at, updated_at) VALUES (?, 'Copa', ?, ?, ?, 'registration', 16, ?, ?, ?)",
            (f"r-{self.rng.random()}", self.game, self.players[0], fmt, json.dumps(cfg), now(), now())).lastrowid
        for player in self.players[:players]:
            store.enroll(self.db, self.t(tid), player, f"Time {player}")
        return tid

    def t(self, tid):
        return store.get_tournament(self.db, tid)

    def count(self, sql, *args):
        return self.db.execute(sql, args).fetchone()[0]

    def assert_clean(self, tid, entries):
        t = self.t(tid)
        self.assertEqual((t["status"], t["started_at"]), ("registration", None))
        self.assertEqual(self.count("SELECT COUNT(*) FROM tournament_stages WHERE tournament_id = ?", tid), 0)
        for table in ("tournament_ties", "tournament_matches", "tournament_groups", "tournament_group_members"):
            self.assertEqual(self.count(f"SELECT COUNT(*) FROM {table}"), 0, table)
        self.assertEqual(len(store.entries_of(self.db, tid)), entries)

    def test_draft_bracket_reopens_and_new_player_joins(self):
        tid = self.tournament(10, ko_arrange="on", ko_legs="2", third_place="on")
        store.start(self.db, self.t(tid), self.rng)
        self.assertIsNone(store.reopen_block(self.db, self.t(tid)))
        store.reopen_registration(self.db, self.t(tid))
        self.assert_clean(tid, 10)
        store.enroll(self.db, self.t(tid), self.players[10], "Time novo")
        store.start(self.db, self.t(tid), self.rng)
        stage = store.stages_of(self.db, tid)[-1]
        placed = [e for a, b in store.bracket_pairs(self.db, stage["id"]) for e in (a, b) if e]
        self.assertEqual(len(placed), 11)

    def test_groups_with_scheduled_matches_reopen(self):
        tid = self.tournament(8, fmt="world_cup", groups="2")
        store.start(self.db, self.t(tid), self.rng)
        self.assertGreater(self.count("SELECT COUNT(*) FROM tournament_matches"), 0)
        self.db.execute("INSERT INTO tournament_notices_seen (player_id, tournament_id, seen_at) VALUES (?, ?, ?)",
                        (self.players[1], tid, now()))
        store.reopen_registration(self.db, self.t(tid))
        self.assert_clean(tid, 8)
        self.assertEqual(self.count("SELECT COUNT(*) FROM tournament_notices_seen WHERE tournament_id = ?", tid), 0)

    def test_blocked_after_a_result(self):
        tid = self.tournament(4, fmt="league")
        store.start(self.db, self.t(tid), self.rng)
        match = self.db.execute("SELECT id FROM tournament_matches LIMIT 1").fetchone()[0]
        store.record_result(self.db, self.t(tid), match, 1, 0)
        self.assertIn("placar", store.reopen_block(self.db, self.t(tid)))
        with self.assertRaises(TournamentError):
            store.reopen_registration(self.db, self.t(tid))
        self.assertEqual(self.t(tid)["status"], "running")

    def test_blocked_with_reported_score_or_live_link(self):
        tid = self.tournament(4, fmt="league")
        store.start(self.db, self.t(tid), self.rng)
        match = self.db.execute("SELECT id FROM tournament_matches LIMIT 1").fetchone()[0]
        self.db.execute("UPDATE tournament_matches SET status = 'reported' WHERE id = ?", (match,))
        self.assertIsNotNone(store.reopen_block(self.db, self.t(tid)))
        self.db.execute("UPDATE tournament_matches SET status = 'scheduled', quitter_id = ? WHERE id = ?",
                        (self.players[1], match))
        self.assertIsNotNone(store.reopen_block(self.db, self.t(tid)))

    def test_only_running(self):
        tid = self.tournament(4)
        self.assertIsNotNone(store.reopen_block(self.db, self.t(tid)))
        with self.assertRaises(TournamentError):
            store.reopen_registration(self.db, self.t(tid))


class ReopenWebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
            db = get_db()
            self.players = [add_player(db, f"jog{i}") for i in range(6)]
            self.game = db.execute("SELECT id FROM game_titles WHERE code = 'we2002'").fetchone()[0]
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

    def create(self, owner, **extra):
        owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa Reabre", "starts_on": "2026-12-01", "rules": "Regras",
            "game_title_id": self.game, "format": "knockout", "max_players": "8", "entry_fee": "0,00",
            "ko_legs": "1", "final_legs": "1", "team_mode": "free", **extra})
        slug = self.query("SELECT slug FROM tournaments ORDER BY id DESC")[0][0]
        for player in self.players[:4]:
            self.client(player).post(f"/torneios/{slug}/inscrever", data={"csrf_token": CSRF, "team_name": f"Time {player}"})
        with mock.patch("app.tournaments.notify.tournament_started"):
            owner.post(f"/torneios/{slug}/iniciar", data={"csrf_token": CSRF})
        return slug

    def test_button_reopen_and_join(self):
        owner = self.client(self.players[0])
        slug = self.create(owner)
        manage = owner.get(f"/torneios/{slug}/gerenciar").get_data(as_text=True)
        self.assertIn('data-dialog="reabrir-inscricoes"', manage)
        self.assertIn(f"/torneios/{slug}/reabrir-inscricoes", manage)
        # Só o organizador.
        self.assertEqual(self.client(self.players[1]).post(f"/torneios/{slug}/reabrir-inscricoes",
                                                            data={"csrf_token": CSRF}).status_code, 403)
        with mock.patch("app.tournaments.notify.entry_notices") as notices:
            response = owner.post(f"/torneios/{slug}/reabrir-inscricoes", data={"csrf_token": CSRF},
                                  follow_redirects=True)
        self.assertIn("Inscrições reabertas", response.get_data(as_text=True))
        # Os jogos já tinham saído por e-mail: os 4 inscritos são avisados.
        notices.assert_called_once()
        self.assertEqual(len(notices.call_args[0][2]), 4)
        self.assertEqual(self.query("SELECT status FROM tournaments")[0][0], "registration")
        self.assertEqual(self.query("SELECT COUNT(*) FROM tournament_matches")[0][0], 0)
        # Quem faltou entra.
        self.client(self.players[4]).post(f"/torneios/{slug}/inscrever", data={"csrf_token": CSRF, "team_name": "Novo"})
        self.assertEqual(self.query("SELECT COUNT(*) FROM tournament_entries")[0][0], 5)
        manage = owner.get(f"/torneios/{slug}/gerenciar").get_data(as_text=True)
        self.assertNotIn('data-dialog="reabrir-inscricoes"', manage)
        self.assertIn("Fechar inscrições e sortear (5 inscritos)", manage)

    def test_draft_bracket_sends_no_mail(self):
        owner = self.client(self.players[0])
        slug = self.create(owner, ko_arrange="on")
        with mock.patch("app.tournaments.notify.entry_notices") as notices:
            owner.post(f"/torneios/{slug}/reabrir-inscricoes", data={"csrf_token": CSRF})
        notices.assert_not_called()
        self.assertEqual(self.query("SELECT status FROM tournaments")[0][0], "registration")

    def test_no_button_after_a_result(self):
        owner = self.client(self.players[0])
        slug = self.create(owner)
        with self.app.app_context():
            db = get_db()
            t = db.execute("SELECT * FROM tournaments").fetchone()
            store.record_result(db, t, db.execute("SELECT id FROM tournament_matches LIMIT 1").fetchone()[0], 2, 1)
            db.commit()
        manage = owner.get(f"/torneios/{slug}/gerenciar").get_data(as_text=True)
        self.assertNotIn('data-dialog="reabrir-inscricoes"', manage)
        self.assertIn("As inscrições não podem mais ser reabertas.", manage)
        response = owner.post(f"/torneios/{slug}/reabrir-inscricoes", data={"csrf_token": CSRF}, follow_redirects=True)
        self.assertIn("Não dá para reabrir as inscrições", response.get_data(as_text=True))
        self.assertEqual(self.query("SELECT status FROM tournaments")[0][0], "running")


if __name__ == "__main__":
    unittest.main()
