"""Foto do perfil, imagem do torneio e o popup com o resumo do jogador (app/images.py)."""
import io
import tempfile
import unittest
from pathlib import Path

from app.db import get_db, init_db, now
from app.tournament import store
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class ImagesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        with self.app.app_context():
            init_db()
            db = get_db()
            self.players = [add_player(db, f"jog{i}") for i in range(3)]
            db.execute("INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('adm', 'x', ?, ?)",
                       (now(), now()))
            self.game = db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES ('WE', ?, ?)",
                                   (now(), now())).lastrowid
            db.execute("INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                       "settings_json, created_at, updated_at) VALUES ('copa', 'Copa', ?, ?, 'league', 'registration', "
                       "4, '{\"legs\": 1}', ?, ?)", (self.game, self.players[0], now(), now()))
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def client(self, player=None, admin=False):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            if player:
                session["player_id"] = player
            if admin:
                session["user_id"] = 1
        return client

    def one(self, sql, *args):
        with self.app.app_context():
            return get_db().execute(sql, args).fetchone()[0]

    def upload(self, client, url, field, data=PNG, **extra):
        return client.post(url, data={"csrf_token": CSRF, field: (io.BytesIO(data), "foto.png"), **extra},
                           content_type="multipart/form-data")

    def avatar_of(self, index):
        return self.one("SELECT avatar FROM players WHERE id = ?", self.players[index])

    def test_avatar_upload_shows_in_menu_and_replaces_file(self):
        c1 = self.client(self.players[1])
        self.assertEqual(self.upload(c1, "/conta/foto", "avatar").status_code, 302)
        first = self.avatar_of(1)
        self.assertTrue((self.root / "avatars" / first).exists())
        served = c1.get(f"/imagens/avatar/{first}")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.headers["X-Content-Type-Options"], "nosniff")
        served.close()
        self.assertIn(f'class="avatar avatar-xs" src="/imagens/avatar/{first}"', c1.get("/conta/").data.decode())

        # trocar apaga o arquivo antigo; remover apaga o atual
        self.upload(c1, "/conta/foto", "avatar")
        second = self.avatar_of(1)
        self.assertNotEqual(first, second)
        self.assertFalse((self.root / "avatars" / first).exists())
        page = c1.get("/conta/").data.decode()
        self.assertIn('name="acao" value="remover" formnovalidate>Remover foto', page)
        c1.post("/conta/foto", data={"csrf_token": CSRF, "acao": "remover"})
        self.assertIsNone(self.avatar_of(1))
        self.assertFalse((self.root / "avatars" / second).exists())

    def test_rejects_non_images_and_bad_names(self):
        c1 = self.client(self.players[1])
        self.upload(c1, "/conta/foto", "avatar", data=b"<svg onload=alert(1)>")
        self.assertIsNone(self.avatar_of(1))
        self.upload(c1, "/conta/foto", "avatar", data=PNG + b"\x00" * (1024 * 1024))
        self.assertIsNone(self.avatar_of(1))
        self.assertEqual(c1.get("/imagens/avatar/..%2Farena17.db").status_code, 404)
        self.assertEqual(c1.get("/imagens/outro/0123456789abcdef01234567.png").status_code, 404)
        self.assertEqual(self.client().post("/conta/foto", data={"csrf_token": CSRF}).status_code, 302)

    def test_list_thumbnail_opens_player_summary(self):
        with self.app.app_context():
            db = get_db()
            tournament = db.execute("SELECT * FROM tournaments WHERE slug = 'copa'").fetchone()
            for player in self.players[:2]:
                store.enroll(db, tournament, player, f"Time {player}")
            db.commit()
        self.upload(self.client(self.players[1]), "/conta/foto", "avatar")
        page = self.client().get("/torneios/copa?aba=geral").data.decode()
        self.assertIn('data-player-card="/torneios/jogadores/jog1/resumo"', page)
        self.assertIn('class="avatar avatar-sm avatar-initial" aria-hidden="true">J<', page)  # jog0 sem foto
        self.assertIn("/static/js/player-card.js", page)
        manage = self.client(self.players[0]).get("/torneios/copa/gerenciar").data.decode()
        self.assertIn('data-player-card="/torneios/jogadores/jog1/resumo"', manage)

        card = self.client().get("/torneios/jogadores/jog1/resumo")
        self.assertEqual(card.status_code, 200)
        html = card.data.decode()
        self.assertIn("<h2>jog1</h2>", html)
        self.assertIn("Últimos 5 resultados", html)
        self.assertIn("Nenhum jogo confirmado ainda", html)
        self.assertIn('class="avatar avatar-lg" src="/imagens/avatar/', html)
        self.assertEqual(self.client().get("/torneios/jogadores/ninguem/resumo").status_code, 404)

    def test_admin_removes_avatar(self):
        self.upload(self.client(self.players[1]), "/conta/foto", "avatar")
        name = self.avatar_of(1)
        self.assertEqual(self.client(self.players[2]).post(
            f"/torneios/jogadores/{self.players[1]}/foto/remover", data={"csrf_token": CSRF}).status_code, 403)
        admin = self.client(admin=True)
        self.assertIn("Remover foto", admin.get("/torneios/jogadores/jog1").data.decode())
        admin.post(f"/torneios/jogadores/{self.players[1]}/foto/remover", data={"csrf_token": CSRF})
        self.assertIsNone(self.avatar_of(1))
        self.assertFalse((self.root / "avatars" / name).exists())

    def test_avatars_on_friends_and_compare(self):
        self.upload(self.client(self.players[1]), "/conta/foto", "avatar")
        c0, c1 = self.client(self.players[0]), self.client(self.players[1])
        c0.post("/torneios/amigos/adicionar", data={"csrf_token": CSRF, "nome": "jog1"})
        c1.post(f"/torneios/amigos/{self.players[0]}/aceitar", data={"csrf_token": CSRF})
        friends = c0.get("/torneios/amigos").data.decode()
        self.assertIn('data-player-card="/torneios/jogadores/jog1/resumo"', friends)
        self.assertIn('class="avatar avatar-sm" src="/imagens/avatar/', friends)
        # Comparar: foto ao lado das vitórias do confronto direto (jog0 sem foto = inicial)
        compare = c0.get("/torneios/comparar?a=jog0&b=jog1").data.decode()
        self.assertIn('class="avatar avatar-md avatar-initial" aria-hidden="true">J<', compare)
        self.assertIn('class="avatar avatar-md" src="/imagens/avatar/', compare)
        self.assertIn('class="h2h-side h2h-b"', compare)

    def test_tournament_image_manage_show_and_card(self):
        self.assertEqual(self.upload(self.client(self.players[1]), "/torneios/copa/imagem", "image").status_code, 403)
        owner = self.client(self.players[0])
        self.upload(owner, "/torneios/copa/imagem", "image")
        name = self.one("SELECT image FROM tournaments WHERE slug = 'copa'")
        self.assertTrue((self.root / "tournament-images" / name).exists())
        self.assertIn(f'class="t-header-cover t-image" src="/imagens/torneio/{name}"',
                      owner.get("/torneios/copa").data.decode())
        self.assertIn(f'class="t-cover t-image" src="/imagens/torneio/{name}"',
                      owner.get("/torneios/meus-campeonatos").data.decode())
        self.assertIn(f"/imagens/torneio/{name}", self.client().get("/torneios/").data.decode())
        self.assertIn('name="acao" value="remover" formnovalidate>Remover imagem',
                      owner.get("/torneios/copa/gerenciar").data.decode())
        owner.post("/torneios/copa/imagem", data={"csrf_token": CSRF, "acao": "remover"})
        self.assertIsNone(self.one("SELECT image FROM tournaments WHERE slug = 'copa'"))
        self.assertFalse((self.root / "tournament-images" / name).exists())

    def share_image(self, client, origin, data=JPG):
        return client.post("/torneios/copa/imagem-compartilhar", content_type="multipart/form-data", data={
            "csrf_token": CSRF, "origem": origin, "image": (io.BytesIO(data), "compartilhar.jpg")})

    def test_share_image_card_for_shared_links(self):
        """O cartão JPEG do link compartilhado: quem gerencia envia (share-image.js) e a troca da imagem o apaga."""
        owner, folder = self.client(self.players[0]), self.root / "tournament-images"
        self.assertEqual(self.share_image(owner, "").status_code, 409)  # campeonato sem imagem
        self.upload(owner, "/torneios/copa/imagem", "image")
        image = self.one("SELECT image FROM tournaments WHERE slug = 'copa'")
        # Sem o cartão, a página de quem gerencia (e o Gerenciar) pede para o navegador montar; a dos outros não.
        script = f'data-origin="{image}" data-post="/torneios/copa/imagem-compartilhar"'
        self.assertIn(script, owner.get("/torneios/copa").data.decode())
        self.assertIn(script, owner.get("/torneios/copa/gerenciar").data.decode())
        self.assertIn(script, self.client(admin=True).get("/torneios/copa").data.decode())
        visitor = self.client(self.players[1]).get("/torneios/copa").data.decode()
        self.assertNotIn("share-image.js", visitor)
        self.assertIn(f'<meta property="og:image" content="http://localhost/imagens/torneio/{image}">', visitor)
        self.assertNotIn("og:image:width", visitor)

        self.assertEqual(self.share_image(self.client(self.players[1]), image).status_code, 403)
        self.assertEqual(self.share_image(owner, "outra.webp").status_code, 409)  # a imagem já foi trocada
        self.assertEqual(self.share_image(owner, image, data=PNG).status_code, 400)  # só JPEG
        self.assertIsNone(self.one("SELECT share_image FROM tournaments WHERE slug = 'copa'"))
        self.assertEqual(self.share_image(owner, image).status_code, 204)
        card = self.one("SELECT share_image FROM tournaments WHERE slug = 'copa'")
        self.assertTrue(card.endswith(".jpg") and (folder / card).exists())
        page = self.client().get("/torneios/copa").data.decode()
        self.assertIn(f'<meta property="og:image" content="http://localhost/imagens/torneio/{card}">', page)
        self.assertIn('<meta property="og:image:width" content="1200">', page)
        self.assertIn('<meta name="twitter:card" content="summary_large_image">', page)
        self.assertNotIn("share-image.js", owner.get("/torneios/copa").data.decode())
        self.assertNotIn("share-image.js", owner.get("/torneios/copa/gerenciar").data.decode())

        # Trocar a imagem apaga o cartão da antiga (o navegador monta o da nova); remover também.
        self.upload(owner, "/torneios/copa/imagem", "image")
        self.assertIsNone(self.one("SELECT share_image FROM tournaments WHERE slug = 'copa'"))
        self.assertFalse((folder / card).exists())
        self.assertIn("share-image.js", owner.get("/torneios/copa/gerenciar").data.decode())
        self.assertEqual(self.share_image(owner, self.one("SELECT image FROM tournaments WHERE slug = 'copa'")).status_code, 204)
        card = self.one("SELECT share_image FROM tournaments WHERE slug = 'copa'")
        owner.post("/torneios/copa/imagem", data={"csrf_token": CSRF, "acao": "remover"})
        self.assertIsNone(self.one("SELECT share_image FROM tournaments WHERE slug = 'copa'"))
        self.assertEqual(list(folder.iterdir()), [])
        self.assertNotIn("og:image", self.client().get("/torneios/copa").data.decode())

    def test_create_tournament_with_image(self):
        owner = self.client(self.players[2])
        self.assertIn('enctype="multipart/form-data"', owner.get("/torneios/novo").data.decode())
        response = owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa Com Imagem", "starts_on": "2026-12-01", "rules": "Regras",
            "game_title_id": self.game, "format": "knockout", "max_players": "2", "entry_fee": "0",
            "ko_legs": "1", "final_legs": "1", "image": (io.BytesIO(PNG), "logo.png")},
            content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        name = self.one("SELECT image FROM tournaments WHERE name = 'Copa Com Imagem'")
        self.assertTrue((self.root / "tournament-images" / name).exists())


if __name__ == "__main__":
    unittest.main()
