import tempfile
import unittest
from pathlib import Path

from app.accounts import username_error
from app.db import get_db, init_db
from tests.test_tournament_store import make_app

CSRF = "test-csrf"


class UsernameRulesTest(unittest.TestCase):
    """Usuário = nick do Kaillera: sem espaço, sem acento, só os caracteres aceitos."""

    def test_messages_say_what_is_wrong(self):
        self.assertIsNone(username_error("Pele_10"))
        self.assertIsNone(username_error("[WE]Zico"))
        self.assertIn("Informe o usuário", username_error(""))
        self.assertIn("espaços", username_error("Joao Silva"))
        self.assertIn("acentos", username_error("João"))
        self.assertIn("acentos", username_error("Conceição"))
        self.assertIn("2 a 31", username_error("a"))
        self.assertIn("2 a 31", username_error("a" * 32))
        self.assertIn("(#@)", username_error("ze@#"))


class RegisterFormTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()

    def tearDown(self):
        self.temp.cleanup()

    def client(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
        return client

    def post(self, username):
        return self.client().post("/conta/cadastro", data={
            "csrf_token": CSRF, "username": username, "email": "novo@example.com", "password": "senha1234",
            "password_confirm": "senha1234", "accept": "on"}).get_data(as_text=True)

    def players(self):
        with self.app.app_context():
            return get_db().execute("SELECT COUNT(*) FROM players").fetchone()[0]

    def test_form_marks_required_fields_and_checks_before_sending(self):
        page = self.client().get("/conta/cadastro").get_data(as_text=True)
        self.assertIn("Todos os campos são obrigatórios", page)
        self.assertEqual(page.count('class="req"'), 6)  # aviso + 4 campos + confirmação
        self.assertIn("Sem espaços e sem acentos", page)
        self.assertIn('data-username-pattern="', page)
        self.assertIn('id="register-submit"', page)

    def test_server_refuses_space_and_accents(self):
        self.assertIn("não pode ter espaços", self.post("Joao Silva"))
        self.assertIn("não pode ter acentos", self.post("João"))
        self.assertEqual(self.players(), 0)
        self.post("Joao_Silva")
        self.assertEqual(self.players(), 1)

    def check(self, field, value):
        return self.client().get("/conta/cadastro/verificar", query_string={"campo": field, "valor": value}).get_json()

    def test_check_tells_if_username_or_email_is_taken(self):
        self.assertIn("Campeonatos do WE Camp", self.client().get("/conta/cadastro").get_data(as_text=True))
        self.assertEqual(self.check("usuario", "Joao_Silva"), {"ok": True, "message": ""})
        self.assertEqual(self.check("email", "novo@example.com"), {"ok": True, "message": ""})
        self.post("Joao_Silva")
        self.assertIn("usuário já está cadastrado", self.check("usuario", "JOAO_SILVA")["message"])
        self.assertIn("e-mail já está cadastrado", self.check("email", "Novo@Example.com")["message"])
        self.assertIn("espaços", self.check("usuario", "Joao Silva")["message"])
        self.assertIn("e-mail válido", self.check("email", "novo@")["message"])
        # Cadastro nunca confirmado e antigo não reserva o nome nem o e-mail.
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE players SET created_at = '2020-01-01T00:00:00+00:00'")
            db.commit()
        self.assertTrue(self.check("usuario", "Joao_Silva")["ok"])
        self.assertTrue(self.check("email", "novo@example.com")["ok"])
        self.assertEqual(self.client().get("/conta/cadastro/verificar?campo=senha&valor=x").status_code, 404)


if __name__ == "__main__":
    unittest.main()
