"""Ordem dinâmica do mata-mata (settings_json ko_arrange): o mata-mata sorteado no
início ou montado depois da fase anterior fica em rascunho, sem jogos; o
organizador arrasta os jogadores nas chaves iniciais e, ao confirmar, a chave é
montada de vez (com os jogos e os e-mails) e não muda mais."""
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


class KnockoutArrangeStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.ctx = self.app.app_context()
        self.ctx.push()
        init_db()
        self.db = get_db()
        self.game = self.db.execute("SELECT id FROM game_titles ORDER BY id LIMIT 1").fetchone()[0]
        self.players = [add_player(self.db, f"p{i}") for i in range(10)]
        self.rng = random.Random(5)

    def tearDown(self):
        self.db.close()
        self.ctx.pop()
        self.temp.cleanup()

    def tournament(self, players, fmt="knockout", **config):
        cfg = formats.normalize(fmt, {k: str(v) for k, v in config.items()})
        tid = self.db.execute(
            "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
            "settings_json, created_at, updated_at) VALUES (?, 'Copa', ?, ?, ?, 'registration', 16, ?, ?, ?)",
            (f"ko-{self.rng.random()}", self.game, self.players[0], fmt, json.dumps(cfg), now(), now())).lastrowid
        for player in self.players[:players]:
            store.enroll(self.db, self.t(tid), player, f"Time {player}")
        return tid

    def t(self, tid):
        return store.get_tournament(self.db, tid)

    def stage(self, tid):
        return store.stages_of(self.db, tid)[-1]

    def matches(self, stage_id):
        return self.db.execute("SELECT * FROM tournament_matches WHERE stage_id = ?", (stage_id,)).fetchall()

    def slots(self, tid):
        return [entry for pair in store.bracket_pairs(self.db, self.stage(tid)["id"]) for entry in pair]

    def test_flag_in_config_and_description(self):
        config = formats.normalize("knockout", {"ko_arrange": "on"})
        self.assertTrue(config["ko_arrange"])
        self.assertIn("definidas pelo organizador", formats.describe("knockout", config))
        # Formulário sem o checkbox marcado: desligado; campeonato antigo (sem a chave): desligado.
        self.assertFalse(formats.normalize("world_cup", {"_form": "1"})["ko_arrange"])
        self.assertFalse(formats.normalize("ucl", {})["ko_arrange"])
        self.assertNotIn("ko_arrange", formats.DEFAULTS["league"])

    def test_knockout_starts_as_a_draft_without_matches(self):
        tid = self.tournament(5, ko_arrange="on")
        store.start(self.db, self.t(tid), self.rng)
        stage = self.stage(tid)
        self.assertEqual(stage["arranging"], 1)
        self.assertEqual(store.arranging_stage(self.db, self.t(tid))["id"], stage["id"])
        # 5 numa chave de 8: os confrontos existem (inclusive o da 2ª rodada com os dois que passam direto),
        # mas nenhum jogo - nem para reportar, nem para o Ao vivo ligar.
        self.assertEqual(self.matches(stage["id"]), [])
        self.assertEqual(sorted(e for e in self.slots(tid) if e), sorted(e["id"] for e in store.entries_of(self.db, tid)))
        self.assertFalse(store.stage_complete(self.db, stage))
        # A janela "está valendo!" espera a confirmação da chave.
        from app.tournament import announce
        with self.app.test_request_context():
            self.assertIsNone(announce.pending(self.db, self.players[1]))
            store.arrange_bracket(self.db, self.t(tid), None, confirm=True)
            self.assertEqual(announce.pending(self.db, self.players[1])["id"], tid)

    def test_organizer_swaps_positions_then_confirms(self):
        tid = self.tournament(5, ko_arrange="on", third_place="on")
        store.start(self.db, self.t(tid), self.rng)
        slots = self.slots(tid)
        # O jogador sozinho no 1º confronto troca de lugar com o primeiro do 2º.
        lone, other = slots[0], slots[2]
        new = list(slots)
        new[0], new[2] = other, lone
        store.arrange_bracket(self.db, self.t(tid), new)
        self.assertIsNone(slots[1])  # 5 numa chave de 8: o 1º confronto é (1º, vaga)
        self.assertEqual(self.slots(tid), new)
        self.assertEqual(self.matches(self.stage(tid)["id"]), [])
        store.arrange_bracket(self.db, self.t(tid), None, confirm=True)
        stage = self.stage(tid)
        self.assertEqual(stage["arranging"], 0)
        self.assertIsNone(store.arranging_stage(self.db, self.t(tid)))
        played = {(m["home_entry_id"], m["away_entry_id"]) for m in self.matches(stage["id"])}
        for a, b in store.bracket_pairs(self.db, stage["id"]):
            if a and b:
                self.assertTrue((a, b) in played or (b, a) in played)
        # Confirmado: não muda mais.
        with self.assertRaisesRegex(TournamentError, "já foi confirmado"):
            store.arrange_bracket(self.db, self.t(tid), self.slots(tid))

    def test_byes_moved_by_the_organizer(self):
        # 3 jogadores: (1º, vaga) e (2º, 3º); o organizador dá a vaga ao 3º, que passa direto (sem repescagem).
        tid = self.tournament(3, ko_arrange="on")
        store.start(self.db, self.t(tid), self.rng)
        a, bye, b, c = self.slots(tid)
        self.assertIsNone(bye)
        store.arrange_bracket(self.db, self.t(tid), [a, b, None, c], confirm=True)
        stage = self.stage(tid)
        self.assertFalse(store.settings_of(stage)["repechage"])
        ties = self.db.execute("SELECT * FROM tournament_ties WHERE stage_id = ? ORDER BY round, position",
                               (stage["id"],)).fetchall()
        self.assertEqual((ties[0]["entry_a"], ties[0]["entry_b"]), (a, b))
        self.assertEqual((ties[1]["entry_a"], ties[1]["winner_entry_id"]), (c, c))
        self.assertIsNone(ties[0]["loser_next_tie_id"])
        self.assertEqual(ties[2]["entry_b"], c)  # já na final, esperando o vencedor de a x b
        self.assertEqual(len(self.matches(stage["id"])), 1)

    def play(self, tid, home_wins=True):
        """Confirma os jogos abertos do campeonato (o mandante vence); devolve quantos."""
        open_ = self.db.execute("SELECT m.id FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
                                "WHERE s.tournament_id = ? AND m.status != 'confirmed' ORDER BY m.id", (tid,)).fetchall()
        for match in open_:
            store.record_result(self.db, self.t(tid), match["id"], *((2, 0) if home_wins else (0, 2)))
        return len(open_)

    def tie(self, stage_id, round_no, position):
        return self.db.execute("SELECT * FROM tournament_ties WHERE stage_id = ? AND round = ? AND position = ? "
                               "AND is_third_place = 0", (stage_id, round_no, position)).fetchone()

    def finished_ranking(self, tid):
        return [e["id"] for e in sorted((e for e in store.entries_of(self.db, tid) if e["final_position"]),
                                        key=lambda e: e["final_position"])]

    def test_empty_matchup_lets_the_neighbor_winner_pass(self):
        # 6 jogadores numa chave de 8: o confronto 2 vazio - quem vencer o 1 vai direto para a final
        # (a semifinal 1 só tem esse lado e passa direto, sem jogo).
        tid = self.tournament(6, ko_arrange="on", third_place="on")
        store.start(self.db, self.t(tid), self.rng)
        p = [e for e in self.slots(tid) if e]
        store.arrange_bracket(self.db, self.t(tid), [p[0], p[1], None, None, p[2], p[3], p[4], p[5]], confirm=True)
        stage = self.stage(tid)
        empty, semi = self.tie(stage["id"], 1, 1), self.tie(stage["id"], 2, 0)
        self.assertEqual((empty["is_bye"], empty["entry_a"], empty["winner_entry_id"]), (1, None, None))
        self.assertEqual(semi["is_bye"], 1)
        # Semifinal sem jogo: sem disputa de 3º (o 3º é o semifinalista eliminado).
        self.assertIsNone(self.db.execute("SELECT 1 FROM tournament_ties WHERE stage_id = ? AND is_third_place = 1",
                                          (stage["id"],)).fetchone())
        self.assertEqual(len(self.matches(stage["id"])), 3)  # as 3 quartas com jogadores
        self.play(tid)
        semi = self.tie(stage["id"], 2, 0)
        self.assertEqual(semi["winner_entry_id"], p[0])  # o mandante do confronto 1 venceu
        self.assertEqual(self.tie(stage["id"], 3, 0)["entry_a"], p[0])
        self.assertIsNone(self.db.execute("SELECT 1 FROM tournament_matches WHERE tie_id = ?",
                                          (semi["id"],)).fetchone())
        while self.play(tid):
            pass
        self.assertTrue(store.stage_complete(self.db, stage))
        store.finish(self.db, self.t(tid))
        ranking = self.finished_ranking(tid)
        self.assertEqual(ranking[0], p[0])
        self.assertEqual(len(ranking), 3)

    def test_empty_quarter_of_the_bracket_chains(self):
        # 10 jogadores numa chave de 16 (oitavas): os confrontos 3 e 4 vazios - a quartas 2 fica vazia
        # e quem vencer a quartas 1 passa a semifinal 1 sem jogo.
        tid = self.tournament(10, ko_arrange="on", third_place="on")
        store.start(self.db, self.t(tid), self.rng)
        p = [e for e in self.slots(tid) if e]
        slots = [p[0], p[1], p[2], p[3], None, None, None, None,
                 p[4], p[5], p[6], p[7], p[8], None, p[9], None]
        store.arrange_bracket(self.db, self.t(tid), slots, confirm=True)
        stage = self.stage(tid)
        self.assertEqual(self.tie(stage["id"], 2, 1)["is_bye"], 1)  # quartas 2: vazia (3 e 4 vazios)
        self.assertEqual(self.tie(stage["id"], 3, 0)["is_bye"], 1)  # semifinal 1: só um lado
        self.assertIsNone(self.db.execute("SELECT 1 FROM tournament_ties WHERE stage_id = ? AND is_third_place = 1",
                                          (stage["id"],)).fetchone())
        while self.play(tid):
            pass
        self.assertTrue(store.stage_complete(self.db, stage))
        store.finish(self.db, self.t(tid))
        self.assertEqual(self.t(tid)["status"], "finished")
        self.assertEqual(len(self.finished_ranking(tid)), 3)

    def test_correcting_a_result_moves_who_passed_through(self):
        tid = self.tournament(6, ko_arrange="on")
        store.start(self.db, self.t(tid), self.rng)
        p = [e for e in self.slots(tid) if e]
        store.arrange_bracket(self.db, self.t(tid), [p[0], p[1], None, None, p[2], p[3], p[4], p[5]], confirm=True)
        stage = self.stage(tid)
        first = self.db.execute("SELECT * FROM tournament_matches WHERE tie_id = ?",
                                (self.tie(stage["id"], 1, 0)["id"],)).fetchone()
        store.record_result(self.db, self.t(tid), first["id"], 2, 0)
        self.assertEqual(self.tie(stage["id"], 3, 0)["entry_a"], first["home_entry_id"])
        store.record_result(self.db, self.t(tid), first["id"], 0, 2)  # corrigido: o visitante venceu
        self.assertEqual(self.tie(stage["id"], 2, 0)["winner_entry_id"], first["away_entry_id"])
        self.assertEqual(self.tie(stage["id"], 3, 0)["entry_a"], first["away_entry_id"])
        store.clear_result(self.db, self.t(tid), first["id"])
        self.assertIsNone(self.tie(stage["id"], 2, 0)["winner_entry_id"])
        self.assertIsNone(self.tie(stage["id"], 3, 0)["entry_a"])

    def test_invalid_arrangements(self):
        tid = self.tournament(5, ko_arrange="on")
        store.start(self.db, self.t(tid), self.rng)
        slots = self.slots(tid)
        with self.assertRaisesRegex(TournamentError, "mudou enquanto"):
            store.arrange_bracket(self.db, self.t(tid), slots[:-1])
        outsider = list(slots)
        outsider[outsider.index(next(e for e in slots if e))] = 999999
        with self.assertRaisesRegex(TournamentError, "exatamente os jogadores"):
            store.arrange_bracket(self.db, self.t(tid), outsider)
        # A chave é a menor potência de 2 que cabe todos: uma metade nunca comporta todos os jogadores,
        # então a final sempre acontece (deixar de fora alguém para esvaziar uma metade não pode).
        p = [e for e in slots if e]
        with self.assertRaisesRegex(TournamentError, "exatamente os jogadores"):
            store.arrange_bracket(self.db, self.t(tid), p[:4] + [None] * 4)
        self.assertEqual(self.slots(tid), slots)
        # Confronto vazio pode.
        empty_pair = [None, None, p[0], p[1], p[2], None, p[3], p[4]]
        store.arrange_bracket(self.db, self.t(tid), empty_pair)
        self.assertEqual(self.slots(tid), empty_pair)

    def test_no_draft_without_the_flag_or_with_two_players(self):
        tid = self.tournament(4)
        store.start(self.db, self.t(tid), self.rng)
        self.assertEqual(self.stage(tid)["arranging"], 0)
        self.assertEqual(len(self.matches(self.stage(tid)["id"])), 2)
        tid = self.tournament(2, ko_arrange="on")
        store.start(self.db, self.t(tid), self.rng)
        self.assertEqual(self.stage(tid)["arranging"], 0)  # só a final: nada para arrumar
        self.assertEqual(len(self.matches(self.stage(tid)["id"])), 1)
        with self.assertRaisesRegex(TournamentError, "já foi confirmado"):
            store.arrange_bracket(self.db, self.t(tid), None, confirm=True)

    def test_knockout_after_groups_is_a_draft(self):
        tid = self.tournament(8, fmt="world_cup", groups=2, qualifiers=2, ko_arrange="on")
        store.start(self.db, self.t(tid), self.rng)
        self.assertEqual(self.stage(tid)["arranging"], 0)  # a fase de grupos não
        for match in self.db.execute("SELECT id FROM tournament_matches").fetchall():
            store.record_result(self.db, self.t(tid), match["id"], self.rng.randint(0, 3), self.rng.randint(0, 3))
        store.advance(self.db, self.t(tid))
        stage = self.stage(tid)
        self.assertEqual((stage["kind"], stage["arranging"]), ("knockout", 1))
        self.assertEqual(self.matches(stage["id"]), [])
        slots = self.slots(tid)
        store.arrange_bracket(self.db, self.t(tid), list(reversed(slots)), confirm=True)
        self.assertEqual(self.slots(tid), list(reversed(slots)))
        self.assertEqual(len(self.matches(stage["id"])), 2)  # as semifinais

    def test_withdrawal_during_the_draft_becomes_walkover_on_confirm(self):
        tid = self.tournament(4, ko_arrange="on")
        store.start(self.db, self.t(tid), self.rng)
        quitter = next(e for e in store.entries_of(self.db, tid) if e["player_id"] == self.players[1])
        store.withdraw_running(self.db, self.t(tid), quitter, "Não vou poder jogar mais.")
        store.arrange_bracket(self.db, self.t(tid), None, confirm=True)
        match = self.db.execute("SELECT * FROM tournament_matches WHERE ? IN (home_entry_id, away_entry_id)",
                                (quitter["id"],)).fetchone()
        self.assertEqual((match["status"], match["walkover_entry_id"]), ("confirmed", quitter["id"]))


class KnockoutArrangeWebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
            db = get_db()
            self.players = [add_player(db, f"jog{i}") for i in range(5)]
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

    def test_create_start_arrange_and_confirm(self):
        owner = self.client(self.players[0])
        self.assertIn('name="ko_arrange"', owner.get("/torneios/novo").get_data(as_text=True))
        response = owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa Mata", "starts_on": "2026-12-01", "rules": "Regras",
            "game_title_id": self.game, "format": "knockout", "max_players": "8", "entry_fee": "0,00",
            "ko_legs": "1", "final_legs": "1", "ko_arrange": "on", "team_mode": "free"})
        self.assertEqual(response.status_code, 302)
        slug, settings, tid = self.query("SELECT slug, settings_json, id FROM tournaments")[0]
        self.assertTrue(json.loads(settings)["ko_arrange"])
        for player in self.players[:5]:
            self.client(player).post(f"/torneios/{slug}/inscrever",
                                     data={"csrf_token": CSRF, "team_name": f"Time {player}"})

        with mock.patch("app.tournaments.notify.tournament_started") as started:
            response = owner.post(f"/torneios/{slug}/iniciar", data={"csrf_token": CSRF})
            self.assertTrue(response.headers["Location"].endswith(f"/torneios/{slug}/gerenciar#chaveamento"))
            started.assert_not_called()  # os e-mails dos jogos esperam a confirmação

        manage = owner.get(f"/torneios/{slug}/gerenciar").get_data(as_text=True)
        self.assertIn('id="bracket-board"', manage)
        self.assertIn("js/bracket-board.js", manage)
        self.assertEqual(manage.count('class="group-chip bracket-chip"'), 5)
        self.assertEqual(manage.count('class="group-chip bracket-chip bye-chip"'), 3)
        self.assertNotIn("Resultados: Mata-mata", manage)
        show = owner.get(f"/torneios/{slug}?aba=tabela").get_data(as_text=True)
        self.assertIn("O organizador está definindo o chaveamento", show)

        with self.app.app_context():
            stage = store.stages_of(get_db(), tid)[-1]
            slots = [e for pair in store.bracket_pairs(get_db(), stage["id"]) for e in pair]
        form = {f"posicao_{i}": str(e or "") for i, e in enumerate(slots)}
        # Só o organizador.
        forbidden = self.client(self.players[1]).post(f"/torneios/{slug}/chaveamento", data={"csrf_token": CSRF, **form})
        self.assertEqual(forbidden.status_code, 403)
        # Troca as duas primeiras posições com jogador e salva.
        filled = [i for i, e in enumerate(slots) if e]
        swapped = dict(form, **{f"posicao_{filled[0]}": form[f"posicao_{filled[1]}"],
                                f"posicao_{filled[1]}": form[f"posicao_{filled[0]}"]})
        saved = owner.post(f"/torneios/{slug}/chaveamento", data={"csrf_token": CSRF, "acao": "salvar", **swapped},
                           follow_redirects=True)
        self.assertIn("Chaveamento salvo.", saved.get_data(as_text=True))
        with self.app.app_context():
            now_slots = [e for pair in store.bracket_pairs(get_db(), stage["id"]) for e in pair]
        self.assertEqual((now_slots[filled[0]], now_slots[filled[1]]), (slots[filled[1]], slots[filled[0]]))
        self.assertEqual(self.query("SELECT COUNT(*) FROM tournament_matches")[0][0], 0)

        with mock.patch("app.tournaments.notify.tournament_started") as started:
            confirmed = owner.post(f"/torneios/{slug}/chaveamento", data={"csrf_token": CSRF, "acao": "confirmar",
                                                                         **swapped})
            self.assertTrue(confirmed.headers["Location"].endswith(f"/torneios/{slug}?aba=tabela"))
            started.assert_called_once()
        self.assertEqual(self.query("SELECT arranging FROM tournament_stages")[0][0], 0)
        self.assertGreater(self.query("SELECT COUNT(*) FROM tournament_matches")[0][0], 0)
        manage = owner.get(f"/torneios/{slug}/gerenciar").get_data(as_text=True)
        self.assertNotIn('id="bracket-board"', manage)
        again = owner.post(f"/torneios/{slug}/chaveamento", data={"csrf_token": CSRF, "acao": "salvar", **form},
                           follow_redirects=True)
        self.assertIn("já foi confirmado", again.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
