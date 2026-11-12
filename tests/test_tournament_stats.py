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
        meetings = stats.history(self.db, p1, opponent_id=p0, limit=5)
        self.assertEqual(len(meetings), 1)
        row = meetings[0]
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
        self.assertNotIn("Gols pró".encode(), page.data)
        self.assertIn("Entre</a> para ver os seus últimos resultados".encode(), page.data)  # sem login
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

    def test_compare_shows_my_last_results_against_each_player(self):
        p0, p1, p2 = self.players[:3]
        tid = self.league([p0, p1, p2])
        self.play(tid, p0, p1, 2, 1)
        self.play(tid, p0, p2, 0, 1)
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["player_id"] = p0
        # Comparando a si mesmo com outro: um card só.
        page = client.get("/torneios/comparar?a=p0&b=p1").get_data(as_text=True)
        self.assertEqual(page.count('class="my-last"'), 1)
        self.assertIn("Seus últimos resultados contra", page)
        self.assertEqual(page.count('class="match-dialog"'), 1)
        self.assertIn('class="f-btn f-V"', page)
        # Comparando dois outros jogadores: um card para cada.
        page = client.get("/torneios/comparar?a=p1&b=p2").get_data(as_text=True)
        self.assertEqual(page.count('class="my-last"'), 2)
        self.assertEqual(page.count('class="match-dialog"'), 2)
        self.assertIn('class="f-btn f-D"', page)  # perdeu para p2


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
        self.assertEqual(page.count('class="my-match mm-'), 2)  # o confirmado e o que falta
        self.assertIn('my-match mm-ok', page)
        self.assertIn('my-match mm-none', page)
        self.assertIn("Resultado confirmado entre os jogadores", page)  # legenda
        self.assertIn("1 confirmado", page)
        self.assertIn("1 para reportar", page)

    def test_home_and_away_league_shows_two_blocks(self):
        cfg = formats.normalize("league", {"legs": "2"})
        tid = self.db.execute(
            "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
            "settings_json, created_at, updated_at) VALUES ('liga-iv', 'Liga', ?, ?, 'league', 'registration', 4, "
            "?, ?, ?)", (self.game, self.players[0], json.dumps(cfg), now(), now())).lastrowid
        for i, pid in enumerate(self.players[:4]):
            store.enroll(self.db, store.get_tournament(self.db, tid), pid, f"Time {i}")
        store.start(self.db, store.get_tournament(self.db, tid), random.Random(1))
        self.db.commit()
        page = self.app.test_client().get("/torneios/liga-iv?aba=jogos").get_data(as_text=True)
        # Um bloco só, com os botões Ida/Volta girando entre os dois turnos.
        self.assertIn('data-leg="0" aria-pressed="true">Ida</button>', page)
        self.assertIn('data-leg="1" aria-pressed="false">Volta</button>', page)
        self.assertNotIn("Rodada 1", page)
        self.assertNotIn("<details", page)
        faces = page.split('class="leg-face back"')
        self.assertEqual(faces[0].count('class="match-line'), 6)  # 4 times: 6 jogos em cada turno
        self.assertEqual(faces[1].count('class="match-line'), 6)

class TrophyGalleryTest(unittest.TestCase):
    """Galeria de troféus (1º, 2º e 3º dos campeonatos encerrados) no Status e na
    página do jogador, e o filtro de jogo em cards, só com os liberados."""

    # Os helpers do StatsTest, sem herdar (e repetir) os testes dele.
    setUp, tearDown, add_game, league, play = (StatsTest.setUp, StatsTest.tearDown, StatsTest.add_game,
                                               StatsTest.league, StatsTest.play)

    def finished_league(self, players, game=None):
        """Liga encerrada: players[0] campeão, players[1] vice, players[2] 3º."""
        tid = self.league(players, game)
        for i, a in enumerate(players):
            for b in players[i + 1:]:
                self.play(tid, a, b, 2, 0)
        store.finish(self.db, store.get_tournament(self.db, tid))
        self.db.commit()
        return tid

    def test_trophies_pages_and_game_filter(self):
        p0, p1, p2, p3 = self.players
        other = self.add_game("WE 4 Teste")
        retired = self.db.execute("INSERT INTO game_titles (name, is_active, created_at, updated_at) "
                                  "VALUES ('Jogo Antigo', 0, ?, ?)", (now(), now())).lastrowid
        self.db.commit()
        first = self.finished_league([p0, p1, p2])
        second = self.finished_league([p1, p0, p2], game=other)
        self.league([p0, p1, p3])  # em andamento: não dá troféu

        trophies = stats.trophies(self.db, p0)
        self.assertEqual([(t["tournament_id"], t["final_position"]) for t in trophies], [(second, 2), (first, 1)])
        self.assertEqual([t["tournament_id"] for t in stats.trophies(self.db, p0, other)], [second])
        self.assertEqual([t["final_position"] for t in stats.trophies(self.db, p2)], [3, 3])
        self.assertEqual(stats.trophies(self.db, p3), [])

        client = self.app.test_client()
        page = client.get("/torneios/jogadores/p0").get_data(as_text=True)
        self.assertIn("Galeria de troféus", page)
        self.assertIn("Campeão", page)
        self.assertIn("Vice-campeão", page)
        self.assertIn(f'href="/torneios/{store.get_tournament(self.db, first)["slug"]}"', page)
        # Filtro em cards: os jogos liberados (com o atual marcado), sem os desativados.
        self.assertIn('class="game-chip all active"', page)
        self.assertIn(f'href="/torneios/jogadores/p0?jogo={other}"', page)
        self.assertNotIn(f"jogo={retired}", page)
        self.assertNotIn('<select name="jogo"', page)
        filtered = client.get(f"/torneios/jogadores/p0?jogo={other}").get_data(as_text=True)
        self.assertIn("Vice-campeão", filtered)
        self.assertNotIn(">Campeão<", filtered)
        self.assertIn("Nenhum troféu ainda", client.get("/torneios/jogadores/p3").get_data(as_text=True))
        # Jogo desativado no ?jogo=: vale como "Todos os jogos".
        self.assertIn('class="game-chip all active"',
                      client.get(f"/torneios/ranking?jogo={retired}").get_data(as_text=True))

        with client.session_transaction() as session:
            session["player_id"] = p0
        status = client.get("/torneios/status").get_data(as_text=True)
        self.assertIn("Galeria de troféus", status)
        self.assertIn("<strong>1</strong><span>título</span>", status)
        self.assertIn("<strong>1</strong><span>vice</span>", status)
        # Os quadros dos totais filtram os cards: 3º lugar sem troféu fica desabilitado.
        self.assertIn("Selecione o troféu para filtrar.", status)
        self.assertIn('data-trophy-filter="1" aria-pressed="false" title', status)
        self.assertIn('data-trophy-filter="3" aria-pressed="false" disabled', status)
        self.assertIn('data-place="2"', status)
        with client.session_transaction() as session:
            session["player_id"] = p3
        self.assertIn("Você ainda não tem troféus", client.get("/torneios/status").get_data(as_text=True))

    def test_player_history_filters_and_opponent_photos(self):
        from datetime import timedelta
        from app.timeutil import to_local
        p0, p1, p2, _ = self.players
        liga = self.league([p0, p1, p2])
        copa = self.league([p0, p1])
        self.db.execute("UPDATE tournaments SET name = 'Copa Lendária' WHERE id = ?", (copa,))
        self.db.commit()
        self.play(liga, p0, p1, 1, 0)
        self.play(liga, p0, p2, 2, 2)
        self.play(copa, p0, p1, 0, 3)
        client = self.app.test_client()

        def history(**args):
            page = client.get("/torneios/jogadores/p0", query_string=args).get_data(as_text=True)
            return page[page.index('id="historico"'):]

        page = history()
        self.assertEqual(page.count('class="recent"><span class="f-'), 3)
        self.assertIn('class="avatar-btn"', page)  # foto e rank do adversário
        self.assertIn('data-nick="p1"', page)  # adversários no autocompletar com foto
        self.assertIn("Copa Lend\\u00e1ria", page)  # campeonatos no autocompletar
        # Adversário: o nick escolhido (exato) ou um trecho.
        self.assertEqual(history(adversario="P1").count('class="recent"><span class="f-'), 2)
        self.assertEqual(history(adversario="p").count('class="recent"><span class="f-'), 3)
        # Campeonato: trecho do nome, sem acento.
        self.assertEqual(history(campeonato="lendaria").count('class="recent"><span class="f-'), 1)
        self.assertEqual(history(campeonato="liga", adversario="p2").count('class="recent"><span class="f-'), 1)
        # Datas (dia local do jogo).
        today = to_local(now()).date()
        self.assertEqual(history(de=today.isoformat(), ate=today.isoformat()).count('class="recent"><span class="f-'),
                         3)
        self.assertIn("Nenhum jogo com esses filtros.", history(de=(today + timedelta(days=1)).isoformat()))
        self.assertIn("Nenhum jogo com esses filtros.", history(ate=(today - timedelta(days=1)).isoformat()))
        # Os cards de jogo mantêm os filtros do histórico.
        full = client.get("/torneios/jogadores/p0?adversario=p1").get_data(as_text=True)
        self.assertIn(f"/torneios/jogadores/p0?jogo={self.game}&amp;adversario=p1", full)
        # Adversários mais enfrentados: com a foto, sem a coluna "Confronto".
        top = full[full.index("Adversários mais enfrentados"):full.index('id="historico"')]
        self.assertIn('class="avatar-btn"', top)
        self.assertNotIn("Confronto", top)

    def test_player_history_page_size_and_top_ten_opponents(self):
        """Histórico: os 20 mais recentes por padrão, com o "Mostrar" (20/50/100);
        Adversários mais enfrentados: sempre os 10 primeiros."""
        p0 = self.players[0]
        others = self.players[1:] + [add_player(self.db, f"x{i:02d}") for i in range(18)]
        tid = self.league([p0] + others)
        for other in others:  # 21 jogos do p0
            self.play(tid, p0, other, 1, 0)
        client = self.app.test_client()
        rows = 'class="recent"><span class="f-'

        page = client.get("/torneios/jogadores/p0").get_data(as_text=True)
        history = page[page.index('id="historico"'):]
        self.assertEqual(history.count(rows), 20)
        self.assertIn('<form method="get" class="per-page-form" action="#historico">', history)
        self.assertIn('<option value="50" >21</option>', history)
        self.assertIn("Mostrando 20 de 21 jogos.", history)
        top = page[page.index("Adversários mais enfrentados"):page.index('id="historico"')]
        self.assertEqual(top.count("<tr><td class=\"name-col\">"), 10)

        page = client.get("/torneios/jogadores/p0?mostrar=50").get_data(as_text=True)
        history = page[page.index('id="historico"'):]
        self.assertEqual(history.count(rows), 21)
        self.assertNotIn("Mostrando 20 de", history)
        self.assertIn('<input type="hidden" name="mostrar" value="50">', history)  # o filtro mantém
        self.assertIn(f"/torneios/jogadores/p0?jogo={self.game}&amp;mostrar=50", page)  # os cards de jogo também


if __name__ == "__main__":
    unittest.main()


class EvolutionTest(unittest.TestCase):
    def test_cumulative_efficiency_oldest_first(self):
        from app.tournament.stats import evolution
        rows = [  # history(): mais recente primeiro
            {"goals_for": 0, "goals_against": 1, "opponent_name": "c", "played_at": "3"},
            {"goals_for": 1, "goals_against": 1, "opponent_name": "b", "played_at": "2"},
            {"goals_for": 2, "goals_against": 0, "opponent_name": "a", "played_at": "1"},
        ]
        points = evolution(rows)
        self.assertEqual([p["outcome"] for p in points], ["V", "E", "D"])
        self.assertEqual([p["efficiency"] for p in points], [100, 67, 44])
        self.assertEqual(points[0]["opponent"], "a")
        self.assertEqual(evolution([]), [])
