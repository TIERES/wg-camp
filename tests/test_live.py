import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import get_db, init_db, now
from tests.test_tournament_store import add_player, make_app

KEY = "chave-do-dll"
CSRF = "test-csrf"


def state(kickoff=1000, frame=20000, period=0, clock=9000, goals=(1, 0), final=False, pads=(0, 1),
          names=("GRECIA", "CROACIA"), slots=(16, 18), scorers=None):
    scorers = scorers or ([["S.Vryzas", 17, "Karagounis"]] * goals[0], [["J.Leko", 20, ""]] * goals[1])
    return json.dumps({
        "v": 1, "content": "BA9BDE21:1C473F60", "kickoff": kickoff, "frame": frame, "period": period,
        "clock": clock, "half": 18000, "final": int(final), "pads": list(pads),
        "teams": [{"id": slots[i], "name": names[i], "goals": goals[i], "scorers": scorers[i]} for i in (0, 1)],
    })


class LiveMatchesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.app.config["SPECTATE_API_KEY"] = KEY
        with self.app.app_context():
            init_db()
            db = get_db()
            self.tieres, self.anders = add_player(db, "TIERES"), add_player(db, "ANDERSHOW")
            db.execute("INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('adm', 'x', ?, ?)",
                       (now(), now()))
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def post(self, st, reporter="TIERES", players="TIERES,ANDERSHOW", test=False, key=KEY):
        return self.app.test_client().post("/api/live/match", headers={"X-Api-Key": key}, data={
            "state": st, "players": players, "reporter": reporter, "room": "PCSX ReARMed: Euro vs Copa America 2004.bin",
            "test": "1" if test else "0"})

    def rows(self, sql, *args):
        with self.app.app_context():
            return get_db().execute(sql, args).fetchall()

    def admin(self):
        client = self.app.test_client()
        with client.session_transaction() as s:
            s["user_id"] = 1
            s["csrf_token"] = CSRF
        return client

    def cards(self, client=None, **params):
        return (client or self.app.test_client()).get("/ao-vivo/cards", query_string=params).get_data(as_text=True)

    def test_auth_and_bad_state(self):
        self.assertEqual(self.post(state(), key="errada").status_code, 401)
        self.assertEqual(self.post("{nada").status_code, 400)
        self.assertEqual(self.post(json.dumps({"content": "x", "teams": []})).status_code, 400)

    def test_two_pcs_one_match_and_card(self):
        self.assertIn("ok=1", self.post(state()).get_data(as_text=True))
        self.post(state(frame=20060), reporter="ANDERSHOW")
        match = self.rows("SELECT * FROM live_matches")
        self.assertEqual(len(match), 1)
        self.assertEqual((match[0]["reporters"], match[0]["local_goals"], match[0]["status"]), (2, 1, "live"))
        html = self.cards()
        # Sem campeonato nem cadastro, o nome da tabela da ISO não vale sozinho.
        for text in ("Não identificado", "TIERES", "ANDERSHOW", "1º tempo · 22&#39;", "S.Vryzas 17'",
                     "assist. Karagounis", "Partida livre", "Ao vivo", "Euro vs Copa America 2004.bin"):
            self.assertIn(text, html)
        self.assertNotIn("Grecia", html)
        self.assertRegex(html, r"\d\d/\d\d/\d{4} \d\d:\d\d · Euro vs Copa America")
        self.assertIn("TIERES", self.app.test_client().get("/ao-vivo").get_data(as_text=True))

    def test_admin_uses_the_game_names(self):
        self.post(state())
        self.post(state(frame=40000, period=1, names=("", "")))  # no 2º tempo a tabela some da RAM
        self.admin().post("/admin/ao-vivo/times", data={"csrf_token": CSRF, "content_id": "BA9BDE21:1C473F60",
                                                        "acao": "nomes_do_jogo"})
        html = self.cards()
        self.assertIn("Grecia", html)
        self.assertIn("Croacia", html)
        self.assertNotIn("Não identificado", html)

    def test_cpu_and_unknown_players(self):
        self.post(state(pads=(0, 255)))
        html = self.cards()
        self.assertIn("CPU", html)
        self.post(state(kickoff=7000, pads=(255, 255)))
        self.assertIn("Player 2", self.cards())

    def test_filters_and_limit(self):
        for n in range(12):
            self.post(state(kickoff=1000 + n, goals=(n % 3, 0), final=True), players="TIERES,P%d" % n)
        self.post(state(kickoff=99), players="Fulano,Beltrano")
        html = self.cards()
        self.assertEqual(html.count('<article class="live-card'), 10)
        self.assertEqual(self.cards(jogador="beltr").count('<article class="live-card'), 1)
        self.assertEqual(self.cards(jogador="P11").count('<article class="live-card'), 1)
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(self.cards(de="2000-01-01", ate="2000-01-02"), "")
        self.assertEqual(self.cards(de=today, ate=today, jogador="Fulano").count('<article class="live-card'), 1)
        page = self.app.test_client().get("/ao-vivo?jogador=Fulano").get_data(as_text=True)
        self.assertIn("Beltrano", page)
        self.assertIn("/ao-vivo/cards?jogador=Fulano", page)
        self.assertNotIn("P11", page)

    def test_final_agreement(self):
        self.post(state(period=1, clock=0, goals=(4, 4), final=True))
        self.post(state(period=1, clock=0, goals=(4, 4), final=True), reporter="ANDERSHOW")
        match = self.rows("SELECT * FROM live_matches")[0]
        self.assertEqual((match["status"], match["agreed"]), ("finished", 1))
        self.assertIn("Placar conferido nos 2 PCs", self.cards())
        self.assertIn("Encerrado", self.cards())
        # Partida seguinte da sessão: outro pontapé inicial, outra partida.
        self.post(state(kickoff=64201, goals=(0, 0)))
        self.assertEqual(len(self.rows("SELECT * FROM live_matches")), 2)

    def test_disagreeing_finals_are_not_agreed(self):
        self.post(state(period=1, clock=0, goals=(2, 1), final=True))
        self.post(state(period=1, clock=0, goals=(2, 2), final=True), reporter="ANDERSHOW")
        self.assertEqual(self.rows("SELECT agreed FROM live_matches")[0]["agreed"], 0)

    def test_pc_joining_midway_joins_the_live_match(self):
        self.post(state())
        self.post(state(kickoff=0, frame=30000, goals=(2, 0)), reporter="ANDERSHOW")
        match = self.rows("SELECT * FROM live_matches")
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["local_goals"], 2)

    def make_tournament(self):
        with self.app.app_context():
            db = get_db()
            game = db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES ('Euro 2004', ?, ?)",
                              (now(), now())).lastrowid
            t = db.execute("INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                           "created_at, updated_at) VALUES ('euro-1', 'Euro WE Camp', ?, ?, 'knockout', 'running', 2, ?, ?)",
                           (game, self.tieres, now(), now())).lastrowid
            greece = db.execute("SELECT id FROM teams WHERE name = 'Grécia' LIMIT 1").fetchone()
            ea = db.execute("INSERT INTO tournament_entries (tournament_id, player_id, team_name, team_id, created_at) "
                            "VALUES (?, ?, 'Grécia', ?, ?)", (t, self.tieres, greece["id"] if greece else None, now())).lastrowid
            eb = db.execute("INSERT INTO tournament_entries (tournament_id, player_id, team_name, created_at) "
                            "VALUES (?, ?, 'Croácia', ?)", (t, self.anders, now())).lastrowid
            stage = db.execute("INSERT INTO tournament_stages (tournament_id, seq, kind, name, created_at) "
                               "VALUES (?, 1, 'knockout', 'Final', ?)", (t, now())).lastrowid
            tie = db.execute("INSERT INTO tournament_ties (stage_id, round, position, legs, entry_a, entry_b) "
                             "VALUES (?, 1, 1, 2, ?, ?)", (stage, eb, ea)).lastrowid
            legs = []
            for leg, (home, away) in enumerate(((eb, ea), (ea, eb)), 1):
                legs.append(db.execute(
                    "INSERT INTO tournament_matches (stage_id, tie_id, round, leg, home_entry_id, away_entry_id, created_at, "
                    "updated_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?)", (stage, tie, leg, home, away, now(), now())).lastrowid)
            db.commit()
            return legs

    def test_tournament_ida_then_volta(self):
        ida, volta = self.make_tournament()
        # CAN-like: a tabela da ISO diz FINLAND/CROACIA, mas a inscrição manda.
        self.post(state(goals=(4, 4), names=("FINLAND", "CROACIA")))
        first = self.rows("SELECT * FROM live_matches")[0]
        self.assertEqual(first["tournament_match_id"], ida)
        html = self.cards()
        self.assertIn("Euro WE Camp · Final · Ida", html)
        self.assertIn("Grécia", html)
        self.assertNotIn("Finland", html)
        learned = self.rows("SELECT * FROM iso_team_slots WHERE slot = 16")[0]
        self.assertEqual((learned["name"], learned["source"], learned["ram_name"]), ("Grécia", "aprendido", "FINLAND"))
        self.post(state(period=1, clock=0, goals=(4, 4), final=True))
        self.post(state(kickoff=64201, goals=(0, 1)))
        second = self.rows("SELECT * FROM live_matches ORDER BY id")[1]
        self.assertEqual(second["tournament_match_id"], volta)
        self.assertIn("Euro WE Camp · Final · Volta", self.cards())
        tournament_id = self.rows("SELECT id FROM tournaments")[0]["id"]
        self.assertIn("Euro WE Camp", self.cards(campeonato=tournament_id))
        self.assertIn("Euro 2004", self.cards())  # jogo base do campeonato
        profile = self.rows("SELECT * FROM iso_profiles")[0]
        self.assertEqual((profile["tournament_id"], profile["source"]), (tournament_id, "aprendido"))
        # Partida livre depois (amistoso/ISO): o slot aprendido dá o nome, a ISO dá o jogo e o campeonato.
        self.post(state(kickoff=99000, goals=(0, 0)), players="TIERES,outro")
        html = self.cards(jogador="outro")
        self.assertIn("Grécia", html)
        self.assertIn("Partida livre", html)
        self.assertIn("Euro 2004", html)
        self.assertIn("Euro WE Camp", html)
        self.assertEqual(self.cards(campeonato=tournament_id).count('<article class="live-card'), 3)
        self.assertEqual(self.cards(campeonato=tournament_id, recentes="1").count('<article class="live-card'), 3)

    def test_profile_from_iso_file_name(self):
        with self.app.app_context():
            db = get_db()
            game = db.execute("SELECT id FROM game_titles WHERE code = 'we2002'").fetchone()["id"]
            db.execute("INSERT INTO catalog_isos (name, game_title_id, original_filename, is_active, created_at, "
                       "updated_at) VALUES ('Euro', ?, 'Euro vs Copa America 2004.bin', 1, ?, ?)", (game, now(), now()))
            db.commit()
        self.post(state())
        html = self.cards()
        self.assertIn("Winning Eleven 2002", html)
        self.assertIn("images/games/", html)

    def test_simulation_does_not_take_the_real_leg(self):
        ida, volta = self.make_tournament()
        self.post(state(), test=True)
        self.assertEqual(self.rows("SELECT tournament_match_id FROM live_matches")[0]["tournament_match_id"], ida)
        self.assertEqual(self.rows("SELECT source FROM iso_team_slots WHERE slot = 16")[0]["source"], "")
        self.post(state(kickoff=5000))
        real = self.rows("SELECT * FROM live_matches WHERE is_test = 0")[0]
        self.assertEqual(real["tournament_match_id"], ida)

    def test_friendly_context(self):
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO friendly_matches (home_id, away_id, status, created_at, updated_at) "
                       "VALUES (?, ?, 'accepted', ?, ?)", (self.anders, self.tieres, now(), now()))
            db.commit()
        self.post(state())
        self.assertIsNotNone(self.rows("SELECT friendly_id FROM live_matches")[0]["friendly_id"])
        self.assertIn("Amistoso", self.cards())

    def test_test_matches_only_for_admin(self):
        self.post(state(), test=True)
        self.assertNotIn("S.Vryzas", self.cards())
        admin = self.admin()
        self.assertIn("S.Vryzas", self.cards(admin))
        self.assertIn("Teste", self.cards(admin))
        admin.post("/ao-vivo/limpar-testes", data={"csrf_token": CSRF})
        self.assertEqual(len(self.rows("SELECT * FROM live_matches")), 0)

    def test_stale_then_reaped(self):
        self.post(state(period=1, clock=0))
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        with self.app.app_context():
            get_db().execute("UPDATE live_matches SET updated_at = ?", (old,))
            get_db().commit()
        self.assertIn("Sem sinal", self.cards())
        older = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
        with self.app.app_context():
            get_db().execute("UPDATE live_matches SET updated_at = ?", (older,))
            get_db().commit()
        self.cards()
        self.assertEqual(self.rows("SELECT status FROM live_matches")[0]["status"], "finished")
        self.assertEqual(self.cards(so_ao_vivo="1"), "")

    def test_admin_names_a_slot(self):
        self.post(state(names=("FINLAND", "CROACIA")))
        admin = self.admin()
        self.assertIn("FINLAND", admin.get("/admin/ao-vivo/times").get_data(as_text=True))
        admin.post("/admin/ao-vivo/times", data={"csrf_token": CSRF, "content_id": "BA9BDE21:1C473F60", "slot": "16",
                                                 "name": "Camarões"})
        row = self.rows("SELECT * FROM iso_team_slots WHERE slot = 16")[0]
        self.assertEqual(row["source"], "admin")
        self.assertIn("Camarões", self.cards())


if __name__ == "__main__":
    unittest.main()
