import json
import tempfile
import unittest
from pathlib import Path

from flask import g

from app.db import get_db, init_db, now
from app.tournament import free, stats
from app.tournament.store import TournamentError
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"


class FreeMatchesTest(unittest.TestCase):
    """Partidas livres (tournament/free.py): o card "Partidas livres" do
    perfil, as interrompidas em laranja até os dois combinarem o placar final
    e os três cards do perfil/popup (stats.summary / all_history)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.ctx = self.app.app_context()
        self.ctx.push()
        init_db()
        self.db = get_db()
        self.a, self.b, self.c = (add_player(self.db, name) for name in ("TIERES", "ANDERSHOW", "RIVAL"))
        self.db.commit()
        self.stamp = 0

    def tearDown(self):
        self.db.close()
        self.ctx.pop()
        self.temp.cleanup()

    def fresh(self):
        """Uma requisição nova: sem o cache de g (free.matches, stats._free_records)."""
        for key in ("free_matches", "free_records", "free_games"):
            g.pop(key, None)

    def live(self, local, visita, goals, status="finished", pens=None, **columns):
        """Uma partida do Ao vivo já encerrada/interrompida, LOCAL x VISITA (nicks)."""
        self.stamp += 1
        at = "2026-11-10T20:%02d:00+00:00" % self.stamp
        state = {"teams": [{"goals": goals[0]}, {"goals": goals[1]}], **({"pens": list(pens)} if pens else {})}
        values = {"match_key": "k%d" % self.stamp, "content_id": "BA9BDE21:1C473F60",
                  "room": "PCSX ReARMed: Euro.bin", "players_json": json.dumps([local, visita]),
                  "local_players_json": json.dumps([local] if local else []),
                  "visita_players_json": json.dumps([visita] if visita else []),
                  "status": status, "local_goals": goals[0], "visita_goals": goals[1], "state_json": json.dumps(state),
                  "verified": 1, "is_test": 0, "started_at": at, "updated_at": at, "finished_at": at}
        values.update(columns)
        live_id = self.db.execute(f"INSERT INTO live_matches ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                                  list(values.values())).lastrowid
        self.db.commit()
        self.fresh()
        return live_id

    def client(self, player):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            session["player_id"] = player
        return client

    def get(self, player, url, **kwargs):
        """GET como o jogador, numa "requisição nova" (sem o cache de g)."""
        self.fresh()
        return self.client(player).get(url, **kwargs).get_data(as_text=True)

    def post(self, player, url, data):
        self.fresh()
        return self.client(player).post(url, data=dict(data, csrf_token=CSRF))

    def free_card(self, player):
        self.fresh()
        return next(s for s in stats.summary(self.db, player) if s["key"] == "free")

    def test_finished_counts_and_interrupted_needs_both_players(self):
        self.live("TIERES", "ANDERSHOW", (2, 1), pens=(4, 3))
        stopped = self.live("ANDERSHOW", "TIERES", (1, 1), status="abandoned")
        card = self.free_card(self.a)
        self.assertEqual((card["record"]["played"], card["record"]["wins"], card["position"]), (1, 1, 1))
        self.assertEqual(self.free_card(self.b)["position"], 2)
        rows = stats.all_history(self.db, self.a)
        self.assertEqual([(r["kind"], r["pending"], stats.outcome(r)) for r in rows], [("l", True, "E"), ("l", False, "V")])
        self.assertEqual((rows[1]["pens_for"], rows[1]["pens_against"]), (4, 3))
        self.assertIn("Registrem um placar final", rows[0]["tip"])

        # Um informa (na ordem do card: LOCAL x VISITA), o outro confirma ou não aceita.
        with self.assertRaisesRegex(TournamentError, "não encontrada"):
            free.report(self.db, stopped, self.c, 1, 0)
        free.report(self.db, stopped, self.a, 1, 3)  # ANDERSHOW 1 x 3 TIERES
        self.db.commit()
        self.assertEqual(free.todo_count(self.db, self.b), 1)
        self.assertEqual(free.todo_count(self.db, self.a), 0)
        self.fresh()
        self.assertEqual([m["id"] for m in free.waiting_for(self.db, self.b)], [stopped])
        self.assertIn("TIERES informou o placar final ANDERSHOW 1 x 3 TIERES: falta ANDERSHOW confirmar",
                      stats.all_history(self.db, self.a)[0]["tip"])
        with self.assertRaisesRegex(TournamentError, "falta o adversário"):
            free.report(self.db, stopped, self.a, 1, 3)
        with self.assertRaisesRegex(TournamentError, "Não há placar"):
            free.confirm(self.db, stopped, self.a)  # quem informou não confirma
        with self.assertRaisesRegex(TournamentError, "Explique"):
            free.reject(self.db, stopped, self.b, " ")
        match = free.reject(self.db, stopped, self.b, "estava 1 x 1")
        self.assertEqual((match["score_status"], match["rejected_by"], match["counted"]), ("suspended", self.b, False))
        self.assertEqual(self.free_card(self.a)["record"]["played"], 1)  # ainda não conta

        free.report(self.db, stopped, self.b, 2, 2)  # qualquer um propõe outro
        match = free.confirm(self.db, stopped, self.a)
        self.db.commit()
        self.assertEqual((match["score_status"], match["goals"], match["counted"]), ("confirmed", (2, 2), True))
        record = self.free_card(self.a)["record"]
        self.assertEqual((record["played"], record["wins"], record["draws"], record["goals_for"]), (2, 1, 1, 4))
        row = stats.all_history(self.db, self.a)[0]
        self.assertEqual((row["pending"], row["interrupted"], stats.outcome(row)), (False, True, "E"))
        with self.assertRaisesRegex(TournamentError, "já foi combinado"):
            free.report(self.db, stopped, self.a, 0, 0)
        finished = free.matches(self.db)[-1]["id"]
        with self.assertRaisesRegex(TournamentError, "terminou normalmente"):
            free.report(self.db, finished, self.a, 0, 0)
        # Partida livre não mexe no Ranking geral nem no XP.
        self.assertEqual(stats.player_record(self.db, self.a)["played"], 0)

    def test_only_one_account_per_side_outside_championships_and_friendlies(self):
        friendly = self.db.execute(
            "INSERT INTO friendly_matches (home_id, away_id, status, created_at, updated_at) VALUES (?, ?, 'accepted', ?, ?)",
            (self.a, self.b, now(), now())).lastrowid
        self.live("TIERES", "ANDERSHOW", (1, 0), friendly_id=friendly)
        self.live("TIERES", "ANDERSHOW", (1, 0), is_test=1)
        self.live("TIERES", "ANDERSHOW", (1, 0), verified=0)
        self.live("TIERES", "ANDERSHOW", (1, 0), status="live")
        self.live("TIERES", "", (5, 0))  # contra a CPU
        self.live("TIERES", "SEMCONTA", (1, 0))
        self.live("tieres", "TIERES", (1, 0))
        self.live("TIERES", "ANDERSHOW", (1, 0), local_players_json=json.dumps(["TIERES", "RIVAL"]))
        self.assertEqual(free.matches(self.db), [])
        kept = self.live("tieres", "andershow", (0, 3))  # o nick sem diferenciar maiúsculas
        self.assertEqual([(m["id"], m["local_name"], m["visita_name"]) for m in free.matches(self.db)],
                         [(kept, "TIERES", "ANDERSHOW")])

    def test_three_cards_split_ranked_friendlies_and_free_matches(self):
        for ranked, goals in ((1, (3, 0)), (0, (1, 1)), (0, (0, 2))):
            self.db.execute(
                "INSERT INTO friendly_matches (home_id, away_id, status, home_goals, away_goals, ranked, confirmed_at, "
                "created_at, updated_at) VALUES (?, ?, 'confirmed', ?, ?, ?, ?, ?, ?)",
                (self.a, self.b, goals[0], goals[1], ranked, now(), now(), now()))
        self.live("TIERES", "RIVAL", (1, 0))
        self.db.commit()
        self.fresh()
        cards = {s["key"]: s for s in stats.summary(self.db, self.a)}
        self.assertEqual([s["title"] for s in stats.summary(self.db, self.a)],
                         ["Campeonatos e amistosos rankeados", "Amistosos", "Partidas livres"])
        self.assertEqual({key: (s["record"]["played"], s["record"]["wins"], s["record"]["draws"], s["record"]["losses"])
                          for key, s in cards.items()},
                         {"ranked": (1, 1, 0, 0), "friendly": (2, 0, 1, 1), "free": (1, 1, 0, 0)})
        self.assertEqual((cards["ranked"]["position"], cards["friendly"]["position"], cards["free"]["position"]), (1, 2, 1))
        self.assertEqual(cards["friendly"]["record"]["efficiency"], 17)
        self.assertEqual(stats.position(self.db, self.b, scope="friendly"), 1)
        self.assertIsNone(stats.position(self.db, self.b, scope="free"))
        rows = stats.all_history(self.db, self.a)
        self.assertEqual(sorted(r["scope"] for r in rows), ["free", "friendly", "friendly", "ranked"])
        # O XP e o Ranking geral continuam só com os rankeados.
        self.assertEqual(stats.ranking(self.db)[0]["played"], 1)

    def test_championship_filter_fixed_options(self):
        """Filtro "Campeonato" (Ao vivo e Histórico do perfil): Rankeado, Amistoso
        e Partida livre fixos no topo do autocompletar, os campeonatos abaixo."""
        friendly_ids = {}
        for ranked in (1, 0):
            friendly_ids[ranked] = self.db.execute(
                "INSERT INTO friendly_matches (home_id, away_id, status, home_goals, away_goals, ranked, ranked_requested, "
                "confirmed_at, created_at, updated_at) VALUES (?, ?, 'confirmed', 1, 0, ?, ?, ?, ?, ?)",
                (self.a, self.b, ranked, ranked, now(), now(), now())).lastrowid
        ranked_live = self.live("TIERES", "ANDERSHOW", (1, 0), friendly_id=friendly_ids[1])
        friendly_live = self.live("TIERES", "ANDERSHOW", (1, 0), friendly_id=friendly_ids[0])
        free_live = self.live("TIERES", "ANDERSHOW", (2, 0))
        for text, expected in (("Rankeado", ranked_live), ("Amistoso", friendly_live), ("Partida livre", free_live)):
            html = self.get(self.c, "/ao-vivo/cards", query_string={"campeonato": text})
            self.assertEqual(html.count('<article class="live-card'), 1, text)
            self.assertIn("Partida livre" if expected == free_live else "Amistoso", html)
        page = self.get(self.a, "/ao-vivo")
        self.assertIn('data-fixed="[&#34;Rankeado&#34;, &#34;Amistoso&#34;, &#34;Partida livre&#34;]"', page)

        # Histórico do perfil (o gráfico): cada opção fixa = um card.
        self.live("TIERES", "RIVAL", (0, 1))  # 2ª partida livre: com ela o filtro tem gráfico
        points = lambda html: html.split('id="historico"')[1].count('<g class="evo-pt"')  # noqa: E731
        self.assertEqual(points(self.get(self.a, "/torneios/jogadores/TIERES")), 4)
        for text in ("Rankeado", "amistoso"):
            self.assertIn("O gráfico aparece a partir da segunda partida com esses filtros.",
                          self.get(self.a, "/torneios/jogadores/TIERES", query_string={"campeonato": text}))
        html = self.get(self.a, "/torneios/jogadores/TIERES", query_string={"campeonato": "Partida livre"})
        self.assertEqual((points(html), html.count('"origin": "Partida livre"')), (2, 2))
        html = self.get(self.a, "/torneios/jogadores/TIERES")
        self.assertIn('data-fixed="[&#34;Rankeado&#34;, &#34;Amistoso&#34;, &#34;Partida livre&#34;]"', html)
        self.assertIn('data-options="[]"', html)  # sem campeonatos: "Amistoso"/"Partida livre" não se repetem abaixo

    def test_pages(self):
        stopped = self.live("TIERES", "ANDERSHOW", (2, 2), status="abandoned")
        self.live("TIERES", "ANDERSHOW", (3, 1))
        popup = self.get(self.c, "/torneios/jogadores/TIERES/resumo")
        for text in ("Campeonatos e amistosos rankeados", "Amistosos</h3>", "Partidas livres</h3>",
                     '<span>Rank.</span>', 'title="Aproveitamento"><span>Aprov.</span>', "Últimos 2 resultados",
                     # Só os V/E/D clicáveis: a interrompida em laranja, com o pedido no balão.
                     'class="f-btn f-E f-pending" href="/ao-vivo?partida=%d" title="2 x 2 contra ANDERSHOW · '
                     'Partida livre · interrompida · ' % stopped, "Registrem um placar final",
                     'class="f-btn f-V" href="/ao-vivo?partida=', "Em laranja:</span> interrompida"):
            self.assertIn(text, popup)
        # Cada card leva ao Ao vivo com o jogador e o tipo de partida filtrados.
        for kind in ("Rankeado", "Amistoso", "Partida+livre"):
            self.assertIn('href="/ao-vivo?jogador=TIERES&amp;campeonato=%s"' % kind, popup)
        # Para os dois jogadores, a interrompida leva à página do placar final.
        mine = self.get(self.a, "/torneios/jogadores/TIERES")
        self.assertIn('class="f-btn f-E f-pending" href="/torneios/partidas-livres/%d"' % stopped, mine)
        # Meus jogos: o mesmo Histórico (filtros + gráfico), voltando para a própria página.
        matches = self.get(self.a, "/torneios/meus-jogos", query_string={"campeonato": "Partida livre"})
        history = matches.split('<section id="historico">')[1]
        self.assertIn('action="/torneios/meus-jogos#historico"', history)
        self.assertIn('id="historico-campeonato" value="Partida livre"', history)
        self.assertIn("O gráfico aparece a partir da segunda partida com esses filtros.", history)  # a interrompida não conta
        self.live("TIERES", "RIVAL", (2, 0))
        self.assertEqual(self.get(self.a, "/torneios/meus-jogos").split('id="historico"')[1].count('<g class="evo-pt"'), 2)
        self.fresh()
        self.assertEqual(self.client(self.c).get("/torneios/partidas-livres/%d" % stopped).status_code, 404)
        page = self.get(self.a, "/torneios/partidas-livres/%d" % stopped)
        self.assertIn("A partida foi interrompida com TIERES <strong>2 x 2</strong> ANDERSHOW", page)
        self.assertIn('name="local_goals"', page)
        self.assertIn("Registrar o placar final", self.get(self.a, "/ao-vivo/cards", query_string={"partida": stopped}))
        self.assertNotIn("placar final", self.get(self.c, "/ao-vivo/cards", query_string={"partida": stopped}))
        # Quem joga: a foto com o rank; logado, a foto abre o resumo e o nick, o perfil completo.
        logged = self.get(self.c, "/ao-vivo/cards", query_string={"partida": stopped})
        self.assertIn('data-player-card="/torneios/jogadores/ANDERSHOW/resumo"', logged)
        self.assertIn('<a href="/torneios/jogadores/TIERES" title="Perfil de TIERES">TIERES</a>', logged)
        self.fresh()
        anonymous = self.app.test_client().get("/ao-vivo/cards", query_string={"partida": stopped}).get_data(as_text=True)
        self.assertIn('class="rank-badge rank-F"', anonymous)
        self.assertIn("<small>TIERES</small>", anonymous)
        self.assertNotIn("data-player-card", anonymous)

        url = "/torneios/partidas-livres/%d/" % stopped
        self.post(self.a, url + "informar", {"local_goals": "3", "visita_goals": "2"})
        friendlies = self.get(self.b, "/torneios/amistosos")
        self.assertIn("Partidas livres esperando você (1)", friendlies)
        # Meus jogos: o placar final da partida livre e o do amistoso esperando a confirmação.
        fid = self.db.execute(
            "INSERT INTO friendly_matches (home_id, away_id, status, home_goals, away_goals, reported_by, reported_at, "
            "created_at, updated_at) VALUES (?, ?, 'reported', 2, 1, ?, ?, ?, ?)",
            (self.a, self.b, self.a, now(), now(), now())).lastrowid
        self.db.commit()
        waiting = self.get(self.b, "/torneios/meus-jogos").split('<section id="historico">')[0]
        self.assertIn("Esperando você (2)", waiting)
        self.assertIn('id="amistoso-%d"' % fid, waiting)
        self.assertIn("Confirme o placar do amistoso", waiting)
        self.assertIn('<span class="score-val score-reported">2 x 1</span>', waiting)
        self.assertIn('href="/torneios/partidas-livres/%d">Confirmar ou não aceitar</a>' % stopped, waiting)
        self.assertNotIn("Esperando você", self.get(self.a, "/torneios/meus-jogos"))  # quem informou só espera
        self.db.execute("DELETE FROM friendly_matches WHERE id = ?", (fid,))
        self.db.commit()
        self.assertIn('<span class="todo-badge" title="Amistosos esperando você">1</span>', friendlies)
        page = self.get(self.b, "/torneios/partidas-livres/%d" % stopped)
        self.assertIn("TIERES informou o placar final TIERES <strong>3 x 2</strong> ANDERSHOW", page)
        self.assertIn("Confirmar placar", page)
        self.assertEqual(self.post(self.b, url + "rejeitar", {"motivo": ""}).status_code, 302)
        self.assertEqual(self.db.execute("SELECT status FROM free_match_scores").fetchone()[0], "reported")
        self.post(self.b, url + "confirmar", {})
        self.assertEqual(self.db.execute("SELECT status FROM free_match_scores").fetchone()[0], "confirmed")
        popup = self.get(self.c, "/torneios/jogadores/TIERES/resumo")
        self.assertNotIn("f-pending", popup)
        self.assertIn('class="f-btn f-V" href="/ao-vivo?partida=%d" title="3 x 2 contra ANDERSHOW · '
                      'Partida livre · placar final combinado' % stopped, popup)
        self.assertIn("Placar final combinado: 3 x 2", self.get(self.c, "/ao-vivo/cards", query_string={"partida": stopped}))


if __name__ == "__main__":
    unittest.main()
