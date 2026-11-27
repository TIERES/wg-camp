"""Cadastro de campeonato em etapas (Básico, Patch, Formato, Participantes, Premiação,
Administradores), o Gerenciar em abas, a ISO enviada junto com o cadastro e os times dos
inscritos por arrastar e soltar (store.assign_teams)."""
import io
import random
import tempfile
import unittest
from pathlib import Path

from app.db import get_db, init_db
from app.tournament import store
from app.tournament.store import TournamentError
from app.wallet import ensure_wallet
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"
ISO = b"PS1 ISO de teste" * 64


class StepsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
            db = get_db()
            self.owner, self.p1, self.p2, self.p3 = (add_player(db, f"jog{i}") for i in range(4))
            for player in (self.owner, self.p1, self.p2, self.p3):
                ensure_wallet(db, player)
            self.games = [r[0] for r in db.execute("SELECT id FROM game_titles WHERE is_active = 1 ORDER BY id")]
            self.teams = [r[0] for r in db.execute("SELECT id FROM teams WHERE is_active = 1 ORDER BY id LIMIT 8")]
            self.names = {r[0]: r[1] for r in db.execute("SELECT id, name FROM teams")}
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def client(self, player):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = CSRF
            session["player_id"] = player
        return client

    def query(self, sql, *args):
        with self.app.app_context():
            return get_db().execute(sql, args).fetchall()

    def mails(self):
        box = self.app.extensions.setdefault("mail_outbox", [])
        sent = [(m["To"], m["Subject"], m.get_body(("plain",)).get_content()) for m in box]
        box.clear()
        return sent

    def form(self, **changes):
        data = {"csrf_token": CSRF, "name": "Copa Teste", "starts_on": "2026-12-01", "ends_on": "2026-12-20",
                "rules": "Regras", "game_title_id": str(self.games[0]), "format": "knockout", "max_players": "4",
                "entry_fee": "W$ 0,00", "prize_split": "60,30,10", "ko_legs": "1", "final_legs": "1",
                "team_mode": "choose", "team_ids": [str(i) for i in self.teams[:6]]}
        data.update(changes)
        return data

    def create(self, **changes):
        response = self.client(self.owner).post("/torneios/novo", data=self.form(**changes),
                                                content_type="multipart/form-data")
        rows = self.query("SELECT * FROM tournaments WHERE name = ?", changes.get("name", "Copa Teste"))
        return response, rows[0] if rows else None

    def enroll(self, player, slug, team_id=None, team_name=""):
        self.client(player).post(f"/torneios/{slug}/inscrever", data={
            "csrf_token": CSRF, "team_id": str(team_id or ""), "team_name": team_name})

    def entries(self, tournament_id):
        return {r["player_id"]: r for r in self.query(
            "SELECT * FROM tournament_entries WHERE tournament_id = ?", tournament_id)}

    # --- Telas ---------------------------------------------------------------------

    def test_create_page_is_a_wizard_with_help_popups(self):
        page = self.client(self.owner).get("/torneios/novo").get_data(as_text=True)
        self.assertIn('data-steps="wizard"', page)
        for step in ("basico", "patch", "formato", "participantes", "premiacao", "administradores"):
            self.assertIn(f'data-step-go="{step}"', page)
            self.assertIn(f'id="etapa-{step}"', page)
        self.assertIn("data-step-next", page)
        self.assertIn('id="create-button" data-step-finish', page)
        self.assertIn('novalidate data-step-validate data-upload=', page)
        self.assertIn('<input type="file" name="iso"', page)  # a ISO junto com o cadastro
        # As vagas ficam em Participantes (os times da lista e os grupos dependem delas), antes dos times.
        vagas = page.index('name="max_players"')
        self.assertLess(page.index('id="etapa-participantes"'), vagas)
        self.assertLess(vagas, page.index('name="team_mode"'))
        self.assertLess(vagas, page.index('id="etapa-premiacao"'))
        # As explicações longas ficam nas janelas (i).
        self.assertIn('data-dialog="ajuda-grupos"', page)
        self.assertIn('id="ajuda-grupos"', page)
        self.assertIn("js/steps.js", page)

    def test_manage_has_the_same_steps_as_tabs(self):
        _, t = self.create()
        page = self.client(self.owner).get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True)
        self.assertIn('data-steps="tabs"', page)
        for step in ("basico", "patch", "formato", "participantes", "premiacao", "administradores"):
            self.assertIn(f'id="etapa-{step}"', page)
        self.assertIn('<input type="hidden" name="etapa" value="basico">', page)
        self.assertEqual(page.count('<button form="edit-tournament">Salvar alterações</button>'), 5)
        # Cada coisa na sua aba: a ISO no Patch, os inscritos nos Participantes, os ajudantes nos Administradores.
        self.assertLess(page.index('id="etapa-patch"'), page.index('id="iso"'))
        self.assertLess(page.index('id="iso"'), page.index('id="etapa-formato"'))
        self.assertLess(page.index('id="etapa-participantes"'), page.index('id="inscritos"'))
        self.assertLess(page.index('id="etapa-administradores"'), page.index('id="ajudantes"'))
        self.assertLess(page.index('id="etapa-participantes"'), page.index('name="max_players"'))
        self.assertLess(page.index('name="max_players"'), page.index('id="etapa-premiacao"'))

    def test_saving_returns_to_the_same_tab(self):
        _, t = self.create()
        response = self.client(self.owner).post(f"/torneios/{t['slug']}/editar", data=self.form(etapa="formato"))
        self.assertTrue(response.headers["Location"].endswith("#etapa-formato"))
        response = self.client(self.owner).post(f"/torneios/{t['slug']}/editar", data=self.form(etapa="x"))
        self.assertTrue(response.headers["Location"].endswith("#dados"))

    # --- ISO junto com o cadastro ------------------------------------------------------

    def test_create_with_the_iso_file(self):
        owner = self.client(self.owner)
        response = owner.post("/torneios/novo", data=self.form(iso=(io.BytesIO(ISO), "Copa Teste.bin")),
                              content_type="multipart/form-data")
        t = self.query("SELECT * FROM tournaments WHERE name = 'Copa Teste'")[0]
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/torneios/{t['slug']}/gerenciar#iso"))
        row = self.query("SELECT * FROM tournament_isos WHERE tournament_id = ?", t["id"])[0]
        self.assertEqual((row["original_filename"], row["file_size"]), ("Copa Teste.bin", len(ISO)))
        page = owner.get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True)
        self.assertIn("A ISO foi recebida e fica só neste servidor", page)  # sem as chaves do archive.org

    def test_create_refuses_a_bad_iso_without_creating(self):
        response, t = self.create(iso=(io.BytesIO(ISO), "copa.exe"))
        self.assertIsNone(t)
        self.assertIn("A ISO precisa ser", response.get_data(as_text=True))

    # --- Times dos inscritos (arrastar e soltar) -----------------------------------------

    def started(self, mode="choose", **changes):
        _, t = self.create(team_mode=mode, **changes)
        return t

    def test_organizer_assigns_swaps_and_releases_teams(self):
        t = self.started("choose")
        self.enroll(self.p1, t["slug"], self.teams[0])
        self.enroll(self.p2, t["slug"], self.teams[1])
        self.enroll(self.p3, t["slug"], self.teams[2])
        e = {p: r["id"] for p, r in self.entries(t["id"]).items()}
        self.mails()
        owner = self.client(self.owner)
        page = owner.get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True)
        self.assertIn('id="team-board"', page)
        self.assertIn(f'data-token="t{self.teams[3]}"', page)  # time da lista sem dono
        self.assertIn(f'name="time_{e[self.p1]}" value="e{e[self.p1]}"', page)
        # p1 e p2 trocam; p3 fica com um time da lista; ninguém fica com dois.
        response = owner.post(f"/torneios/{t['slug']}/times", data={
            "csrf_token": CSRF, f"time_{e[self.p1]}": f"e{e[self.p2]}", f"time_{e[self.p2]}": f"e{e[self.p1]}",
            f"time_{e[self.p3]}": f"t{self.teams[3]}"}, follow_redirects=True)
        self.assertIn("3 jogador(es) avisado(s)", response.get_data(as_text=True))
        rows = self.entries(t["id"])
        self.assertEqual((rows[self.p1]["team_id"], rows[self.p2]["team_id"], rows[self.p3]["team_id"]),
                         (self.teams[1], self.teams[0], self.teams[3]))
        mails = {to: (subject, body) for to, subject, body in self.mails()}
        self.assertIn(f"era {self.names[self.teams[2]]} e agora é {self.names[self.teams[3]]}", mails["jog3@x.com"][1])
        # Devolver para a lista: sem time, e o aviso para escolher outro.
        owner.post(f"/torneios/{t['slug']}/times", data={"csrf_token": CSRF, f"time_{e[self.p3]}": ""})
        self.assertIsNone(self.entries(t["id"])[self.p3]["team_id"])
        self.assertIn("escolha o seu time", self.mails()[0][1])
        # Time repetido e time de fora da lista: recusado, nada muda.
        for value in (f"e{e[self.p1]}", f"t{self.teams[7]}"):
            response = owner.post(f"/torneios/{t['slug']}/times", data={
                "csrf_token": CSRF, f"time_{e[self.p3]}": value}, follow_redirects=True)
            self.assertIsNone(self.entries(t["id"])[self.p3]["team_id"])
        self.assertEqual(self.client(self.p1).post(f"/torneios/{t['slug']}/times", data={
            "csrf_token": CSRF, f"time_{e[self.p1]}": ""}).status_code, 403)

    def test_free_mode_only_swaps(self):
        t = self.started("free", team_ids=[])
        self.enroll(self.p1, t["slug"], team_name="Leões")
        self.enroll(self.p2, t["slug"], team_name="Tigres")
        e = {p: r["id"] for p, r in self.entries(t["id"]).items()}
        page = self.client(self.owner).get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True)
        self.assertIn("team-board no-pool", page)
        with self.app.app_context():
            db = get_db()
            tournament = store.get_tournament(db, t["id"])
            with self.assertRaises(TournamentError):
                store.assign_teams(db, tournament, {e[self.p1]: f"t{self.teams[0]}"})
            store.assign_teams(db, tournament, {e[self.p1]: f"e{e[self.p2]}", e[self.p2]: f"e{e[self.p1]}"})
            db.commit()
        rows = self.entries(t["id"])
        self.assertEqual((rows[self.p1]["team_name"], rows[self.p2]["team_name"]), ("Tigres", "Leões"))

    def test_round_mode_has_no_team_board(self):
        t = self.started("round", team_ids=[])
        self.enroll(self.p1, t["slug"])
        page = self.client(self.owner).get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True)
        self.assertNotIn('id="team-board"', page)
        with self.app.app_context():
            db = get_db()
            with self.assertRaisesRegex(TournamentError, "no próprio jogo"):
                store.assign_teams(db, store.get_tournament(db, t["id"]), {})

    def test_draw_keeps_the_teams_set_by_the_organizer(self):
        t = self.started("draw")
        for player in (self.p1, self.p2, self.p3, self.owner):
            self.enroll(player, t["slug"])
        e = {p: r["id"] for p, r in self.entries(t["id"]).items()}
        page = self.client(self.owner).get(f"/torneios/{t['slug']}/gerenciar").get_data(as_text=True)
        self.assertIn('data-empty="Sorteado no início"', page)
        self.client(self.owner).post(f"/torneios/{t['slug']}/times", data={
            "csrf_token": CSRF, f"time_{e[self.p1]}": f"t{self.teams[5]}"})
        with self.app.app_context():
            db = get_db()
            store.start(db, store.get_tournament(db, t["id"]), random.Random(1))
            db.commit()
        rows = self.entries(t["id"])
        self.assertEqual(rows[self.p1]["team_id"], self.teams[5])
        others = [rows[p]["team_id"] for p in (self.p2, self.p3, self.owner)]
        self.assertTrue(all(others) and self.teams[5] not in others and len(set(others)) == 3)
        with self.app.app_context():
            db = get_db()
            with self.assertRaisesRegex(TournamentError, "já começou"):
                store.assign_teams(db, store.get_tournament(db, t["id"]), {})

    def test_draw_team_removed_from_the_list_goes_back_to_the_draw(self):
        t = self.started("draw")
        self.enroll(self.p1, t["slug"])
        e = self.entries(t["id"])[self.p1]["id"]
        self.client(self.owner).post(f"/torneios/{t['slug']}/times", data={
            "csrf_token": CSRF, f"time_{e}": f"t{self.teams[5]}"})
        self.mails()
        self.client(self.owner).post(f"/torneios/{t['slug']}/editar", data=self.form(
            team_mode="draw", team_ids=[str(i) for i in self.teams[:5]]))
        self.assertIsNone(self.entries(t["id"])[self.p1]["team_id"])
        self.assertIn("o seu time será sorteado no início", self.mails()[0][2])


if __name__ == "__main__":
    unittest.main()
