import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from app import create_app
from app.arena17_import import Arena17InfoParser, championship_id_from_url
from app.db import get_db, init_db, now


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test",
            "INSTANCE_PATH": str(root / "instance"),
            "DATABASE": str(root / "arena17.db"),
            "DOWNLOADS_DIR": str(root / "downloads"),
            "UPLOAD_TMP_DIR": str(root / "tmp"),
        })
        with self.app.app_context():
            init_db()
            db = get_db()
            db.execute("INSERT INTO championships (slug,name,league_name,description,is_published,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", ("copa", "Copa", "Liga Arena17", "Teste", 1, now(), now()))
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_public_page_lists_published_championships(self):
        response = self.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Copa", response.data)
        self.assertIn(b"Liga Arena17", response.data)
        self.assertIn(b'<table class="championship-table">', response.data)
        self.assertIn(b'aria-controls="championship-details-1"', response.data)
        self.assertIn(b'<td colspan="6">', response.data)
        self.assertIn(b'class="championship-data-row tone-light"', response.data)
        self.assertIn(b'class="expansion-indicator"', response.data)
        self.assertIn(b'tone-light', response.data)
        self.assertNotIn(b'Ver detalhes', response.data)

    def test_arena17_info_parser_extracts_settings_and_details(self):
        parser = Arena17InfoParser()
        parser.feed("""
            <h3>Copa de Teste</h3>
            <div id="formula-aba-info">
              <label>Liga</label><div class="card-text">Liga Arena</div>
              <label>Jogo</label><div class="card-text"><a>PES 2021</a></div>
              <h4>Configurações</h4>
              <label>Equipes</label><div class="card-text">16</div>
              <label>Fase de grupos</label><div class="card-text">Sim</div>
            </div>
        """)
        self.assertEqual(parser.title, "Copa de Teste")
        self.assertEqual(parser.info["Liga"], "Liga Arena")
        self.assertEqual(parser.info["Jogo"], "PES 2021")
        self.assertEqual(parser.settings["Equipes"], "16")
        self.assertEqual(parser.settings["Fase de grupos"], "Sim")

    def test_arena17_url_only_accepts_championship_pages(self):
        self.assertEqual(championship_id_from_url("https://www.arena17.com/campeonato/12345"), "12345")
        with self.assertRaises(ValueError):
            championship_id_from_url("https://example.com/campeonato/12345")

    def test_upload_redirects_to_dashboard_and_rejects_a_second_file(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["csrf_token"] = "test-csrf-token"

        first = client.post("/admin/championships/1/files", data={
            "csrf_token": "test-csrf-token",
            "file": (BytesIO(b"first file"), "campeonato.chd"),
            "file_type": "other",
            "is_published": "on",
        })
        self.assertEqual(first.status_code, 302)
        self.assertTrue(first.headers["Location"].endswith("/admin/"))

        second = client.post("/admin/championships/1/files", data={
            "csrf_token": "test-csrf-token",
            "file": (BytesIO(b"second file"), "outro.iso"),
            "file_type": "iso",
            "is_published": "on",
        })
        self.assertEqual(second.status_code, 302)
        self.assertTrue(second.headers["Location"].endswith("/admin/championships/1/files"))
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM files WHERE championship_id=1").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
