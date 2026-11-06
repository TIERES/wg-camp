import json
import random
import tempfile
import unittest
from pathlib import Path

from app import create_app
from app.db import get_db, init_db, now
from app.tournament import formats, store
from app.tournament.store import TournamentError
from app.wallet import InsufficientFunds, balance, ensure_wallet, post


def make_app(root):
    return create_app({
        "TESTING": True, "SECRET_KEY": "test",
        "DATABASE": str(root / "arena17.db"), "DOWNLOADS_DIR": str(root / "downloads"),
        "UPLOAD_TMP_DIR": str(root / "tmp"), "LIVE_DIR": str(root / "live"), "UPDATES_DIR": str(root / "updates"),
        "REPLAY_BACKUPS_DIR": str(root / "backups"), "MEMCARDS_DIR": str(root / "memcards"),
        "AVATARS_DIR": str(root / "avatars"), "TOURNAMENT_IMAGES_DIR": str(root / "tournament-images"),
    })


def add_player(db, name):
    return db.execute("INSERT INTO players (username, email, password_hash, email_verified_at, created_at, updated_at) "
                      "VALUES (?, ?, 'x', ?, ?, ?)", (name, f"{name}@x.com", now(), now(), now())).lastrowid


class TournamentStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.ctx = self.app.app_context()
        self.ctx.push()
        init_db()
        self.db = get_db()
        self.game = self.db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES ('WE 2002', ?, ?)",
                                    (now(), now())).lastrowid
        self.players = [add_player(self.db, f"p{i}") for i in range(40)]
        self.rng = random.Random(7)

    def tearDown(self):
        self.db.close()
        self.ctx.pop()
        self.temp.cleanup()

    def tournament(self, fmt, players, fee=1000, **config):
        cfg = formats.normalize(fmt, {k: str(v) for k, v in config.items()})
        tid = self.db.execute(
            "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
            "entry_fee_cents, settings_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'registration', ?, ?, ?, ?, ?)",
            (f"t-{fmt}-{players}-{self.rng.random()}", f"Campeonato {fmt}", self.game, self.players[0], fmt, players, fee,
             json.dumps(cfg), now(), now())).lastrowid
        t = store.get_tournament(self.db, tid)
        for pid in self.players[:players]:
            store.enroll(self.db, t, pid, f"Time {pid}")
        store.start(self.db, store.get_tournament(self.db, tid), self.rng)
        self.db.commit()
        return tid

    def t(self, tid):
        return store.get_tournament(self.db, tid)

    def play_stage(self, tid, draws=False):
        """Lança placares aleatórios até a fase atual não ter jogo pendente."""
        while True:
            stage = store.stages_of(self.db, tid)[-1]
            pending = self.db.execute("SELECT * FROM tournament_matches WHERE stage_id = ? AND status != 'confirmed' "
                                      "ORDER BY round, leg, id", (stage["id"],)).fetchall()
            if not pending:
                return stage
            match = pending[0]
            hg, ag = (1, 1) if draws else (self.rng.randint(0, 3), self.rng.randint(0, 3))
            try:
                store.record_result(self.db, self.t(tid), match["id"], hg, ag)
            except TournamentError:
                self.db.rollback()
                store.record_result(self.db, self.t(tid), match["id"], hg, ag, 4, 3)
            self.db.commit()

    def finish(self, tid):
        store.finish(self.db, self.t(tid))
        self.db.commit()
        return [row["player_id"] for row in self.db.execute(
            "SELECT player_id FROM tournament_entries WHERE tournament_id = ? AND final_position IS NOT NULL "
            "ORDER BY final_position", (tid,))]

    def assert_money_conserved(self):
        total_balance = self.db.execute("SELECT SUM(wallet_cents) FROM players").fetchone()[0]
        total_ledger = self.db.execute("SELECT SUM(amount_cents) FROM wallet_entries").fetchone()[0]
        self.assertEqual(total_balance, total_ledger)

    # --- formatos ---------------------------------------------------------

    def test_league_then_knockout(self):
        """Pontos corridos + mata-mata: os 4 primeiros da tabela, 1º x 4º e 2º x 3º."""
        tid = self.tournament("league_knockout", 6, legs=1, playoff_qualifiers=4)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tournament_matches").fetchone()[0], 15)
        league = self.play_stage(tid)
        table = store.group_tables(self.db, league["id"])[0][1]
        self.assertTrue(store.has_next_stage(self.t(tid), league))
        with self.assertRaises(TournamentError):
            store.finish(self.db, self.t(tid))  # antes, o mata-mata
        self.db.rollback()
        store.advance(self.db, self.t(tid))
        self.db.commit()
        top = [row["entry_id"] for row in table[:4]]
        first_round = [tuple(r) for r in self.db.execute(
            "SELECT entry_a, entry_b FROM tournament_ties t JOIN tournament_stages s ON s.id = t.stage_id "
            "WHERE s.seq = 2 AND t.round = 1 ORDER BY position")]
        self.assertEqual(first_round, [(top[0], top[3]), (top[1], top[2])])
        self.play_stage(tid)
        ranking = self.finish(tid)
        entry_player = {r["id"]: r["player_id"] for r in store.entries_of(self.db, tid)}
        self.assertEqual(len(ranking), 3)
        self.assertTrue(set(ranking) <= {entry_player[e] for e in top})
        self.assert_money_conserved()

    def test_league_then_knockout_odd_qualifiers_use_repechage(self):
        tid = self.tournament("league_knockout", 6, legs=1, playoff_qualifiers=5)
        self.play_stage(tid)
        store.advance(self.db, self.t(tid))
        self.db.commit()
        self.assertGreaterEqual(len(self.db.execute(
            "SELECT 1 FROM tournament_ties r JOIN tournament_ties f ON f.loser_next_tie_id = r.id "
            "WHERE r.is_third_place = 0").fetchall()), 1)
        self.play_stage(tid)
        self.assertEqual(len(self.finish(tid)), 3)

    def test_league_knockout_qualifiers_below_players(self):
        config = formats.normalize("league_knockout", {"playoff_qualifiers": "8"})
        self.assertIn("menos que o número de jogadores", formats.config_error("league_knockout", config, 8))
        self.assertIsNone(formats.config_error("league_knockout", config, 9))
        self.assertIsNotNone(formats.config_error("league_knockout", formats.normalize(
            "league_knockout", {"playoff_qualifiers": "1"}), 8))

    def test_league_two_legs(self):
        tid = self.tournament("league", 5, legs=2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tournament_matches").fetchone()[0], 20)
        self.play_stage(tid)
        top = self.finish(tid)
        self.assertEqual(len(top), 3)
        table = store.group_tables(self.db, store.stages_of(self.db, tid)[0]["id"])[0][1]
        entry_player = {r["id"]: r["player_id"] for r in store.entries_of(self.db, tid)}
        self.assertEqual(top, [entry_player[row["entry_id"]] for row in table[:3]])
        # prêmio: 5 x W$10 = W$50 -> 30 / 15 / 5
        prizes = [r[0] for r in self.db.execute(
            "SELECT amount_cents FROM wallet_entries WHERE kind = 'premio' ORDER BY ref_key")]
        self.assertEqual(prizes, [3000, 1500, 500])
        self.assert_money_conserved()

    def repechage_ties(self):
        return self.db.execute(
            "SELECT r.* FROM tournament_ties r JOIN tournament_ties f ON f.loser_next_tie_id = r.id "
            "WHERE r.is_third_place = 0").fetchall()

    def test_knockout_with_repechage_and_third_place(self):
        tid = self.tournament("knockout", 6, ko_legs=1, final_legs=1, third_place=1)
        # 6 numa chave de 8: ninguém passa direto; os 2 que sobram esperam perdedores.
        self.assertIsNone(self.db.execute("SELECT 1 FROM tournament_ties WHERE is_bye = 1").fetchone())
        self.assertEqual(len(self.repechage_ties()), 2)
        self.play_stage(tid, draws=True)  # tudo empatado: decide nos pênaltis
        top = self.finish(tid)
        self.assertEqual(len(top), 3)
        third = self.db.execute("SELECT * FROM tournament_ties WHERE is_third_place = 1").fetchone()
        self.assertIsNotNone(third["winner_entry_id"])
        self.assert_money_conserved()

    def test_knockout_of_three_has_no_third_place_match(self):
        tid = self.tournament("knockout", 3, third_place=1)
        self.assertIsNone(self.db.execute("SELECT 1 FROM tournament_ties WHERE is_third_place = 1").fetchone())
        self.play_stage(tid)
        self.assertEqual(len(self.finish(tid)), 3)

    def test_knockout_of_three_uses_repechage(self):
        """A x B; C espera o perdedor (repescagem); a final é vencedor de A x B
        contra o vencedor da repescagem - ninguém vai direto à final."""
        tid = self.tournament("knockout", 3)
        first = self.db.execute("SELECT * FROM tournament_ties WHERE round = 1 AND entry_b IS NOT NULL").fetchone()
        (waiting,) = self.repechage_ties()
        self.assertIsNone(waiting["entry_b"])
        self.assertNotIn(waiting["entry_a"], (first["entry_a"], first["entry_b"]))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tournament_matches").fetchone()[0], 1)
        match = self.db.execute("SELECT * FROM tournament_matches WHERE tie_id = ?", (first["id"],)).fetchone()
        store.record_result(self.db, self.t(tid), match["id"], 2, 0)
        winner = match["home_entry_id"]
        loser = match["away_entry_id"]
        waiting = store._tie(self.db, waiting["id"])
        self.assertEqual(waiting["entry_b"], loser)  # perdedor foi para a repescagem
        final = self.db.execute("SELECT * FROM tournament_ties WHERE round = 2").fetchone()
        self.assertEqual(final["entry_a"] or final["entry_b"], winner)
        self.assertIsNone(final["entry_a"] and final["entry_b"])  # o outro finalista sai da repescagem
        # O perdedor vence a repescagem e vai à final; o 3º é quem esperava.
        rep = self.db.execute("SELECT * FROM tournament_matches WHERE tie_id = ?", (waiting["id"],)).fetchone()
        store.record_result(self.db, self.t(tid), rep["id"], *((0, 1) if rep["away_entry_id"] == loser else (1, 0)))
        self.db.commit()
        # Mudar o 1º jogo depois da repescagem jogada é bloqueado.
        with self.assertRaises(TournamentError):
            store.record_result(self.db, self.t(tid), match["id"], 0, 2)
        self.db.rollback()
        self.play_stage(tid)
        entry_player = {r["id"]: r["player_id"] for r in store.entries_of(self.db, tid)}
        self.assertEqual(self.finish(tid)[2], entry_player[waiting["entry_a"]])
        self.assert_money_conserved()

    def test_world_cup_cross(self):
        tid = self.tournament("world_cup", 16, groups=4, third_place=1)
        with self.assertRaises(TournamentError):
            store.advance(self.db, self.t(tid))
        self.db.rollback()
        groups_stage = self.play_stage(tid)
        tables = store.group_tables(self.db, groups_stage["id"])
        store.advance(self.db, self.t(tid))
        self.db.commit()
        first_round = self.db.execute(
            "SELECT entry_a, entry_b FROM tournament_ties t JOIN tournament_stages s ON s.id = t.stage_id "
            "WHERE s.seq = 2 AND t.round = 1 ORDER BY position").fetchall()
        firsts = [table[0]["entry_id"] for _, table in tables]
        seconds = [table[1]["entry_id"] for _, table in tables]
        self.assertEqual([tuple(r) for r in first_round], [(firsts[0], seconds[1]), (firsts[2], seconds[3]),
                                                           (firsts[1], seconds[0]), (firsts[3], seconds[2])])
        # Fase de grupos travada.
        group_match = self.db.execute("SELECT id FROM tournament_matches WHERE stage_id = ?",
                                      (groups_stage["id"],)).fetchone()
        with self.assertRaises(TournamentError):
            store.record_result(self.db, self.t(tid), group_match["id"], 0, 0)
        self.db.rollback()
        self.play_stage(tid)
        self.assertEqual(len(self.finish(tid)), 3)
        self.assert_money_conserved()

    def test_libertadores_two_legs_seeded(self):
        tid = self.tournament("libertadores", 12, groups=3, qualifiers=2, ko_legs=2, final_legs=1)
        self.play_stage(tid)
        store.advance(self.db, self.t(tid))
        self.db.commit()
        # 6 classificados -> chave de 8: os 2 melhores 1ºs esperam na repescagem
        byes = self.db.execute("SELECT COUNT(*) FROM tournament_ties WHERE is_bye = 1").fetchone()[0]
        self.assertEqual(byes, 0)
        self.assertEqual(len(self.repechage_ties()), 2)
        legs = self.db.execute("SELECT legs FROM tournament_ties t JOIN tournament_stages s ON s.id = t.stage_id "
                               "WHERE s.seq = 2 ORDER BY round DESC LIMIT 1").fetchone()[0]
        self.assertEqual(legs, 1)  # final em jogo único
        self.play_stage(tid, draws=True)
        self.assertEqual(len(self.finish(tid)), 3)
        self.assert_money_conserved()

    def test_ucl_league_phase_then_playoff(self):
        tid = self.tournament("ucl", 16, pots=4, per_pot=1, direct=4)
        counts = self.db.execute("SELECT home_entry_id, away_entry_id FROM tournament_matches").fetchall()
        self.assertEqual(len(counts), 16 * 4 // 2)
        self.play_stage(tid)
        table = store.group_tables(self.db, store.stages_of(self.db, tid)[0]["id"])[0][1]
        store.advance(self.db, self.t(tid))
        self.db.commit()
        byes = {r[0] for r in self.db.execute("SELECT entry_a FROM tournament_ties WHERE is_bye = 1")}
        self.assertEqual(byes, {row["entry_id"] for row in table[:4]})
        out = {row["entry_id"] for row in table[12:]}
        in_ko = {r[0] for r in self.db.execute(
            "SELECT entry_a FROM tournament_ties WHERE entry_a IS NOT NULL UNION "
            "SELECT entry_b FROM tournament_ties WHERE entry_b IS NOT NULL")}
        self.assertFalse(out & in_ko)
        self.play_stage(tid)
        self.assertEqual(len(self.finish(tid)), 3)
        self.assert_money_conserved()

    # --- W$ ---------------------------------------------------------------

    def test_match_rewards_and_correction(self):
        tid = self.tournament("league", 2, fee=0, legs=1)
        match = self.db.execute("SELECT * FROM tournament_matches").fetchone()
        home = self.db.execute("SELECT player_id FROM tournament_entries WHERE id = ?",
                               (match["home_entry_id"],)).fetchone()[0]
        away = self.db.execute("SELECT player_id FROM tournament_entries WHERE id = ?",
                               (match["away_entry_id"],)).fetchone()[0]
        store.record_result(self.db, self.t(tid), match["id"], 2, 1)
        self.db.commit()
        self.assertEqual((balance(self.db, home), balance(self.db, away)), (100050, 100000))
        store.record_result(self.db, self.t(tid), match["id"], 1, 1)  # corrigido para empate
        self.db.commit()
        self.assertEqual((balance(self.db, home), balance(self.db, away)), (100025, 100025))
        store.clear_result(self.db, self.t(tid), match["id"])
        self.db.commit()
        self.assertEqual((balance(self.db, home), balance(self.db, away)), (100000, 100000))
        self.db.execute("INSERT INTO site_settings (key, value, updated_at) VALUES ('torneios_vitoria', '0', ?)",
                        (now(),))
        store.record_result(self.db, self.t(tid), match["id"], 3, 0)
        self.db.commit()
        self.assertEqual(balance(self.db, home), 100000)
        self.assert_money_conserved()

    def test_entry_fee_refund_and_cancel(self):
        tid = self.db.execute(
            "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
            "entry_fee_cents, settings_json, created_at, updated_at) "
            "VALUES ('x', 'X', ?, ?, 'knockout', 'registration', 2, 5000, '{}', ?, ?)",
            (self.game, self.players[0], now(), now())).lastrowid
        store.enroll(self.db, self.t(tid), self.players[1], "Time 1")
        self.assertEqual(balance(self.db, self.players[1]), 95000)
        with self.assertRaises(TournamentError):
            store.enroll(self.db, self.t(tid), self.players[1], "Time 1")
        entry = self.db.execute("SELECT * FROM tournament_entries").fetchone()
        store.withdraw(self.db, self.t(tid), entry)
        self.assertEqual(balance(self.db, self.players[1]), 100000)
        store.enroll(self.db, self.t(tid), self.players[1], "Time 1")
        store.enroll(self.db, self.t(tid), self.players[2], "Time 2")
        with self.assertRaises(TournamentError):
            store.enroll(self.db, self.t(tid), self.players[3], "Time 3")  # lotado
        self.assertEqual(store.prize_pool(self.db, tid), 10000)
        store.cancel(self.db, self.t(tid))
        self.assertEqual(balance(self.db, self.players[1]), 100000)
        self.assertEqual(balance(self.db, self.players[2]), 100000)
        self.assertEqual(store.prize_pool(self.db, tid), 0)
        self.assert_money_conserved()

    def test_insufficient_funds(self):
        pid = self.players[5]
        ensure_wallet(self.db, pid)
        self.assertFalse(ensure_wallet(self.db, pid))  # só uma vez
        post(self.db, pid, -99990, "ajuste_admin", "teste")
        with self.assertRaises(InsufficientFunds):
            post(self.db, pid, -20, "inscricao", "teste2")
        self.assertEqual(balance(self.db, pid), 10)

    def test_cannot_change_winner_after_next_round_played(self):
        tid = self.tournament("knockout", 4, fee=0)
        semi = self.db.execute("SELECT * FROM tournament_matches WHERE round = 1 ORDER BY id").fetchall()
        store.record_result(self.db, self.t(tid), semi[0]["id"], 2, 0)
        store.record_result(self.db, self.t(tid), semi[1]["id"], 2, 0)
        final = self.db.execute("SELECT * FROM tournament_matches WHERE round = 2").fetchone()
        self.assertEqual(final["home_entry_id"], semi[0]["home_entry_id"])
        # antes da final ter resultado, dá para virar o jogo da semi
        store.record_result(self.db, self.t(tid), semi[0]["id"], 0, 1)
        final = self.db.execute("SELECT * FROM tournament_matches WHERE round = 2").fetchone()
        self.assertEqual(final["home_entry_id"], semi[0]["away_entry_id"])
        store.record_result(self.db, self.t(tid), final["id"], 1, 0)
        with self.assertRaises(TournamentError):
            store.record_result(self.db, self.t(tid), semi[0]["id"], 3, 0)
        self.db.rollback()

    def test_draw_without_pens_is_refused(self):
        tid = self.tournament("knockout", 2, fee=0)
        match = self.db.execute("SELECT * FROM tournament_matches").fetchone()
        with self.assertRaises(TournamentError):
            store.record_result(self.db, self.t(tid), match["id"], 1, 1)
        self.db.rollback()
        store.record_result(self.db, self.t(tid), match["id"], 1, 1, 5, 4)
        self.assertEqual(self.finish(tid)[0],
                         self.db.execute("SELECT player_id FROM tournament_entries WHERE id = ?",
                                         (match["home_entry_id"],)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
