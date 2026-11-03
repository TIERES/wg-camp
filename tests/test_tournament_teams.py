import json
import random
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from app.db import get_db, init_db, now
from app.tournament import formats, store
from app.tournament.store import TournamentError
from app.tournament.teams import generated_badge, seed_teams
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"
STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


class TeamsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app = make_app(root)
        self.app.config["TEAM_LOGOS_DIR"] = str(root / "team-logos")
        (root / "team-logos").mkdir()
        self.ctx = self.app.app_context()
        self.ctx.push()
        init_db()
        self.db = get_db()
        self.game = self.db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES ('WE', ?, ?)",
                                    (now(), now())).lastrowid
        self.players = [add_player(self.db, f"p{i}") for i in range(6)]
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.ctx.pop()
        self.temp.cleanup()

    def team_ids(self, group_slug, n=None):
        rows = self.db.execute("SELECT m.team_id FROM team_group_members m JOIN team_groups g ON g.id = m.group_id "
                               "WHERE g.slug = ? ORDER BY m.team_id", (group_slug,)).fetchall()
        return [r[0] for r in rows][:n]

    def tournament(self, mode, teams, players=4):
        cfg = formats.normalize("league", {"legs": "1"})
        tid = self.db.execute(
            "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
            "settings_json, team_mode, created_at, updated_at) VALUES (?, 'T', ?, ?, 'league', 'registration', ?, ?, "
            "?, ?, ?)", (f"t{random.random()}", self.game, self.players[0], players, json.dumps(cfg), mode, now(),
                         now())).lastrowid
        self.db.executemany("INSERT INTO tournament_teams VALUES (?, ?)", [(tid, t) for t in teams])
        return tid

    def test_seed_base_is_complete_and_idempotent(self):
        teams = self.db.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
        self.assertGreaterEqual(teams, 580)
        self.assertEqual(len(self.team_ids("brasileirao-a")), 20)
        self.assertGreaterEqual(len(self.team_ids("selecoes")), 220)
        brasil = self.db.execute("SELECT * FROM teams WHERE kind = 'national' AND country = 'br'").fetchone()
        self.assertEqual(brasil["name"], "Brasil")
        self.assertEqual(brasil["flag"], "br.svg")
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM teams WHERE name = 'Portugal' AND kind = 'national'")
                             .fetchone())
        # Todo escudo e bandeira da base existe no disco.
        for logo, flag in self.db.execute("SELECT logo, flag FROM teams"):
            self.assertTrue((STATIC / "teams" / logo[5:]).is_file(), logo)
            if flag:
                self.assertTrue((STATIC / "flags" / flag).is_file(), flag)
        # Rodar de novo não duplica nem sobrescreve o que o admin mudou.
        self.db.execute("UPDATE teams SET name = 'Mengão' WHERE id = ?", (self.team_ids("brasileirao-a")[0],))
        self.db.execute("DELETE FROM site_settings WHERE key = 'teams_seed_version'")
        seed_teams(self.db)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM teams").fetchone()[0], teams)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM teams WHERE name = 'Mengão'").fetchone()[0], 1)

    def test_free_mode_requires_unique_name(self):
        t = store.get_tournament(self.db, self.tournament("free", []))
        with self.assertRaises(TournamentError):
            store.enroll(self.db, t, self.players[1], " ")
        store.enroll(self.db, t, self.players[1], "  Real   Suciedad ")
        self.assertEqual(self.db.execute("SELECT team_name FROM tournament_entries").fetchone()[0], "Real Suciedad")
        with self.assertRaises(TournamentError):
            store.enroll(self.db, t, self.players[2], "real suciedad")

    def test_choose_mode(self):
        teams = self.team_ids("brasileirao-a", 4)
        t = store.get_tournament(self.db, self.tournament("choose", teams))
        store.enroll(self.db, t, self.players[1], team_id=teams[0])
        with self.assertRaises(TournamentError):
            store.enroll(self.db, t, self.players[2], team_id=teams[0])  # já escolhido
        with self.assertRaises(TournamentError):
            store.enroll(self.db, t, self.players[2], team_id=self.team_ids("la-liga", 1)[0])  # fora da lista
        self.assertEqual(len(store.available_teams(self.db, t["id"])), 3)
        entry = self.db.execute("SELECT * FROM tournament_entries").fetchone()
        self.assertEqual(entry["team_id"], teams[0])
        store.withdraw(self.db, t, entry)
        self.assertEqual(len(store.available_teams(self.db, t["id"])), 4)

    def test_draw_mode_assigns_distinct_teams(self):
        teams = self.team_ids("selecoes", 6)
        tid = self.tournament("draw", teams)
        for pid in self.players[1:5]:
            store.enroll(self.db, store.get_tournament(self.db, tid), pid, "ignorado", 999)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tournament_entries WHERE team_id IS NULL")
                         .fetchone()[0], 4)
        store.start(self.db, store.get_tournament(self.db, tid), random.Random(1))
        rows = self.db.execute("SELECT team_id, team_name FROM tournament_entries").fetchall()
        self.assertEqual(len({r[0] for r in rows}), 4)
        self.assertTrue(all(r[0] in teams and r[1] for r in rows))

    def test_generated_badge(self):
        self.assertTrue(generated_badge("Real Suciedad").startswith("data:image/svg+xml;base64,"))
        self.assertEqual(generated_badge("A"), generated_badge("A"))

    def test_web_create_choose_and_admin_upload(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            session["player_id"] = self.players[0]
        page = client.get("/torneios/novo")
        self.assertIn("Brasileirão Série A".encode(), page.data)
        teams = self.team_ids("brasileirao-a")
        data = {"csrf_token": CSRF, "name": "Copa BR", "starts_on": "2026-12-01", "rules": "Regras", "game_title_id": self.game, "format": "league",
                "max_players": "4", "entry_fee": "0", "prize_split": "60,30,10", "legs": "1",
                "team_mode": "choose", "team_ids": [str(t) for t in teams[:3]]}
        refused = client.post("/torneios/novo", data=data)
        self.assertIn("pelo menos 4 times".encode(), refused.data)
        data["team_ids"] = [str(t) for t in teams[:6]] + [str(teams[0])]
        self.assertEqual(client.post("/torneios/novo", data=data).status_code, 302)
        tid, slug = self.db.execute("SELECT id, slug FROM tournaments").fetchone()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tournament_teams WHERE tournament_id = ?",
                                         (tid,)).fetchone()[0], 6)
        client.post(f"/torneios/{slug}/inscrever", data={"csrf_token": CSRF, "team_id": str(teams[2])})
        page = client.get(f"/torneios/{slug}?aba=geral").data.decode()
        self.assertIn("/static/teams/", page)
        self.assertIn('class="team-option taken"', page)  # o time escolhido aparece com o dono

        admin = self.app.test_client()
        with admin.session_transaction() as session:
            session["csrf_token"] = CSRF
            session["user_id"] = 1
        self.assertEqual(admin.get("/admin/torneios/times?q=fla").status_code, 200)
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
        response = admin.post(f"/admin/torneios/times/{teams[2]}", data={
            "csrf_token": CSRF, "name": "Time Novo", "kind": "club", "is_active": "on",
            "logo": (BytesIO(png), "escudo.png")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        logo = self.db.execute("SELECT logo FROM teams WHERE id = ?", (teams[2],)).fetchone()[0]
        self.assertTrue(logo.startswith("upload:"))
        served = admin.get(f"/torneios/escudos/{logo[7:]}")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.headers["X-Content-Type-Options"], "nosniff")
        bad = admin.post(f"/admin/torneios/times/{teams[2]}", data={
            "csrf_token": CSRF, "name": "Time Novo", "kind": "club",
            "logo": (BytesIO(b"<svg onload=alert(1)>"), "x.svg")}, content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 302)
        self.assertEqual(self.db.execute("SELECT logo FROM teams WHERE id = ?", (teams[2],)).fetchone()[0], logo)


if __name__ == "__main__":
    unittest.main()
