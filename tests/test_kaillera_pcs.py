"""PCs conectados no Kaillera só no site (48 h desde a entrada ou o último
uso do token - o DLL chama /api/mc/whoami ao abrir o Kaillera) e a janela
"Conecte sua Conta WE Camp no Kaillera"."""
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

from app import create_app
from app.accounts import iso, utcnow
from app.db import get_db, init_db

POPUP = 'id="conta-wecamp-popup"'


def ago(**delta):
    return iso(utcnow() - timedelta(**delta))


class KaillerapcsTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": str(root / "arena17.db"),
            "DOWNLOADS_DIR": str(root / "downloads"),
            "UPLOAD_TMP_DIR": str(root / "tmp"),
            "MEMCARDS_DIR": str(root / "memcards"),
            "LIVE_DIR": str(root / "live"),
            "PUBLIC_URL": "https://we2002.example",
            "SPECTATE_API_KEY": "dll-key",
        })
        with self.app.app_context():
            init_db()
            db = get_db()
            for username in ("Pele", "Garrincha"):
                db.execute(
                    "INSERT INTO players (username, email, password_hash, email_verified_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (username, f"{username.lower()}@example.com",
                     generate_password_hash("segredo123"), ago(days=30),
                     ago(days=30), ago(days=30)))
            db.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["csrf_token"] = "csrf"

    def tearDown(self):
        self.temp.cleanup()

    def web_login(self, username="Pele", client=None):
        client = client or self.client
        response = client.post("/conta/entrar", data={"csrf_token": "csrf", "username": username,
                                                      "password": "segredo123"})
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            session["csrf_token"] = "csrf"
        return response

    def api_login(self, username="Pele"):
        """Entra pelo Kaillera (DLL): um PC novo, conectado agora. Guarda o
        token em self.raw[id] para o whoami."""
        reply = self.client.post("/api/mc/login", data={"username": username, "password": "segredo123"})
        self.assertEqual(reply.status_code, 200)
        fields = dict(line.split("=", 1) for line in reply.get_data(as_text=True).splitlines())
        with self.app.app_context():
            token_id = get_db().execute("SELECT MAX(id) AS id FROM player_tokens WHERE kind = 'api'").fetchone()["id"]
        self.raw = getattr(self, "raw", {})
        self.raw[token_id] = fields["token"]
        return token_id

    def open_kaillera(self, token_id):
        """O DLL abrindo a janela do Kaillera com o login salvo."""
        reply = self.client.get("/api/mc/whoami", headers={"Authorization": f"Bearer {self.raw[token_id]}"})
        return reply.status_code

    def set_token(self, token_id, **columns):
        with self.app.app_context():
            db = get_db()
            for column, value in columns.items():
                db.execute(f"UPDATE player_tokens SET {column} = ? WHERE id = ?", (value, token_id))
            db.commit()

    def token(self, token_id):
        with self.app.app_context():
            return dict(get_db().execute("SELECT * FROM player_tokens WHERE id = ?", (token_id,)).fetchone())

    def page(self, path="/"):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)


class PopupTest(KaillerapcsTestBase):
    def test_login_without_pc_shows_the_popup_once(self):
        self.web_login()
        first = self.page()
        self.assertIn(POPUP, first)
        self.assertIn("data-auto-open", first)
        self.assertIn("contaWeCamp.gif", first)
        self.assertIn("Conta WE Camp", first)
        self.assertIn("<strong>Pele</strong>", first)   # o Nick que o Kaillera passa a usar
        self.assertNotIn(POPUP, self.page())
        self.assertNotIn(POPUP, self.page("/conta/"))

        # Entrar de novo no site: aparece de novo.
        self.web_login()
        self.assertIn(POPUP, self.page("/conta/"))

    def test_first_login_says_the_account_was_approved(self):
        """Primeira entrada no site depois de confirmar o cadastro: o texto inicial avisa
        que a conta foi aprovada; nas outras entradas, o texto de sempre."""
        self.web_login()
        first = self.page()
        self.assertIn(POPUP, first)
        self.assertIn("Sua conta foi aprovada!", first)
        self.assertIn("Agora você já pode entrar na <strong>Conta WE Camp</strong> do Kaillera Netplay.", first)
        self.assertNotIn("Sua conta foi aprovada!", self.page("/conta/"))   # o botão da Minha conta: texto de sempre
        self.web_login()
        again = self.page()
        self.assertIn(POPUP, again)
        self.assertNotIn("Sua conta foi aprovada!", again)

    def test_visitor_and_admin_never_see_it(self):
        self.assertNotIn(POPUP, self.page())
        self.web_login()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
        self.assertNotIn(POPUP, self.client.get("/").get_data(as_text=True))

    def test_connected_pc_hides_the_popup(self):
        self.api_login()
        self.web_login()
        self.assertNotIn(POPUP, self.page())

    def test_popup_returns_when_the_pc_leaves_the_site(self):
        self.web_login()
        self.assertIn(POPUP, self.page())          # visto, sem PC
        token = self.api_login()                    # conectou depois
        self.assertNotIn(POPUP, self.page())
        # 48 h sem abrir o Kaillera: o PC sai do site e a janela volta, uma vez.
        self.set_token(token, created_at=ago(hours=49))
        with self.client.session_transaction() as session:
            session["pc_popup_at"] = int(time.time()) - 49 * 3600
        self.assertIn(POPUP, self.page())
        self.assertNotIn(POPUP, self.page())

    def test_seen_popup_stays_hidden_when_nothing_changed(self):
        """Visto depois que o último PC saiu do site: não volta sozinha."""
        token = self.api_login()
        self.set_token(token, created_at=ago(hours=60))
        self.web_login()
        self.assertIn(POPUP, self.page())
        self.assertNotIn(POPUP, self.page("/conta/"))

    def test_disconnecting_the_last_pc_brings_it_back(self):
        token = self.api_login()
        self.web_login()
        self.assertNotIn(POPUP, self.page())
        with self.client.session_transaction() as session:
            session["pc_popup_at"] = int(time.time()) - 60
        response = self.client.post(f"/conta/conexoes/{token}/revogar", data={"csrf_token": "csrf"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(POPUP, self.page())


class AccountPageTest(KaillerapcsTestBase):
    def test_lists_only_pcs_active_in_the_last_48_hours(self):
        fresh = self.api_login()
        old = self.api_login()
        opened = self.api_login()
        self.set_token(old, created_at=ago(hours=50), last_used_at=ago(hours=49))
        self.set_token(opened, created_at=ago(days=5), last_used_at=ago(hours=10))
        self.web_login()
        page = self.page("/conta/")
        self.assertIn('id="pcs"', page)
        self.assertEqual(page.count('/revogar"'), 2)
        self.assertIn(f"/conta/conexoes/{fresh}/revogar", page)
        self.assertIn(f"/conta/conexoes/{opened}/revogar", page)
        self.assertNotIn(f"/conta/conexoes/{old}/revogar", page)
        self.assertIn("se o Kaillera não for aberto nele", page)
        self.assertIn('data-dialog="conta-wecamp-tutorial"', page)

    def test_without_pcs(self):
        self.web_login()
        page = self.page("/conta/")
        self.assertIn("Nenhum PC conectado nas últimas 48 horas.", page)
        self.assertIn('id="conta-wecamp-tutorial"', page)


class OpenKailleraTest(KaillerapcsTestBase):
    def test_opening_kaillera_brings_the_pc_back_to_the_site(self):
        token = self.api_login()
        self.set_token(token, created_at=ago(days=3), last_used_at=ago(hours=50))
        self.web_login()
        page = self.page("/conta/")
        self.assertNotIn(f"/conta/conexoes/{token}/revogar", page)
        self.assertIn(POPUP, page)

        self.assertEqual(self.open_kaillera(token), 200)
        self.assertGreater(self.token(token)["last_used_at"], ago(minutes=1))
        self.assertIn(f"/conta/conexoes/{token}/revogar", self.page("/conta/"))
        self.assertNotIn(POPUP, self.page())

    def test_only_the_pc_that_opened_kaillera_stays(self):
        home = self.api_login()
        notebook = self.api_login()
        self.set_token(home, created_at=ago(days=4), last_used_at=ago(days=3))
        self.set_token(notebook, created_at=ago(days=4), last_used_at=ago(days=3))
        self.open_kaillera(notebook)
        self.web_login()
        page = self.page("/conta/")
        self.assertIn(f"/conta/conexoes/{notebook}/revogar", page)
        self.assertNotIn(f"/conta/conexoes/{home}/revogar", page)

    def test_disconnected_pc_does_not_come_back(self):
        token = self.api_login()
        self.web_login()
        self.client.post(f"/conta/conexoes/{token}/revogar", data={"csrf_token": "csrf"})
        self.assertEqual(self.open_kaillera(token), 401)   # o DLL esquece o login (fix/conta-recusada)
        self.assertNotIn(f"/conta/conexoes/{token}/revogar", self.page("/conta/"))


class AdminKailleraInviteTest(KaillerapcsTestBase):
    """Administração > Jogadores: os mais novos primeiro, a última verificação da Conta WE Camp
    no Kaillera e o "Enviar convite do Kaillera" na mesma regra da janela (sem PC conectado)."""

    def setUp(self):
        super().setUp()
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('adm', 'x', ?, ?)",
                       (ago(days=1), ago(days=1)))
            db.execute("UPDATE players SET created_at = ? WHERE username = 'Garrincha'", (ago(days=1),))
            db.commit()
            self.ids = {r["username"]: r["id"] for r in db.execute("SELECT id, username FROM players")}
        self.admin = self.app.test_client()
        with self.admin.session_transaction() as session:
            session["csrf_token"] = "csrf"
            session["user_id"] = 1

    def mails(self):
        box = self.app.extensions.setdefault("mail_outbox", [])
        sent = list(box)
        box.clear()
        return sent

    def invite(self, username):
        return self.admin.post(f"/admin/jogadores/{self.ids[username]}/convidar-kaillera",
                               data={"csrf_token": "csrf", "voltar": "/admin/jogadores"},
                               follow_redirects=True).get_data(as_text=True)

    def card(self, page, username):
        """O pedaço da lista com o card do jogador."""
        start = page.index(f'<strong class="t-name">{username}</strong>')
        end = page.find('<div class="t-card', start)
        return page[start:end if end > 0 else len(page)]

    def button(self, username):
        return f"/admin/jogadores/{self.ids[username]}/convidar-kaillera"

    def test_newest_first_with_the_kaillera_line_and_the_button_rule(self):
        self.api_login("Pele")
        page = self.admin.get("/admin/jogadores").get_data(as_text=True)
        self.assertLess(page.index(">Garrincha</strong>"), page.index(">Pele</strong>"))  # cadastro mais novo primeiro
        # A foto (sem foto: a inicial) com o rank no canto, em cada card.
        self.assertEqual(page.count('class="player-photo"'), 2)
        self.assertIn('<span class="avatar avatar-lg avatar-initial" aria-hidden="true">P</span>', page)
        self.assertEqual(page.count('class="rank-badge rank-F"'), 2)
        pele, garrincha = self.card(page, "Pele"), self.card(page, "Garrincha")
        self.assertIn("Validação no Kaillera", pele)
        self.assertNotIn("PC conectado", pele)
        self.assertNotIn(self.button("Pele"), pele)                 # PC conectado: sem a janela, sem o botão
        self.assertIn("Nunca entrou pelo Kaillera", garrincha)
        self.assertIn(self.button("Garrincha"), garrincha)
        self.assertIn("Enviar convite do Kaillera", garrincha)

    def names(self, query):
        page = self.admin.get(f"/admin/jogadores?{query}").get_data(as_text=True)
        return sorted(n for n in ("Pele", "Garrincha") if f'<strong class="t-name">{n}</strong>' in page)

    def test_status_filter(self):
        token = self.api_login("Pele")                       # Pele: PC conectado; Garrincha: nunca entrou
        page = self.admin.get("/admin/jogadores?status=sem-pc-48h").get_data(as_text=True)
        self.assertIn("<label>Status<select name=\"status\">", page)
        self.assertIn('<option value="sem-pc-48h" selected>Não conectado em 48h</option>', page)
        self.assertEqual(self.names("status=confirmado"), ["Garrincha", "Pele"])
        self.assertEqual(self.names("status=sem-pc-48h"), ["Garrincha"])
        self.assertEqual(self.names("status=nunca-kaillera"), ["Garrincha"])
        self.assertEqual(self.names("status=nao-confirmado"), [])
        # O PC do Pele não abre o Kaillera há mais de 48 h: entra em "Não conectado em 48h".
        self.set_token(token, created_at=ago(hours=49))
        self.assertEqual(self.names("status=sem-pc-48h"), ["Garrincha", "Pele"])
        self.assertEqual(self.names("status=nunca-kaillera"), ["Garrincha"])
        # Cadastro não confirmado sai dos filtros do Kaillera; o link antigo ?confirmado= ainda vale.
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE players SET email_verified_at = NULL WHERE username = 'Garrincha'")
            db.commit()
        self.assertEqual(self.names("status=nao-confirmado"), ["Garrincha"])
        self.assertEqual(self.names("status=nunca-kaillera"), [])
        self.assertEqual(self.names("confirmado=nao"), ["Garrincha"])
        self.assertEqual(self.names("status=qualquer"), ["Garrincha", "Pele"])

    def test_pc_gone_from_the_site_brings_the_button_back(self):
        token = self.api_login("Pele")
        self.set_token(token, created_at=ago(hours=49))
        pele = self.card(self.admin.get("/admin/jogadores").get_data(as_text=True), "Pele")
        self.assertIn("Validação no Kaillera", pele)
        self.assertIn(self.button("Pele"), pele)
        self.invite("Pele")
        mail = self.mails()[0]
        self.assertIn("Faz mais de 48 horas que nenhum PC abre o Kaillera", mail.get_body(("plain",)).get_content())

    def test_invite_sends_the_card_email_and_reopens_the_popup(self):
        self.web_login("Garrincha")
        self.assertIn(POPUP, self.page())
        self.assertNotIn(POPUP, self.page())
        with self.client.session_transaction() as session:
            session["pc_popup_at"] = int(time.time()) - 60     # a janela foi vista antes do convite
        page = self.invite("Garrincha")
        self.assertIn("Convite enviado para garrincha@example.com", page)
        [mail] = self.mails()
        self.assertEqual((mail["To"], mail["Subject"]), ("garrincha@example.com", "Conecte sua Conta WE Camp no Kaillera"))
        text = mail.get_body(("plain",)).get_content()
        html = mail.get_body(("html",)).get_content()
        self.assertIn("ainda não foi conectada no Kaillera", text)
        self.assertIn("  3. Digite o e-mail (garrincha@example.com)", text)
        self.assertIn("https://we2002.example/static/images/gif/contaWeCamp.gif", text)
        self.assertIn("não é preciso responder", text)
        self.assertIn('src="https://we2002.example/static/images/gif/contaWeCamp.gif"', html)
        self.assertIn("<strong style=\"color:#ffffff;\">Garrincha</strong> e o PC aparece", html)
        self.assertIn('href="https://we2002.example/conta/#pcs"', html)
        self.assertIn(">Winning Eleven</span>", html)   # card padrão
        with self.app.app_context():
            invited = get_db().execute("SELECT kaillera_invite_at FROM players WHERE username = 'Garrincha'").fetchone()[0]
        self.assertGreater(invited, ago(minutes=1))
        # A janela volta a aparecer no site, uma vez.
        self.assertIn(POPUP, self.page())
        self.assertNotIn(POPUP, self.page())
        # O card mostra o convite; outro em menos de 2 minutos é recusado.
        card = self.card(self.admin.get("/admin/jogadores").get_data(as_text=True), "Garrincha")
        self.assertIn("Convite do Kaillera enviado", card)
        self.assertIn("Reenviar convite</button>", card)
        self.assertNotIn("Enviar convite do Kaillera", card)
        self.assertIn("há menos de 2 minutos", self.invite("Garrincha"))
        self.assertEqual(self.mails(), [])

    def test_no_invite_with_a_connected_pc_or_an_unconfirmed_account(self):
        self.api_login("Pele")
        self.assertIn("tem PC conectado no Kaillera", self.invite("Pele"))
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE players SET email_verified_at = NULL WHERE username = 'Garrincha'")
            db.commit()
        self.assertIn("ainda não confirmou o cadastro", self.invite("Garrincha"))
        self.assertNotIn(self.button("Garrincha"), self.admin.get("/admin/jogadores").get_data(as_text=True))
        self.assertEqual(self.mails(), [])
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM players WHERE kaillera_invite_at IS NOT NULL")
                             .fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
