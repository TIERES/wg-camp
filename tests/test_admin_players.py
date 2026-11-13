"""Administração > Jogadores cadastrados: corrigir o e-mail, reenviar a confirmação
e excluir um cadastro não confirmado (app/admin.py). E a regra da reserva do nick:
o cadastro pendente segura o nick e o e-mail por 48 h contadas do cadastro OU do
último link de confirmação enviado (app/accounts.py)."""
import re
import tempfile
import unittest
from pathlib import Path

from app.accounts import already_registered
from app.db import get_db, init_db, now
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"
OLD = "2020-01-01T00:00:00+00:00"


class AdminPlayersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
            db = get_db()
            db.execute("INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('adm', 'x', ?, ?)",
                       (now(), now()))
            self.confirmed = add_player(db, "Confirmado")
            db.commit()
        self.register("Pendente", "pendente@example.com")  # cadastro não confirmado
        self.mails()

    def tearDown(self):
        self.temp.cleanup()

    def client(self, admin=True):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            if admin:
                session["user_id"] = 1
        return client

    def register(self, username, email):
        return self.client(admin=False).post("/conta/cadastro", data={
            "csrf_token": CSRF, "username": username, "email": email, "password": "senha1234",
            "password_confirm": "senha1234", "accept": "on"}, follow_redirects=True).get_data(as_text=True)

    def query(self, sql, *args):
        with self.app.app_context():
            return get_db().execute(sql, args).fetchall()

    def player(self, username):
        rows = self.query("SELECT * FROM players WHERE username = ? COLLATE NOCASE", username)
        return rows[0] if rows else None

    def mails(self):
        box = self.app.extensions.setdefault("mail_outbox", [])
        sent = [(m["To"], m["Subject"], m.get_content()) for m in box]
        box.clear()
        return sent

    def post(self, path, **data):
        return self.client().post(path, data={"csrf_token": CSRF, "voltar": "/admin/jogadores?confirmado=nao", **data},
                                  follow_redirects=True)

    def taken(self, field, value):
        with self.app.app_context():
            return already_registered(get_db(), field, value)

    def age(self, player_id):
        """O cadastro e os links de confirmação dele ficam com mais de 48 h."""
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE players SET created_at = ? WHERE id = ?", (OLD, player_id))
            db.execute("UPDATE player_tokens SET created_at = ? WHERE player_id = ?", (OLD, player_id))
            db.commit()

    def test_list_shows_the_buttons_and_the_reservation(self):
        self.register("Pendente2", "pendente2@example.com")
        page = self.client().get("/admin/jogadores").get_data(as_text=True)
        pending = self.player("Pendente2")
        self.assertIn(f'/admin/jogadores/{pending["id"]}/reenviar-confirmacao', page)
        self.assertIn(f'data-player-delete="/admin/jogadores/{pending["id"]}/excluir"', page)
        self.assertIn(f'data-player-edit="/admin/jogadores/{self.confirmed}/editar"', page)
        # Conta confirmada: só "Editar".
        self.assertNotIn(f"/admin/jogadores/{self.confirmed}/reenviar-confirmacao", page)
        self.assertNotIn(f"/admin/jogadores/{self.confirmed}/excluir", page)
        self.assertIn("Nick reservado até", page)
        self.age(pending["id"])
        self.assertIn("Nick livre para um novo cadastro", self.client().get("/admin/jogadores").get_data(as_text=True))
        # Só a administração.
        self.assertIn("/admin/login", self.client(admin=False).get("/admin/jogadores").headers["Location"])
        response = self.client(admin=False).post(f"/admin/jogadores/{pending['id']}/excluir", data={"csrf_token": CSRF})
        self.assertIn("/admin/login", response.headers["Location"])
        self.assertIsNotNone(self.player("Pendente2"))

    def test_fix_the_email_of_a_pending_registration(self):
        self.register("Digitou", "digitou.errado@example.com")
        player = self.player("Digitou")
        old_link = re.search(r"/conta/confirmar/\S+", self.mails()[0][2]).group(0)
        page = self.post(f"/admin/jogadores/{player['id']}/editar", email="  Digitou.Certo@Example.com ").get_data(as_text=True)
        self.assertIn("Digitou: e-mail corrigido para digitou.certo@example.com. O link de confirmação foi enviado", page)
        self.assertEqual(self.player("Digitou")["email"], "digitou.certo@example.com")
        (to, subject, body), = self.mails()
        self.assertEqual(to, "digitou.certo@example.com")
        self.assertIn("Confirme seu cadastro", subject)
        # O link que foi para o e-mail errado não vale mais; o novo confirma.
        self.assertEqual(self.client(admin=False).get(old_link).status_code, 400)
        new_link = re.search(r"/conta/confirmar/\S+", body).group(0)
        self.client(admin=False).get(new_link)
        self.assertIsNotNone(self.player("Digitou")["email_verified_at"])

    def test_email_rules(self):
        player = self.player("Pendente")
        url = f"/admin/jogadores/{player['id']}/editar"
        self.assertIn("parece ter erro de digitação", self.post(url, email="pendente@gmial.com").get_data(as_text=True))
        self.assertIn("Nada foi alterado", self.post(url, email=player["email"]).get_data(as_text=True))
        self.assertIn("já é de outra conta", self.post(url, email="Confirmado@x.com").get_data(as_text=True))
        self.assertEqual(self.player("Pendente")["email"], player["email"])
        self.assertEqual(self.mails(), [])
        # Um cadastro pendente vencido com o e-mail não segura: sai, como num cadastro novo.
        self.register("Velho", "velho@example.com")
        self.age(self.player("Velho")["id"])
        self.mails()
        self.assertIn("e-mail corrigido para velho@example.com", self.post(url, email="velho@example.com").get_data(as_text=True))
        self.assertIsNone(self.player("Velho"))
        self.assertEqual(self.player("Pendente")["email"], "velho@example.com")

    def test_fix_the_email_of_a_confirmed_player(self):
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO player_tokens (player_id, kind, token_hash, created_at, expires_at) "
                       "VALUES (?, 'reset', 'h', ?, '2999-01-01T00:00:00+00:00')", (self.confirmed, now()))
            db.commit()
        page = self.post(f"/admin/jogadores/{self.confirmed}/editar", email="novo.confirmado@example.com").get_data(as_text=True)
        self.assertIn("Confirmado: e-mail corrigido para novo.confirmado@example.com.", page)
        player = self.player("Confirmado")
        self.assertEqual(player["email"], "novo.confirmado@example.com")
        self.assertIsNotNone(player["email_verified_at"])  # continua confirmado
        (to, subject, body), = self.mails()
        self.assertEqual(to, "novo.confirmado@example.com")
        self.assertIn("Sua conta no WE Camp foi atualizada", subject)
        self.assertIn("O e-mail da sua conta passou a ser este endereço", body)
        self.assertNotIn("senha foi trocada", body)
        # O link de nova senha que foi para o e-mail antigo não vale mais.
        self.assertIsNotNone(self.query("SELECT used_at FROM player_tokens WHERE kind = 'reset'")[0][0])

    def test_set_a_new_password(self):
        from werkzeug.security import check_password_hash
        url = f"/admin/jogadores/{self.confirmed}/editar"
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO player_tokens (player_id, kind, token_hash, created_at) VALUES (?, 'api', 'a', ?)",
                       (self.confirmed, now()))
            db.commit()
        email = self.player("Confirmado")["email"]
        self.assertIn("pelo menos 8 caracteres", self.post(url, email=email, password="curta").get_data(as_text=True))
        page = self.post(url, email=email, password="senhaNova99").get_data(as_text=True)
        self.assertIn("Confirmado: nova senha definida. Os PCs conectados precisam entrar de novo no Kaillera", page)
        player = self.player("Confirmado")
        self.assertTrue(check_password_hash(player["password_hash"], "senhaNova99"))
        self.assertEqual(player["email"], email)
        self.assertIsNotNone(self.query("SELECT used_at FROM player_tokens WHERE kind = 'api'")[0][0])  # conexões caem
        (to, subject, body), = self.mails()
        self.assertEqual(to, email)
        self.assertIn("Sua conta no WE Camp foi atualizada", subject)
        self.assertIn("A sua senha foi trocada", body)
        self.assertNotIn("senhaNova99", body)  # a senha nunca vai por e-mail
        # Entra com a senha nova.
        login = self.client(admin=False).post("/conta/entrar", data={
            "csrf_token": CSRF, "username": "Confirmado", "password": "senhaNova99"})
        self.assertEqual(login.status_code, 302)
        self.assertNotIn("/conta/entrar", login.headers["Location"])
        # E-mail e senha juntos, num cadastro pendente: a confirmação vai para o e-mail novo.
        pending = self.player("Pendente")
        page = self.post(f"/admin/jogadores/{pending['id']}/editar", email="pendente.novo@example.com",
                         password="outraSenha1").get_data(as_text=True)
        self.assertIn("Pendente: e-mail corrigido para pendente.novo@example.com e nova senha definida.", page)
        (to, subject, body), = self.mails()
        self.assertEqual(to, "pendente.novo@example.com")
        self.assertIn("Confirme seu cadastro", subject)
        self.assertNotIn("outraSenha1", body)
        self.assertTrue(check_password_hash(self.player("Pendente")["password_hash"], "outraSenha1"))

    def test_resend_the_confirmation_and_renew_the_reservation(self):
        player = self.player("Pendente")
        url = f"/admin/jogadores/{player['id']}/reenviar-confirmacao"
        # Enviado agora há pouco (no cadastro): espera os 2 minutos.
        self.assertIn("há menos de 2 minutos", self.post(url).get_data(as_text=True))
        self.assertEqual(self.mails(), [])
        self.age(player["id"])
        self.assertFalse(self.taken("username", "Pendente"))  # 48 h sem link novo: o nick está livre
        response = self.post(url)
        self.assertEqual(response.request.path, "/admin/jogadores")
        self.assertIn("confirmado=nao", response.request.query_string.decode())  # volta para a lista como estava
        self.assertIn("Confirmação reenviada para", response.get_data(as_text=True))
        (to, subject, _), = self.mails()
        self.assertEqual(to, player["email"])
        self.assertIn("Confirme seu cadastro", subject)
        # O link novo renova a reserva: o nick e o e-mail voltam a estar ocupados.
        self.assertTrue(self.taken("username", "Pendente"))
        self.assertTrue(self.taken("email", player["email"]))
        self.assertIn("Este usuário já está cadastrado", self.register("pendente", "outro@example.com"))
        # Conta confirmada não tem o que reenviar.
        self.assertIn("já confirmou o cadastro", self.post(f"/admin/jogadores/{self.confirmed}/reenviar-confirmacao")
                      .get_data(as_text=True))

    def test_delete_a_pending_registration_frees_the_nick_right_away(self):
        player = self.player("Pendente")
        self.assertIn("Este usuário já está cadastrado", self.register("Pendente", "outro@example.com"))
        page = self.post(f"/admin/jogadores/{player['id']}/excluir").get_data(as_text=True)
        self.assertIn("Cadastro de Pendente excluído", page)
        self.assertIsNone(self.player("Pendente"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM player_tokens WHERE player_id = ?", player["id"])[0][0], 0)
        # O nick e o e-mail estão livres na hora.
        self.mails()
        self.assertIn("Confirme seu e-mail", self.register("Pendente", player["email"]))
        self.assertIsNotNone(self.player("Pendente"))
        # Conta confirmada não é excluída por aqui.
        page = self.post(f"/admin/jogadores/{self.confirmed}/excluir").get_data(as_text=True)
        self.assertIn("só dá para excluir cadastro não confirmado", page)
        self.assertIsNotNone(self.player("Confirmado"))
        self.assertEqual(self.client().post("/admin/jogadores/9999/excluir", data={"csrf_token": CSRF}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
