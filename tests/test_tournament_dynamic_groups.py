"""Grupos dinâmicos: o organizador nomeia os grupos, cada jogador escolhe o seu na
inscrição (até vagas / grupos por grupo), troca enquanto houver vaga, e o
organizador reorganiza até o início; depois, mata-mata como na Copa."""
import json
import random
import tempfile
import unittest
from pathlib import Path

from app.db import get_db, init_db, now
from app.tournament import formats, store
from app.tournament.store import TournamentError
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"


class DynamicGroupsStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.ctx = self.app.app_context()
        self.ctx.push()
        init_db()
        self.db = get_db()
        self.game = self.db.execute("SELECT id FROM game_titles ORDER BY id LIMIT 1").fetchone()[0]
        self.players = [add_player(self.db, f"p{i}") for i in range(10)]
        self.rng = random.Random(3)

    def tearDown(self):
        self.db.close()
        self.ctx.pop()
        self.temp.cleanup()

    def tournament(self, max_players=8, names=("Norte", "Sul"), **config):
        cfg = formats.normalize("dynamic_groups", {"groups": str(len(names)), "group_names": list(names),
                                                   **{k: str(v) for k, v in config.items()}})
        tid = self.db.execute(
            "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
            "settings_json, created_at, updated_at) VALUES (?, 'Dinâmico', ?, ?, 'dynamic_groups', 'registration', "
            "?, ?, ?, ?)", (f"din-{self.rng.random()}", self.game, self.players[0], max_players, json.dumps(cfg),
                            now(), now())).lastrowid
        return tid

    def t(self, tid):
        return store.get_tournament(self.db, tid)

    def enroll(self, tid, player, slot):
        store.enroll(self.db, self.t(tid), player, f"Time {player}", group_slot=slot)

    def slots(self, tid):
        return {row["player_id"]: row["group_slot"] for row in store.entries_of(self.db, tid)}

    def test_config_names_and_errors(self):
        config = formats.normalize("dynamic_groups", {"groups": "3", "group_names": [" Norte ", "", "Sul"]})
        self.assertEqual(config["group_names"], ["Norte", "Grupo B", "Sul"])
        self.assertIsNone(formats.config_error("dynamic_groups", config, 9))
        self.assertIn("Norte, Grupo B, Sul", formats.describe("dynamic_groups", config))
        same = formats.normalize("dynamic_groups", {"groups": "2", "group_names": ["Norte", "norte"]})
        self.assertIn("diferentes", formats.config_error("dynamic_groups", same, 8))
        one = formats.normalize("dynamic_groups", {"groups": "1"})
        self.assertIn("de 2 a", formats.config_error("dynamic_groups", one, 8))
        self.assertIn("no máximo", formats.config_error("dynamic_groups", config, 5))  # 5 vagas, 3 grupos

    def test_enroll_needs_a_group_with_room(self):
        tid = self.tournament(max_players=4)  # 2 por grupo
        with self.assertRaisesRegex(TournamentError, "Escolha o seu grupo"):
            self.enroll(tid, self.players[1], None)
        with self.assertRaisesRegex(TournamentError, "não existe"):
            self.enroll(tid, self.players[1], 2)
        self.enroll(tid, self.players[1], 0)
        self.enroll(tid, self.players[2], 0)
        with self.assertRaisesRegex(TournamentError, "Norte está cheio"):
            self.enroll(tid, self.players[3], 0)
        self.enroll(tid, self.players[3], 1)
        self.assertEqual(self.slots(tid), {self.players[1]: 0, self.players[2]: 0, self.players[3]: 1})

    def test_player_switches_group_only_with_room(self):
        tid = self.tournament(max_players=4)
        self.enroll(tid, self.players[1], 0)
        self.enroll(tid, self.players[2], 0)
        self.enroll(tid, self.players[3], 1)
        entry = lambda player: next(e for e in store.entries_of(self.db, tid) if e["player_id"] == player)
        with self.assertRaisesRegex(TournamentError, "cheio"):
            store.choose_group(self.db, self.t(tid), entry(self.players[3]), 0)
        store.choose_group(self.db, self.t(tid), entry(self.players[1]), 1)
        store.choose_group(self.db, self.t(tid), entry(self.players[3]), 0)
        self.assertEqual(self.slots(tid), {self.players[1]: 1, self.players[2]: 0, self.players[3]: 0})

    def test_organizer_arranges_without_the_limit_and_warns(self):
        tid = self.tournament(max_players=4)
        for player, slot in ((self.players[0], 0), (self.players[1], 1), (self.players[2], 1)):
            self.enroll(tid, player, slot)
        ids = {e["player_id"]: e["id"] for e in store.entries_of(self.db, tid)}
        notices = store.arrange_groups(self.db, self.t(tid), {ids[self.players[1]]: 0, ids[self.players[0]]: 0},
                                       editor_id=self.players[0])
        self.assertEqual(self.slots(tid)[self.players[1]], 0)
        self.assertEqual(len(notices), 1)  # quem editou (e quem não mudou) não recebe aviso
        player, subject, text = notices[0]
        self.assertEqual(player, self.players[1])
        self.assertIn("grupo Norte", subject)
        self.assertIn("estava no grupo Sul", text)
        # Sem o limite de 2 por grupo; grupo que não existe, não.
        store.arrange_groups(self.db, self.t(tid), {ids[self.players[2]]: 0})
        self.assertEqual(sorted(self.slots(tid).values()), [0, 0, 0])
        with self.assertRaisesRegex(TournamentError, "Escolha um grupo"):
            store.arrange_groups(self.db, self.t(tid), {ids[self.players[2]]: 7})

    def test_start_uses_the_chosen_groups_then_knockout(self):
        tid = self.tournament(max_players=8)
        chosen = {player: index % 2 for index, player in enumerate(self.players[:8])}
        for player, slot in chosen.items():
            self.enroll(tid, player, slot)
        store.start(self.db, self.t(tid), self.rng)
        stage = store.stages_of(self.db, tid)[0]
        groups = self.db.execute("SELECT g.name, e.player_id FROM tournament_groups g JOIN tournament_group_members m "
                                 "ON m.group_id = g.id JOIN tournament_entries e ON e.id = m.entry_id "
                                 "WHERE g.stage_id = ? ORDER BY g.seq", (stage["id"],)).fetchall()
        by_name = {}
        for row in groups:
            by_name.setdefault(row["name"], set()).add(row["player_id"])
        self.assertEqual(by_name, {"Norte": {p for p, s in chosen.items() if s == 0},
                                   "Sul": {p for p, s in chosen.items() if s == 1}})
        self.assertTrue(store.has_next_stage(self.t(tid), stage))
        for match in self.db.execute("SELECT id FROM tournament_matches").fetchall():
            store.record_result(self.db, self.t(tid), match["id"], self.rng.randint(0, 3), self.rng.randint(0, 3))
        store.advance(self.db, self.t(tid))
        stages = store.stages_of(self.db, tid)
        self.assertEqual([s["kind"] for s in stages], ["groups", "knockout"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tournament_ties WHERE stage_id = ? AND round = 1",
                                         (stages[1]["id"],)).fetchone()[0], 2)  # 2 x 2 classificados: semifinais

    def test_start_waits_for_every_group(self):
        tid = self.tournament(max_players=8)
        for player, slot in zip(self.players[:4], (0, 0, 1, 1)):
            self.enroll(tid, player, slot)
        self.db.execute("UPDATE tournament_entries SET group_slot = NULL WHERE player_id = ?", (self.players[3],))
        with self.assertRaisesRegex(TournamentError, "Ainda sem grupo: p3"):
            store.start(self.db, self.t(tid), self.rng)
        self.db.execute("UPDATE tournament_entries SET group_slot = 0 WHERE player_id = ?", (self.players[3],))
        with self.assertRaisesRegex(TournamentError, r"pelo menos 2 jogadores: Sul \(1\)"):
            store.start(self.db, self.t(tid), self.rng)
        self.assertEqual(self.t(tid)["status"], "registration")

    def values(self, tid, fmt, names):
        t = self.t(tid)
        raw = {"groups": str(len(names)), "group_names": list(names)} if fmt == "dynamic_groups" else {}
        return {"name": t["name"], "game_id": t["game_title_id"], "format": fmt, "max_players": t["max_players"],
                "fee": 0, "split": t["prize_split"], "config": formats.normalize(fmt, raw), "starts_on": None,
                "ends_on": None, "rules": "", "team_mode": "free", "friends_only": 0, "team_ids": []}

    def test_editing_the_groups_warns_who_lost_the_group(self):
        tid = self.tournament(max_players=9, names=("Norte", "Sul", "Leste"))
        for player, slot in zip(self.players[1:4], (0, 1, 2)):
            self.enroll(tid, player, slot)
        notices = store.update_before_start(self.db, self.t(tid), self.values(tid, "dynamic_groups", ("A", "B")),
                                            self.players[0])
        self.assertEqual(self.slots(tid), {self.players[1]: 0, self.players[2]: 1, self.players[3]: None})
        self.assertEqual([(p, s.rsplit(": ", 1)[-1]) for p, s, _ in notices],
                         [(self.players[3], "escolha o seu grupo")])
        self.assertIn("O grupo Leste saiu", notices[0][2])
        self.assertEqual(store.group_names(self.t(tid)), ["A", "B"])
        # Deixou de ser grupos dinâmicos: os grupos somem, sem aviso.
        self.assertEqual(store.update_before_start(self.db, self.t(tid), self.values(tid, "world_cup", ())), [])
        self.assertEqual(set(self.slots(tid).values()), {None})
        # Voltou a ser: todos escolhem o grupo.
        notices = store.update_before_start(self.db, self.t(tid), self.values(tid, "dynamic_groups", ("A", "B")))
        self.assertEqual(len(notices), 3)
        self.assertIn("cada jogador agora escolhe o seu grupo", notices[0][2])


class DynamicGroupsWebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
            db = get_db()
            self.players = [add_player(db, f"jog{i}") for i in range(4)]
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

    def test_create_enroll_switch_and_arrange(self):
        owner = self.client(self.players[0])
        new_page = owner.get("/torneios/novo").get_data(as_text=True)
        self.assertIn('value="dynamic_groups"', new_page)
        self.assertIn('id="group-names"', new_page)
        response = owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa das Regiões", "starts_on": "2026-12-01", "rules": "Regras",
            "game_title_id": self.game, "format": "dynamic_groups", "max_players": "4", "entry_fee": "0,00",
            "groups": "2", "group_name": ["Norte", " Sul "], "qualifiers": "1", "group_legs": "1", "ko_legs": "1",
            "final_legs": "1", "team_mode": "free"})
        self.assertEqual(response.status_code, 302)
        slug, settings = self.query("SELECT slug, settings_json FROM tournaments")[0]
        self.assertEqual(json.loads(settings)["group_names"], ["Norte", "Sul"])

        page = self.client(self.players[1]).get(f"/torneios/{slug}").get_data(as_text=True)
        self.assertIn('name="group_slot" value="0"', page)
        self.assertIn("<strong>Sul</strong><small>0 de 2</small>", page)
        for player, slot in ((self.players[1], 0), (self.players[2], 0)):
            self.client(player).post(f"/torneios/{slug}/inscrever", data={
                "csrf_token": CSRF, "team_name": f"Time {player}", "group_slot": str(slot)})
        full = self.client(self.players[3]).post(f"/torneios/{slug}/inscrever", data={
            "csrf_token": CSRF, "team_name": "Time 3", "group_slot": "0"}, follow_redirects=True)
        self.assertIn("O grupo Norte está cheio", full.get_data(as_text=True))
        self.assertEqual(len(self.query("SELECT id FROM tournament_entries")), 2)

        # O inscrito troca de grupo pela página.
        mine = self.client(self.players[2]).get(f"/torneios/{slug}").get_data(as_text=True)
        self.assertIn("Seu grupo: <strong>Norte</strong>", mine)
        self.client(self.players[2]).post(f"/torneios/{slug}/meu-grupo", data={"csrf_token": CSRF, "group_slot": "1"})
        self.assertEqual(self.query("SELECT group_slot FROM tournament_entries e JOIN players p ON p.id = e.player_id "
                                    "WHERE p.username = 'jog2'")[0][0], 1)

        # O organizador reorganiza em Gerenciar; outro jogador não pode.
        manage = owner.get(f"/torneios/{slug}/gerenciar").get_data(as_text=True)
        self.assertIn('id="grupos"', manage)
        # Arrastar e soltar (static/js/group-board.js): um cartão por jogador com o grupo num campo oculto.
        self.assertIn('id="group-board"', manage)
        self.assertIn("js/group-board.js", manage)
        self.assertEqual(manage.count('class="group-chip"'), 2)
        self.assertNotIn('<select name="grupo_', manage)
        self.assertRegex(manage, r'<ul class="group-drop[^"]*" data-slot="1" data-name="Sul"')
        entry = self.query("SELECT e.id FROM tournament_entries e JOIN players p ON p.id = e.player_id "
                           "WHERE p.username = 'jog1'")[0][0]
        forbidden = self.client(self.players[1]).post(f"/torneios/{slug}/grupos",
                                                      data={"csrf_token": CSRF, f"grupo_{entry}": "1"})
        self.assertEqual(forbidden.status_code, 403)
        saved = owner.post(f"/torneios/{slug}/grupos", data={"csrf_token": CSRF, f"grupo_{entry}": "1"},
                           follow_redirects=True)
        self.assertIn("1 jogador(es) avisado(s)", saved.get_data(as_text=True))
        self.assertEqual(sorted(r[0] for r in self.query("SELECT group_slot FROM tournament_entries")), [1, 1])

        # Visão geral: os inscritos por grupo.
        overview = owner.get(f"/torneios/{slug}?aba=geral").get_data(as_text=True)
        self.assertIn('<h3 class="group-title">Sul <small class="muted">2 de 2</small></h3>', overview)
        self.assertIn("Ninguém neste grupo ainda.", overview)


if __name__ == "__main__":
    unittest.main()
