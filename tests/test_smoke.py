import tempfile
import unittest
from pathlib import Path

from arena17 import create_app
from arena17.db import get_db, init_db, now


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


if __name__ == "__main__":
    unittest.main()
