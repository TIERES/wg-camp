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
                self.assertIn("league_knockout", db.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'tournaments'").fetchone()[0])
                row = db.execute("SELECT * FROM tournaments WHERE id = ?", (tid,)).fetchone()
                self.assertEqual((row["slug"], row["format"], row["all_played_at"]), ("velho", "league", None))
                self.assertEqual(db.execute("SELECT tournament_id FROM tournament_entries").fetchone()[0], tid)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                db.execute("UPDATE tournaments SET format = 'league_knockout' WHERE id = ?", (tid,))
                self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name = 'tournaments_status_idx'")
                                     .fetchone())


if __name__ == "__main__":
    unittest.main()
