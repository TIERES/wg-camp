import hashlib
import re
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from app import create_app
from app.db import get_db, init_db
from app.memcards import MCD_SIZE, blank_card, content_id_for_path
from app.mailer import outbox

CONTENT_ID = "1A2B3C4D:2A3B4C00"


def zip_entries(response):
    with zipfile.ZipFile(BytesIO(response.data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def downloaded_card(response):
    cards = [data for name, data in zip_entries(response).items() if name.endswith(".srm")]
    assert len(cards) == 1, cards
    return cards[0]


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

        self.assertEqual(downloaded_card(self.client.get("/conta/memory-cards/1/baixar")), saved)
        self.assertEqual(downloaded_card(self.client.get("/conta/memory-cards/1/baixar?versao=1")), blank_card())

        self.client.post("/conta/memory-cards/1/restaurar", data={"csrf_token": "csrf", "version": "1"})
        self.assertEqual(downloaded_card(self.client.get("/conta/memory-cards/1/baixar")), blank_card())
        history = self.client.get("/conta/memory-cards/1").get_data(as_text=True)
        self.assertIn("Restaurada a versão 1", history)

    def test_download_is_a_zip_ready_to_extract_into_retroarch(self):
        response = self.client.post("/conta/memory-cards/enviar", data={
            "csrf_token": "csrf", "game_id": "1", "file": (BytesIO(card_with(0x55)), "we2002_1p.srm"),
        })
        self.assertEqual(response.status_code, 302)
        response = self.client.get("/conta/memory-cards/1/baixar")
        self.assertEqual(response.mimetype, "application/zip")
        self.assertIn(".zip", response.headers["Content-Disposition"])
        entries = zip_entries(response)
        self.assertEqual(sorted(entries), ["LEIA-ME.txt", "saves/PCSX-ReARMed/WE2002.srm"])
        self.assertEqual(entries["saves/PCSX-ReARMed/WE2002.srm"], card_with(0x55))
        readme = entries["LEIA-ME.txt"].decode("utf-8-sig")
        self.assertIn("RetroArch-1.16.0.FFW.TIERES.0.x", readme)
        self.assertIn("substituir os arquivos, se necessário.", readme)

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


class ChampionshipIsosTest(PlayersTestBase):
    """"Enviar um Memory Card" lista as ISOs identificadas dos campeonatos publicados."""

    def setUp(self):
        super().setUp()
        self.register()
        self.web_login()
        with self.app.app_context():
            db = get_db()
            championships = [
                # id, nome, publicado, início, (tipo, arquivo, content_id, arquivo publicado)
                (1, "Master Liga 12", 1, "2026-09-01", ("iso", "MasterLeague12.bin", "AAAA1111:1C478C60", 1)),
                (2, "Copa Antiga", 1, "2026-01-01", ("iso", "WE2002.bin", CONTENT_ID, 1)),
                (3, "Rascunho", 0, "2026-10-01", ("iso", "Rascunho.bin", "BBBB2222:10", 1)),
                (4, "Arquivo Oculto", 1, "2026-10-01", ("iso", "Oculto.bin", "CCCC3333:10", 0)),
                (5, "So Patch", 1, "2026-10-01", ("patch", "Patch.bin", "DDDD4444:10", 1)),
                (6, "Compactada", 1, "2026-10-01", ("iso", "Compactada.7z", None, 1)),
            ]
            for cid, name, published, start, (file_type, filename, content_id, file_published) in championships:
                db.execute("INSERT INTO championships (id, slug, name, is_published, start_date, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, 'x', 'x')", (cid, f"c{cid}", name, published, start))
                db.execute("INSERT INTO files (championship_id, display_name, stored_name, original_filename, file_type, "
                           "file_size, content_id, is_published, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, 'x', 'x')",
                           (cid, filename, f"{cid:032x}.bin", filename, file_type, content_id, file_published))
            db.execute("INSERT INTO games (content_id, name, created_at, updated_at) VALUES (?, 'WE2002 da partida', 'x', 'x')", (CONTENT_ID,))
            db.execute("INSERT INTO games (content_id, name, created_at, updated_at) VALUES ('EEEE5555:10', 'Outro Jogo', 'x', 'x')")
            db.commit()

    def upload(self, content_id, data):
        return self.client.post("/conta/memory-cards/enviar", data={
            "csrf_token": "csrf", "content_id": content_id, "file": (BytesIO(data), "cartao.mcd"),
        })

    def card_for(self, content_id, data):
        self.assertEqual(self.upload(content_id, data).status_code, 302)
        with self.app.app_context():
            return get_db().execute("SELECT m.id FROM memcards m JOIN games g ON g.id = m.game_id "
                                    "WHERE g.content_id = ?", (content_id,)).fetchone()["id"]

    def test_srm_takes_the_championship_iso_name_over_the_match_name(self):
        card = self.card_for(CONTENT_ID, card_with(0x11))  # games.name = "WE2002 da partida"
        entries = zip_entries(self.client.get(f"/conta/memory-cards/{card}/baixar"))
        self.assertEqual(entries["saves/PCSX-ReARMed/WE2002.srm"], card_with(0x11))

    def test_srm_of_a_game_without_iso_uses_its_name_without_extension(self):
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE games SET name = 'BR Turbo 2025 by Prof Denis Erivelton.bin' WHERE content_id = 'EEEE5555:10'")
            db.commit()
        card = self.card_for("EEEE5555:10", card_with(0x22))
        entries = zip_entries(self.client.get(f"/conta/memory-cards/{card}/baixar"))
        self.assertEqual(entries["saves/PCSX-ReARMed/BR Turbo 2025 by Prof Denis Erivelton.srm"], card_with(0x22))

    def test_account_lists_isos_of_published_championships(self):
        page = self.client.get("/conta/").get_data(as_text=True)
        master = page.index("Master Liga 12 — MasterLeague12.bin")
        copa = page.index("Copa Antiga — WE2002.bin")
        self.assertLess(master, copa)  # campeonatos mais novos primeiro
        self.assertLess(copa, page.index('label="Outros jogos"'))
        self.assertIn("Outro Jogo", page)
        # O jogo já cadastrado com o conteúdo de uma ISO de campeonato não se repete em "Outros jogos".
        self.assertEqual(page.count(f'value="{CONTENT_ID}"'), 1)
        self.assertNotIn("WE2002 da partida", page)
        for hidden in ("Rascunho", "Oculto.bin", "Patch.bin", "Compactada"):
            self.assertNotIn(hidden, page)

    def test_upload_to_a_championship_iso_creates_its_game(self):
        saved = card_with(0x66)
        self.assertEqual(self.upload("aaaa1111:1c478c60", saved).status_code, 302)
        with self.app.app_context():
            game = get_db().execute("SELECT * FROM games WHERE content_id = 'AAAA1111:1C478C60'").fetchone()
            self.assertEqual(game["name"], "MasterLeague12.bin")
            card = get_db().execute("SELECT * FROM memcards WHERE game_id = ?", (game["id"],)).fetchone()
        response = self.client.get(f"/conta/memory-cards/{card['id']}/baixar")
        self.assertEqual(zip_entries(response)["saves/PCSX-ReARMed/MasterLeague12.srm"], saved)
        # A partida com essa ISO usa o cartão enviado.
        _, fields = self.api_login()
        response = self.client.post("/api/mc/checkout", headers={"Authorization": f"Bearer {fields['token']}"}, data={
            "content_id": "AAAA1111:1C478C60", "game_name": "MasterLeague12.bin", "players": "Pele",
        })
        self.assertEqual(self.kv(response)["slot1_sha256"], hashlib.sha256(saved).hexdigest())

    def test_upload_to_an_iso_already_played_uses_the_existing_game(self):
        self.assertEqual(self.upload(CONTENT_ID, card_with(0x67)).status_code, 302)
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM games").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT g.name FROM memcards m JOIN games g ON g.id = m.game_id").fetchone()[0],
                             "WE2002 da partida")

    def test_upload_rejects_isos_not_listed(self):
        for content_id in ("BBBB2222:10", "CCCC3333:10", "DDDD4444:10", "lixo"):
            self.upload(content_id, card_with(0x68))
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memcards").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM games").fetchone()[0], 2)


class OneAccountOnlineTest(PlayersTestBase):
    """M. Card Online com só um jogador com conta: o outro slot fica vazio."""

    def setUp(self):
        super().setUp()
        self.register("Pele", "pele@example.com")
        self.pele = self.api_login("Pele")[1]["token"]

    def checkout(self, headers, players="SemConta,Pele", accounts="Pele"):
        return self.client.post("/api/mc/checkout", headers=headers, data={
            "content_id": CONTENT_ID, "game_name": "WE2002.bin", "players": players, "accounts": accounts})

    def test_player_without_account_gets_an_empty_slot_on_every_pc(self):
        logged = self.kv(self.checkout({"Authorization": f"Bearer {self.pele}"}))
        self.assertEqual(logged["slots"], "2")
        self.assertEqual((logged["slot1_player"], logged["slot1_sha256"]), ("", ""))
        self.assertEqual(logged["slot2_player"], "Pele")
        self.assertEqual(logged["slot2_sha256"], hashlib.sha256(blank_card()).hexdigest())
        # O PC sem login (host ou convidado) pede com a chave do DLL e recebe o mesmo.
        guest = self.kv(self.checkout({"X-Api-Key": "dll-key"}))
        self.assertEqual(guest, logged)
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM memcards").fetchone()[0], 1)

    def test_a_registered_player_not_confirmed_in_the_room_also_gets_an_empty_slot(self):
        # "Pele" tem conta, mas quem está com esse nick na sala não provou que é ele.
        fields = self.kv(self.checkout({"X-Api-Key": "dll-key"}, players="Pele,SemConta", accounts=""))
        self.assertEqual(fields["error"], "no_account")
        self.register("Mtgamess", "mt@example.com")
        fields = self.kv(self.checkout({"X-Api-Key": "dll-key"}, players="Pele,Mtgamess", accounts="Mtgamess"))
        self.assertEqual(fields["slot1_sha256"], "")
        self.assertEqual(fields["slot2_player"], "Mtgamess")

    def test_only_the_player_with_the_account_saves_its_card(self):
        base = self.kv(self.checkout({"Authorization": f"Bearer {self.pele}"}))["slot2_sha256"]
        saved = card_with(0x21)
        result = self.client.post("/api/mc/commit", headers={"Authorization": f"Bearer {self.pele}"}, data=saved,
                                  query_string={"content_id": CONTENT_ID, "slot_player": "Pele", "base_sha256": base,
                                                "players": "SemConta,Pele"})
        self.assertEqual(self.kv(result)["status"], "committed")
        # Sem token não se grava o M. Card Online (só o da ISO aceita a chave).
        keyed = self.client.post("/api/mc/commit", headers={"X-Api-Key": "dll-key"}, data=card_with(0x22),
                                 query_string={"content_id": CONTENT_ID, "slot_player": "Pele", "base_sha256": base,
                                               "players": "SemConta,Pele", "reporter": "SemConta"})
        self.assertEqual(keyed.status_code, 401)

    def test_old_dll_without_accounts_still_needs_both_accounts(self):
        response = self.client.post("/api/mc/checkout", headers={"Authorization": f"Bearer {self.pele}"}, data={
            "content_id": CONTENT_ID, "players": "SemConta,Pele"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.kv(response)["error"], "missing_account")
        keyed = self.client.post("/api/mc/checkout", headers={"X-Api-Key": "dll-key"}, data={
            "content_id": CONTENT_ID, "players": "SemConta,Pele"})
        self.assertEqual(keyed.status_code, 401)

    def test_host_without_account_verifies_the_guests_ticket_with_the_dll_key(self):
        ticket = self.kv(self.client.post("/api/mc/ticket", headers={"Authorization": f"Bearer {self.pele}"},
                                          data={"room": "77"}))["ticket"]
        self.assertEqual(self.client.post("/api/mc/verify-ticket", data={"ticket": ticket, "room": "77"}).status_code, 401)
        verified = self.client.post("/api/mc/verify-ticket", headers={"X-Api-Key": "dll-key"},
                                    data={"ticket": ticket, "room": "77"})
        self.assertEqual(self.kv(verified)["username"], "Pele")


class IsoMemcardTest(PlayersTestBase):
    """"M. Card da ISO": um cartão só do jogo, o mesmo para todos da sala."""

    def setUp(self):
        super().setUp()
        self.register("Pele", "pele@example.com")
        self.pele = self.api_login("Pele")[1]["token"]

    def checkout(self, headers, players="Pele,Convidado", content_id=CONTENT_ID):
        return self.client.post("/api/mc/checkout", headers=headers, data={
            "mode": "iso", "content_id": content_id, "game_name": "WE2002.bin", "players": players})

    def commit(self, headers, base, data, players="Pele,Convidado", reporter=""):
        return self.client.post("/api/mc/commit", headers=headers, data=data, query_string={
            "mode": "iso", "content_id": CONTENT_ID, "base_sha256": base, "players": players, "reporter": reporter})

    def host(self):
        return {"Authorization": f"Bearer {self.pele}"}

    def key(self):
        return {"X-Api-Key": "dll-key"}

    def test_host_and_guest_without_account_get_the_same_single_card(self):
        host = self.kv(self.checkout(self.host()))
        self.assertEqual(host["mode"], "iso")
        self.assertEqual(host["slots"], "1")
        self.assertEqual(host["slot1_player"], "ISO")
        self.assertEqual(host["slot1_sha256"], hashlib.sha256(blank_card()).hexdigest())
        self.assertNotIn("slot2_sha256", host)
        guest = self.kv(self.checkout(self.key()))
        self.assertEqual(guest["slot1_sha256"], host["slot1_sha256"])
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memcards").fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT player_id FROM memcards").fetchone()[0])

    def test_iso_card_is_not_any_players_card(self):
        self.checkout(self.host())
        players_card = self.client.post("/api/mc/checkout", headers=self.host(), data={
            "content_id": CONTENT_ID, "game_name": "WE2002.bin", "players": "Pele"})
        self.assertEqual(self.kv(players_card)["slot1_player"], "Pele")
        with self.app.app_context():
            ids = [row[0] for row in get_db().execute("SELECT id FROM memcards ORDER BY player_id IS NULL")]
        self.assertEqual(len(ids), 2)
        self.web_login()
        self.assertEqual(self.client.get(f"/conta/memory-cards/{ids[0]}").status_code, 200)
        self.assertEqual(self.client.get(f"/conta/memory-cards/{ids[1]}").status_code, 404)

    def test_host_commits_and_the_next_match_starts_from_it(self):
        base = self.kv(self.checkout(self.host()))["slot1_sha256"]
        saved = card_with(0x31)
        first = self.kv(self.commit(self.host(), base, saved))
        self.assertEqual(first["status"], "committed")
        self.assertEqual(first["version"], "2")
        # O convidado (sem conta) enviando depois o mesmo cartão não muda nada.
        self.assertEqual(self.kv(self.commit(self.key(), base, saved, reporter="Convidado"))["status"], "already")
        self.assertEqual(self.kv(self.checkout(self.key()))["slot1_sha256"], hashlib.sha256(saved).hexdigest())
        with self.app.app_context():
            note = get_db().execute("SELECT note FROM memcard_versions WHERE version = 2").fetchone()[0]
        self.assertIn("enviado por Pele", note)

    def test_guest_saves_when_the_host_did_not(self):
        base = self.kv(self.checkout(self.key()))["slot1_sha256"]
        result = self.kv(self.commit(self.key(), base, card_with(0x32), reporter="Convidado"))
        self.assertEqual(result["status"], "committed")
        with self.app.app_context():
            note = get_db().execute("SELECT note FROM memcard_versions WHERE version = 2").fetchone()[0]
        self.assertIn("enviado por Convidado", note)

    def test_two_matches_at_once_do_not_overwrite_each_other(self):
        base = self.kv(self.checkout(self.host()))["slot1_sha256"]
        other_base = self.kv(self.checkout(self.key(), players="A,B"))["slot1_sha256"]
        self.assertEqual(base, other_base)
        self.assertEqual(self.kv(self.commit(self.host(), base, card_with(0x01)))["status"], "committed")
        late = self.commit(self.key(), other_base, card_with(0x02), players="A,B", reporter="A")
        self.assertEqual(late.status_code, 409)
        self.assertEqual(self.kv(late)["status"], "conflict")

    def test_key_only_works_for_the_iso_mode_and_needs_a_reporter_in_the_match(self):
        normal = self.client.post("/api/mc/checkout", headers=self.key(), data={
            "content_id": CONTENT_ID, "players": "Pele,Convidado"})
        self.assertEqual(normal.status_code, 401)
        self.assertEqual(self.checkout({}).status_code, 401)
        base = self.kv(self.checkout(self.key()))["slot1_sha256"]
        self.assertEqual(self.commit(self.key(), base, card_with(0x03), reporter="Intruso").status_code, 400)
        self.assertEqual(self.commit({"X-Api-Key": "errada"}, base, card_with(0x03), reporter="Convidado").status_code, 401)
        # Com token, quem envia tem de estar na partida.
        self.assertEqual(self.commit(self.host(), base, card_with(0x03), players="A,B").status_code, 400)

    def test_replays_fetch_the_iso_card_versions(self):
        base = self.kv(self.checkout(self.host()))["slot1_sha256"]
        self.commit(self.host(), base, card_with(0x44))
        sha = hashlib.sha256(card_with(0x44)).hexdigest()
        self.assertEqual(self.client.get(f"/api/mc/card/{sha}", headers=self.key()).data, card_with(0x44))
        self.assertEqual(self.client.get(f"/api/mc/card/{base}", headers=self.key()).data, blank_card())


class AdminIsoMemcardTest(PlayersTestBase):
    def setUp(self):
        super().setUp()
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO championships (id, slug, name, is_published, start_date, created_at, updated_at) "
                       "VALUES (1, 'ml', 'Master Liga 12', 1, '2026-09-01', 'x', 'x')")
            db.execute("INSERT INTO files (championship_id, display_name, stored_name, original_filename, file_type, "
                       "file_size, content_id, is_published, created_at, updated_at) "
                       "VALUES (1, 'ML12', 'a.bin', 'MasterLeague12.bin', 'iso', 1, 'AAAA1111:1C478C60', 1, 'x', 'x')")
            db.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = 1

    def upload(self, data, content_id="AAAA1111:1C478C60"):
        return self.client.post(f"/admin/jogos/{content_id}/memory-card/enviar", data={
            "csrf_token": "csrf", "file": (BytesIO(data), "ml.srm")})

    def test_upload_for_a_championship_iso_creates_the_game_and_the_card(self):
        page = self.client.get("/admin/jogos").get_data(as_text=True)
        self.assertIn("/admin/jogos/AAAA1111:1C478C60/memory-card", page)
        self.assertIn("ainda não tem M. Card da ISO", self.client.get("/admin/jogos/AAAA1111:1C478C60/memory-card")
                      .get_data(as_text=True))
        self.assertEqual(self.upload(card_with(0x10)).status_code, 302)
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT name FROM games").fetchone()[0], "MasterLeague12.bin")
            # A versão 1 já é o cartão enviado, mais a formatada antes dela.
            row = db.execute("SELECT current_version, player_id FROM memcards").fetchone()
            self.assertEqual(tuple(row), (2, None))
        self.register()
        token = self.api_login()[1]["token"]
        fields = self.kv(self.client.post("/api/mc/checkout", headers={"Authorization": f"Bearer {token}"}, data={
            "mode": "iso", "content_id": "AAAA1111:1C478C60", "players": "Pele"}))
        self.assertEqual(fields["slot1_sha256"], hashlib.sha256(card_with(0x10)).hexdigest())

    def test_download_and_restore(self):
        self.upload(card_with(0x10))
        self.upload(card_with(0x20))
        entries = zip_entries(self.client.get("/admin/jogos/AAAA1111:1C478C60/memory-card/baixar?versao=2"))
        self.assertEqual(entries["saves/PCSX-ReARMed/MasterLeague12.srm"], card_with(0x10))
        self.client.post("/admin/jogos/AAAA1111:1C478C60/memory-card/restaurar", data={"csrf_token": "csrf", "version": 2})
        entries = zip_entries(self.client.get("/admin/jogos/AAAA1111:1C478C60/memory-card/baixar"))
        self.assertEqual(entries["saves/PCSX-ReARMed/MasterLeague12.srm"], card_with(0x10))
        history = self.client.get("/admin/jogos/AAAA1111:1C478C60/memory-card").get_data(as_text=True)
        self.assertIn("Restaurada a versão 2", history)

    def test_requires_admin_and_a_known_iso(self):
        self.assertEqual(self.upload(b"x" * MCD_SIZE).status_code, 302)  # cartão inválido: volta com erro
        self.assertEqual(self.client.get("/admin/jogos/FFFF0000:10/memory-card").status_code, 404)
        with self.client.session_transaction() as session:
            session.pop("user_id")
        self.assertEqual(self.client.get("/admin/jogos/AAAA1111:1C478C60/memory-card").status_code, 302)
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM memcards").fetchone()[0], 0)


class MigrationTest(unittest.TestCase):
    def test_old_memcards_table_is_rebuilt_keeping_cards_and_versions(self):
        import sqlite3
        from app.db import migrate_db
        with tempfile.TemporaryDirectory() as folder:
            app = create_app({"TESTING": True, "SECRET_KEY": "t", "DATABASE": str(Path(folder) / "a.db"),
                              "DOWNLOADS_DIR": str(Path(folder) / "d"), "UPLOAD_TMP_DIR": str(Path(folder) / "t"),
                              "MEMCARDS_DIR": str(Path(folder) / "mc")})
            with app.app_context():
                init_db()
                db = get_db()
                db.executescript("""
                    DROP INDEX memcards_iso_idx;
                    DROP TABLE memcard_versions;
                    DROP TABLE memcards;
                    CREATE TABLE memcards (id INTEGER PRIMARY KEY,
                        player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
                        game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE RESTRICT,
                        current_version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL, UNIQUE (player_id, game_id));
                    CREATE TABLE memcard_versions (id INTEGER PRIMARY KEY,
                        memcard_id INTEGER NOT NULL REFERENCES memcards(id) ON DELETE CASCADE,
                        version INTEGER NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                        source TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                        UNIQUE (memcard_id, version));
                    INSERT INTO players (id, username, email, password_hash, created_at, updated_at)
                        VALUES (7, 'Pele', 'p@x', 'h', 'x', 'x');
                    INSERT INTO games (id, content_id, name, created_at, updated_at) VALUES (3, 'AA:1', 'WE', 'x', 'x');
                    INSERT INTO memcards (id, player_id, game_id, current_version, created_at, updated_at)
                        VALUES (5, 7, 3, 2, 'x', 'x');
                    INSERT INTO memcard_versions (memcard_id, version, sha256, size, source, created_at)
                        VALUES (5, 1, 'a', 1, 'blank', 'x'), (5, 2, 'b', 1, 'match', 'x');
                """)
                db.commit()
                migrate_db()
                migrate_db()  # idempotente
                db = get_db()
                columns = {c["name"]: c for c in db.execute("PRAGMA table_info(memcards)")}
                self.assertEqual(columns["player_id"]["notnull"], 0)
                self.assertEqual(tuple(db.execute("SELECT id, player_id, current_version FROM memcards").fetchone()),
                                 (5, 7, 2))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM memcard_versions WHERE memcard_id = 5").fetchone()[0], 2)
                db.execute("INSERT INTO memcards (player_id, game_id, created_at, updated_at) VALUES (NULL, 3, 'x', 'x')")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO memcards (player_id, game_id, created_at, updated_at) VALUES (NULL, 3, 'x', 'x')")
                # Apagar o jogador ainda apaga o cartão dele e as versões (ON DELETE CASCADE).
                db.execute("DELETE FROM players WHERE id = 7")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM memcard_versions").fetchone()[0], 0)


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
