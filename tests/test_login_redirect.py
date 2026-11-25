"""Páginas que pedem o login: sem ele, toda página que depende de quem está logado
leva para /conta/entrar e, depois de entrar, volta para ela (security.login_redirect
e security.deny) - em vez do 403 "Forbidden" ou do 404 de campeonato escondido."""
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash

from app.db import get_db, init_db, now
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"
SITE = "http://localhost"


class LoginRedirectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
            db = get_db()
            self.players = [add_player(db, f"jog{i}") for i in range(3)]
            db.execute("UPDATE players SET password_hash = ? WHERE id = ?",
                       (generate_password_hash("segredo123"), self.players[2]))
            db.commit()
            self.game = db.execute("SELECT id FROM game_titles WHERE code = 'we2002'").fetchone()["id"]

    def tearDown(self):
        self.temp.cleanup()

    def client(self, player=None):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            if player:
                session["player_id"] = player
        return client

    def tournament(self, **extra):
        owner = self.client(self.players[0])
        owner.post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa", "starts_on": "2026-12-01", "rules": "Regras",
            "game_title_id": self.game, "format": "league", "max_players": "4", "entry_fee": "0", "legs": "1",
            **extra})
        with self.app.app_context():
            return get_db().execute("SELECT slug FROM tournaments ORDER BY id DESC").fetchone()["slug"]

    def assert_login(self, response, back):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/conta/entrar?next={back}")

    def test_manage_pages_ask_for_login(self):
        """O caso relatado: /gerenciar de um campeonato "Somente amigos" dava 404 sem login."""
        for extra in ({}, {"friends_only": "on"}):
            slug = self.tournament(**extra)
            anon = self.client()
            for path in (f"/torneios/{slug}/gerenciar", f"/torneios/{slug}/alteracao/iso"):
                self.assert_login(anon.get(path), path)
            # Com a busca da página junto.
            self.assert_login(anon.get(f"/torneios/{slug}/gerenciar?aba=dados"),
                              f"/torneios/{slug}/gerenciar?aba%3Ddados")

    def test_hidden_tournament_pages_ask_for_login(self):
        slug = self.tournament(friends_only="on")
        for path in (f"/torneios/{slug}/tabela-ao-vivo", f"/torneios/{slug}/jogos/1"):
            self.assert_login(self.client().get(path), path)
        # Logado e não amigo: continua não existindo.
        self.assertEqual(self.client(self.players[1]).get(f"/torneios/{slug}/gerenciar").status_code, 404)
        # Campeonato que não existe: 404, sem login.
        self.assertEqual(self.client().get("/torneios/nao-existe/gerenciar").status_code, 404)

    def test_logged_without_access_sees_the_portuguese_page(self):
        slug = self.tournament()
        response = self.client(self.players[1]).get(f"/torneios/{slug}/gerenciar")
        self.assertEqual(response.status_code, 403)
        page = response.get_data(as_text=True)
        self.assertIn("Sem acesso", page)
        self.assertIn("Você entrou como <strong>jog1</strong>", page)
        self.assertIn(f'href="/conta/entrar?next=/torneios/{slug}/gerenciar"', page)
        self.assertNotIn("Forbidden", page)

    def test_form_sent_without_login_comes_back_to_the_page(self):
        """Sessão expirada num formulário: volta para a página dele, não para o
        endereço do POST (que daria 405 ao abrir)."""
        slug = self.tournament()
        anon = self.client()
        page = f"/torneios/{slug}?aba=jogos"
        for path in (f"/torneios/{slug}/inscrever", f"/torneios/{slug}/editar", f"/torneios/{slug}/iniciar"):
            response = anon.post(path, data={"csrf_token": CSRF}, headers={"Referer": SITE + page})
            self.assert_login(response, f"/torneios/{slug}?aba%3Djogos")
        # Sem o Referer (ou de outro site), só o login.
        for headers in ({}, {"Referer": "https://outro.site/torneios/x"}):
            response = anon.post(f"/torneios/{slug}/inscrever", data={"csrf_token": CSRF}, headers=headers)
            self.assertEqual(response.headers["Location"], "/conta/entrar")

    def test_login_goes_back_to_the_page(self):
        def login(next_value):
            client = self.client()
            return client.post("/conta/entrar", query_string={"next": next_value} if next_value else None,
                               data={"csrf_token": CSRF, "username": "jog2", "password": "segredo123"})
        self.assertEqual(login("/torneios/copa-1/gerenciar?aba=dados").headers["Location"],
                         "/torneios/copa-1/gerenciar?aba=dados")
        self.assertEqual(login("/ao-vivo").headers["Location"], "/ao-vivo")
        for unsafe in (None, "//outro.site/x", "/\\outro.site", "https://outro.site/", "torneios"):
            self.assertEqual(login(unsafe).headers["Location"], "/torneios/meus-campeonatos", unsafe)

    def test_live_card_actions_ask_for_login(self):
        anon = self.client()
        confirm = anon.post("/ao-vivo/1/confirmar", data={"csrf_token": CSRF}, headers={"Referer": SITE + "/ao-vivo"})
        self.assertEqual(confirm.status_code, 401)
        self.assertEqual(confirm.get_json()["login"], "/conta/entrar?next=/ao-vivo")
        delete = anon.post("/ao-vivo/1/excluir", data={"csrf_token": CSRF}, headers={"Referer": SITE + "/ao-vivo"})
        self.assert_login(delete, "/ao-vivo")

    def test_unconfirmed_friendly_asks_for_login(self):
        with self.app.app_context():
            db = get_db()
            fid = db.execute(
                "INSERT INTO friendly_matches (home_id, away_id, status, created_at, updated_at) "
                "VALUES (?, ?, 'invited', ?, ?)", (self.players[0], self.players[1], now(), now())).lastrowid
            db.commit()
        self.assert_login(self.client().get(f"/torneios/amistosos/{fid}"), f"/torneios/amistosos/{fid}")
        self.assertEqual(self.client(self.players[2]).get(f"/torneios/amistosos/{fid}").status_code, 404)
        self.assertEqual(self.client(self.players[0]).get(f"/torneios/amistosos/{fid}").status_code, 200)


if __name__ == "__main__":
    unittest.main()
