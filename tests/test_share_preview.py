"""Prévia padrão dos links do portal (public.site_share): título da aba, descrição
da página e o cartão da logo do WE Camp; favicon e logo no cabeçalho."""
import tempfile
import unittest
from pathlib import Path

from app.db import init_db
from app.public import SHARE_DESCRIPTION, SHARE_DESCRIPTIONS
from tests.test_tournament_store import make_app

CARD = '<meta property="og:image" content="http://localhost/static/images/logo/wecamp-share.jpg">'


class SharePreviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def test_register_page_preview(self):
        page = self.page("/conta/cadastro")
        self.assertIn("<title>Cadastro · WE Camp</title>", page)
        self.assertIn('<meta property="og:title" content="Cadastro · WE Camp">', page)
        self.assertIn('<meta property="og:site_name" content="WE Camp">', page)
        self.assertIn('<meta property="og:url" content="http://localhost/conta/cadastro">', page)
        self.assertIn(f'<meta property="og:description" content="{SHARE_DESCRIPTIONS["players.register"]}">', page)
        self.assertIn(CARD, page)
        self.assertIn('<meta property="og:image:width" content="1200">', page)
        self.assertIn('<meta name="twitter:card" content="summary_large_image">', page)

    def test_every_public_page_has_one_preview(self):
        for path in ("/", "/conta/entrar", "/torneios/", "/torneios/ranking", "/ao-vivo", "/replays/"):
            page = self.page(path)
            self.assertEqual(page.count('property="og:title"'), 1, path)
            self.assertIn(CARD, page, path)
        # Página sem descrição própria: a geral.
        self.assertIn(SHARE_DESCRIPTION, self.page("/conta/recuperar"))

    def test_logo_in_header_and_favicon(self):
        page = self.page("/")
        self.assertIn('href="/static/images/logo/wecamp-32.png"', page)
        self.assertIn('rel="apple-touch-icon" href="/static/images/logo/wecamp-180.png"', page)
        self.assertIn('<a class="brand" href="/"><img src="/static/images/logo/wecamp-96.png"', page)
        for name in ("wecamp-share.jpg", "wecamp-32.png", "wecamp-96.png", "wecamp-180.png", "wecamp-192.png"):
            response = self.client.get(f"/static/images/logo/{name}")
            self.assertEqual(response.status_code, 200, name)
            response.close()


if __name__ == "__main__":
    unittest.main()
