"""Editar os dados básicos do campeonato em Gerenciar antes do início
(tournaments.edit / store.update_before_start): o que acontece com quem já está
inscrito, os e-mails e o "escolha o seu time"."""
import tempfile
import unittest
from pathlib import Path

from app.db import get_db, init_db
from app.tournament import friends
from app.wallet import ensure_wallet
from tests.test_tournament_store import add_player, make_app

CSRF = "test-csrf"


class TournamentEditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        with self.app.app_context():
            init_db()
            db = get_db()
            self.owner, self.p1, self.p2, self.p3 = (add_player(db, f"jog{i}") for i in range(4))
            for player in (self.owner, self.p1, self.p2, self.p3):
                ensure_wallet(db, player)  # o saldo inicial, antes de comparar
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
        sent = [(m["To"], m["Subject"], m.get_content()) for m in box]
        box.clear()
        return sent

    def wallet(self, player):
        return self.query("SELECT wallet_cents FROM players WHERE id = ?", player)[0][0]

    def form(self, **changes):
        """O formulário de criar/editar (como a página manda): mata-mata de 4, inscrição de W$10,
        times escolhidos de uma lista de 6."""
        data = {"csrf_token": CSRF, "name": "Copa Teste", "starts_on": "2026-12-01", "ends_on": "2026-12-20",
                "rules": "Regras", "game_title_id": str(self.games[0]), "format": "knockout", "max_players": "4",
                "entry_fee": "W$ 10,00", "prize_split": "60,30,10", "ko_legs": "1", "final_legs": "1",
                "team_mode": "choose", "team_ids": [str(i) for i in self.teams[:6]]}
        data.update(changes)
        return data

    def create(self, **changes):
        self.client(self.owner).post("/torneios/novo", data=self.form(**changes))
        return self.query("SELECT * FROM tournaments WHERE name = ?", changes.get("name", "Copa Teste"))[0]

    def enroll(self, player, slug, team_id):
        self.client(player).post(f"/torneios/{slug}/inscrever", data={"csrf_token": CSRF, "team_id": str(team_id)})

    def edit(self, slug, **changes):
        return self.client(self.owner).post(f"/torneios/{slug}/editar", data=self.form(**changes), follow_redirects=True)

    def entries(self, tournament_id):
        return {r["player_id"]: r for r in self.query("SELECT * FROM tournament_entries WHERE tournament_id = ?",
                                                       tournament_id)}

    def test_everything_from_create_is_editable_before_start(self):
        t = self.create()
        page = self.client(self.owner).get(f"/torneios/{t['slug']}/gerenciar").data.decode()
        # O formulário de criar com os dados atuais.
        self.assertIn("Salvar alterações", page)
        self.assertIn(f'name="game_title_id" value="{self.games[0]}" required checked', page)
        self.assertIn('value="W$10,00"', page)
        self.assertIn(f'name="team_ids" value="{self.teams[0]}" checked', page)
        self.assertIn('"format": "knockout"', page)  # o JavaScript preenche o formato com os dados atuais
        response = self.edit(t["slug"], name="Copa Nova", starts_on="2026-12-05", ends_on="2027-01-10",
                             game_title_id=str(self.games[1]), format="world_cup", max_players="8", groups="2",
                             qualifiers="2", group_legs="2", ko_legs="2", final_legs="2", third_place="on",
                             entry_fee="W$ 0,00", team_mode="draw", team_ids=[str(i) for i in self.teams[:8]])
        self.assertIn("Campeonato atualizado.", response.data.decode())
        row = self.query("SELECT * FROM tournaments WHERE id = ?", t["id"])[0]
        self.assertEqual((row["name"], row["starts_on"], row["ends_on"], row["game_title_id"], row["format"],
                          row["max_players"], row["entry_fee_cents"], row["team_mode"]),
                         ("Copa Nova", "2026-12-05", "2027-01-10", self.games[1], "world_cup", 8, 0, "draw"))
        self.assertIn('"group_legs": 2', row["settings_json"])
        self.assertIn('"final_legs": 2', row["settings_json"])
        self.assertIn('"third_place": true', row["settings_json"])
        self.assertEqual(len(self.query("SELECT 1 FROM tournament_teams WHERE tournament_id = ?", t["id"])), 8)
        self.assertEqual(self.mails(), [])  # ninguém inscrito: nenhum aviso

    def test_team_picker_has_csv_import_and_chosen_only(self):
        """Criar e editar: importar CSV, "Somente times escolhidos" e a contagem (o resto é no navegador)."""
        t = self.create()
        for url in ("/torneios/novo", f"/torneios/{t['slug']}/gerenciar"):
            page = self.client(self.owner).get(url).data.decode()
            self.assertIn('id="team-csv" accept=".csv,.txt,text/csv,text/plain"', page)
            self.assertIn("Somente times escolhidos", page)
            self.assertIn('Times escolhidos: <strong id="team-count">', page)
            self.assertIn('id="team-chosen"', page)
            self.assertIn('id="csv-missing"', page)

    def test_fee_change_cancels_entries_with_refund_and_email(self):
        t = self.create()
        before = {p: self.wallet(p) for p in (self.p1, self.p2)}
        self.enroll(self.p1, t["slug"], self.teams[0])
        self.enroll(self.p2, t["slug"], self.teams[1])
        self.assertEqual(self.wallet(self.p1), before[self.p1] - 1000)
        self.mails()
        page = self.client(self.owner).get(f"/torneios/{t['slug']}/gerenciar").data.decode()
        self.assertIn("Mudar o valor da inscrição cancela as 2 inscrições feitas", page)
        response = self.edit(t["slug"], entry_fee="W$ 20,00").data.decode()
        self.assertIn("2 inscrito(s) avisado(s) por e-mail", response)
        self.assertEqual(self.entries(t["id"]), {})
        self.assertEqual(self.wallet(self.p1), before[self.p1])  # devolvido o que pagou (W$10)
        self.assertEqual(self.wallet(self.p2), before[self.p2])
        mails = self.mails()
        self.assertEqual(sorted(m[0] for m in mails), ["jog1@x.com", "jog2@x.com"])
        self.assertEqual(mails[0][1], "Copa Teste: sua inscrição foi cancelada")
        self.assertIn("mudou de W$10,00 para W$20,00", mails[0][2])
        self.assertIn("O valor pago (W$10,00) voltou para a sua carteira W$.", mails[0][2])
        self.assertIn(f"/torneios/{t['slug']}?aba=geral", mails[0][2])
        # Podem se inscrever de novo, pelo valor novo.
        self.enroll(self.p1, t["slug"], self.teams[0])
        self.assertEqual(self.wallet(self.p1), before[self.p1] - 2000)

    def test_friends_only_removes_who_is_not_a_friend(self):
        with self.app.app_context():
            db = get_db()
            friends.request(db, self.owner, self.p1)
            friends.respond(db, self.p1, self.owner, True)
            db.commit()
        t = self.create()
        before = self.wallet(self.p2)
        self.enroll(self.p1, t["slug"], self.teams[0])
        self.enroll(self.p2, t["slug"], self.teams[1])
        self.mails()
        self.edit(t["slug"], friends_only="on")
        self.assertEqual(list(self.entries(t["id"])), [self.p1])  # o amigo continua
        self.assertEqual(self.wallet(self.p2), before)
        mails = self.mails()
        self.assertEqual([m[0] for m in mails], ["jog2@x.com"])
        self.assertIn("passou a ser somente para amigos", mails[0][2])

    def test_removed_team_asks_to_choose_another_before_start(self):
        t = self.create(max_players="2", team_ids=[str(i) for i in self.teams[:4]])
        self.enroll(self.p1, t["slug"], self.teams[0])
        self.enroll(self.p2, t["slug"], self.teams[1])
        self.mails()
        # Tira o time do jog1 da lista (e põe outros): ele continua inscrito, sem time.
        self.edit(t["slug"], max_players="2", team_ids=[str(i) for i in self.teams[1:6]])
        entries = self.entries(t["id"])
        self.assertEqual((entries[self.p1]["team_id"], entries[self.p1]["team_name"]), (None, ""))
        self.assertEqual(entries[self.p2]["team_id"], self.teams[1])
        mails = self.mails()
        self.assertEqual([m[0] for m in mails], ["jog1@x.com"])
        self.assertEqual(mails[0][1], "Copa Teste: escolha o seu time")
        self.assertIn(f"O time {self.names[self.teams[0]]}, que você escolheu, saiu da lista", mails[0][2])
        self.assertIn("sem time: avisado para escolher",
                      self.client(self.owner).get(f"/torneios/{t['slug']}/gerenciar").data.decode())
        # Sem time, o campeonato não começa.
        response = self.client(self.owner).post(f"/torneios/{t['slug']}/iniciar", data={"csrf_token": CSRF},
                                                follow_redirects=True).data.decode()
        self.assertIn("Ainda sem time: jog1", response)
        self.assertEqual(self.query("SELECT status FROM tournaments WHERE id = ?", t["id"])[0][0], "registration")
        # O jogador escolhe outro na página do campeonato (o do jog2 aparece tomado).
        page = self.client(self.p1).get(f"/torneios/{t['slug']}?aba=geral").data.decode()
        self.assertIn("Você está inscrito, mas ainda sem time", page)
        self.assertIn("Escolher este time", page)
        self.assertIn(f"já escolhido por jog2", page)
        self.client(self.p1).post(f"/torneios/{t['slug']}/meu-time",
                                  data={"csrf_token": CSRF, "team_id": str(self.teams[2])})
        self.assertEqual(self.entries(t["id"])[self.p1]["team_id"], self.teams[2])
        self.client(self.owner).post(f"/torneios/{t['slug']}/iniciar", data={"csrf_token": CSRF})
        self.assertEqual(self.query("SELECT status FROM tournaments WHERE id = ?", t["id"])[0][0], "running")

    def test_slots_cannot_go_below_the_entries(self):
        t = self.create()
        for player, team in ((self.p1, 0), (self.p2, 1), (self.p3, 2)):
            self.enroll(player, t["slug"], self.teams[team])
        response = self.edit(t["slug"], max_players="2").data.decode()
        self.assertIn("Já há 3 inscritos", response)
        self.assertEqual(self.query("SELECT max_players FROM tournaments WHERE id = ?", t["id"])[0][0], 4)

    def test_after_the_start_only_name_dates_and_rules(self):
        t = self.create(max_players="2", entry_fee="W$ 0,00")
        self.enroll(self.p1, t["slug"], self.teams[0])
        self.enroll(self.p2, t["slug"], self.teams[1])
        self.client(self.owner).post(f"/torneios/{t['slug']}/iniciar", data={"csrf_token": CSRF})
        page = self.client(self.owner).get(f"/torneios/{t['slug']}/gerenciar").data.decode()
        self.assertNotIn("Salvar alterações", page)
        self.assertNotIn('name="game_title_id"', page)
        self.edit(t["slug"], name="Copa Renomeada", game_title_id=str(self.games[1]), max_players="8",
                  entry_fee="W$ 50,00")
        row = self.query("SELECT * FROM tournaments WHERE id = ?", t["id"])[0]
        self.assertEqual((row["name"], row["game_title_id"], row["max_players"], row["entry_fee_cents"]),
                         ("Copa Renomeada", self.games[0], 2, 0))


if __name__ == "__main__":
    unittest.main()
