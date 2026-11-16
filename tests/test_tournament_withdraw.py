"""Desistência com o campeonato em andamento (store.withdraw_running): WO 3 x 0
nos jogos que faltam, mata-mata decidido pelo WO, suplente no mata-mata, pódio
sem quem desistiu, o V/E/D com WO, a janela "Desistir" e os e-mails."""
import json
import random
import tempfile
import unittest
from pathlib import Path

from app.db import get_db, init_db, now
from app.tournament import engine, formats, reports, stats, store
from app.tournament.store import TournamentError
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"
REASON = "Mudei de horário no trabalho e não consigo mais jogar."


class WithdrawStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.ctx = self.app.app_context()
        self.ctx.push()
        init_db()
        self.db = get_db()
        self.game = self.db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES ('WE 2002', ?, ?)",
                                    (now(), now())).lastrowid
        self.players = [add_player(self.db, f"p{i}") for i in range(12)]
        self.rng = random.Random(11)

    def tearDown(self):
        self.db.close()
        self.ctx.pop()
        self.temp.cleanup()

    def tournament(self, fmt, players, fee=1000, **config):
        cfg = formats.normalize(fmt, {k: str(v) for k, v in config.items()})
        tid = self.db.execute(
            "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
            "entry_fee_cents, settings_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'registration', ?, ?, ?, ?, ?)",
            (f"t-{fmt}-{self.rng.random()}", f"Campeonato {fmt}", self.game, self.players[0], fmt, players, fee,
             json.dumps(cfg), now(), now())).lastrowid
        for pid in self.players[:players]:
            store.enroll(self.db, self.t(tid), pid, f"Time {pid}")
        store.start(self.db, self.t(tid), self.rng)
        self.db.commit()
        return tid

    def t(self, tid):
        return store.get_tournament(self.db, tid)

    def entry(self, entry_id):
        return self.db.execute("SELECT * FROM tournament_entries WHERE id = ?", (entry_id,)).fetchone()

    def matches_of(self, entry_id, where=""):
        return self.db.execute(f"SELECT * FROM tournament_matches WHERE ? IN (home_entry_id, away_entry_id){where} "
                               "ORDER BY id", (entry_id,)).fetchall()

    def play(self, tid, match, home_goals, away_goals):
        store.record_result(self.db, self.t(tid), match["id"], home_goals, away_goals)
        self.db.commit()

    def play_stage(self, tid):
        while True:
            stage = store.stages_of(self.db, tid)[-1]
            pending = self.db.execute("SELECT * FROM tournament_matches WHERE stage_id = ? AND status != 'confirmed' "
                                      "ORDER BY round, leg, id", (stage["id"],)).fetchall()
            if not pending:
                return stage
            hg, ag = self.rng.randint(0, 3), self.rng.randint(0, 3)
            try:
                store.record_result(self.db, self.t(tid), pending[0]["id"], hg, ag)
            except TournamentError:
                self.db.rollback()
                store.record_result(self.db, self.t(tid), pending[0]["id"], hg, ag, 4, 3)
            self.db.commit()

    def withdraw(self, tid, entry_id, reason=REASON):
        result = store.withdraw_running(self.db, self.t(tid), self.entry(entry_id), reason)
        self.db.commit()
        return result

    def wallet_net(self, player_id, tid):
        return self.db.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM wallet_entries WHERE player_id = ? "
                               "AND tournament_id = ?", (player_id, tid)).fetchone()[0]

    # --- regras ------------------------------------------------------------

    def test_reason_and_status_are_checked(self):
        tid = self.tournament("league", 4, legs=1)
        entry = store.entries_of(self.db, tid)[0]
        for reason in ("curto", "x" * 501, "   ", None):
            with self.assertRaises(TournamentError):
                store.withdraw_running(self.db, self.t(tid), self.entry(entry["id"]), reason)
            self.db.rollback()
        self.assertIsNone(self.entry(entry["id"])["withdrawn_at"])
        self.withdraw(tid, entry["id"], "  " + "m" * 10 + "  ")
        self.assertEqual(self.entry(entry["id"])["withdraw_reason"], "m" * 10)
        self.assertIn("já desistiu", store.withdraw_error(self.db, self.t(tid), self.entry(entry["id"])))
        with self.assertRaises(TournamentError):
            self.withdraw(tid, entry["id"])
        self.db.rollback()
        self.assertIsNotNone(store.withdraw_error(self.db, self.t(tid), None))

    def test_pending_matches_become_walkovers(self):
        tid = self.tournament("league", 4, legs=1)
        gone = store.entries_of(self.db, tid)[1]
        first = self.matches_of(gone["id"])[0]
        self.play(tid, first, *((2, 0) if first["home_entry_id"] == gone["id"] else (0, 2)))  # ele venceu um
        fee = self.wallet_net(gone["player_id"], tid)
        preview = store.withdraw_preview(self.db, self.t(tid), self.entry(gone["id"]))
        self.assertEqual(preview, {"substitute": False, "walkovers": 2, "kept": 0, "table": True})

        result = self.withdraw(tid, gone["id"])
        self.assertEqual(len(result["walkovers"]), 2)
        self.assertIsNone(result["substitute"])
        for match in self.matches_of(gone["id"], " AND id != %d" % first["id"]):
            self.assertEqual(match["status"], "confirmed")
            self.assertEqual(match["walkover_entry_id"], gone["id"])
            mine = match["home_goals"] if match["home_entry_id"] == gone["id"] else match["away_goals"]
            theirs = match["away_goals"] if match["home_entry_id"] == gone["id"] else match["home_goals"]
            self.assertEqual((mine, theirs), (0, 3))
            self.assertIn("desistiu", reports.history(self.db, match["id"])[-1]["comment"])
        # A inscrição não volta; o W$ do jogo que ele venceu continua.
        self.assertFalse(self.db.execute("SELECT 1 FROM wallet_entries WHERE kind = 'reembolso'").fetchone())
        self.assertEqual(self.wallet_net(gone["player_id"], tid), fee)
        self.assertGreater(store.prize_pool(self.db, tid), 0)
        # Na tabela: V, WD, WD para ele; WV para os adversários.
        table = store.group_tables(self.db, store.stages_of(self.db, tid)[0]["id"])[0][1]
        rows = {row["entry_id"]: row for row in table}
        self.assertEqual(rows[gone["id"]]["form"], ["V", engine.WO_LOSS, engine.WO_LOSS])
        self.assertEqual(rows[gone["id"]]["lost"], 2)
        winners = [e for e in rows if e != gone["id"] and engine.WO_WIN in rows[e]["form"]]
        self.assertEqual(len(winners), 2)
        # Histórico e Comparar: WO na cor da derrota/vitória e as derrotas por WO.
        history = stats.history(self.db, gone["player_id"])
        self.assertEqual(sorted(stats.outcome(row) for row in history), ["V", "WD", "WD"])
        self.assertEqual(stats.player_record(self.db, gone["player_id"])["wo_losses"], 2)
        self.assertEqual(stats.player_record(self.db, gone["player_id"])["losses"], 2)
        opponent = self.entry(winners[0])["player_id"]
        self.assertIn(engine.WO_WIN, [stats.outcome(row) for row in stats.history(self.db, opponent)])
        self.assertEqual(stats.player_record(self.db, opponent)["wo_losses"], 0)
        self.assertEqual([p["outcome"] for p in stats.evolution(history)][-2:], [engine.WO_LOSS] * 2)
        # Quem desistiu não reporta mais nada; os outros seguem jogando.
        self.assertIsNone(reports.player_entry(self.db, self.t(tid), first, gone["player_id"]))

    def test_reported_win_of_the_opponent_is_kept(self):
        tid = self.tournament("league", 4, legs=1)
        gone = store.entries_of(self.db, tid)[2]
        lost, won, *_ = self.matches_of(gone["id"])
        for match, gone_goals, other_goals in ((lost, 1, 4), (won, 2, 0)):
            other = match["away_entry_id"] if match["home_entry_id"] == gone["id"] else match["home_entry_id"]
            score = (gone_goals, other_goals) if match["home_entry_id"] == gone["id"] else (other_goals, gone_goals)
            reports.report(self.db, self.t(tid), match["id"], self.entry(other)["player_id"], (*score, None, None))
        self.db.commit()
        self.assertEqual(store.withdraw_preview(self.db, self.t(tid), self.entry(gone["id"]))["kept"], 1)
        result = self.withdraw(tid, gone["id"])
        self.assertEqual(result["kept"], [lost["id"]])
        self.assertEqual(len(result["walkovers"]), 2)
        kept = self.db.execute("SELECT * FROM tournament_matches WHERE id = ?", (lost["id"],)).fetchone()
        self.assertEqual((kept["status"], kept["walkover_entry_id"]), ("confirmed", None))
        self.assertEqual(sorted((kept["home_goals"], kept["away_goals"])), [1, 4])
        # O placar em que ele vencia não vale: WO.
        wo = self.db.execute("SELECT * FROM tournament_matches WHERE id = ?", (won["id"],)).fetchone()
        self.assertEqual(wo["walkover_entry_id"], gone["id"])

    def test_next_stage_skips_who_gave_up(self):
        tid = self.tournament("world_cup", 8, groups=2, qualifiers=2, third_place=0)
        groups = store.group_tables(self.db, store.stages_of(self.db, tid)[0]["id"])
        gone = groups[0][1][0]["entry_id"]
        self.withdraw(tid, gone)
        stage = self.play_stage(tid)
        tables = store.group_tables(self.db, stage["id"])
        store.advance(self.db, self.t(tid))
        self.db.commit()
        knockout = store.stages_of(self.db, tid)[-1]
        qualified = {e for row in self.db.execute("SELECT entry_a, entry_b FROM tournament_ties WHERE stage_id = ?",
                                                  (knockout["id"],)) for e in row if e}
        expected = {row["entry_id"] for _, table in tables
                    for row in [r for r in table if r["entry_id"] != gone][:2]}
        self.assertNotIn(gone, qualified)
        self.assertEqual(qualified, expected)
        self.assertIsNotNone(store.withdraw_error(self.db, self.t(tid), self.entry(gone)))

    def test_knockout_walkover_beats_the_aggregate(self):
        tid = self.tournament("knockout", 4, ko_legs=2, final_legs=1)
        tie = self.db.execute("SELECT * FROM tournament_ties WHERE round = 1 ORDER BY position").fetchone()
        gone, other = tie["entry_a"], tie["entry_b"]
        first_leg = store.tie_matches(self.db, tie["id"])[0]
        self.play(tid, first_leg, *((5, 0) if first_leg["home_entry_id"] == gone else (0, 5)))
        result = self.withdraw(tid, gone)
        self.assertEqual(len(result["walkovers"]), 1)
        tie = self.db.execute("SELECT * FROM tournament_ties WHERE id = ?", (tie["id"],)).fetchone()
        self.assertEqual(tie["winner_entry_id"], other)  # 5 x 3 no agregado, mas ele desistiu
        final = self.db.execute("SELECT * FROM tournament_ties WHERE id = ?", (tie["next_tie_id"],)).fetchone()
        self.assertIn(other, (final["entry_a"], final["entry_b"]))

    def test_tie_waiting_for_the_opponent_becomes_walkover_later(self):
        tid = self.tournament("knockout", 4, ko_legs=1, final_legs=1)
        semi, other_semi = self.db.execute("SELECT * FROM tournament_ties WHERE round = 1 ORDER BY position").fetchall()
        match = store.tie_matches(self.db, semi["id"])[0]
        self.play(tid, match, 2, 0)
        finalist = match["home_entry_id"]
        self.assertIsNone(store.withdraw_error(self.db, self.t(tid), self.entry(finalist)))
        self.assertEqual(store.withdraw_preview(self.db, self.t(tid), self.entry(finalist))["walkovers"], 0)
        self.assertEqual(self.withdraw(tid, finalist)["walkovers"], [])
        # O adversário chega na final: o jogo já nasce WO e o campeonato termina.
        self.play(tid, store.tie_matches(self.db, other_semi["id"])[0], 1, 0)
        final = self.db.execute("SELECT * FROM tournament_ties WHERE round = 2").fetchone()
        champion = store.tie_matches(self.db, other_semi["id"])[0]["home_entry_id"]
        self.assertEqual(final["winner_entry_id"], champion)
        final_match = store.tie_matches(self.db, final["id"])[0]
        self.assertEqual(final_match["walkover_entry_id"], finalist)
        store.finish(self.db, self.t(tid))
        self.db.commit()
        self.assertIsNone(self.entry(finalist)["final_position"])  # fora do pódio e do prêmio
        self.assertEqual(self.entry(champion)["final_position"], 1)

    def test_substitute_takes_the_knockout_spot(self):
        tid = self.tournament("world_cup", 8, groups=2, qualifiers=2, third_place=0)
        stage = self.play_stage(tid)
        tables = store.group_tables(self.db, stage["id"])
        store.advance(self.db, self.t(tid))
        self.db.commit()
        group = next(table for _, table in tables if any(r["entry_id"] == tables[0][1][0]["entry_id"] for r in table))
        gone, substitute = group[0]["entry_id"], group[2]["entry_id"]  # o 1º do grupo; o 3º é o suplente
        knockout = store.stages_of(self.db, tid)[-1]
        self.assertTrue(store.withdraw_preview(self.db, self.t(tid), self.entry(gone))["substitute"])
        result = self.withdraw(tid, gone)
        self.assertEqual(result, {"substitute": substitute, "walkovers": [], "kept": []})
        in_ties = {e for row in self.db.execute("SELECT entry_a, entry_b FROM tournament_ties WHERE stage_id = ?",
                                                (knockout["id"],)) for e in row if e}
        self.assertIn(substitute, in_ties)
        self.assertNotIn(gone, in_ties)
        self.assertEqual(len(self.matches_of(substitute, f" AND stage_id = {knockout['id']}")), 1)
        self.assertFalse(self.db.execute("SELECT 1 FROM tournament_matches WHERE walkover_entry_id IS NOT NULL").fetchone())
        self.assertEqual(self.entry(gone)["replaced_by_entry_id"], substitute)

    def test_who_already_played_the_knockout_gives_walkover(self):
        tid = self.tournament("world_cup", 8, groups=2, qualifiers=2, third_place=0)
        self.play_stage(tid)
        store.advance(self.db, self.t(tid))
        self.db.commit()
        tie = self.db.execute("SELECT * FROM tournament_ties WHERE stage_id = ? AND round = 1 ORDER BY position",
                              (store.stages_of(self.db, tid)[-1]["id"],)).fetchone()
        self.play(tid, store.tie_matches(self.db, tie["id"])[0], 1, 0)
        winner = self.db.execute("SELECT winner_entry_id FROM tournament_ties WHERE id = ?", (tie["id"],)).fetchone()[0]
        result = self.withdraw(tid, winner)
        self.assertIsNone(result["substitute"])  # já jogou o mata-mata: sem suplente

    def test_podium_without_who_gave_up(self):
        tid = self.tournament("league", 4, legs=1)
        stage = self.play_stage(tid)
        leader, second = [row["entry_id"] for row in store.group_tables(self.db, stage["id"])[0][1][:2]]
        self.withdraw(tid, leader)  # tudo jogado: só sai do pódio
        store.finish(self.db, self.t(tid))
        self.db.commit()
        self.assertIsNone(self.entry(leader)["final_position"])
        self.assertEqual(self.entry(second)["final_position"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tournament_entries WHERE tournament_id = ? "
                                         "AND final_position IS NOT NULL", (tid,)).fetchone()[0], 3)

    def test_correcting_a_walkover_clears_the_mark(self):
        tid = self.tournament("league", 4, legs=1)
        gone = store.entries_of(self.db, tid)[0]
        match_id = self.withdraw(tid, gone["id"])["walkovers"][0]
        store.record_result(self.db, self.t(tid), match_id, 1, 1)
        self.db.commit()
        row = self.db.execute("SELECT * FROM tournament_matches WHERE id = ?", (match_id,)).fetchone()
        self.assertIsNone(row["walkover_entry_id"])
        store.clear_result(self.db, self.t(tid), match_id)
        self.db.commit()


class WithdrawWebTest(unittest.TestCase):
    """O botão "Desistir", a janela com DESISTIR e o motivo, os e-mails e o Comparar."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
            db = get_db()
            self.players = [add_player(db, f"jog{i}") for i in range(4)]
            game = db.execute("SELECT id FROM game_titles WHERE is_active = 1 ORDER BY id LIMIT 1").fetchone()[0]
            cfg = formats.normalize("league", {"legs": "1"})
            self.tid = db.execute(
                "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                "entry_fee_cents, settings_json, created_at, updated_at) "
                "VALUES ('copa-wo', 'Copa WO', ?, ?, 'league', 'registration', 3, 500, ?, ?, ?)",
                (game, self.players[0], json.dumps(cfg), now(), now())).lastrowid
            for pid in self.players[1:]:
                store.enroll(db, store.get_tournament(db, self.tid), pid, f"Time {pid}")
            store.start(db, store.get_tournament(db, self.tid), random.Random(3))
            # jog1 e jog2 são amigos (o Comparar é só entre amigos).
            db.execute("INSERT INTO friendships (requester_id, addressee_id, status, created_at) "
                       "VALUES (?, ?, 'accepted', ?)", (self.players[1], self.players[2], now()))
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
        sent = [(m["To"], m["Subject"], (m.get_body(("plain",)) or m).get_content()) for m in box]
        box.clear()
        return sent

    def test_give_up_flow(self):
        player = self.players[1]
        client = self.client(player)
        page = client.get("/torneios/copa-wo").get_data(as_text=True)
        self.assertIn('data-dialog="desistir"', page)
        self.assertIn("Desistir do campeonato Copa WO", page)
        self.assertIn("não é devolvido", page)
        self.assertIn("2 jogos pendentes", page)
        self.assertNotIn('data-dialog="desistir"', self.client(self.players[0]).get("/torneios/copa-wo").get_data(as_text=True))

        url = "/torneios/copa-wo/desistir"
        client.post(url, data={"csrf_token": CSRF, "confirmar": "sair", "motivo": REASON})
        client.post(url, data={"csrf_token": CSRF, "confirmar": "DESISTIR", "motivo": "curto"})
        self.assertIsNone(self.query("SELECT withdrawn_at FROM tournament_entries WHERE player_id = ?", player)[0][0])
        self.mails()

        response = client.post(url, data={"csrf_token": CSRF, "confirmar": "desistir", "motivo": REASON})
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(self.query("SELECT withdrawn_at FROM tournament_entries WHERE player_id = ?", player)[0][0])
        self.assertEqual(self.query("SELECT COUNT(*) FROM tournament_matches WHERE walkover_entry_id IS NOT NULL")[0][0], 2)
        sent = self.mails()
        organizer = [m for m in sent if m[0] == "jog0@x.com"]
        self.assertEqual(len(organizer), 1)
        self.assertIn("jog1 desistiu", organizer[0][1])
        self.assertIn(REASON, organizer[0][2])
        self.assertIn("WO:", organizer[0][2])
        self.assertTrue(any(m[0] == "jog2@x.com" and "Vitória por WO" in m[1] for m in sent))
        self.assertTrue(any(m[0] == "jog1@x.com" and "Você desistiu" in m[1] for m in sent))

        page = client.get("/torneios/copa-wo?aba=tabela").get_data(as_text=True)
        self.assertNotIn('data-dialog="desistir"', page)
        self.assertIn("Você desistiu deste campeonato", page)
        self.assertIn('class="gave-up"', page)
        self.assertIn('class="f-WD"', page)
        self.assertIn(">WO<", page)

        compare = client.get(f"/torneios/comparar?a=jog1&b=jog2").get_data(as_text=True)
        self.assertIn("Derrotas por WO", compare)
        self.assertNotIn("Derrotas por WO", client.get(f"/torneios/comparar?a=jog1&b=jog2&modalidade=friendly")
                         .get_data(as_text=True))
        match_id = self.query("SELECT id FROM tournament_matches WHERE walkover_entry_id IS NOT NULL")[0][0]
        self.assertIn("Vitória por WO: jog1 desistiu", client.get(f"/torneios/copa-wo/jogos/{match_id}")
                      .get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
