"""Jogo base "Jogo de Playstation" (games.ISO_ONLY): para cadastrar ISO que não
é de Winning Eleven e jogar no emulador. Aparece só no cadastro de ISO (Enviar
e Editar em Minhas ISOs, administração) e na página da ISO; fica fora da criação
de campeonato, dos filtros e do ranking, e as ISOs dele não entram no convite do
amistoso nem na escolha da ISO do campeonato."""
import io
import unittest

from app.db import get_db
from app.tournament import games, iso_catalog
from app.tournament.store import TournamentError
from tests import test_friendly_iso as base

CSRF, ISO = base.CSRF, base.ISO
PS = "Jogo de Playstation"


class IsoPlaystationTest(unittest.TestCase):
    tearDown, client, query, mails = (base.FriendlyIsoTest.tearDown, base.FriendlyIsoTest.client,
                                      base.FriendlyIsoTest.query, base.FriendlyIsoTest.mails)

    def setUp(self):
        base.FriendlyIsoTest.setUp(self)  # p0 e p1 amigos
        self.p0, self.p1, self.p2 = self.players
        self.ps = self.query("SELECT id FROM game_titles WHERE code = 'playstation'")[0]["id"]

    def upload(self, player, game, name="Crash Bandicoot"):
        return self.client(player).post("/torneios/amistosos/isos/enviar", data={
            "csrf_token": CSRF, "name": name, "game_title_id": game,
            "iso": (io.BytesIO(ISO), f"{name}.bin")}, content_type="multipart/form-data", follow_redirects=True)

    def released(self, game=None, name="Crash Bandicoot"):
        game = game or self.ps
        self.upload(self.p0, game, name)
        row = self.query("SELECT * FROM catalog_isos ORDER BY id DESC")[0]
        self.client(admin=True).post(f"/admin/torneios/isos/{row['id']}", data={
            "csrf_token": CSRF, "name": row["name"], "game_title_id": game, "is_active": "on", "liberar": "1"})
        self.mails()
        return self.query("SELECT * FROM catalog_isos WHERE id = ?", row["id"])[0]

    def test_seed_keeps_it_out_of_championships(self):
        row = self.query("SELECT * FROM game_titles WHERE code = 'playstation'")[0]
        self.assertEqual((row["name"], row["is_active"], row["platform"]), (PS, 0, "PS1"))
        with self.app.app_context():
            db = get_db()
            games.seed_games(db)  # de novo (todo migrate_db): continua um só, inativo
            db.commit()
            names = [g["name"] for g in games.iso_games(db)]
        self.assertEqual(len(self.query("SELECT id FROM game_titles WHERE code = 'playstation'")), 1)
        self.assertEqual(names[-1], PS)  # depois dos Winning Eleven
        self.assertIn("Winning Eleven 2002", names)

    def test_only_iso_forms_list_it(self):
        option = f'<option value="{self.ps}">{PS}</option>'
        self.assertIn(option, self.client(self.p0).get("/torneios/amistosos").get_data(as_text=True))
        self.assertIn(f'<option value="{self.ps}" >{PS}</option>',
                      self.client(admin=True).get("/admin/torneios/isos/nova").get_data(as_text=True))
        for path in ("/torneios/novo", "/torneios/", "/torneios/ranking", "/torneios/jogadores/p0",
                     "/torneios/comparar"):
            self.assertNotIn(PS, self.client(self.p0).get(path).get_data(as_text=True), path)
        # Criar campeonato com ele (formulário montado à mão) é recusado.
        self.client(self.p0).post("/torneios/novo", data={
            "csrf_token": CSRF, "name": "Copa PS", "starts_on": "2026-12-01", "rules": "Regras",
            "game_title_id": self.ps, "format": "league", "max_players": "4", "entry_fee": "0", "legs": "1"})
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM tournaments")[0]["n"], 0)

    def test_iso_page_and_my_isos(self):
        self.assertIn("ISO recebida!", self.upload(self.p0, self.ps).get_data(as_text=True))
        row = self.query("SELECT * FROM catalog_isos ORDER BY id DESC")[0]
        self.assertEqual(row["game_title_id"], self.ps)
        mine = self.client(self.p0).get("/torneios/amistosos").get_data(as_text=True)
        self.assertIn('<span class="muted">Jogo de Playstation</span>', mine)
        self.assertIn("/static/images/games/playstation.jpg", mine)
        row = self.released()
        page = self.client(self.p1).get(f"/torneios/isos/{row['id']}").get_data(as_text=True)
        self.assertIn("/static/images/games/playstation.jpg", page)
        self.assertIn(f"{PS} · para jogar no emulador do WE Camp", page)
        self.assertIn(f"<dt>Jogo base</dt><dd>{PS}</dd>", page)
        self.assertNotIn("Chamar um amigo para um amistoso", page)
        # A de Winning Eleven continua com o convite.
        we = self.released(game=self.game, name="WE 2002 Copa")
        self.assertIn("Chamar um amigo para um amistoso",
                      self.client(self.p1).get(f"/torneios/isos/{we['id']}").get_data(as_text=True))

    def test_not_for_friendlies_nor_championships(self):
        ps, we = self.released(), self.released(game=self.game, name="WE 2002 Copa")
        with self.app.app_context():
            db = get_db()
            values = [o["value"] for o in iso_catalog.options(db, self.p1)]
            self.assertIn(f"c:{we['id']}", values)
            self.assertNotIn(f"c:{ps['id']}", values)
            self.assertNotIn(f"c:{ps['id']}", [o["value"] for o in iso_catalog.tournament_options(db, self.p1, self.app)])
            with self.assertRaisesRegex(TournamentError, "não vale para amistosos nem campeonatos"):
                iso_catalog.resolve(db, self.p0, f"c:{ps['id']}")
        # O convite com ela (o ?iso= do link ou o formulário montado à mão) é recusado.
        picker = self.client(self.p0).get(f"/torneios/amistosos?iso=c:{ps['id']}").get_data(as_text=True)
        self.assertIn(f'data-value="c:{we["id"]}"', picker)
        self.assertNotIn(f'data-value="c:{ps["id"]}"', picker)
        page = self.client(self.p0).post("/torneios/amistosos/novo", data={
            "csrf_token": CSRF, "amigo": self.p1, "iso": f"c:{ps['id']}"}, follow_redirects=True)
        self.assertIn("não vale para amistosos nem campeonatos", page.get_data(as_text=True))
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM friendly_matches")[0]["n"], 0)

    def test_edit_to_and_from_playstation(self):
        self.upload(self.p0, self.game, "Copa errada")
        row = self.query("SELECT * FROM catalog_isos ORDER BY id DESC")[0]
        self.client(self.p0).post(f"/torneios/amistosos/isos/{row['id']}/editar", data={
            "csrf_token": CSRF, "name": "Copa errada", "game_title_id": self.ps}, content_type="multipart/form-data")
        self.assertEqual(self.query("SELECT game_title_id FROM catalog_isos WHERE id = ?", row["id"])[0][0], self.ps)
        # Um jogo base inativo de verdade (fora das duas listas) continua recusado.
        with self.app.app_context():
            db = get_db()
            old = db.execute("INSERT INTO game_titles (name, is_active, created_at, updated_at) "
                             "VALUES ('Jogo antigo', 0, '', '')").lastrowid
            db.commit()
        page = self.client(self.p0).post(f"/torneios/amistosos/isos/{row['id']}/editar", data={
            "csrf_token": CSRF, "name": "Copa errada", "game_title_id": old}, content_type="multipart/form-data",
            follow_redirects=True).get_data(as_text=True)
        self.assertIn("Escolha o jogo base da ISO.", page)


if __name__ == "__main__":
    unittest.main()
