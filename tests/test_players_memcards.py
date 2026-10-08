import hashlib
import re
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from app import create_app
from app.db import get_db, init_db
from app.memcards import MCD_SIZE, blank_card, content_id_for_path
from app.mailer import outbox

CONTENT_ID = "1A2B3C4D:2A3B4C00"


def card_with(byte):
    data = bytearray(blank_card())
    data[8192] = byte  # dentro do bloco 1 - um "save" qualquer
    return bytes(data)


class PlayersTestBase(unittest.TestCase):
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
            "PUBLIC_URL": "https://we2002.example",
            "SPECTATE_API_KEY": "dll-key",
        })
        Path(self.app.config["MEMCARDS_DIR"]).mkdir(parents=True, exist_ok=True)
        with self.app.app_context():
            init_db()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["csrf_token"] = "csrf"

    def tearDown(self):
        self.temp.cleanup()

    def mails(self):
        return self.app.extensions.get("mail_outbox", [])

    def last_link(self, kind):
        body = self.mails()[-1].get_content()
        match = re.search(r"https://we2002\.example(/conta/%s/[^\s]+)" % kind, body)
        self.assertIsNotNone(match, body)
        return match.group(1)

    def register(self, username="Pele", email="pele@example.com", password="segredo123", confirm=True):
        response = self.client.post("/conta/cadastro", data={
            "csrf_token": "csrf", "username": username, "email": email,
            "password": password, "password_confirm": password, "accept": "on",
        })
        if confirm:
            self.client.get(self.last_link("confirmar"))
        return response

    def web_login(self, username="Pele", password="segredo123"):
        response = self.client.post("/conta/entrar", data={"csrf_token": "csrf", "username": username, "password": password})
        # O login troca o token anti-CSRF da sessão.
        with self.client.session_transaction() as session:
            session["csrf_token"] = "csrf"
        return response

    def api_login(self, username="Pele", password="segredo123"):
        response = self.client.post("/api/mc/login", data={"username": username, "password": password})
        fields = dict(line.split("=", 1) for line in response.get_data(as_text=True).splitlines())
        return response, fields

    def kv(self, response):
        return dict(line.split("=", 1) for line in response.get_data(as_text=True).splitlines() if "=" in line)


class RegistrationTest(PlayersTestBase):
    def test_register_sends_confirmation_and_blocks_login_until_confirmed(self):
        response = self.register(confirm=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Confirme seu e-mail", response.get_data(as_text=True))
        self.assertEqual(len(self.mails()), 1)
        self.assertIn("Pele", self.mails()[0].get_content())

        login = self.client.post("/conta/entrar", data={"csrf_token": "csrf", "username": "Pele", "password": "segredo123"})
        self.assertEqual(login.status_code, 302)
        self.assertIn("/conta/reenviar-confirmacao", login.headers["Location"])
        api, fields = self.api_login()
        self.assertEqual(api.status_code, 403)
        self.assertEqual(fields["error"], "not_verified")

        self.client.get(self.last_link("confirmar"))
        login = self.client.post("/conta/entrar", data={"csrf_token": "csrf", "username": "pele@example.com", "password": "segredo123"})
        self.assertEqual(login.status_code, 302)
        self.assertTrue(login.headers["Location"].endswith("/conta/"))

    def test_confirmation_link_is_single_use(self):
        self.register(confirm=False)
        link = self.last_link("confirmar")
        self.assertEqual(self.client.get(link).status_code, 302)
        self.assertEqual(self.client.get(link).status_code, 400)

    def test_duplicate_username_and_email_are_rejected_case_insensitively(self):
        self.register()
        again = self.register(username="PELE", email="outro@example.com", confirm=False)
        self.assertIn("Este usuário já está cadastrado", again.get_data(as_text=True))
        again = self.register(username="Outro", email="PELE@example.com", confirm=False)
        self.assertIn("Este e-mail já está cadastrado", again.get_data(as_text=True))

    def test_invalid_username_is_rejected(self):
        response = self.register(username="com espaco", confirm=False)
        self.assertIn("sem espaços", response.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM players").fetchone()[0], 0)

    def test_forgot_password_sends_username_and_resets_password(self):
        self.register()
        self.api_login()
        response = self.client.post("/conta/recuperar", data={"csrf_token": "csrf", "email": "PELE@example.com"})
        self.assertEqual(response.status_code, 200)
        body = self.mails()[-1].get_content()
        self.assertIn("Seu usuário é: Pele", body)

        link = self.last_link("redefinir")
        self.client.post(link, data={"csrf_token": "csrf", "password": "novasenha1", "password_confirm": "novasenha1"})
        old, _ = self.api_login(password="segredo123")
        self.assertEqual(old.status_code, 401)
        new, fields = self.api_login(username="PELE@example.com", password="novasenha1")
        self.assertEqual(new.status_code, 200)
        self.assertEqual(fields["username"], "Pele")
        self.assertEqual(fields["email"], "pele@example.com")
        with self.app.app_context():
            # O token emitido antes da troca de senha foi revogado.
            active = get_db().execute("SELECT COUNT(*) FROM player_tokens WHERE kind='api' AND used_at IS NULL").fetchone()[0]
        self.assertEqual(active, 1)
        self.assertEqual(self.client.get(link).status_code, 400)  # link de uso único

    def test_forgot_password_does_not_reveal_unknown_email(self):
        response = self.client.post("/conta/recuperar", data={"csrf_token": "csrf", "email": "ninguem@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mails(), [])

    def test_there_is_no_way_to_change_the_username(self):
        self.register()
        self.web_login()
        self.client.post("/conta/senha", data={
            "csrf_token": "csrf", "current_password": "segredo123",
            "password": "outrasenha", "password_confirm": "outrasenha", "username": "Hacker",
        })
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT username FROM players").fetchone()[0], "Pele")
        rules = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertFalse([rule for rule in rules if "usuario" in rule or "username" in rule])


class MemcardApiTest(PlayersTestBase):
    def setUp(self):
        super().setUp()
        self.register("Pele", "pele@example.com")
        self.register("Mtgamess", "mt@example.com")
        self.pele = self.api_login("Pele")[1]["token"]
        self.mt = self.api_login("Mtgamess")[1]["token"]

    def checkout(self, token, players="Pele,Mtgamess"):
        return self.client.post("/api/mc/checkout", headers={"Authorization": f"Bearer {token}"},
                                data={"content_id": CONTENT_ID.lower(), "game_name": "WE2002.bin", "players": players})

    def commit(self, token, slot_player, base, data, players="Pele,Mtgamess"):
        return self.client.post(
            "/api/mc/commit", headers={"Authorization": f"Bearer {token}"}, data=data,
            query_string={"content_id": CONTENT_ID, "slot_player": slot_player, "base_sha256": base, "players": players},
        )

    def test_blank_card_is_a_valid_formatted_ps1_card(self):
        card = blank_card()
        self.assertEqual(len(card), MCD_SIZE)
        self.assertEqual(card[:2], b"MC")
        self.assertEqual(card[127], 0x0E)
        self.assertEqual(card[128], 0xA0)
        self.assertEqual(card[128 + 127], 0xA0)

    def test_checkout_creates_blank_cards_for_both_slots(self):
        fields = self.kv(self.checkout(self.pele))
        self.assertEqual(fields["ok"], "1")
        self.assertEqual(fields["slot1_player"], "Pele")
        self.assertEqual(fields["slot2_player"], "Mtgamess")
        blank_sha = hashlib.sha256(blank_card()).hexdigest()
        self.assertEqual(fields["slot1_sha256"], blank_sha)
        self.assertEqual(fields["slot2_version"], "1")
        # Mesmo resultado para o outro jogador da partida.
        self.assertEqual(self.kv(self.checkout(self.mt))["slot1_sha256"], blank_sha)
        card = self.client.get(f"/api/mc/card/{blank_sha}", headers={"Authorization": f"Bearer {self.mt}"})
        self.assertEqual(card.data, blank_card())
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT name FROM games").fetchone()[0], "WE2002.bin")

    def test_checkout_requires_a_verified_account_for_both_slots(self):
        response = self.checkout(self.pele, players="Pele,SemConta")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.kv(response)["message"], "SemConta")

    def test_checkout_rejects_a_caller_outside_the_match(self):
        response = self.checkout(self.pele, players="Mtgamess,Outro")
        self.assertEqual(response.status_code, 400)

    def test_first_report_commits_and_records_the_sender(self):
        base = self.kv(self.checkout(self.pele))["slot2_sha256"]
        saved = card_with(0x42)

        # O host (Pele) envia o cartão do 2P sozinho - os dois PCs têm o mesmo.
        first = self.kv(self.commit(self.pele, "Mtgamess", base, saved))
        self.assertEqual(first["status"], "committed")
        self.assertEqual(first["version"], "2")
        # O outro jogador enviando o mesmo conteúdo depois não muda nada.
        second = self.kv(self.commit(self.mt, "Mtgamess", base, saved))
        self.assertEqual(second["status"], "already")
        self.assertEqual(second["version"], "2")

        fields = self.kv(self.checkout(self.pele))
        self.assertEqual(fields["slot2_sha256"], hashlib.sha256(saved).hexdigest())
        with self.app.app_context():
            note = get_db().execute("SELECT note FROM memcard_versions WHERE version = 2").fetchone()[0]
        self.assertIn("enviado por Pele", note)

    def test_divergent_second_report_is_a_conflict(self):
        base = self.kv(self.checkout(self.pele))["slot1_sha256"]
        self.assertEqual(self.kv(self.commit(self.pele, "Pele", base, card_with(0x01)))["status"], "committed")
        response = self.commit(self.mt, "Pele", base, card_with(0x02))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.kv(response)["status"], "conflict")
        fields = self.kv(self.checkout(self.pele))
        self.assertEqual(fields["slot1_sha256"], hashlib.sha256(card_with(0x01)).hexdigest())

    def test_card_changed_on_the_site_during_the_match_is_a_conflict(self):
        base = self.kv(self.checkout(self.pele))["slot1_sha256"]
        with self.app.app_context():
            from app.memcards import add_version
            db = get_db()
            add_version(db, 1, card_with(0x77), "upload", "Enviado pelo site")
            db.commit()
        response = self.commit(self.mt, "Pele", base, card_with(0x01))
        self.assertEqual(response.status_code, 409)

    def test_solo_owner_can_commit_alone(self):
        base = self.kv(self.checkout(self.pele, players="Pele"))["slot1_sha256"]
        result = self.kv(self.commit(self.pele, "Pele", base, card_with(0x07), players="Pele"))
        self.assertEqual(result["status"], "committed")

    def test_unchanged_card_is_not_a_new_version(self):
        base = self.kv(self.checkout(self.pele))["slot1_sha256"]
        self.assertEqual(self.kv(self.commit(self.pele, "Pele", base, blank_card()))["status"], "unchanged")

    def test_invalid_card_is_rejected(self):
        base = self.kv(self.checkout(self.pele))["slot1_sha256"]
        response = self.commit(self.pele, "Pele", base, b"x" * MCD_SIZE)
        self.assertEqual(response.status_code, 400)

    def test_api_requires_token(self):
        self.assertEqual(self.client.post("/api/mc/checkout", data={}).status_code, 401)
        self.assertEqual(self.client.get("/api/mc/whoami", headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(self.kv(self.client.get("/api/mc/whoami", headers={"Authorization": f"Bearer {self.pele}"}))["username"], "Pele")

    def ticket(self, token, room="1234"):
        return self.kv(self.client.post("/api/mc/ticket", headers={"Authorization": f"Bearer {token}"}, data={"room": room}))

    def verify(self, token, ticket, room="1234"):
        return self.client.post("/api/mc/verify-ticket", headers={"Authorization": f"Bearer {token}"},
                                data={"ticket": ticket, "room": room})

    def test_room_ticket_proves_the_account_once(self):
        issued = self.ticket(self.mt)
        self.assertEqual(issued["username"], "Mtgamess")
        self.assertLessEqual(len(issued["ticket"]), 32)
        first = self.verify(self.pele, issued["ticket"])
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.kv(first)["username"], "Mtgamess")
        # Uso único: quem copiar o ingresso do chat não consegue usá-lo.
        again = self.verify(self.pele, issued["ticket"])
        self.assertEqual(self.kv(again)["error"], "used_ticket")

    def test_room_ticket_is_bound_to_the_room(self):
        issued = self.ticket(self.mt, room="1234")
        self.assertEqual(self.kv(self.verify(self.pele, issued["ticket"], room="999"))["error"], "wrong_room")

    def test_room_ticket_rejects_garbage_and_expired(self):
        self.assertEqual(self.kv(self.verify(self.pele, "nao-existe"))["error"], "invalid_ticket")
        issued = self.ticket(self.mt)
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE room_tickets SET expires_at = datetime('now', '-1 minute')")
            db.commit()
        self.assertEqual(self.kv(self.verify(self.pele, issued["ticket"]))["error"], "expired_ticket")

    def test_room_ticket_endpoints_require_login(self):
        self.assertEqual(self.client.post("/api/mc/ticket", data={"room": "1"}).status_code, 401)
        self.assertEqual(self.client.post("/api/mc/verify-ticket", data={"ticket": "x", "room": "1"}).status_code, 401)

    def test_spectators_can_fetch_a_card_with_the_dll_key(self):
        sha = self.kv(self.checkout(self.pele))["slot1_sha256"]
        self.assertEqual(self.client.get(f"/api/mc/card/{sha}").status_code, 401)
        self.assertEqual(self.client.get(f"/api/mc/card/{sha}", headers={"X-Api-Key": "dll-key"}).data, blank_card())


class AccountPagesTest(PlayersTestBase):
    def setUp(self):
        super().setUp()
        self.register()
        self.web_login()
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO games (content_id, name, created_at, updated_at) VALUES (?, 'WE2002', 'x', 'x')", (CONTENT_ID,))
            db.commit()

    def test_upload_download_and_restore(self):
        saved = card_with(0x55)
        response = self.client.post("/conta/memory-cards/enviar", data={
            "csrf_token": "csrf", "game_id": "1", "file": (BytesIO(saved), "we2002_1p.srm"),
        })
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/conta/").get_data(as_text=True)
        self.assertIn("WE2002", page)

        self.assertEqual(self.client.get("/conta/memory-cards/1/baixar").data, saved)
        self.assertEqual(self.client.get("/conta/memory-cards/1/baixar?versao=1").data, blank_card())

        self.client.post("/conta/memory-cards/1/restaurar", data={"csrf_token": "csrf", "version": "1"})
        self.assertEqual(self.client.get("/conta/memory-cards/1/baixar").data, blank_card())
        history = self.client.get("/conta/memory-cards/1").get_data(as_text=True)
        self.assertIn("Restaurada a versão 1", history)

    def test_upload_rejects_non_card_files(self):
        self.client.post("/conta/memory-cards/enviar", data={
            "csrf_token": "csrf", "game_id": "1", "file": (BytesIO(b"abc"), "x.mcd"),
        })
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM memcards").fetchone()[0], 0)

    def test_cannot_see_another_players_card(self):
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO players (username, email, password_hash, created_at, updated_at) VALUES ('X','x@x.com','h','x','x')")
            db.execute("INSERT INTO memcards (player_id, game_id, created_at, updated_at) VALUES (2, 1, 'x', 'x')")
            db.commit()
        self.assertEqual(self.client.get("/conta/memory-cards/1").status_code, 404)

    def test_account_page_requires_login(self):
        other = self.app.test_client()
        self.assertEqual(other.get("/conta/").status_code, 302)


class ContentIdTest(unittest.TestCase):
    def test_cue_resolves_to_the_bin_like_retroarch(self):
        import zlib
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "game.bin").write_bytes(b"abc" * 1000)
            (folder / "game.cue").write_text('FILE "game.bin" BINARY\n  TRACK 01 MODE2/2352\n')
            expected = f"{zlib.crc32(b'abc' * 1000):08X}:{3000:X}"
            self.assertEqual(content_id_for_path(folder / "game.cue"), expected)
            self.assertEqual(content_id_for_path(folder / "game.bin"), expected)


if __name__ == "__main__":
    unittest.main()
