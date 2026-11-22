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
            self.assertTrue(logo or flag)  # seleção: só a bandeira
            if logo:
                self.assertTrue((STATIC / "teams" / logo[5:]).is_file(), logo)
            if flag:
                self.assertTrue((STATIC / "flags" / flag).is_file(), flag)
        # Rodar de novo não duplica nem sobrescreve o que o admin mudou.
        self.db.execute("UPDATE teams SET name = 'Mengão' WHERE id = ?", (self.team_ids("brasileirao-a")[0],))
        self.db.execute("DELETE FROM site_settings WHERE key = 'teams_seed_version'")
        seed_teams(self.db)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM teams").fetchone()[0], teams)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM teams WHERE name = 'Mengão'").fetchone()[0], 1)

    def test_seed_has_each_team_in_one_group_and_fixes_old_national_duplicates(self):
        # Nenhum time em dois grupos da base (seleção só em Seleções, não na liga do país).
        self.assertIsNone(self.db.execute("SELECT team_id FROM team_group_members GROUP BY team_id "
                                          "HAVING COUNT(*) > 1").fetchone())
        # Banco carregado com a base antiga: a Bolívia virou "clube" em inglês e estava também na liga.
        bolivia = self.db.execute("SELECT id FROM teams WHERE source_key = 'bolivia/bolivia-national-team'").fetchone()[0]
        self.db.execute("UPDATE teams SET name = 'Bolivia National Team', kind = 'club', flag = NULL, "
                        "logo = 'base:bolivia/bolivia-national-team.webp' WHERE id = ?", (bolivia,))
        self.db.execute("INSERT INTO team_group_members SELECT id, ? FROM team_groups WHERE slug = 'bolivia'", (bolivia,))
        tid = self.tournament("choose", [bolivia])
        self.db.execute("DELETE FROM site_settings WHERE key = 'teams_seed_version'")
        seed_teams(self.db)
        row = self.db.execute("SELECT * FROM teams WHERE id = ?", (bolivia,)).fetchone()
        self.assertEqual((row["name"], row["kind"], row["flag"], row["logo"]),
                         ("Bolívia", "national", "bo.svg", None))
        self.assertNotIn(bolivia, self.team_ids("bolivia"))
        self.assertIn(bolivia, self.team_ids("selecoes"))
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM tournament_teams WHERE tournament_id = ? AND team_id = ?",
                                             (tid, bolivia)).fetchone())

    def test_free_mode_requires_unique_name(self):
        t = store.get_tournament(self.db, self.tournament("free", []))
        with self.assertRaises(TournamentError):
            store.enroll(self.db, t, self.players[1], " ")
        store.enroll(self.db, t, self.players[1], "  Real   Suciedad ")
        self.assertEqual(self.db.execute("SELECT team_name FROM tournament_entries").fetchone()[0], "Real Suciedad")
        with self.assertRaises(TournamentError):
            store.enroll(self.db, t, self.players[2], "real suciedad")

    def test_free_mode_uses_existing_team_by_name(self):
        brasil = self.db.execute("SELECT * FROM teams WHERE kind = 'national' AND country = 'br'").fetchone()
        t = store.get_tournament(self.db, self.tournament("free", []))
        # Sem acento, minúsculas e espaços sobrando: acha o time da base.
        store.enroll(self.db, t, self.players[1], "  BRASIL ")
        entry = self.db.execute("SELECT * FROM tournament_entries WHERE player_id = ?", (self.players[1],)).fetchone()
        self.assertEqual((entry["team_id"], entry["team_name"]), (brasil["id"], "Brasil"))
        with self.assertRaises(TournamentError):
            store.enroll(self.db, t, self.players[2], "brasil")  # o mesmo time da base
        # Nome que não existe na base continua livre, sem time ligado.
        store.enroll(self.db, t, self.players[2], "Unidos da Várzea")
        entry = self.db.execute("SELECT * FROM tournament_entries WHERE player_id = ?", (self.players[2],)).fetchone()
        self.assertEqual((entry["team_id"], entry["team_name"]), (None, "Unidos da Várzea"))
        with self.assertRaises(TournamentError):
            store.enroll(self.db, t, self.players[3], "unidos da varzea")  # acento não faz outro time

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

    def test_loose_team(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            session["player_id"] = self.players[0]
        self.assertEqual(client.post("/torneios/times-avulsos", data={"csrf_token": CSRF, "name": " x "}).status_code, 400)
        created = client.post("/torneios/times-avulsos", data={"csrf_token": CSRF, "name": "  Real   Suciedad "}).get_json()
        self.assertEqual(created["name"], "Real Suciedad")
        self.assertFalse(created["existing"])
        self.assertTrue(created["image"].startswith("data:image/svg+xml"))  # escudo genérico
        self.assertEqual(self.team_ids("avulsos"), [created["id"]])
        # Mesmo nome (sem acento/maiúsculas) reaproveita; time da base também.
        again = client.post("/torneios/times-avulsos", data={"csrf_token": CSRF, "name": "real suciedád"}).get_json()
        self.assertEqual((again["id"], again["existing"]), (created["id"], True))
        flamengo = client.post("/torneios/times-avulsos", data={"csrf_token": CSRF, "name": "FLAMENGO"}).get_json()
        self.assertTrue(flamengo["existing"])
        self.assertEqual(self.team_ids("avulsos"), [created["id"]])
        # Aparece no Criar torneio e na administração.
        self.assertIn("Real Suciedad".encode(), client.get("/torneios/novo").data)
        admin = self.app.test_client()
        with admin.session_transaction() as session:
            session["user_id"] = 1
        self.assertIn("Times avulsos (1)".encode(), admin.get("/admin/torneios/times").data)
        # Sem login de jogador não cria.
        anon = self.app.test_client()
        with anon.session_transaction() as session:
            session["csrf_token"] = CSRF
        self.assertNotEqual(anon.post("/torneios/times-avulsos", data={"csrf_token": CSRF, "name": "Outro"}).status_code, 200)


    # --- Livre: um time diferente a cada rodada (team_mode 'round') ---

    def start_round(self, players=4):
        tid = self.tournament("round", [], players)
        for pid in self.players[1:1 + players]:
            store.enroll(self.db, store.get_tournament(self.db, tid), pid, "ignorado", 999)
        store.start(self.db, store.get_tournament(self.db, tid), random.Random(1))
        return store.get_tournament(self.db, tid)

    def test_round_mode_has_no_team_on_the_entry(self):
        tid = self.tournament("round", [])
        t = store.get_tournament(self.db, tid)
        store.enroll(self.db, t, self.players[1], "Brasil", 999)
        entry = self.db.execute("SELECT * FROM tournament_entries").fetchone()
        self.assertEqual((entry["team_name"], entry["team_id"]), ("", None))
        self.assertEqual(store.entries_without_team(self.db, t), [])  # ninguém precisa escolher
        with self.assertRaises(TournamentError):
            store.choose_team(self.db, t, entry, "Brasil")
        for pid in self.players[2:5]:
            store.enroll(self.db, t, pid)
        store.start(self.db, store.get_tournament(self.db, tid), random.Random(1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tournament_entries WHERE team_name != ''")
                         .fetchone()[0], 0)

    def test_round_mode_keeps_the_team_of_each_match(self):
        from app import tournaments
        from app.tournament import reports
        t = self.start_round()
        brasil = self.db.execute("SELECT * FROM teams WHERE kind = 'national' AND country = 'br'").fetchone()
        # O nome de um time da base (sem acento/maiúsculas) vira o time; outro nome fica como digitado.
        self.assertEqual(store.match_team(self.db, "  BRASIL "), ("Brasil", brasil["id"]))
        self.assertEqual(store.match_team(self.db, "Unidos da Várzea"), ("Unidos da Várzea", None))
        self.assertEqual(store.match_team(self.db, "   "), ("", None))
        m1, m2 = self.db.execute("SELECT m.* FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
                                 "WHERE s.tournament_id = ? ORDER BY m.id LIMIT 2", (t["id"],)).fetchall()
        store.set_match_teams(self.db, t, m1["id"], store.match_team(self.db, "brasil"),
                              store.match_team(self.db, "Unidos da Várzea"))
        store.set_match_teams(self.db, t, m2["id"], None, store.match_team(self.db, "Brasil"))
        m1 = self.db.execute("SELECT * FROM tournament_matches WHERE id = ?", (m1["id"],)).fetchone()
        self.assertEqual((m1["home_team_name"], m1["home_team_id"], m1["away_team_name"], m1["away_team_id"]),
                         ("Brasil", brasil["id"], "Unidos da Várzea", None))
        # O mesmo time em jogos diferentes (e para jogadores diferentes) vale.
        m2 = self.db.execute("SELECT * FROM tournament_matches WHERE id = ?", (m2["id"],)).fetchone()
        self.assertEqual((m2["home_team_name"], m2["away_team_name"]), ("", "Brasil"))
        with self.app.test_request_context():
            names = tournaments._names(self.db, t)
            home = self.db.execute("SELECT p.username FROM tournament_entries e JOIN players p ON p.id = e.player_id "
                                   "WHERE e.id = ?", (m1["home_entry_id"],)).fetchone()[0]
            # A inscrição é só o jogador; o lado do jogo tem o time daquele jogo, com o jogador embaixo.
            self.assertNotIn("crest", names[m1["home_entry_id"]])
            self.assertIn("Brasil<small>%s</small>" % home, tournaments.side_name(names, m1, "home"))
            self.assertIn("/static/flags/br.svg", tournaments.side_name(names, m1, "home"))
            self.assertIn("Unidos da Várzea", tournaments.side_name(names, m1, "away"))
            self.assertEqual(tournaments.side_name(names, m2, "home"), names[m2["home_entry_id"]])
        # Placar apagado/descartado: o jogo volta sem placar e sem os times.
        reports.resolve(self.db, t, m1["id"], (1, 0, None, None))
        reports.clear(self.db, t, m1["id"])
        m1 = self.db.execute("SELECT * FROM tournament_matches WHERE id = ?", (m1["id"],)).fetchone()
        self.assertEqual((m1["status"], m1["home_team_name"], m1["away_team_name"], m1["home_team_id"]),
                         ("scheduled", "", "", None))

    def test_match_teams_only_in_round_mode(self):
        tid = self.tournament("free", [])
        for pid, name in zip(self.players[1:5], ("A1", "B2", "C3", "D4")):
            store.enroll(self.db, store.get_tournament(self.db, tid), pid, name)
        t = store.get_tournament(self.db, tid)
        store.start(self.db, t, random.Random(1))
        match = self.db.execute("SELECT id FROM tournament_matches LIMIT 1").fetchone()[0]
        store.set_match_teams(self.db, t, match, ("Brasil", None), ("Brasil", None))
        self.assertEqual(self.db.execute("SELECT home_team_name FROM tournament_matches WHERE id = ?",
                                         (match,)).fetchone()[0], "")

    def test_switching_to_round_mode_drops_the_entry_teams(self):
        tid = self.tournament("free", [])
        t = store.get_tournament(self.db, tid)
        store.enroll(self.db, t, self.players[1], "Real Suciedad")
        store.enroll(self.db, t, self.players[2], "Unidos da Várzea")
        values = {"name": "T", "game_id": self.game, "format": "league", "max_players": 4, "fee": 0,
                  "split": t["prize_split"], "config": store.settings_of(t), "starts_on": None, "ends_on": None,
                  "rules": "", "team_mode": "round", "friends_only": 0, "team_ids": []}
        notices = store.update_before_start(self.db, t, values, editor_id=self.players[0])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tournament_entries WHERE team_name = '' "
                                         "AND team_id IS NULL").fetchone()[0], 2)
        self.assertEqual(len(notices), 2)
        self.assertIn("Real Suciedad", " ".join(n[2] for n in notices))
        # E de volta para o livre: cada um escolhe o nome do seu time de novo (avisado).
        t = store.get_tournament(self.db, tid)
        self.assertEqual(t["team_mode"], "round")
        notices = store.update_before_start(self.db, t, dict(values, team_mode="free"), editor_id=self.players[0])
        self.assertEqual(len(notices), 2)
        self.assertEqual(len(store.entries_without_team(self.db, store.get_tournament(self.db, tid))), 2)

    def test_web_round_mode(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            session["player_id"] = self.players[0]
        page = client.get("/torneios/novo").get_data(as_text=True)
        self.assertIn('<option value="round" >Livre: um time diferente a cada rodada</option>', page)
        self.assertIn('id="round-hint" hidden', page)
        data = {"csrf_token": CSRF, "name": "Copa Rodada", "starts_on": "2026-12-01", "rules": "Regras",
                "game_title_id": self.game, "format": "league", "max_players": "4", "entry_fee": "0",
                "prize_split": "60,30,10", "legs": "1", "team_mode": "round"}
        self.assertEqual(client.post("/torneios/novo", data=data).status_code, 302)  # sem lista de times
        tid, slug, mode = self.db.execute("SELECT id, slug, team_mode FROM tournaments").fetchone()
        self.assertEqual(mode, "round")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tournament_teams").fetchone()[0], 0)
        self.db.execute("UPDATE tournaments SET status = 'registration' WHERE id = ?", (tid,))
        self.db.commit()
        page = client.get(f"/torneios/{slug}?aba=geral").get_data(as_text=True)
        self.assertIn("Um time diferente a cada rodada", page)
        self.assertIn("a cada partida você escolhe o time no próprio jogo", page)
        self.assertNotIn('name="team_name"', page)
        for pid in self.players[0:4]:
            store.enroll(self.db, store.get_tournament(self.db, tid), pid)
        store.start(self.db, store.get_tournament(self.db, tid), random.Random(1))
        self.db.commit()
        match = self.db.execute("SELECT m.*, ph.username AS home, pa.username AS away FROM tournament_matches m "
                                "JOIN tournament_entries h ON h.id = m.home_entry_id JOIN players ph ON ph.id = h.player_id "
                                "JOIN tournament_entries a ON a.id = m.away_entry_id JOIN players pa ON pa.id = a.player_id "
                                "WHERE h.player_id = ? OR a.player_id = ? LIMIT 1",
                                (self.players[0], self.players[0])).fetchone()
        page = client.get(f"/torneios/{slug}/jogos/{match['id']}").get_data(as_text=True)
        self.assertIn('name="home_team"', page)
        self.assertIn("Time de %s" % match["home"], page)
        self.assertIn('<datalist id="round-team-list">', page)
        reply = client.post(f"/torneios/{slug}/jogos/{match['id']}/reportar", data={
            "csrf_token": CSRF, "home_goals": "2", "away_goals": "1", "home_team": "brasil", "away_team": " Várzea FC "})
        self.assertEqual(reply.status_code, 302)
        row = self.db.execute("SELECT * FROM tournament_matches WHERE id = ?", (match["id"],)).fetchone()
        self.assertEqual((row["status"], row["home_team_name"], row["away_team_name"]), ("reported", "Brasil", "Várzea FC"))
        games = client.get(f"/torneios/{slug}?aba=jogos").get_data(as_text=True)
        self.assertIn("Brasil<small>%s</small>" % match["home"], games)
        self.assertIn("Várzea FC<small>%s</small>" % match["away"], games)
        # A classificação é do jogador: sem o time na tabela.
        table = client.get(f"/torneios/{slug}?aba=tabela").get_data(as_text=True)
        standings = table[table.index('<table class="standings">'):]
        self.assertIn('<span class="entry-name">%s</span>' % match["home"], standings)
        self.assertNotIn("Brasil<small>", standings)
        self.assertIn("Brasil<small>%s</small>" % match["home"], table)  # nos seus jogos pendentes, o time do jogo
        # O adversário confirma em Meus jogos: o jogo aparece com os times dele.
        other = self.app.test_client()
        opponent = self.db.execute("SELECT id FROM players WHERE username = ?", (match["away"],)).fetchone()[0]
        with other.session_transaction() as session:
            session["player_id"] = opponent
        mine = other.get("/torneios/meus-jogos").get_data(as_text=True)
        self.assertIn("Várzea FC<small>%s</small>" % match["away"], mine)


if __name__ == "__main__":
    unittest.main()
