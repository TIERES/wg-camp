import json
import random
import tempfile
import unittest
from pathlib import Path

from app.db import get_db, init_db, now
from app.tournament import formats, stats, store
from tests.test_tournament_store import add_player, make_app


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.ctx = self.app.app_context()
        self.ctx.push()
        init_db()
        self.db = get_db()
        self.game = self.add_game("WE 2002")
        self.players = [add_player(self.db, f"p{i}") for i in range(4)]
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.ctx.pop()
        self.temp.cleanup()

    def add_game(self, name):
        return self.db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES (?, ?, ?)",
                               (name, now(), now())).lastrowid

    def league(self, players, game=None):
        cfg = formats.normalize("league", {"legs": "1"})
        tid = self.db.execute(
            "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
            "settings_json, created_at, updated_at) VALUES (?, 'Liga', ?, ?, 'league', 'registration', ?, ?, ?, ?)",
            (f"liga-{random.random()}", game or self.game, players[0], len(players), json.dumps(cfg), now(),
             now())).lastrowid
        for i, pid in enumerate(players):
            store.enroll(self.db, store.get_tournament(self.db, tid), pid, f"Time {i}")
        store.start(self.db, store.get_tournament(self.db, tid), random.Random(1))
        self.db.commit()
        return tid

    def play(self, tid, a, b, goals_a, goals_b):
        """Placar do jogo entre os jogadores a e b, seja quem for o mandante."""
        match = self.db.execute(
            "SELECT m.id, he.player_id AS home FROM tournament_matches m "
            "JOIN tournament_stages s ON s.id = m.stage_id JOIN tournament_entries he ON he.id = m.home_entry_id "
            "JOIN tournament_entries ae ON ae.id = m.away_entry_id WHERE s.tournament_id = ? "
            "AND ((he.player_id = ? AND ae.player_id = ?) OR (he.player_id = ? AND ae.player_id = ?))",
            (tid, a, b, b, a)).fetchone()
        score = (goals_a, goals_b) if match["home"] == a else (goals_b, goals_a)
        store.record_result(self.db, store.get_tournament(self.db, tid), match["id"], *score)
        self.db.commit()
        return match["id"]

    def test_record_ranking_and_head_to_head(self):
        p0, p1, p2 = self.players[:3]
        tid = self.league([p0, p1, p2])
        self.play(tid, p0, p1, 3, 1)
        self.play(tid, p0, p2, 2, 2)
        self.play(tid, p1, p2, 1, 0)

        r0 = stats.player_record(self.db, p0)
        self.assertEqual((r0["played"], r0["wins"], r0["draws"], r0["losses"]), (2, 1, 1, 0))
        self.assertEqual((r0["goals_for"], r0["goals_against"], r0["goal_diff"], r0["points"]), (5, 3, 2, 4))
        self.assertEqual(r0["efficiency"], 67)
        r2 = stats.player_record(self.db, p2)
        self.assertEqual((r2["wins"], r2["draws"], r2["losses"]), (0, 1, 1))
        self.assertEqual(stats.player_record(self.db, self.players[3])["played"], 0)

        top = stats.ranking(self.db)
        self.assertEqual([r["player_id"] for r in top], [p0, p1, p2])
        self.assertEqual(stats.position(self.db, p1), 2)
        self.assertIsNone(stats.position(self.db, self.players[3]))

        h2h = stats.compare(self.db, p1, p0)
        self.assertEqual((h2h["head_to_head"]["wins"], h2h["head_to_head"]["losses"]), (0, 1))
        self.assertEqual(len(h2h["matches"]), 1)
        row = h2h["matches"][0]
        self.assertEqual((row["goals_for"], row["goals_against"], stats.outcome(row)), (1, 3, "D"))
        self.assertEqual(sorted(stats.form(stats.history(self.db, p0))), ["E", "V"])

    def test_cleared_result_and_cancelled_tournament_do_not_count(self):
        p0, p1 = self.players[:2]
        tid = self.league([p0, p1])
        match_id = self.play(tid, p0, p1, 1, 0)
        self.assertEqual(stats.player_record(self.db, p0)["wins"], 1)
        store.clear_result(self.db, store.get_tournament(self.db, tid), match_id)
        self.db.commit()
        self.assertEqual(stats.player_record(self.db, p0)["played"], 0)

        self.play(tid, p0, p1, 0, 2)  # corrigido: agora p1 venceu
        self.assertEqual(stats.player_record(self.db, p1)["wins"], 1)
        self.assertEqual(stats.player_record(self.db, p0)["losses"], 1)
        store.cancel(self.db, store.get_tournament(self.db, tid))
        self.db.commit()
        self.assertEqual(stats.player_record(self.db, p1)["played"], 0)
        self.assertEqual(stats.ranking(self.db), [])

    def test_game_filter(self):
        p0, p1 = self.players[:2]
        other = self.add_game("PES 6")
        self.play(self.league([p0, p1]), p0, p1, 2, 0)
        self.play(self.league([p0, p1], game=other), p0, p1, 0, 1)
        self.assertEqual(stats.player_record(self.db, p0)["played"], 2)
        self.assertEqual(stats.player_record(self.db, p0, self.game)["wins"], 1)
        self.assertEqual(stats.player_record(self.db, p0, other)["losses"], 1)
        self.assertEqual(stats.ranking(self.db, other)[0]["player_id"], p1)
        self.assertEqual(stats.compare(self.db, p0, p1, other)["head_to_head"]["losses"], 1)

    def test_ranking_limit_is_ten(self):
        players = self.players + [add_player(self.db, f"x{i}") for i in range(8)]
        tid = self.league(players)
        for i, a in enumerate(players):
            for b in players[i + 1:]:
                self.play(tid, a, b, 1, 0)
        self.assertEqual(len(stats.ranking(self.db)), 10)
        self.assertEqual(stats.ranking(self.db)[0]["player_id"], players[0])
        self.assertEqual(stats.position(self.db, players[-1]), 12)

    def test_pages(self):
        p0, p1 = self.players[:2]
        tid = self.league([p0, p1])
        match_id = self.play(tid, p0, p1, 2, 1)
        slug = store.get_tournament(self.db, tid)["slug"]
        client = self.app.test_client()
        page = client.get("/torneios/ranking")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"p0", page.data)
        page = client.get(f"/torneios/ranking?jogo={self.game}")
        self.assertEqual(page.status_code, 200)
        page = client.get("/torneios/jogadores/P0")  # nome sem diferenciar maiúsculas
        self.assertEqual(page.status_code, 200)
        self.assertIn("Histórico de partidas".encode(), page.data)
        self.assertEqual(client.get("/torneios/jogadores/ninguem").status_code, 404)
        page = client.get("/torneios/comparar?a=p0&b=p1")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Confronto direto".encode(), page.data)
        self.assertIn("Jogador não encontrado".encode(), client.get("/torneios/comparar?a=p0&b=zzz").data)
        self.assertIn("dois jogadores diferentes".encode(), client.get("/torneios/comparar?a=p0&b=p0").data)
        self.assertEqual(client.get("/torneios/comparar").status_code, 200)
        page = client.get(f"/torneios/{slug}/jogos/{match_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Comparar os dois".encode(), page.data)
        with client.session_transaction() as session:
            session["player_id"] = p1
        page = client.get("/torneios/ranking")
        self.assertIn(b"qualified", page.data)  # a linha de quem está logado fica marcada
        self.assertIn(b'value="p1"', client.get("/torneios/comparar").data)


    def test_reported_score_is_shown_with_status_color(self):
        from app.tournament import reports
        p0, p1 = self.players[:2]
        tid = self.league([p0, p1])
        t = store.get_tournament(self.db, tid)
        match_id = self.db.execute("SELECT id FROM tournament_matches").fetchone()[0]
        home = self.db.execute("SELECT e.player_id FROM tournament_matches m JOIN tournament_entries e "
                               "ON e.id = m.home_entry_id WHERE m.id = ?", (match_id,)).fetchone()[0]
        reports.report(self.db, t, match_id, home, (4, 2, None, None))
        self.db.commit()
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["player_id"] = home
        for url in (f"/torneios/{t['slug']}?aba=jogos", f"/torneios/{t['slug']}/jogos/{match_id}", "/torneios/meus-jogos"):
            page = client.get(url).get_data(as_text=True)
            self.assertIn('score-val score-reported', page, url)
            self.assertIn("<strong>4 x 2</strong>", page, url)
            self.assertIn("aguardando a confirmação do adversário", page, url)
        page = client.get(f"/torneios/{t['slug']}/jogos/{match_id}").get_data(as_text=True)
        self.assertIn("Você reportou o placar", page)
        self.assertNotIn("Enviar placar", page)
        self.assertEqual(stats.player_record(self.db, home)["played"], 0)  # só conta depois de confirmado

    def test_my_matches_lists_all_games(self):
        p0, p1, p2 = self.players[:3]
        tid = self.league([p0, p1, p2])
        self.play(tid, p0, p1, 2, 0)  # confirmado
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["player_id"] = p0
        page = client.get("/torneios/meus-jogos").get_data(as_text=True)
        self.assertEqual(page.count('class="my-match status-'), 2)  # o confirmado e o que falta
        self.assertIn("resultado confirmado", page)
        self.assertIn("1 confirmado", page)
        self.assertIn("1 para reportar", page)

if __name__ == "__main__":
    unittest.main()
