"""Confronto na fase de grupos (Copa e Grupos dinâmicos): entre o grupo
(padrão), entre outros grupos ou entre todos os grupos. A classificação é
sempre por grupo e conta os jogos contra os outros grupos."""
import json
import random
import tempfile
import unittest
from pathlib import Path

from app.db import get_db, init_db, now
from app.tournament import engine, formats, store
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"


class MatchupEngineTest(unittest.TestCase):
    def rounds(self, fixtures):
        out = {}
        for rnd, home, away in fixtures:
            out.setdefault(rnd, []).append((home, away))
        return out

    def test_cross_group_pairs_never_inside_a_group(self):
        groups = [[1, 2, 3], [4, 5], [6]]
        pairs = engine.cross_group_pairs(groups)
        group_of = {e: i for i, g in enumerate(groups) for e in g}
        self.assertEqual(len(pairs), 3 * 2 + 3 * 1 + 2 * 1)
        self.assertTrue(all(group_of[a] != group_of[b] for a, b in pairs))

    def test_schedule_rounds_and_home_balance(self):
        fixtures = engine.schedule(engine.cross_group_pairs([[1, 2, 3, 4], [5, 6, 7, 8]]))
        rounds = self.rounds(fixtures)
        self.assertEqual(len(rounds), 4)  # 2 grupos de 4: o mínimo possível
        for matches in rounds.values():
            players = [p for match in matches for p in match]
            self.assertEqual(len(players), len(set(players)))  # ninguém joga 2 vezes na rodada
        homes = {}
        for _, home, _ in fixtures:
            homes[home] = homes.get(home, 0) + 1
        self.assertEqual(sorted(homes.values()), [2] * 8)
        # O mínimo possível também quando a primeira tentativa sobra (2 grupos de 3: 3 rodadas; 3 de 3: 7).
        self.assertEqual(len(self.rounds(engine.schedule(engine.cross_group_pairs([[1, 2, 3], [4, 5, 6]])))), 3)
        nine = engine.cross_group_pairs([[1, 2, 3], [4, 5, 6], [7, 8, 9]])
        self.assertEqual(len(self.rounds(engine.schedule(nine))), 7)
        self.assertEqual(engine.schedule(nine), engine.schedule(nine))  # sempre o mesmo resultado
        both = engine.schedule(engine.cross_group_pairs([[1, 2], [3, 4]]), legs=2)
        self.assertEqual(len(both), 8)
        first = {(h, a) for r, h, a in both if r <= 2}
        self.assertEqual({(a, h) for r, h, a in both if r > 2}, first)  # returno com o mando invertido

    def test_partial_standings_count_games_against_outsiders(self):
        results = [engine.Result(1, 9, 2, 0), engine.Result(9, 2, 1, 1), engine.Result(1, 2, 0, 1)]
        table = {row["entry_id"]: row for row in engine.standings([1, 2], results, partial=True)}
        self.assertEqual((table[1]["played"], table[1]["points"]), (2, 3))
        self.assertEqual((table[2]["played"], table[2]["points"]), (2, 4))
        strict = {row["entry_id"]: row for row in engine.standings([1, 2], results)}
        self.assertEqual(strict[1]["played"], 1)  # sem partial, só os jogos entre os da tabela

    def test_formats_default_choice_and_text(self):
        for fmt in formats.GROUPED:
            self.assertEqual(formats.normalize(fmt, {})["group_matchups"], "within")
            self.assertEqual(formats.normalize(fmt, {"group_matchups": "x"})["group_matchups"], "within")
        config = formats.normalize("world_cup", {"groups": "1", "group_matchups": "others"})
        self.assertIn("pelo menos 2 grupos", formats.config_error("world_cup", config, 8))
        self.assertNotIn("contra", formats.describe("world_cup", formats.normalize("world_cup", {})))
        self.assertIn("contra os jogadores dos outros grupos",
                      formats.describe("world_cup", formats.normalize("world_cup", {"group_matchups": "others"})))
        self.assertIn("contra todos os inscritos",
                      formats.describe("dynamic_groups", formats.normalize("dynamic_groups", {"group_matchups": "all"})))


class MatchupStoreTest(unittest.TestCase):
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

    def start(self, fmt, players, slots=None, **config):
        cfg = formats.normalize(fmt, {k: (v if isinstance(v, list) else str(v)) for k, v in config.items()})
        tid = self.db.execute(
            "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
            "settings_json, created_at, updated_at) VALUES (?, 'T', ?, ?, ?, 'registration', ?, ?, ?, ?)",
            (f"t-{self.rng.random()}", self.game, self.players[0], fmt, players, json.dumps(cfg), now(), now())
        ).lastrowid
        t = store.get_tournament(self.db, tid)
        for index, player in enumerate(self.players[:players]):
            store.enroll(self.db, t, player, f"Time {player}", group_slot=slots[index] if slots else None)
        store.start(self.db, store.get_tournament(self.db, tid), self.rng)
        return tid

    def groups(self, tid):
        stage = store.stages_of(self.db, tid)[0]
        return [[row[0] for row in self.db.execute(
            "SELECT entry_id FROM tournament_group_members WHERE group_id = ?", (group["id"],))]
            for group in self.db.execute("SELECT id FROM tournament_groups WHERE stage_id = ? ORDER BY seq",
                                         (stage["id"],))]

    def matches(self, tid):
        return self.db.execute("SELECT m.* FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
                               "WHERE s.tournament_id = ?", (tid,)).fetchall()

    def play_all(self, tid):
        for match in self.matches(tid):
            store.record_result(self.db, store.get_tournament(self.db, tid), match["id"], self.rng.randint(0, 3),
                                self.rng.randint(0, 3))

    def test_within_is_still_the_default(self):
        tid = self.start("world_cup", 8, groups=2)
        matches = self.matches(tid)
        self.assertEqual(len(matches), 12)  # 2 grupos de 4: 6 jogos cada
        self.assertTrue(all(m["group_id"] for m in matches))

    def test_others_plays_only_the_other_groups_and_ranks_by_group(self):
        tid = self.start("world_cup", 8, groups=2, group_matchups="others")
        groups = self.groups(tid)
        group_of = {e: i for i, g in enumerate(groups) for e in g}
        matches = self.matches(tid)
        self.assertEqual(len(matches), 16)  # 4 x 4
        self.assertTrue(all(group_of[m["home_entry_id"]] != group_of[m["away_entry_id"]] for m in matches))
        self.assertTrue(all(m["group_id"] is None for m in matches))
        self.assertEqual(max(m["round"] for m in matches), 4)
        self.play_all(tid)
        stage = store.stages_of(self.db, tid)[0]
        tables = store.group_tables(self.db, stage["id"])
        self.assertEqual([len(table) for _, table in tables], [4, 4])
        self.assertTrue(all(row["played"] == 4 for _, table in tables for row in table))
        store.advance(self.db, store.get_tournament(self.db, tid))
        self.assertEqual([s["kind"] for s in store.stages_of(self.db, tid)], ["groups", "knockout"])

    def test_all_plays_everyone(self):
        tid = self.start("world_cup", 8, groups=2, group_matchups="all", group_legs=2)
        matches = self.matches(tid)
        self.assertEqual(len(matches), 56)  # 8 x 7, ida e volta
        self.play_all(tid)
        stage = store.stages_of(self.db, tid)[0]
        tables = store.group_tables(self.db, stage["id"])
        self.assertTrue(all(row["played"] == 14 for _, table in tables for row in table))

    def test_dynamic_groups_against_the_other_groups(self):
        tid = self.start("dynamic_groups", 6, slots=[0, 0, 1, 1, 2, 2], groups=3,
                         group_names=["Norte", "Sul", "Leste"], qualifiers=1, group_matchups="others")
        self.assertEqual(len(self.matches(tid)), 12)  # 3 grupos de 2: cada um enfrenta os 4 de fora
        names = [row[0] for row in self.db.execute("SELECT name FROM tournament_groups ORDER BY seq")]
        self.assertEqual(names, ["Norte", "Sul", "Leste"])
        per_player = {}
        for m in self.matches(tid):
            for entry in (m["home_entry_id"], m["away_entry_id"]):
                per_player[entry] = per_player.get(entry, 0) + 1
        self.assertEqual(set(per_player.values()), {4})


class MatchupWebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
            db = get_db()
            self.owner = add_player(db, "dono")
            self.game = db.execute("SELECT id FROM game_titles WHERE code = 'we2002'").fetchone()[0]
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_required_select_saved_and_described(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            session["player_id"] = self.owner
        page = client.get("/torneios/novo").get_data(as_text=True)
        self.assertIn('<select name="group_matchups" required>', page)
        self.assertIn('<option value="within">Confronto entre o grupo (padrão)</option>', page)
        self.assertIn('<option value="others">Confronto entre outros grupos</option>', page)
        self.assertIn('<option value="all">Confronto entre todos os grupos</option>', page)
        response = client.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa Cruzada", "starts_on": "2026-12-01", "rules": "Regras",
            "game_title_id": self.game, "format": "world_cup", "max_players": "8", "entry_fee": "0,00", "groups": "2",
            "qualifiers": "2", "group_legs": "1", "group_matchups": "others", "ko_legs": "1", "final_legs": "1",
            "team_mode": "free"})
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            slug, settings = get_db().execute("SELECT slug, settings_json FROM tournaments").fetchone()
        self.assertEqual(json.loads(settings)["group_matchups"], "others")
        show = client.get(f"/torneios/{slug}?aba=geral").get_data(as_text=True)
        self.assertIn("cada um contra os jogadores dos outros grupos", show)


if __name__ == "__main__":
    unittest.main()
