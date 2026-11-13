import tempfile
import unittest
from pathlib import Path

from app.accounts import email_error, normalize_email
from app.db import get_db, init_db
from tests.test_tournament_store import make_app

CSRF = "test-csrf"


class EmailRulesTest(unittest.TestCase):
    """E-mail do cadastro: só minúsculas, formato válido e provedor gratuito
    sem erro de digitação (com a sugestão do endereço certo)."""

    def test_normalize_trims_and_lowercases(self):
        self.assertEqual(normalize_email("  Fulano.Silva@Gmail.COM "), "fulano.silva@gmail.com")
        self.assertEqual(normalize_email(None), "")

    def test_valid_addresses(self):
        for email in (
            "novo@example.com", "fulano.silva@gmail.com", "fulano+we@gmail.com", "fulano10@googlemail.com",
            "fulano_10@hotmail.com", "fulano-10@hotmail.com.br", "fulano@hotmail.es", "fulano@hotmail.com.ar",
            "fulano@hotmail.co.uk", "fulano@outlook.com", "fulano@outlook.pt", "fulano@outlook.com.br",
            "fulano@live.com", "fulano@live.cl", "fulano@msn.com", "fulano@yahoo.com", "fulano@yahoo.com.br",
            "fulano@yahoo.com.ar", "fulano@yahoo.co.jp", "fulano@ymail.com", "fulano@icloud.com",
            "fulano@bol.com.br", "fulano@uol.com.br", "fulano@terra.com.br", "fulano@proton.me",
            "fulano@protonmail.ch",
            # Parecidos com os grandes, mas existem.
            "fulano@mail.com", "fulano@email.com", "fulano@cloud.com",
            # Domínios quaisquer, inclusive com final a uma letra de "com".
            "fulano@empresa.co", "fulano@empresa.cm", "fulano@empresa.cam", "fulano@empresa.com.br",
            "fulano@empresa.com.ar", "fulano@usp.br", "fulano@silva.nom.br", "fulano@imoveis.cim.br", "f@x.com",
        ):
            self.assertIsNone(email_error(email), email)

    def test_typos_come_with_the_right_address(self):
        for typed, right in (
            ("fulano@gmial.com", "fulano@gmail.com"), ("fulano@gmai.com", "fulano@gmail.com"),
            ("fulano@gmaill.com", "fulano@gmail.com"), ("fulano@gmail.con", "fulano@gmail.com"),
            ("fulano@gmail.co", "fulano@gmail.com"), ("fulano@gmail.com.br", "fulano@gmail.com"),
            ("fulano@gmail", "fulano@gmail.com"), ("fulano@gmailcom", "fulano@gmail.com"),
            ("fulano@gmial.con", "fulano@gmail.com"),
            ("fulano@hotmal.com", "fulano@hotmail.com"), ("fulano@hotmail.con", "fulano@hotmail.com"),
            ("fulano@hotmail.co", "fulano@hotmail.com"), ("fulano@hotmail.cmo", "fulano@hotmail.com"),
            ("fulano@hotmail.com.b", "fulano@hotmail.com.br"), ("fulano@hotmial.com.br", "fulano@hotmail.com.br"),
            ("fulano@hotmail.combr", "fulano@hotmail.com.br"), ("fulano@hotmailcombr", "fulano@hotmail.com.br"),
            ("fulano@hotmail.co.br", "fulano@hotmail.com.br"), ("fulano@hotmail.con.br", "fulano@hotmail.com.br"),
            ("fulano@hotmial.es", "fulano@hotmail.es"), ("fulano@hotmail", "fulano@hotmail.com"),
            ("fulano@outlok.com", "fulano@outlook.com"), ("fulano@outlook.con", "fulano@outlook.com"),
            ("fulano@yaho.com.br", "fulano@yahoo.com.br"), ("fulano@yahoo.com.brr", "fulano@yahoo.com.br"),
            ("fulano@iclod.com", "fulano@icloud.com"), ("fulano@icloud.com.br", "fulano@icloud.com"),
            ("fulano@bol.com", "fulano@bol.com.br"), ("fulano@uol.com", "fulano@uol.com.br"),
            ("fulano@live.con", "fulano@live.com"), ("fulano@protomail.com", "fulano@protonmail.com"),
            # Domínio qualquer: só o final com cara de .com / .com.br mal digitado.
            ("fulano@empresa.con", "fulano@empresa.com"), ("fulano@empresa.comm", "fulano@empresa.com"),
            ("fulano@empresa.c0m", "fulano@empresa.com"), ("fulano@empresa.con.br", "fulano@empresa.com.br"),
            ("fulano@empresa.com.b", "fulano@empresa.com.br"), ("fulano@empresa.combr", "fulano@empresa.com.br"),
        ):
            error = email_error(typed)
            self.assertIsNotNone(error, typed)
            self.assertIn("erro de digitação", error, typed)
            self.assertIn(f"Você quis dizer {right}?", error, typed)

    def test_messages_say_what_is_wrong(self):
        for email, text in (
            ("", "Informe o e-mail"), (None, "Informe o e-mail"),
            ("Fulano@gmail.com", "minúsculas"), ("fulano@Gmail.com", "minúsculas"),
            ("fulano silva@gmail.com", "espaços"),
            ("fulano", "e-mail válido"), ("novo@", "e-mail válido"), ("@gmail.com", "e-mail válido"),
            ("a@b@gmail.com", "e-mail válido"), ("x" * 65 + "@example.com", "e-mail válido"),
            ("joão.silva@gmail.com", "acentos"), ("fulano@açaí.com.br", "acentos"),
            ("fulano,silva@hotmail.com", "não permitido antes do @ (,)"),
            ("fulano(1)@hotmail.com", "não permitido antes do @ (()"),
            (".fulano@hotmail.com", "ponto"), ("fulano.@hotmail.com", "ponto"), ("fu..lano@hotmail.com", "ponto"),
            ("fulano@empresa", "Falta o final"),
            ("fulano@empresa..com", "não é válido"), ("fulano@-empresa.com", "não é válido"),
            ("fulano@empresa.com.", "não é válido"), ("fulano@empresa,com", "não é válido"),
            ("fulano@empresa.c", "não é válido"), ("fulano@empresa.123", "não é válido"),
            # Regras do Gmail para o nome.
            ("fulano_silva@gmail.com", "No Gmail"), ("fulano-silva@gmail.com", "No Gmail"),
            ("joao@gmail.com", "6 a 30"), ("f" * 31 + "@gmail.com", "6 a 30"),
        ):
            error = email_error(email)
            self.assertIsNotNone(error, email)
            self.assertIn(text, error, email)


class RegisterEmailTest(unittest.TestCase):
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

    def register(self, email, username="Joao_Silva"):
        return self.client().post("/conta/cadastro", data={
            "csrf_token": CSRF, "username": username, "email": email, "password": "senha1234",
            "password_confirm": "senha1234", "accept": "on"}).get_data(as_text=True)

    def emails(self):
        with self.app.app_context():
            return [row["email"] for row in get_db().execute("SELECT email FROM players ORDER BY id")]

    def outbox(self):
        return self.app.extensions.get("mail_outbox", [])

    def check(self, value):
        return self.client().get("/conta/cadastro/verificar", query_string={"campo": "email", "valor": value}).get_json()

    def test_form_asks_for_lowercase(self):
        page = self.client().get("/conta/cadastro").get_data(as_text=True)
        self.assertIn("Só letras minúsculas", page)
        self.assertIn('autocapitalize="off" spellcheck="false" aria-describedby="email-hint email-error"', page)

    def test_email_is_saved_and_sent_in_lowercase(self):
        page = self.register("  Novo.Jogador@Example.COM ")
        self.assertIn("novo.jogador@example.com", page)
        self.assertNotIn("Novo.Jogador", page)
        self.assertEqual(self.emails(), ["novo.jogador@example.com"])
        self.assertEqual([message["To"] for message in self.outbox()], ["novo.jogador@example.com"])

    def test_server_refuses_mistyped_provider(self):
        page = self.register("Fulano@Gmial.com")
        self.assertIn("Você quis dizer fulano@gmail.com?", page)
        self.assertIn('value="fulano@gmial.com"', page)  # volta ao formulário já em minúsculas
        self.assertIn("No Gmail", self.register("joao_10@gmail.com"))
        self.assertEqual(self.emails(), [])
        self.assertEqual(self.outbox(), [])

    def test_check_normalizes_and_suggests(self):
        self.assertEqual(self.check("NOVO@Example.com"), {"ok": True, "message": ""})
        answer = self.check("Fulano@Hotmail.con")
        self.assertFalse(answer["ok"])
        self.assertIn("Você quis dizer fulano@hotmail.com?", answer["message"])
        self.register("novo@example.com")
        self.assertIn("já está cadastrado", self.check(" NOVO@EXAMPLE.COM ")["message"])

    def test_resend_finds_the_account_typed_in_uppercase(self):
        self.register("novo@example.com")
        self.assertEqual(len(self.outbox()), 1)
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE player_tokens SET created_at = '2020-01-01T00:00:00+00:00'")  # fora do intervalo mínimo
            db.commit()
        self.client().post("/conta/reenviar-confirmacao", data={"csrf_token": CSRF, "email": " NOVO@Example.COM "})
        self.assertEqual([message["To"] for message in self.outbox()], ["novo@example.com"] * 2)


if __name__ == "__main__":
    unittest.main()
