import tempfile
import unittest
from pathlib import Path

from app.db import get_db, init_db
from app.tournament import store, xp
from tests import test_tournament_stats as stats_tests
from tests.test_tournament_store import add_player, make_app


def game(home, away, home_goals, away_goals, match_id=1):
    return {"id": match_id, "home": home, "away": away, "home_goals": home_goals, "away_goals": away_goals}


class XpRulesTest(unittest.TestCase):
    """Regras do rank por XP (tournament/xp.py), com o XP inicial escolhido."""

    def test_levels_and_thresholds_grow(self):
        self.assertEqual([xp.level(v) for v in (0, 99, 100, 249, 250, 450, 700, 999, 1000, 5000)],
                         [0, 0, 1, 1, 2, 3, 4, 4, 5, 5])
        steps = [b - a for a, b in zip(xp.THRESHOLDS, xp.THRESHOLDS[1:])]
        self.assertEqual(steps, sorted(steps))  # cada rank pede mais XP que o anterior
        self.assertEqual(len(set(steps)), len(steps))

    def test_same_rank_win_and_draw(self):
        totals, deltas = xp.replay([game(1, 2, 2, 1)], {1: 30, 2: 30})
        self.assertEqual(deltas[1], {1: 20, 2: -20})
        self.assertEqual(totals, {1: 50, 2: 10})
        totals, deltas = xp.replay([game(1, 2, 1, 1)], {1: 30, 2: 30})
        self.assertEqual(deltas[1], {1: 0, 2: 0})

    def test_rank_difference_changes_points(self):
        # 1 = Rank D (250), 2 = Rank F (0 + 50): 2 níveis de diferença.
        start = {1: 260, 2: 50}
        _, upset = xp.replay([game(1, 2, 0, 1)], start)    # o mais fraco ganha: 20 + 2*5
        self.assertEqual(upset[1], {1: -30, 2: 30})
        _, favorite = xp.replay([game(1, 2, 1, 0)], start)  # o favorito ganha: 20 - 2*5
        self.assertEqual(favorite[1], {1: 10, 2: -10})
        _, draw = xp.replay([game(1, 2, 2, 2)], start)      # empate: o mais fraco ganha 2*5
        self.assertEqual(draw[1], {1: -10, 2: 10})
        # A x F (5 níveis): favorito ganha o mínimo; zebra vale 45.
        _, low = xp.replay([game(1, 2, 3, 0)], {1: 1000, 2: 50})
        self.assertEqual(low[1], {1: xp.MIN_POINTS, 2: -xp.MIN_POINTS})
        _, high = xp.replay([game(1, 2, 0, 3)], {1: 1000, 2: 50})
        self.assertEqual(high[1], {1: -45, 2: 45})

    def test_xp_never_negative_and_can_drop_rank(self):
        totals, deltas = xp.replay([game(1, 2, 0, 1)], {1: 5, 2: 0})
        self.assertEqual(deltas[1], {1: -5, 2: 20})
        self.assertEqual(totals[1], 0)
        totals, _ = xp.replay([game(1, 2, 0, 1)], {1: 105, 2: 100})
        self.assertEqual(xp.level(totals[1]), 0)  # caiu do E para o F

    def test_rank_used_is_the_one_before_each_match(self):
        # Cinco vitórias seguidas: a quinta leva o jogador 1 ao Rank E; a sexta já é E x F.
        matches = [game(1, 2, 1, 0, i) for i in range(1, 7)]
        totals, deltas = xp.replay(matches)
        self.assertEqual([deltas[i][1] for i in range(1, 7)], [20, 20, 20, 20, 20, 15])
        self.assertEqual(totals[1], 115)


class XpWebTest(unittest.TestCase):
    """XP a partir dos jogos confirmados, badge na foto e barra no Status."""

    league = stats_tests.StatsTest.league
    play = stats_tests.StatsTest.play
    add_game = stats_tests.StatsTest.add_game

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.ctx = self.app.app_context()
        self.ctx.push()
        init_db()
        self.db = get_db()
        self.game = self.add_game("WE 2002")
        self.players = [add_player(self.db, f"p{i}") for i in range(3)]
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.ctx.pop()
        self.temp.cleanup()

    def client(self, player):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["player_id"] = player
        return client

    def test_confirmed_matches_give_xp_and_pages_show_rank(self):
        p0, p1, p2 = self.players
        tid = self.league([p0, p1, p2])
        match = self.play(tid, p0, p1, 2, 0)
        self.play(tid, p0, p2, 1, 1)
        totals, deltas = xp.compute(self.db)
        self.assertEqual(totals[p0], 20)
        self.assertEqual(deltas[match], {p0: 20, p1: 0})

        status = self.client(p0).get("/torneios/status").data.decode()
        self.assertIn('class="rank-chip rank-F">Rank F', status)
        self.assertIn("Faltam <strong>80 XP</strong> para o Rank E", status)
        self.assertIn("+20 XP", status)  # na janela da partida
        profile = self.client(p1).get("/torneios/jogadores/p0").data.decode()
        self.assertIn('class="rank-badge rank-F" title="Rank F · 20 XP"', profile)
        ranking = self.client(p1).get("/torneios/ranking").data.decode()
        self.assertIn("Como funciona o Rank (XP)", ranking)
        self.assertIn("rank-badge", ranking)

        # Torneio cancelado não conta.
        self.db.execute("UPDATE tournaments SET status = 'cancelled' WHERE id = ?", (tid,))
        self.db.commit()
        self.assertEqual(xp.compute(self.db)[0], {})

    def test_finished_tournament_opens_on_overview_with_winner_photos(self):
        p0, p1, p2 = self.players
        tid = self.league([p0, p1, p2])
        self.play(tid, p0, p1, 2, 0)
        self.play(tid, p0, p2, 1, 0)
        self.play(tid, p1, p2, 1, 0)
        slug = self.db.execute("SELECT slug FROM tournaments WHERE id = ?", (tid,)).fetchone()[0]
        running = self.client(p1).get(f"/torneios/{slug}").data.decode()
        self.assertIn('href="?aba=tabela" class="active"', running)  # em andamento: tabela
        store.finish(self.db, store.get_tournament(self.db, tid))
        self.db.commit()
        page = self.client(p1).get(f"/torneios/{slug}").data.decode()
        self.assertIn('href="?aba=geral" class="active"', page)
        self.assertIn('<div class="podium-1"><span class="podium-photo"><button type="button" class="avatar-btn" '
                      'data-player-card="/torneios/jogadores/p0/resumo"', page)
        self.assertIn("avatar avatar-lg", page)  # o campeão com a foto maior


if __name__ == "__main__":
    unittest.main()
