import tempfile
import unittest
from pathlib import Path

from app.mailer import NO_REPLY, compose, paragraphs, send_mail
from tests.test_tournament_store import make_app


class MailCardTest(unittest.TestCase):
    """Todo e-mail sai com texto e HTML no card padrão (email/_base.html): logo e
    "Campeonatos / Winning Eleven" no alto, o aviso de que não é preciso responder
    no fim e a logo menor com "WE Camp" (link do portal) no rodapé."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_text_mail_gets_the_card(self):
        body = ("Olá, Fulano!\n\nVeja o jogo <A & B> em:\nhttp://localhost/torneios/copa/jogos/3.\n\n"
                "Antes de cada jogo:\n  - Entre na Conta WE Camp.\n")
        with self.app.test_request_context("/"):
            send_mail("fulano@x.com", "Confirme o placar: A x B", body)
        mail = self.app.extensions["mail_outbox"][-1]
        text = mail.get_body(("plain",)).get_content()
        html = mail.get_body(("html",)).get_content()
        self.assertTrue(text.startswith(body.rstrip("\n")))
        self.assertIn(f"\n\n{NO_REPLY}\n--\nWE Camp - Campeonatos Winning Eleven\nhttp://localhost/\n", text)
        # Cabeçalho: logo + as duas linhas do título; rodapé: logo menor + "WE Camp" com o link do portal.
        self.assertIn('src="http://localhost/static/images/logo/wecamp-192.png" width="64" height="64"', html)
        self.assertIn(">Campeonatos</span>", html)
        self.assertIn(">Winning Eleven</span>", html)
        self.assertIn('src="http://localhost/static/images/logo/wecamp-96.png" width="28" height="28"', html)
        self.assertIn('<a href="http://localhost/" style="color:#9ca3af;text-decoration:none;">WE Camp</a>', html)
        self.assertIn("não é preciso responder", html)
        # Assunto como título, texto escapado, link clicável sem o ponto final, recuo da lista mantido.
        self.assertIn(">Confirme o placar: A x B</div>", html)
        self.assertIn("Veja o jogo &lt;A &amp; B&gt; em:<br>", html)
        self.assertIn('<a href="http://localhost/torneios/copa/jogos/3" style="color:#34d399;word-break:break-all;">'
                      'http://localhost/torneios/copa/jogos/3</a>.', html)
        self.assertIn("Antes de cada jogo:<br>&nbsp;&nbsp;- Entre na Conta WE Camp.", html)
        self.assertLess(html.index("Entre na Conta"), html.index("não é preciso responder"))
        self.assertLess(html.index("não é preciso responder"), html.index(">WE Camp</a>"))

    def test_paragraphs_split_on_blank_lines(self):
        self.assertEqual([str(p) for p in paragraphs("\nUm\ndois\n\n \nTrês\n")], ["Um<br>dois", "Três"])

    def test_own_html_is_kept_and_text_still_gets_the_notice(self):
        with self.app.test_request_context("/"):
            text, html = compose("Assunto", "Corpo", "<p>meu html</p>")
        self.assertEqual(html, "<p>meu html</p>")
        self.assertTrue(text.startswith(f"Corpo\n\n{NO_REPLY}\n--\n"))

    def test_outside_a_request_the_mail_is_text_only(self):
        with self.app.app_context():
            text, html = compose("Assunto", "Corpo")
        self.assertIsNone(html)
        self.assertEqual(text, f"Corpo\n\n{NO_REPLY}\n--\nWE Camp - Campeonatos Winning Eleven\n")


if __name__ == "__main__":
    unittest.main()
