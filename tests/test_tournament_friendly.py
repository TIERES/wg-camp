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
        with self.assertRaisesRegex(TournamentError, "Explique"):
            friendly.reject(self.db, fid, p1, "  ")  # o motivo é obrigatório
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
        # Amigo pela busca do nick: com ?com= ele já vem escolhido (id no campo escondido).
        self.assertIn('<input type="hidden" name="amigo" value="%d" data-user-id>' % p1, page)
        self.assertIn('role="combobox"', page)
        self.assertIn('data-id="%d" data-nick="p2"' % self.players[2], page)  # amigos na lista
        self.assertNotIn('data-nick="p3"', page)  # quem não é amigo fica de fora
        page = c0.get("/torneios/amistosos?com=P1").data.decode()  # sem diferenciar maiúscula
        self.assertIn('name="amigo" value="%d"' % p1, page)
        self.assertIn('name="amigo" value=""', c0.get("/torneios/amistosos?com=p3").data.decode())
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
        # Placar informado pelos jogadores: vale como um jogo só e aparece na linha, mesmo sem "Placar agregado".
        self.assertIn('<strong class="friendly-score-text">2 x 1</strong>', shown)
        status = c0.get("/torneios/status").data.decode()
        self.assertIn(f'id="partida-a{fid}"', status)
        self.assertIn(f"/torneios/amistosos/{fid}", c1.get("/torneios/jogadores/p0").data.decode())
        self.assertEqual(c0.get("/torneios/comparar?a=p0&b=p1").status_code, 200)
        self.assertEqual(c0.get("/torneios/ranking").status_code, 200)
        self.assertEqual(c0.get("/torneios/jogadores/p1/resumo").status_code, 200)

    def test_opponent_decides_quitter_penalty_on_confirm(self):
        """Quem saiu no meio de um jogo perde o triplo de XP só se o adversário
        deixar a caixa marcada ao confirmar; quem saiu não decide."""
        p0, p1, p2, _ = self.players
        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        def game_friendly(home, away):
            fid = friendly.invite(self.db, home, away)
            friendly.respond(self.db, fid, away, True)
            friendly.report_from_game(self.db, fid, 1, 3, at, quitter_id=home)
            self.db.commit()
            return fid

        def confirm(player, fid, **form):
            self.client(player).post(f"/torneios/amistosos/{fid}/confirmar", data={"csrf_token": CSRF, **form})
            return friendly.get(self.db, fid)

        # Desmarcada: o adversário tira a penalidade (mesmo confirmando primeiro).
        waived = game_friendly(p0, p1)
        row = confirm(p1, waived, quem_saiu=str(p0))
        self.assertEqual((row["status"], row["quitter_id"]), ("reported", None))
        self.assertEqual(confirm(p0, waived)["status"], "confirmed")
        self.assertIsNone(friendly.get(self.db, waived)["quitter_id"])
        # Marcada: continua valendo o triplo.
        kept = game_friendly(p0, p2)
        confirm(p0, kept)
        row = confirm(p2, kept, quem_saiu=str(p0), penalizar="1")
        self.assertEqual((row["status"], row["quitter_id"]), ("confirmed", p0))
        # Quem saiu não tira a própria penalidade, nem com o formulário forjado.
        self.db.execute("DELETE FROM friendly_matches")
        self.db.commit()
        own = game_friendly(p0, p1)
        self.assertEqual(confirm(p0, own, quem_saiu=str(p0))["quitter_id"], p0)
        # Página velha (outro quitter_id): não mexe na marca.
        self.db.execute("DELETE FROM friendly_matches")
        self.db.commit()
        stale = game_friendly(p0, p1)
        self.assertEqual(confirm(p1, stale, quem_saiu=str(p2))["quitter_id"], p0)

    def test_invite_aggregate_option(self):
        """"Placar agregado" no convite: desmarcado por padrão, guardado no
        amistoso e mostrado a quem é convidado."""
        p0, p1, p2, _ = self.players
        c0 = self.client(p0)
        self.assertIn('<input type="checkbox" name="agregado" value="1">', c0.get("/torneios/amistosos").data.decode())
        c0.post("/torneios/amistosos/novo", data={"csrf_token": CSRF, "amigo": p1})
        c0.post("/torneios/amistosos/novo", data={"csrf_token": CSRF, "amigo": p2, "agregado": "1"})
        rows = {r["away_id"]: r["aggregate"] for r in self.db.execute("SELECT away_id, aggregate FROM friendly_matches")}
        self.assertEqual(rows, {p1: 0, p2: 1})
        invited = self.client(p2).get("/torneios/amistosos").data.decode()
        self.assertIn("chamou você para um amistoso com <strong>placar agregado</strong>", invited)
        self.assertIn("Placar agregado</span>", invited)
        plain = self.client(p1).get("/torneios/amistosos").data.decode()
        self.assertIn("p0 chamou você para um amistoso.", plain)
        self.assertNotIn("Placar agregado</span>", plain)

    def test_list_layout_and_open_filter(self):
        """Lista de amistosos: os em aberto no topo (carrossel, com filtro quando
        tem mais de um), as regras no (i) do título e o placar na linha dos jogadores."""
        p0, p1, p2, _ = self.players
        accepted = friendly.invite(self.db, p0, p1)
        friendly.respond(self.db, accepted, p1, True)
        waiting = friendly.invite(self.db, p0, p2)
        self.db.commit()
        c0 = self.client(p0)
        page = c0.get("/torneios/amistosos").data.decode()
        self.assertLess(page.index("Em aberto (2)"), page.index("Chamar para um amistoso"))
        self.assertIn('name="aberto_adversario"', page)
        self.assertEqual(page.count('class="carousel-slide"'), 2)
        # Regras no (i) do título, sem o bloco recolhido.
        self.assertIn('data-dialog="amistoso-regras"', page)
        self.assertIn("convites para amistosos por dia</strong> contra o mesmo adversário, mas vocês podem", page)
        self.assertNotIn('class="xp-rules"', page)
        # Aceito: gols na linha dos jogadores (form="placar-<id>"), explicação no (i) do card.
        self.assertEqual(page.count(f'form="placar-{accepted}"'), 2)
        self.assertIn(f'id="placar-{accepted}"', page)
        self.assertIn(f'data-dialog="amistoso-info-{accepted}"', page)
        self.assertIn("Depois do jogo, informe o placar. p1 vai precisar confirmar.", page)
        # Filtro dos em aberto: só o do adversário; o filtro dos anteriores continua no formulário.
        filtered = c0.get("/torneios/amistosos?aberto_adversario=P2&adversario=x").data.decode()
        self.assertIn("Em aberto (1 de 2)", filtered)
        self.assertIn(f'id="amistoso-{waiting}"', filtered)
        self.assertNotIn(f'id="amistoso-{accepted}"', filtered)
        self.assertIn('<input type="hidden" name="adversario" value="x">', filtered)
        # Um só em aberto: sem filtro.
        friendly.cancel(self.db, waiting, p0)
        self.db.commit()
        single = c0.get("/torneios/amistosos").data.decode()
        self.assertIn("Em aberto (1)", single)
        self.assertNotIn('name="aberto_adversario"', single)
        # O placar enviado pelos campos da linha continua chegando.
        c0.post(f"/torneios/amistosos/{accepted}/placar", data={"csrf_token": CSRF, "home_goals": "3", "away_goals": "2"})
        row = friendly.get(self.db, accepted)
        self.assertEqual((row["status"], row["home_goals"], row["away_goals"]), ("reported", 3, 2))


if __name__ == "__main__":
    unittest.main()
