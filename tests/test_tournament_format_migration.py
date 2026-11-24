import json
import tempfile
import unittest
from pathlib import Path

from app.db import get_db, init_db, migrate_db, now
from tests.test_tournament_store import add_player, make_app

OLD_CHECK = "CHECK (format IN ('league', 'ucl', 'world_cup', 'libertadores', 'knockout'))"


class TournamentFormatMigrationTest(unittest.TestCase):
    """Banco de antes do "Pontos corridos + mata-mata": migrate_db refaz tournaments sem perder nada."""

    def test_old_check_is_rebuilt_keeping_rows_and_references(self):
        with tempfile.TemporaryDirectory() as temp:
            app = make_app(Path(temp))
            with app.app_context():
                init_db()
                db = get_db()
                # Volta a tabela para a definição antiga (sem league_knockout e sem all_played_at).
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name = 'tournaments'").fetchone()[0]
                old = (sql.replace("CREATE TABLE tournaments", "CREATE TABLE tournaments_old", 1)
                       .replace("    all_played_at TEXT,\n", ""))
                old = old[:old.index("format TEXT NOT NULL")] + "format TEXT NOT NULL " + OLD_CHECK + "," + \
                    old[old.index("\n", old.index("'knockout'))")):]
                db.commit()
                db.execute("PRAGMA foreign_keys = OFF")
                db.executescript(f"""{old};
                    DROP TABLE tournaments;
                    ALTER TABLE tournaments_old RENAME TO tournaments;""")
                db.execute("PRAGMA foreign_keys = ON")
                self.assertNotIn("league_knockout", db.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'tournaments'").fetchone()[0])
                player = add_player(db, "dono")
                game = db.execute("SELECT id FROM game_titles ORDER BY id LIMIT 1").fetchone()[0]
                tid = db.execute(
                    "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                    "created_at, updated_at) VALUES ('velho', 'Velho', ?, ?, 'league', 'registration', 4, ?, ?)",
                    (game, player, now(), now())).lastrowid
                db.execute("INSERT INTO tournament_entries (tournament_id, player_id, team_name, created_at) "
                           "VALUES (?, ?, 'Time', ?)", (tid, player, now()))
                db.commit()

                migrate_db()
                migrate_db()  # idempotente

                db = get_db()
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name = 'tournaments'").fetchone()[0]
                self.assertIn("league_knockout", sql)
                self.assertIn("dynamic_groups", sql)
                row = db.execute("SELECT * FROM tournaments WHERE id = ?", (tid,)).fetchone()
                self.assertEqual((row["slug"], row["format"], row["all_played_at"]), ("velho", "league", None))
                self.assertEqual(db.execute("SELECT tournament_id FROM tournament_entries").fetchone()[0], tid)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                db.execute("UPDATE tournaments SET format = 'league_knockout' WHERE id = ?", (tid,))
                self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name = 'tournaments_status_idx'")
                                     .fetchone())

    def test_libertadores_becomes_the_cup(self):
        """Copa do Mundo e Libertadores viraram a "Copa (grupos + mata-mata)": os
        Libertadores passam a world_cup com a configuração deles (e os grupos em ida
        e volta, que mantêm o mata-mata pela campanha)."""
        with tempfile.TemporaryDirectory() as temp:
            app = make_app(Path(temp))
            with app.app_context():
                init_db()
                db = get_db()
                player = add_player(db, "dono")
                game = db.execute("SELECT id FROM game_titles ORDER BY id LIMIT 1").fetchone()[0]
                ids = {}
                for status, settings in (("registration", '{"groups": 4, "qualifiers": 2, "ko_legs": 1}'),
                                         ("running", '{"groups": 2, "group_legs": 2, "qualifiers": 2, "ko_legs": 2, '
                                                     '"final_legs": 1, "third_place": false}')):
                    ids[status] = db.execute(
                        "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                        "settings_json, created_at, updated_at) VALUES (?, ?, ?, ?, 'libertadores', ?, 8, ?, ?, ?)",
                        (status, status, game, player, status, settings, now(), now())).lastrowid
                change = json.dumps({"kind": "full", "format": "libertadores", "config": {"groups": 2}})
                db.execute("INSERT INTO tournament_changes (tournament_id, values_json, submitted_at, created_at, "
                           "updated_at) VALUES (?, ?, ?, ?, ?)", (ids["registration"], change, now(), now(), now()))
                db.commit()

                migrate_db()
                migrate_db()  # idempotente

                db = get_db()
                rows = {row["status"]: row for row in db.execute("SELECT * FROM tournaments")}
                self.assertEqual({row["format"] for row in rows.values()}, {"world_cup"})
                self.assertEqual(json.loads(rows["registration"]["settings_json"]),
                                 {"groups": 4, "group_legs": 2, "qualifiers": 2, "ko_legs": 1, "final_legs": 1,
                                  "third_place": False})
                self.assertEqual(json.loads(rows["running"]["settings_json"]),
                                 {"groups": 2, "group_legs": 2, "qualifiers": 2, "ko_legs": 2, "final_legs": 1,
                                  "third_place": False})
                values = json.loads(db.execute("SELECT values_json FROM tournament_changes").fetchone()[0])
                self.assertEqual(values["format"], "world_cup")
                self.assertEqual((values["config"]["groups"], values["config"]["group_legs"]), (2, 2))


    def test_old_team_mode_check_gets_round(self):
        """Banco de antes do "um time diferente a cada rodada": o CHECK de team_mode ganha 'round'."""
        with tempfile.TemporaryDirectory() as temp:
            app = make_app(Path(temp))
            with app.app_context():
                init_db()
                db = get_db()
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name = 'tournaments'").fetchone()[0]
                self.assertIn("'draw', 'round'", sql)
                old = (sql.replace("CREATE TABLE tournaments", "CREATE TABLE tournaments_old", 1)
                       .replace("'draw', 'round'", "'draw'"))
                db.commit()
                db.execute("PRAGMA foreign_keys = OFF")
                db.executescript(f"""{old};
                    DROP TABLE tournaments;
                    ALTER TABLE tournaments_old RENAME TO tournaments;""")
                db.execute("PRAGMA foreign_keys = ON")
                player = add_player(db, "dono")
                game = db.execute("SELECT id FROM game_titles ORDER BY id LIMIT 1").fetchone()[0]
                insert = ("INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, "
                          "max_players, team_mode, created_at, updated_at) VALUES (?, 'T', ?, ?, 'league', "
                          "'registration', 4, ?, ?, ?)")
                with self.assertRaises(Exception):
                    db.execute(insert, ("antes", game, player, "round", now(), now()))
                tid = db.execute(insert, ("velho", game, player, "free", now(), now())).lastrowid
                db.execute("INSERT INTO tournament_entries (tournament_id, player_id, team_name, created_at) "
                           "VALUES (?, ?, 'Time', ?)", (tid, player, now()))
                db.commit()

                migrate_db()
                migrate_db()  # idempotente

                db = get_db()
                db.execute(insert, ("novo", game, player, "round", now(), now()))
                self.assertEqual(db.execute("SELECT team_name FROM tournament_entries WHERE tournament_id = ?",
                                            (tid,)).fetchone()[0], "Time")
                columns = {c["name"] for c in db.execute("PRAGMA table_info(tournament_matches)")}
                self.assertTrue({"home_team_name", "home_team_id", "away_team_name", "away_team_id"} <= columns)


if __name__ == "__main__":
    unittest.main()
