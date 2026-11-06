import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from flask import g

from app.db import get_db, init_db
from app.tournament import friendly, friends, stats, xp
from app.tournament.store import TournamentError
from tests import test_tournament_stats as stats_tests
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"


class FriendlyTest(unittest.TestCase):
    """Amistosos: convite entre amigos, placar confirmado pelos dois, suspensão
    e a regra que decide se valem para o Ranking/XP."""

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
        self.players = [add_player(self.db, f"p{i}") for i in range(4)]
        p0, p1, p2, _ = self.players
        for a, b in ((p0, p1), (p0, p2)):
            friends.request(self.db, a, b)
            friends.respond(self.db, b, a, True)
        self.db.commit()
        # MIN_XP baixo para os testes: um jogo de torneio ganho (20 XP) já basta.
        patcher = mock.patch.object(friendly, "MIN_XP", 20)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.db.close()
        self.ctx.pop()
        self.temp.cleanup()

    def client(self, player):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            session["player_id"] = player
        return client

    def give_xp(self, *players):
        """Cada jogador ganha um jogo de torneio do p3 (20 XP)."""
        p3 = self.players[3]
        for pid in players:
            tid = self.league([pid, p3])
            self.play(tid, pid, p3, 1, 0)

    def full(self, a, b, home_goals, away_goals, at=None):
        fid = friendly.invite(self.db, a, b)
        friendly.respond(self.db, fid, b, True)
        friendly.report(self.db, fid, a, home_goals, away_goals)
        row = friendly.confirm(self.db, fid, b, at)
        self.db.commit()
        return row

    def test_only_friends_and_one_open_per_pair(self):
        p0, p1, _, p3 = self.players
        with self.assertRaisesRegex(TournamentError, "amigos"):
            friendly.invite(self.db, p0, p3)
        friendly.invite(self.db, p0, p1)
        with self.assertRaisesRegex(TournamentError, "em aberto"):
            friendly.invite(self.db, p1, p0)

    def test_flow_with_suspension_until_both_accept(self):
        p0, p1 = self.players[:2]
        self.give_xp(p0, p1)
        fid = friendly.invite(self.db, p0, p1, None, "hoje 21h")
        with self.assertRaisesRegex(TournamentError, "aberto para placar"):
            friendly.report(self.db, fid, p0, 1, 0)  # ainda não aceito
        with self.assertRaises(TournamentError):
            friendly.respond(self.db, fid, p0, True)  # só o convidado aceita
        friendly.respond(self.db, fid, p1, True)
        friendly.report(self.db, fid, p0, 3, 0)
        with self.assertRaises(TournamentError):
            friendly.confirm(self.db, fid, p0)  # quem informou não confirma
        friendly.reject(self.db, fid, p1, "foi 1 x 0")
        row = friendly.get(self.db, fid)
        self.assertEqual((row["status"], row["rejected_note"]), ("suspended", "foi 1 x 0"))
        self.assertEqual(stats.player_record(self.db, p0)["played"], 1)  # só o jogo de torneio
        friendly.report(self.db, fid, p1, 1, 0)  # qualquer um propõe outro placar
        row = friendly.confirm(self.db, fid, p0)
        self.db.commit()
        self.assertEqual((row["status"], row["ranked"], row["home_goals"]), ("confirmed", 1, 1))
        self.assertEqual(stats.player_record(self.db, p0)["wins"], 2)
        totals, deltas = xp.compute(self.db)
        self.assertEqual(deltas[f"a{fid}"], {p0: 20, p1: -20})
        self.assertEqual((totals[p0], totals[p1]), (40, 0))
        self.assertIn(("a", f"a{fid}"), [(r["kind"], r["match_key"]) for r in stats.history(self.db, p1)])

    def test_new_accounts_do_not_count(self):
        p0, _, p2, _ = self.players
        self.give_xp(p0)
        row = self.full(p0, p2, 5, 0)  # p2 não tem XP: amistoso fica fora do ranking
        self.assertEqual(row["ranked"], 0)
        self.assertIn("p2 ainda não chegou a 20 XP em campeonatos", row["unranked_reason"])
        self.assertEqual(stats.player_record(self.db, p0)["played"], 1)
        self.assertEqual(xp.compute(self.db)[0][p0], 20)

    def test_unlock_counts_only_tournament_xp_and_stays_after_losing_it(self):
        p0, p1, p2, p3 = self.players
        self.give_xp(p0)
        friends.request(self.db, p1, p2)
        friends.respond(self.db, p2, p1, True)
        # p1 e p2 sem XP de torneio: nem um amistoso "valendo" entre eles libera nada.
        self.db.execute("UPDATE friendly_matches SET status = 'cancelled'")
        with mock.patch.object(friendly, "MIN_XP", 0):
            self.full(p1, p2, 1, 0)  # p1 ganha 20 XP de amistoso
        self.assertEqual(xp.compute(self.db)[0][p1], 20)
        self.assertEqual(self.full(p0, p1, 0, 0)["ranked"], 0)  # XP de amistoso não libera
        # p0 chegou a 20 XP em torneio; perde tudo num jogo de torneio e continua liberado.
        tid = self.league([p0, p3])
        self.play(tid, p0, p3, 0, 1)
        self.give_xp(p1)
        self.assertEqual(xp.compute(self.db)[0][p0], 0)
        self.assertEqual(self.full(p0, p1, 0, 0)["ranked"], 1)

    def test_daily_limit_per_pair(self):
        p0, p1 = self.players[:2]
        self.give_xp(p0, p1)
        start = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
        ranked = [self.full(p0, p1, 0, 0, start + timedelta(hours=i))["ranked"]
                  for i in range(friendly.PAIR_DAILY_LIMIT + 1)]
        self.assertEqual(ranked, [1] * friendly.PAIR_DAILY_LIMIT + [0])
        self.assertEqual(self.full(p0, p1, 0, 0, start + timedelta(hours=25))["ranked"], 1)  # 24 h depois

    def test_cancel_and_decline(self):
        p0, p1, p2, _ = self.players
        fid = friendly.invite(self.db, p0, p1)
        friendly.respond(self.db, fid, p1, False)
        self.assertEqual(friendly.get(self.db, fid)["status"], "declined")
        fid = friendly.invite(self.db, p0, p2)
        with self.assertRaises(TournamentError):
            friendly.cancel(self.db, fid, p2)  # convite: só quem convidou cancela
        friendly.respond(self.db, fid, p2, True)
        friendly.cancel(self.db, fid, p2)  # aceito e sem placar: qualquer um
        self.assertEqual(friendly.get(self.db, fid)["status"], "cancelled")

    def test_pages(self):
        p0, p1, _, p3 = self.players
        self.give_xp(p0, p1)
        c0, c1 = self.client(p0), self.client(p1)
        page = c0.get("/torneios/amistosos?com=p1").data.decode()
        self.assertIn("Chamar para um amistoso", page)
        self.assertIn('value="%d" selected' % p1, page)
        r = c0.post("/torneios/amistosos/novo", data={"csrf_token": CSRF, "amigo": p1,
                                                       "mensagem": "bora"})
        fid = self.db.execute("SELECT id FROM friendly_matches").fetchone()[0]
        self.assertTrue(r.headers["Location"].endswith(f"/torneios/amistosos/{fid}"))
        self.assertIn("Aceitar amistoso", c1.get(f"/torneios/amistosos/{fid}").data.decode())
        self.assertIn('title="Amistosos esperando você">1<', c1.get("/torneios/amistosos").data.decode())
        self.assertEqual(self.client(p3).get(f"/torneios/amistosos/{fid}").status_code, 404)  # ainda não confirmado
        c1.post(f"/torneios/amistosos/{fid}/aceitar", data={"csrf_token": CSRF})
        c1.post(f"/torneios/amistosos/{fid}/placar", data={"csrf_token": CSRF, "home_goals": "2", "away_goals": "1"})
        self.assertIn("Confirmar placar", c0.get(f"/torneios/amistosos/{fid}").data.decode())
        c0.post(f"/torneios/amistosos/{fid}/confirmar", data={"csrf_token": CSRF})
        self.assertEqual(friendly.get(self.db, fid)["status"], "confirmed")
        g.pop("xp_table", None)  # o app context do teste é o mesmo das requisições
        shown = self.client(p3).get(f"/torneios/amistosos/{fid}").data.decode()
        self.assertIn("Vale para o Ranking e o XP", shown)
        self.assertIn("+20 XP", shown)
        status = c0.get("/torneios/status").data.decode()
        self.assertIn(f'id="partida-a{fid}"', status)
        self.assertIn(f"/torneios/amistosos/{fid}", c1.get("/torneios/jogadores/p0").data.decode())
        self.assertEqual(c0.get("/torneios/comparar?a=p0&b=p1").status_code, 200)
        self.assertEqual(c0.get("/torneios/ranking").status_code, 200)
        self.assertEqual(c0.get("/torneios/jogadores/p1/resumo").status_code, 200)


if __name__ == "__main__":
    unittest.main()
