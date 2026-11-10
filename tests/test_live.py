import json
import re
import struct
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.accounts import issue_token
from app.db import get_db, init_db, now
from tests.test_tournament_store import add_player, make_app


def without_suggestions(html):
    """A página sem as listas de sugestão de nick (_user_picker.html), para conferir só os cards."""
    return re.sub(r'<ul class="user-options".*?</ul>', '', html, flags=re.S)

KEY = "chave-do-dll"
CSRF = "test-csrf"


def state(kickoff=1000, frame=20000, period=0, clock=9000, goals=(1, 0), final=False, pads=(0, 1),
          names=("GRECIA", "CROACIA"), slots=(16, 18), scorers=None, v=1, engine=None, pens=None):
    scorers = scorers or ([["S.Vryzas", 17, "Karagounis"]] * goals[0], [["J.Leko", 20, ""]] * goals[1])
    return json.dumps({
        "v": v, "engine": engine, "content": "BA9BDE21:1C473F60", "kickoff": kickoff, "frame": frame, "period": period,
        "clock": clock, "half": 18000, "final": int(final), "pads": list(pads),
        "teams": [{"id": slots[i], "name": names[i], "goals": goals[i], "scorers": scorers[i]} for i in (0, 1)],
        **({"pens": list(pens)} if pens else {}),
    })


def krec(drops):
    """Replay KRC1 da transmissão: registros de controles vazios (0x12, um por
    quadro) e as saídas (0x14) nos quadros pedidos [(nick, quadro)]."""
    out, frame = [b"KRC1" + b"\0" * 396], 0
    for nick, at in sorted(drops, key=lambda drop: drop[1]):
        out.append(b"\x12\x00\x00" * (at - frame))
        frame = at
        out.append(b"\x14" + nick.encode() + b"\0" + struct.pack("<i", 1))
    out.append(b"\x12\x00\x00" * 10)
    return b"".join(out)


class LiveMatchesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name))
        self.app.config["SPECTATE_API_KEY"] = KEY
        with self.app.app_context():
            init_db()
            db = get_db()
            self.tieres, self.anders = add_player(db, "TIERES"), add_player(db, "ANDERSHOW")
            db.execute("INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('adm', 'x', ?, ?)",
                       (now(), now()))
            db.commit()
        self.tokens = {}

    def tearDown(self):
        self.temp.cleanup()

    def token(self, name):
        """Token de API da conta (criada se faltar) - o login do DLL."""
        if name not in self.tokens:
            with self.app.app_context():
                db = get_db()
                row = db.execute("SELECT id FROM players WHERE username = ? COLLATE NOCASE", (name,)).fetchone()
                self.tokens[name] = issue_token(db, row["id"] if row else add_player(db, name), "api")
                db.commit()
        return self.tokens[name]

    def post(self, st, reporter=None, players="TIERES,ANDERSHOW", test=False, key=None):
        """Como o DLL manda: com o token da conta de quem manda (reporter; sem
        ele, o PC de cada jogador, todos logados). Simulação (test) ou key: a
        chave do DLL."""
        data = {"state": st, "players": players, "room": "PCSX ReARMed: Euro vs Copa America 2004.bin",
                "test": "1" if test else "0"}
        client = self.app.test_client()
        if test or key:
            return client.post("/api/live/match", headers={"X-Api-Key": key or KEY}, data=dict(data, reporter=reporter or "TIERES"))
        reply = None
        for name in ([reporter] if reporter else players.split(",")):
            reply = client.post("/api/live/match", headers={"Authorization": "Bearer " + self.token(name)},
                                data=dict(data, reporter=name))
        return reply

    def rows(self, sql, *args):
        with self.app.app_context():
            return get_db().execute(sql, args).fetchall()

    def admin(self):
        client = self.app.test_client()
        with client.session_transaction() as s:
            s["user_id"] = 1
            s["csrf_token"] = CSRF
        return client

    def cards(self, client=None, **params):
        return (client or self.app.test_client()).get("/ao-vivo/cards", query_string=params).get_data(as_text=True)

    def test_auth_and_bad_state(self):
        self.assertEqual(self.post(state(), key="errada").status_code, 401)
        # Partida de verdade só com a conta: a chave do DLL vale só para simulação.
        reply = self.app.test_client().post("/api/live/match", headers={"X-Api-Key": KEY}, data={
            "state": state(), "players": "TIERES,ANDERSHOW", "reporter": "TIERES", "room": "x", "test": "0"})
        self.assertEqual(reply.status_code, 401)
        # Logado, mas sem ser um dos jogadores da partida.
        self.assertEqual(self.post(state(), reporter="ESTRANHO").status_code, 400)
        self.assertEqual(self.post("{nada").status_code, 400)
        self.assertEqual(self.post(json.dumps({"content": "x", "teams": []})).status_code, 400)

    def test_needs_both_players_logged_in(self):
        self.post(state(), reporter="TIERES")  # o adversário sem login: o DLL dele não manda
        match = self.rows("SELECT * FROM live_matches")[0]
        self.assertEqual((match["verified"], match["reporters"]), (0, 1))
        self.assertEqual(self.cards(), "")
        self.post(state(frame=20030), reporter="ANDERSHOW")
        self.assertEqual(self.rows("SELECT verified FROM live_matches")[0]["verified"], 1)
        self.assertIn("TIERES", self.cards())

    def test_two_pcs_one_match_and_card(self):
        self.assertIn("ok=1", self.post(state()).get_data(as_text=True))
        self.post(state(frame=20060), reporter="ANDERSHOW")
        match = self.rows("SELECT * FROM live_matches")
        self.assertEqual(len(match), 1)
        self.assertEqual((match[0]["reporters"], match[0]["local_goals"], match[0]["status"]), (2, 1, "live"))
        html = self.cards()
        # Sem campeonato nem cadastro, o nome da tabela da ISO não vale sozinho.
        for text in ("Não identificado", "TIERES", "ANDERSHOW", "1º tempo · 22&#39;", "S.Vryzas 17'",
                     "assist. Karagounis", "Partida livre", "Ao vivo", "Euro vs Copa America 2004.bin"):
            self.assertIn(text, html)
        self.assertNotIn("Grecia", html)
        self.assertRegex(html, r"\d\d/\d\d/\d{4} \d\d:\d\d · Euro vs Copa America")
        self.assertIn("TIERES", self.app.test_client().get("/ao-vivo").get_data(as_text=True))

    def test_v1_names_are_ignored(self):
        # v1: o nome vinha de uma tabela que patches como o CAN não renomeiam (FINLAND = Camarões).
        self.post(state(names=("FINLAND", "CROACIA")))
        self.assertNotIn("FINLAND", self.cards())
        self.assertEqual(self.rows("SELECT ram_name FROM iso_team_slots WHERE slot = 16")[0]["ram_name"], "")

    def test_select_screen_names(self):
        # v2: o nome da tela de seleção de times ("PAL99", "IND84"), como o jogo mostra.
        self.post(state(names=("PAL99", "IND84"), v=2))
        html = self.cards()
        self.assertIn("PAL99", html)
        self.assertIn("IND84", html)
        self.assertNotIn("Não identificado", html)
        # Um estado sem o nome (PC que entrou no meio) usa o último visto do slot.
        self.post(state(frame=40000, period=1, names=("", ""), v=2), reporter="ANDERSHOW")
        self.assertIn("PAL99", self.cards())
        # O cadastro do admin ganha do nome do jogo.
        self.admin().post("/admin/ao-vivo/times", data={"csrf_token": CSRF, "content_id": "BA9BDE21:1C473F60",
                                                        "slot": "16", "name": "Palmeiras"})
        html = self.cards()
        self.assertIn("Palmeiras", html)
        self.assertNotIn("PAL99", html)

    def test_admin_uses_the_game_names(self):
        self.post(state(v=2))
        self.admin().post("/admin/ao-vivo/times", data={"csrf_token": CSRF, "content_id": "BA9BDE21:1C473F60",
                                                        "acao": "nomes_do_jogo"})
        rows = self.rows("SELECT name, source FROM iso_team_slots ORDER BY slot")
        self.assertEqual([(r["name"], r["source"]) for r in rows], [("Grecia", "admin"), ("Croacia", "admin")])
        self.assertIn("Grecia", self.cards())

    def test_cpu_and_unknown_players(self):
        self.post(state(pads=(0, 255)))
        html = self.cards()
        self.assertIn("CPU", html)
        self.post(state(kickoff=7000, pads=(255, 255)))
        self.assertIn("Player 2", self.cards())

    def test_player_filter_exact_nick_and_underscore(self):
        """Filtro "Jogador": o nick de uma conta traz só as partidas dele (com "_"
        no nick, inclusive); um pedaço do nick procura em todos, com "_" como letra."""
        self.post(state(kickoff=1000), players="TIERES_ART,TUTORIAL")
        self.post(state(kickoff=2000), players="TIERES,ANDERSHOW")

        def count(text):
            return self.cards(jogador=text).count('<article class="live-card')
        self.assertEqual(count("TIERES_ART"), 1)
        self.assertEqual(count("tieres_art"), 1)
        self.assertEqual(count("TIERES"), 1)  # não traz as do TIERES_ART
        self.assertEqual(count("TIER"), 2)  # pedaço do nick: os dois
        self.assertEqual(count("S_"), 1)  # "_" não é curinga (senão "TIERES" + aspas também casaria)

    def test_filters_and_limit(self):
        for n in range(12):
            self.post(state(kickoff=1000 + n, goals=(n % 3, 0), final=True), players="TIERES,P%d" % n)
        self.post(state(kickoff=99), players="Fulano,Beltrano")
        html = self.cards()
        self.assertEqual(html.count('<article class="live-card'), 10)
        self.assertEqual(self.cards(jogador="beltr").count('<article class="live-card'), 1)
        self.assertEqual(self.cards(jogador="P11").count('<article class="live-card'), 1)
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(self.cards(de="2000-01-01", ate="2000-01-02"), "")
        self.assertEqual(self.cards(de=today, ate=today, jogador="Fulano").count('<article class="live-card'), 1)
        page = self.app.test_client().get("/ao-vivo?jogador=Fulano").get_data(as_text=True)
        self.assertIn("Beltrano", page)
        self.assertIn("/ao-vivo/cards?jogador=Fulano", page)
        self.assertNotIn("P11", without_suggestions(page))
        self.assertIn('data-nick="Fulano"', page)  # o filtro "Jogador" sugere quem já jogou logado

    def test_final_agreement(self):
        self.post(state(period=1, clock=0, goals=(4, 4), final=True))
        self.post(state(period=1, clock=0, goals=(4, 4), final=True), reporter="ANDERSHOW")
        match = self.rows("SELECT * FROM live_matches")[0]
        self.assertEqual((match["status"], match["agreed"]), ("finished", 1))
        self.assertIn("Placar conferido nos 2 PCs", self.cards())
        self.assertIn("Encerrado", self.cards())
        # Partida seguinte da sessão: outro pontapé inicial, outra partida.
        self.post(state(kickoff=64201, goals=(0, 0)))
        self.assertEqual(len(self.rows("SELECT * FROM live_matches")), 2)

    def test_disagreeing_finals_are_not_agreed(self):
        self.post(state(period=1, clock=0, goals=(2, 1), final=True))
        self.post(state(period=1, clock=0, goals=(2, 2), final=True), reporter="ANDERSHOW")
        self.assertEqual(self.rows("SELECT agreed FROM live_matches")[0]["agreed"], 0)

    def test_pc_joining_midway_joins_the_live_match(self):
        self.post(state())
        self.post(state(kickoff=0, frame=30000, goals=(2, 0)), reporter="ANDERSHOW")
        match = self.rows("SELECT * FROM live_matches")
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["local_goals"], 2)

    def make_tournament(self):
        with self.app.app_context():
            db = get_db()
            game = db.execute("INSERT INTO game_titles (name, created_at, updated_at) VALUES ('Euro 2004', ?, ?)",
                              (now(), now())).lastrowid
            t = db.execute("INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                           "created_at, updated_at) VALUES ('euro-1', 'Euro WE Camp', ?, ?, 'knockout', 'running', 2, ?, ?)",
                           (game, self.tieres, now(), now())).lastrowid
            greece = db.execute("SELECT id FROM teams WHERE name = 'Grécia' LIMIT 1").fetchone()
            ea = db.execute("INSERT INTO tournament_entries (tournament_id, player_id, team_name, team_id, created_at) "
                            "VALUES (?, ?, 'Grécia', ?, ?)", (t, self.tieres, greece["id"] if greece else None, now())).lastrowid
            eb = db.execute("INSERT INTO tournament_entries (tournament_id, player_id, team_name, created_at) "
                            "VALUES (?, ?, 'Croácia', ?)", (t, self.anders, now())).lastrowid
            stage = db.execute("INSERT INTO tournament_stages (tournament_id, seq, kind, name, created_at) "
                               "VALUES (?, 1, 'knockout', 'Final', ?)", (t, now())).lastrowid
            tie = db.execute("INSERT INTO tournament_ties (stage_id, round, position, legs, entry_a, entry_b) "
                             "VALUES (?, 1, 1, 2, ?, ?)", (stage, eb, ea)).lastrowid
            legs = []
            for leg, (home, away) in enumerate(((eb, ea), (ea, eb)), 1):
                legs.append(db.execute(
                    "INSERT INTO tournament_matches (stage_id, tie_id, round, leg, home_entry_id, away_entry_id, created_at, "
                    "updated_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?)", (stage, tie, leg, home, away, now(), now())).lastrowid)
            db.commit()
            return legs

    def test_tournament_ida_then_volta(self):
        ida, volta = self.make_tournament()
        # CAN-like: a tabela da ISO diz FINLAND/CROACIA, mas a inscrição manda.
        self.post(state(goals=(4, 4), names=("FINLAND", "CROACIA"), v=2))
        first = self.rows("SELECT * FROM live_matches")[0]
        self.assertEqual(first["tournament_match_id"], ida)
        html = self.cards()
        self.assertIn("Euro WE Camp · Final · Ida", html)
        self.assertIn("Grécia", html)
        self.assertNotIn("Finland", html)
        learned = self.rows("SELECT * FROM iso_team_slots WHERE slot = 16")[0]
        self.assertEqual((learned["name"], learned["source"], learned["ram_name"]), ("Grécia", "aprendido", "FINLAND"))
        self.post(state(period=1, clock=0, goals=(4, 4), final=True))
        self.post(state(kickoff=64201, goals=(0, 1)))
        second = self.rows("SELECT * FROM live_matches ORDER BY id")[1]
        self.assertEqual(second["tournament_match_id"], volta)
        self.assertIn("Euro WE Camp · Final · Volta", self.cards())
        tournament_id = self.rows("SELECT id FROM tournaments")[0]["id"]
        self.assertIn("Euro WE Camp", self.cards(campeonato=tournament_id))
        self.assertIn("Euro 2004", self.cards())  # jogo base do campeonato
        profile = self.rows("SELECT * FROM iso_profiles")[0]
        self.assertEqual((profile["tournament_id"], profile["source"]), (tournament_id, "aprendido"))
        # Partida livre depois (amistoso/ISO): o slot aprendido dá o nome, a ISO dá o jogo e o campeonato.
        self.post(state(kickoff=99000, goals=(0, 0)), players="TIERES,outro")
        html = self.cards(jogador="outro")
        self.assertIn("Grécia", html)
        self.assertIn("Partida livre", html)
        self.assertIn("Euro 2004", html)
        self.assertIn("Euro WE Camp", html)
        # Campeonato = só os jogos dele; Amistosos = o resto (cadastrados e partidas livres).
        self.assertEqual(self.cards(campeonato=tournament_id).count('<article class="live-card'), 2)
        self.assertEqual(self.cards(campeonato=tournament_id, recentes="1").count('<article class="live-card'), 2)
        friendlies = self.cards(campeonato="amistosos")
        self.assertEqual(friendlies.count('<article class="live-card'), 1)
        self.assertIn("outro", friendlies)
        page = self.app.test_client().get("/ao-vivo?campeonato=amistosos").get_data(as_text=True)
        self.assertIn('<option value="amistosos" selected>Amistosos</option>', page)
        self.assertIn("Limpar filtro", page)

    def make_league(self):
        """Pontos corridos em andamento, 3 inscritos: TIERES x ANDERSHOW na rodada 1."""
        with self.app.app_context():
            db = get_db()
            third = add_player(db, "TERCEIRO")
            game = db.execute("SELECT id FROM game_titles WHERE code = 'we2002'").fetchone()["id"]
            t = db.execute("INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                           "settings_json, created_at, updated_at) VALUES ('liga-1', 'Liga WE Camp', ?, ?, 'league', 'running', 4, "
                           "'{\"legs\": 1}', ?, ?)",
                           (game, self.tieres, now(), now())).lastrowid
            entries = [db.execute("INSERT INTO tournament_entries (tournament_id, player_id, team_name, created_at) "
                                  "VALUES (?, ?, ?, ?)", (t, player, team, now())).lastrowid
                       for player, team in ((self.tieres, "Grécia"), (self.anders, "Croácia"), (third, "Itália"))]
            stage = db.execute("INSERT INTO tournament_stages (tournament_id, seq, kind, name, created_at) "
                               "VALUES (?, 1, 'league', 'Liga', ?)", (t, now())).lastrowid
            group = db.execute("INSERT INTO tournament_groups (stage_id, name, seq) VALUES (?, 'Liga', 1)", (stage,)).lastrowid
            for order, entry in enumerate(entries):
                db.execute("INSERT INTO tournament_group_members (group_id, entry_id, draw_order) VALUES (?, ?, ?)",
                           (group, entry, order))
            # Mandante no campeonato = ANDERSHOW, mas no jogo o LOCAL é o TIERES (controle 1).
            db.execute("INSERT INTO tournament_matches (stage_id, group_id, round, home_entry_id, away_entry_id, "
                       "created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?, ?)", (stage, group, entries[1], entries[0], now(), now()))
            db.commit()
            return t, group, entries

    def table(self):
        return self.app.test_client().get("/torneios/liga-1/tabela-ao-vivo").get_data(as_text=True)

    def test_league_table_shows_the_live_score(self):
        t, group, (tieres, anders, third) = self.make_league()
        page = self.app.test_client().get("/torneios/liga-1").get_data(as_text=True)
        self.assertIn('data-live-stage=', page)
        for tab in ("tabela", "geral", "jogos"):  # sem card do Ao vivo na página do torneio
            self.assertNotIn("data-live-section", self.app.test_client().get("/torneios/liga-1?aba=" + tab).get_data(as_text=True))
        self.assertNotIn("live-mini", page)
        self.assertNotIn("standings-live", page)
        self.post(state(goals=(1, 0)))  # LOCAL = TIERES (Grécia), visitante no campeonato
        live_id = self.rows("SELECT id FROM live_matches")[0]["id"]
        html = self.table()
        self.assertIn('<a class="standings-live" href="/ao-vivo?campeonato=%d"' % t, html)
        self.assertIn('<a class="live-mini win" href="/ao-vivo?partida=%d" title="Jogando agora: ver a partida no Ao vivo">1-0</a>' % live_id, html)
        self.assertIn('<a class="live-mini loss" href="/ao-vivo?partida=%d" title="Jogando agora: ver a partida no Ao vivo">0-1</a>' % live_id, html)
        # A classificação já soma o jogo, em amarelo, com a legenda.
        first_row = html[html.index("<tbody>"):].split("</tr>")[0]
        self.assertIn("provisional", first_row)
        self.assertIn("Grécia", first_row)
        self.assertIn("<td><strong>3</strong></td>", first_row)
        self.assertIn("em laranja", html)
        self.assertIn("24 h depois do jogo", html)
        self.assertIn('<span class="f-V prov">V</span>', html)
        # Aba Jogos e página do jogo: o placar ao vivo (mandante ANDERSHOW: 0 x 1), em laranja,
        # "Ao vivo" com link para a partida e a legenda.
        games = self.app.test_client().get("/torneios/liga-1?aba=jogos").get_data(as_text=True)
        self.assertIn('<a class="match-live standings-live" href="/ao-vivo?partida=%d"' % live_id, games)
        self.assertIn('score-reported score-live', games)
        self.assertIn("<strong>0 x 1</strong>", games)
        self.assertIn("em laranja", games)
        match_id = self.rows("SELECT id FROM tournament_matches")[0]["id"]
        detail = self.app.test_client().get("/torneios/liga-1/jogos/%d" % match_id).get_data(as_text=True)
        self.assertIn("<strong>0 x 1</strong>", detail)
        self.assertIn("Ao vivo agora", detail)
        self.assertIn('href="/ao-vivo?partida=%d"' % live_id, detail)
        self.assertIn("em laranja", detail)
        # O card do Ao vivo leva à aba Tabela do torneio.
        self.assertIn('href="/torneios/liga-1?aba=tabela"', self.cards())
        # O placar leva ao Ao vivo filtrado naquela partida.
        self.assertEqual(self.cards(partida=str(live_id)).count('<article class="live-card'), 1)
        self.assertEqual(self.cards(partida="999").count('<article class="live-card'), 0)
        self.assertIn("Mostrando só a partida", self.app.test_client().get("/ao-vivo?partida=%d" % live_id).get_data(as_text=True))
        # Sem sinal há mais de 2 minutos: some da tabela (e da conta).
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE live_matches SET updated_at = ?", ((datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(),))
            db.commit()
        html = self.table()
        self.assertNotIn("live-mini", html)
        self.assertNotIn("provisional", html)
        self.post(state(goals=(1, 1), frame=20500))
        self.assertIn('<a class="live-mini draw"', self.table())
        self.assertNotIn("TERCEIRO</small></span></span><a", self.table())

    def finish_live(self, goals=(2, 1), test=False, other_final=None):
        """Partida ao vivo da liga até o fim (os 2 PCs), encerrada há 2 minutos."""
        self.post(state(goals=goals), test=test)
        self.post(state(goals=goals, period=1, clock=0, final=True, frame=90000), test=test)
        self.post(state(goals=other_final or goals, period=1, clock=0, final=True, frame=90000), reporter="ANDERSHOW", test=test)
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE live_matches SET finished_at = ?", ((datetime.now(timezone.utc) - timedelta(minutes=2)).replace(microsecond=0).isoformat(),))
            db.commit()
        self.cards()  # reap -> report_finished

    def test_finished_match_becomes_the_report_and_confirms_in_24h(self):
        from app.tournament import reports, store
        t, group, (tieres, anders, third) = self.make_league()
        self.finish_live(goals=(2, 1))
        match = self.rows("SELECT * FROM tournament_matches WHERE status != 'confirmed'")[0]
        live = self.rows("SELECT * FROM live_matches")[0]
        self.assertEqual((match["status"], match["reported_by_entry_id"], match["reported_at"]),
                         ("reported", None, live["finished_at"]))
        self.assertEqual(live["game_report"], "reportado")
        report = self.rows("SELECT * FROM match_reports WHERE match_id = ?", match["id"])[0]
        # Mandante no campeonato = ANDERSHOW (Croácia): 1 x 2.
        self.assertEqual((report["action"], report["home_goals"], report["away_goals"], report["entry_id"]), ("report", 1, 2, None))
        self.assertIn("RetroArch TIERES", report["comment"])
        # Tabela: conta o placar reportado, em laranja.
        self.assertIn("em laranja", self.table())
        # Página do jogo: placar lido do jogo, qualquer um dos dois confirma ou contesta.
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["player_id"] = self.anders
        html = client.get("/torneios/liga-1/jogos/%d" % match["id"]).get_data(as_text=True)
        self.assertIn("Placar lido do jogo", html)
        self.assertIn("O jogo terminou 1 x 2", html)
        self.assertIn("Confirmar este placar", html)
        # "Não aceitar o placar" à esquerda do confirmar, abrindo a janela com o motivo obrigatório.
        self.assertIn('<button type="button" class="secondary" data-dialog="contestar">Não aceitar o placar</button><form', html)
        self.assertIn('<dialog class="form-dialog" id="contestar"', html)
        self.assertIn('name="comment" rows="3" minlength="3" maxlength="500" required', html)
        # Os dois confirmam: o primeiro só marca a confirmação dele.
        with self.app.app_context():
            db = get_db()
            tournament = store.get_tournament(db, t)
            self.assertFalse(reports.confirm(db, tournament, match["id"], self.anders))
            db.commit()
            with self.assertRaises(store.TournamentError):
                reports.confirm(db, tournament, match["id"], self.anders)
        self.assertEqual(self.rows("SELECT status FROM tournament_matches WHERE id = ?", match["id"])[0]["status"], "reported")
        html = client.get("/torneios/liga-1/jogos/%d" % match["id"]).get_data(as_text=True)
        self.assertIn("Você confirmou o placar 1 x 2", html)
        self.assertNotIn("Confirmar este placar", html)
        other = self.app.test_client()
        with other.session_transaction() as sess:
            sess["player_id"] = self.tieres
        page = other.get("/torneios/liga-1/jogos/%d" % match["id"]).get_data(as_text=True)
        self.assertIn("o adversário já confirmou", page)
        self.assertIn("Confirmar este placar", page)
        mine = other.get("/torneios/meus-jogos").get_data(as_text=True)
        self.assertIn("1 para confirmar", mine)
        self.assertIn('data-dialog="contestar-%d">Não aceitar o placar</button>' % match["id"], mine)
        self.assertIn('<input type="hidden" name="voltar" value="meus-jogos">', mine)
        waiting = client.get("/torneios/meus-jogos").get_data(as_text=True)
        self.assertIn("1 aguardando adversário", waiting)
        # Sem a 2ª confirmação: confirmado sozinho 24 h depois do fim do jogo.
        with self.app.app_context():
            db = get_db()
            self.assertEqual(reports.auto_confirm(db, at=datetime.now(timezone.utc) + timedelta(hours=23)), 0)
            self.assertEqual(reports.auto_confirm(db, at=datetime.now(timezone.utc) + timedelta(hours=24, minutes=1)), 1)
        done = self.rows("SELECT * FROM tournament_matches WHERE id = ?", match["id"])[0]
        self.assertEqual((done["status"], done["home_goals"], done["away_goals"]), ("confirmed", 1, 2))
        self.assertNotIn("em laranja", self.table())

    def test_game_report_needs_both_confirmations(self):
        from app.tournament import reports, store
        t, group, entries = self.make_league()
        self.finish_live(goals=(2, 1))
        match_id = self.rows("SELECT id FROM tournament_matches WHERE status = 'reported'")[0]["id"]
        with self.app.app_context():
            db = get_db()
            tournament = store.get_tournament(db, t)
            self.assertFalse(reports.confirm(db, tournament, match_id, self.tieres))
            self.assertTrue(reports.confirm(db, tournament, match_id, self.anders))
            db.commit()
        done = self.rows("SELECT * FROM tournament_matches WHERE id = ?", match_id)[0]
        self.assertEqual((done["status"], done["home_goals"], done["away_goals"]), ("confirmed", 1, 2))

    def test_game_report_can_be_disputed_and_accepted_by_the_other(self):
        from app.tournament import reports, store
        t, group, (tieres, anders, third) = self.make_league()
        self.finish_live(goals=(2, 1))
        match_id = self.rows("SELECT id FROM tournament_matches WHERE status = 'reported'")[0]["id"]
        with self.app.app_context():
            db = get_db()
            tournament = store.get_tournament(db, t)
            reports.dispute(db, tournament, match_id, self.anders, "foi 2 x 2", (2, 2, None, None))
            db.commit()
            with self.assertRaises(store.TournamentError):
                reports.accept_dispute(db, tournament, match_id, self.anders)  # quem contestou não aceita
            reports.accept_dispute(db, tournament, match_id, self.tieres)
            db.commit()
        done = self.rows("SELECT * FROM tournament_matches WHERE id = ?", match_id)[0]
        self.assertEqual((done["status"], done["home_goals"], done["away_goals"]), ("confirmed", 2, 2))

    def test_no_game_report_for_tests_or_disagreeing_pcs(self):
        t, group, entries = self.make_league()
        self.finish_live(goals=(2, 1), other_final=(2, 2))
        self.assertEqual(self.rows("SELECT status FROM tournament_matches WHERE status != 'confirmed'")[0]["status"], "scheduled")
        self.assertEqual(self.rows("SELECT game_report FROM live_matches")[0]["game_report"], "os PCs discordam do placar")
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM live_matches")
            db.commit()
        self.finish_live(goals=(3, 0), test=True)
        self.assertEqual(self.rows("SELECT status FROM tournament_matches WHERE status != 'confirmed'")[0]["status"], "scheduled")
        self.assertIsNone(self.rows("SELECT game_report FROM live_matches")[0]["game_report"])

    def test_extra_time_and_penalties_on_the_card(self):
        self.post(state(period=2, clock=3000, goals=(5, 5)))
        self.assertIn("Prorrogação · 1º tempo", self.cards())
        self.post(state(period=4, clock=0, goals=(5, 5), pens=(2, 1), frame=30000))
        html = self.cards()
        self.assertIn("Pênaltis 2 × 1", html)
        self.assertIn('live-clock on">Pênaltis<', html)

    def test_second_leg_card_shows_the_aggregate(self):
        from app.tournament import store
        ida, volta = self.make_tournament()
        with self.app.app_context():
            db = get_db()
            # Ida: ANDERSHOW (mandante) 3 x 1 TIERES.
            store.record_result(db, store.get_tournament(db, self.rows("SELECT id FROM tournaments")[0]["id"]), ida, 3, 1)
            db.commit()
        self.post(state(goals=(1, 1)))  # volta: TIERES (LOCAL) 1 x 1 ANDERSHOW
        html = self.cards()
        self.assertIn("Final · Jogo 2 de 2", html)
        self.assertIn("Total com jogo de ida: 2 - 4", html)
        self.assertNotIn("ida não confirmada", html)

    def test_first_leg_card_shows_game_1_of_2(self):
        self.make_tournament()
        self.post(state(goals=(0, 0)))
        html = self.cards()
        self.assertIn("Final · Jogo 1 de 2", html)
        self.assertNotIn("Total com jogo de ida", html)

    def test_penalties_go_into_the_report_of_the_deciding_leg(self):
        from app.tournament import store
        ida, volta = self.make_tournament()
        with self.app.app_context():
            db = get_db()
            store.record_result(db, store.get_tournament(db, self.rows("SELECT id FROM tournaments")[0]["id"]), ida, 1, 1)
            db.commit()
        # Volta: mandante TIERES (o LOCAL do jogo), 2 x 2 e 4 x 3 nos pênaltis.
        self.post(state(goals=(2, 2)))
        self.post(state(period=4, clock=0, goals=(2, 2), pens=(4, 3), final=True, frame=90000))
        self.assertEqual(self.rows("SELECT tournament_match_id FROM live_matches")[0]["tournament_match_id"], volta)
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE live_matches SET finished_at = ?",
                       ((datetime.now(timezone.utc) - timedelta(minutes=2)).replace(microsecond=0).isoformat(),))
            db.commit()
        self.assertIn("Pênaltis 4 × 3", self.cards())
        report = self.rows("SELECT * FROM match_reports WHERE match_id = ? AND action = 'report'", volta)[0]
        self.assertEqual((report["home_goals"], report["away_goals"], report["home_pens"], report["away_pens"]), (2, 2, 4, 3))

    def test_profile_from_iso_file_name(self):
        with self.app.app_context():
            db = get_db()
            game = db.execute("SELECT id FROM game_titles WHERE code = 'we2002'").fetchone()["id"]
            db.execute("INSERT INTO catalog_isos (name, game_title_id, original_filename, is_active, created_at, "
                       "updated_at) VALUES ('Euro', ?, 'Euro vs Copa America 2004.bin', 1, ?, ?)", (game, now(), now()))
            db.commit()
        self.post(state())
        html = self.cards()
        self.assertIn("Winning Eleven 2002", html)
        self.assertIn("images/games/", html)

    def add_catalog_iso(self, filename="Euro vs Copa America 2004.bin", image="euro.webp"):
        with self.app.app_context():
            db = get_db()
            game = db.execute("SELECT id FROM game_titles WHERE code = 'we2002'").fetchone()["id"]
            catalog = db.execute("INSERT INTO catalog_isos (name, game_title_id, image, original_filename, is_active, "
                                 "approved_at, created_at, updated_at) VALUES ('EUROCOPA 2004', ?, ?, ?, 1, ?, ?, ?)",
                                 (game, image, filename, now(), now(), now())).lastrowid
            db.commit()
        return catalog, game

    def test_card_shows_the_friendly_catalog_iso_image(self):
        """Amistoso com a ISO do cadastro escolhida no convite: o card mostra a imagem dela."""
        catalog, game = self.add_catalog_iso(filename="Outro nome.bin")
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO friendly_matches (home_id, away_id, status, iso_catalog_id, game_title_id, "
                       "created_at, updated_at) VALUES (?, ?, 'accepted', ?, ?, ?, ?)",
                       (self.tieres, self.anders, catalog, game, now(), now()))
            db.commit()
        self.post(state())
        html = self.cards()
        self.assertRegex(html, r'<img class="live-tournament" src="[^"]*euro\.webp" alt="EUROCOPA 2004"')
        self.assertIn("Winning Eleven 2002 · EUROCOPA 2004", html)

    def test_card_shows_the_championship_image_or_else_the_catalog_iso(self):
        """Jogo de campeonato: a imagem do campeonato; sem ela, a da mesma ISO no cadastro."""
        self.make_tournament()
        self.add_catalog_iso()  # a mesma ISO (pelo nome do arquivo) no cadastro de amistosos
        self.post(state())
        self.assertIsNotNone(self.rows("SELECT tournament_match_id FROM live_matches")[0]["tournament_match_id"])
        self.assertRegex(self.cards(), r'<img class="live-tournament" src="[^"]*euro\.webp"')
        with self.app.app_context():
            get_db().execute("UPDATE tournaments SET image = 'camp.webp'")
            get_db().commit()
        html = self.cards()
        self.assertRegex(html, r'<a href="[^"]*/euro-1[^"]*"><img class="live-tournament" src="[^"]*camp\.webp" '
                               r'alt="Euro WE Camp"')
        self.assertNotIn("euro.webp", html)

    def test_championship_iso_must_match(self):
        ida, volta = self.make_tournament()
        with self.app.app_context():
            db = get_db()
            t = db.execute("SELECT id FROM tournaments").fetchone()["id"]
            db.execute("INSERT INTO tournament_isos (tournament_id, stored_name, original_filename, file_size, md5, "
                       "archive_name, content_id, created_at, updated_at) VALUES (?, 'x.bin', 'Outra.bin', 1, 'm', "
                       "'torneios/x/Outra.bin', '11111111:10', ?, ?)", (t, now(), now()))
            db.commit()
        self.post(state())  # BA9BDE21:1C473F60 não é a ISO do campeonato
        self.assertIsNone(self.rows("SELECT tournament_match_id FROM live_matches")[0]["tournament_match_id"])
        self.assertIn("Partida livre", self.cards())
        # O admin liga esta ISO (outra versão) ao campeonato: o próximo informe já liga a partida.
        self.admin().post("/admin/ao-vivo/times", data={"csrf_token": CSRF, "content_id": "BA9BDE21:1C473F60",
                                                        "acao": "perfil", "tournament_id": str(t)})
        self.post(state(frame=20100))
        self.assertEqual(self.rows("SELECT tournament_match_id FROM live_matches")[0]["tournament_match_id"], ida)
        # Com a ISO certa (content_id dela), liga sem precisar do admin.
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM iso_profiles")
            db.execute("UPDATE tournament_isos SET content_id = 'ba9bde21:1c473f60'")
            db.commit()
        # (o jogo de ida termina antes; partida nova sem o fim da anterior = jogar de novo o mesmo jogo)
        self.post(state(frame=90000, period=1, clock=0, final=True))
        self.post(state(kickoff=64000))
        second = self.rows("SELECT * FROM live_matches ORDER BY id")[1]
        self.assertEqual(second["tournament_match_id"], volta)

    def test_friendly_iso_must_match(self):
        with self.app.app_context():
            db = get_db()
            game = db.execute("SELECT id FROM game_titles WHERE code = 'we2002'").fetchone()["id"]
            catalog = db.execute("INSERT INTO catalog_isos (name, game_title_id, original_filename, content_id, is_active, "
                                 "created_at, updated_at) VALUES ('Euro', ?, 'Euro.bin', '22222222:20', 1, ?, ?)",
                                 (game, now(), now())).lastrowid
            db.execute("INSERT INTO friendly_matches (home_id, away_id, status, iso_catalog_id, game_title_id, created_at, "
                       "updated_at) VALUES (?, ?, 'accepted', ?, ?, ?, ?)", (self.anders, self.tieres, catalog, game,
                                                                              now(), now()))
            db.commit()
        self.post(state())
        self.assertIsNone(self.rows("SELECT friendly_id FROM live_matches")[0]["friendly_id"])
        with self.app.app_context():
            get_db().execute("UPDATE catalog_isos SET content_id = 'BA9BDE21:1C473F60'")
            get_db().commit()
        self.post(state(frame=20100))
        self.assertIsNotNone(self.rows("SELECT friendly_id FROM live_matches")[0]["friendly_id"])
        self.assertIn("Amistoso", self.cards())

    def test_logos_from_the_exact_iso(self):
        with self.app.app_context():
            db = get_db()
            game = db.execute("SELECT id FROM game_titles WHERE code = 'we2002'").fetchone()["id"]
            db.execute("INSERT INTO catalog_isos (name, game_title_id, original_filename, content_id, is_active, "
                       "created_at, updated_at) VALUES ('Euro', ?, 'nome diferente.bin', 'BA9BDE21:1C473F60', 1, ?, ?)",
                       (game, now(), now()))
            db.commit()
        self.post(state())
        self.assertIn("Winning Eleven 2002", self.cards())
        page = self.admin().get("/admin/ao-vivo/times").get_data(as_text=True)
        self.assertIn("É a ISO de amistoso Euro", page)

    def test_engine_gives_the_base_game(self):
        self.post(state(engine="SLPM_862.91"))
        self.assertIn("Winning Eleven 4", self.cards())

    def test_we3_engine_gives_we3_final_version(self):
        self.post(state(engine="SLPM_861.62"))
        self.assertIn("Winning Eleven 3: Final Version", self.cards())

    def test_new_kickoff_abandons_the_open_match(self):
        self.post(state(period=1, clock=12000, goals=(1, 0)))  # WE4: saíram pelo menu de pausa
        self.post(state(kickoff=62550, goals=(0, 0)))
        first, second = self.rows("SELECT status FROM live_matches ORDER BY id")
        self.assertEqual((first["status"], second["status"]), ("abandoned", "live"))
        self.assertIn("Interrompido", self.cards())
        # Um informe atrasado do outro PC sobre a partida abandonada não a reabre.
        self.post(state(period=1, clock=11900, goals=(1, 0), frame=20030), reporter="ANDERSHOW")
        self.assertEqual(self.rows("SELECT status FROM live_matches ORDER BY id")[0]["status"], "abandoned")

    def test_only_three_base_games(self):
        rows = self.rows("SELECT code FROM game_titles WHERE is_active = 1 ORDER BY year")
        self.assertEqual([r["code"] for r in rows], ["we3fv", "we4", "we2002"])

    def test_simulation_does_not_take_the_real_leg(self):
        ida, volta = self.make_tournament()
        self.post(state(), test=True)
        self.assertEqual(self.rows("SELECT tournament_match_id FROM live_matches")[0]["tournament_match_id"], ida)
        self.assertEqual(self.rows("SELECT source FROM iso_team_slots WHERE slot = 16")[0]["source"], "")
        self.post(state(kickoff=5000))
        real = self.rows("SELECT * FROM live_matches WHERE is_test = 0")[0]
        self.assertEqual(real["tournament_match_id"], ida)

    def test_friendly_context(self):
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO friendly_matches (home_id, away_id, status, created_at, updated_at) "
                       "VALUES (?, ?, 'accepted', ?, ?)", (self.anders, self.tieres, now(), now()))
            db.commit()
        self.post(state())
        friendly_id = self.rows("SELECT friendly_id FROM live_matches")[0]["friendly_id"]
        self.assertIsNotNone(friendly_id)
        self.assertIn("Amistoso", self.cards())
        # A página do amistoso é só dos dois: o link, só para eles.
        link = 'href="/torneios/amistosos/%d"' % friendly_id
        self.assertNotIn(link, self.cards())
        player = self.app.test_client()
        with player.session_transaction() as sess:
            sess["player_id"] = self.tieres
        self.assertIn(link, self.cards(player))

    def test_friendly_gets_the_game_score_and_needs_both(self):
        from app.tournament import friendly
        with self.app.app_context():
            db = get_db()
            fid = db.execute("INSERT INTO friendly_matches (home_id, away_id, status, created_at, updated_at) "
                             "VALUES (?, ?, 'accepted', ?, ?)", (self.anders, self.tieres, now(), now())).lastrowid
            db.commit()
        # LOCAL do jogo = TIERES (visitante no amistoso): 3 x 1 no jogo = 1 x 3 no amistoso.
        self.post(state(goals=(3, 1)))
        self.post(state(goals=(3, 1), period=1, clock=0, final=True, frame=90000))
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE live_matches SET finished_at = ?",
                       ((datetime.now(timezone.utc) - timedelta(minutes=2)).replace(microsecond=0).isoformat(),))
            db.commit()
        self.cards()  # a sessão ainda pode continuar (outro jogo): nada lançado
        self.assertEqual(self.rows("SELECT status FROM friendly_matches WHERE id = ?", fid)[0]["status"], "accepted")
        self.end_session()
        self.cards()  # reap -> report_finished
        row = self.rows("SELECT * FROM friendly_matches WHERE id = ?", fid)[0]
        live = self.rows("SELECT finished_at, game_report FROM live_matches")[0]
        self.assertEqual((row["status"], row["home_goals"], row["away_goals"], row["reported_by"], row["game_report"],
                          row["reported_at"]), ("reported", 1, 3, None, 1, live["finished_at"]))
        self.assertEqual(live["game_report"], "reportado (soma de 1 jogo)")
        # Os dois confirmam: o primeiro só marca a dele.
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["player_id"] = self.tieres
            sess["csrf_token"] = CSRF
        page = client.get("/torneios/amistosos/%d" % fid).get_data(as_text=True)
        self.assertIn("placar lido do jogo", page)
        client.post("/torneios/amistosos/%d/confirmar" % fid, data={"csrf_token": CSRF})
        row = self.rows("SELECT * FROM friendly_matches WHERE id = ?", fid)[0]
        self.assertEqual((row["status"], row["first_confirmed_by"]), ("reported", self.tieres))
        self.assertIn("Você confirmou o placar lido do jogo", client.get("/torneios/amistosos/%d" % fid).get_data(as_text=True))
        with self.app.app_context():
            db = get_db()
            friendly.confirm(db, fid, self.anders)
            db.commit()
        self.assertEqual(self.rows("SELECT status FROM friendly_matches WHERE id = ?", fid)[0]["status"], "confirmed")

    def end_session(self, nicks="TIERES, ANDERSHOW"):
        """A transmissão do Stream ao vivo dos dois terminou: um deles saiu da sessão."""
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO live_sessions (session_id, player_names, status, stored_name, started_at, updated_at, "
                       "ended_at) VALUES (?, ?, 'finished', 'x.krec', ?, ?, ?)",
                       ("s%d" % len(self.rows("SELECT id FROM live_sessions")), nicks, now(), now(), now()))
            db.commit()

    def stream(self, drops, nicks="TIERES, ANDERSHOW", ended=None):
        """A transmissão do Stream ao vivo da sessão (começou antes das
        partidas, terminou agora ou em `ended`) com quem saiu e em que quadro."""
        with self.app.app_context():
            db = get_db()
            sid = "t%d" % len(self.rows("SELECT id FROM live_sessions"))
            live_dir = Path(self.app.config["LIVE_DIR"])
            live_dir.mkdir(parents=True, exist_ok=True)
            (live_dir / (sid + ".krec")).write_bytes(krec(drops))
            started = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()
            db.execute("INSERT INTO live_sessions (session_id, player_names, status, stored_name, started_at, updated_at, "
                       "ended_at) VALUES (?, ?, 'finished', ?, ?, ?, ?)", (sid, nicks, sid + ".krec", started, now(), ended or now()))
            db.commit()

    def abandon(self, kickoff, minutes=3):
        """reap(): sem sinal, a partida vira interrompida com o fim no último informe."""
        old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE live_matches SET status = 'abandoned', finished_at = ?, updated_at = ? "
                       "WHERE kickoff_frame = ?", (old, old, kickoff))
            db.commit()

    def settle(self):
        """Os fins mandados há 2 minutos (passou o REPORT_SETTLE)."""
        old = (datetime.now(timezone.utc) - timedelta(minutes=2)).replace(microsecond=0).isoformat()
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE live_matches SET finished_at = ? WHERE finished_at IS NOT NULL", (old,))
            db.commit()

    def last_report(self):
        return self.rows("SELECT * FROM match_reports WHERE action = 'report' ORDER BY id DESC LIMIT 1")[0]

    def test_half_time_shows_interval_until_the_clock_runs(self):
        self.post(state(period=0, clock=9000))
        self.assertIn("1º tempo · 22&#39;", self.cards())
        self.post(state(period=0, clock=0, frame=30000))  # WE 2002: fim do 1º tempo
        html = self.cards()
        self.assertIn(">Intervalo<", html)
        self.assertNotIn("live-clock on", html)  # sem o risco animado no intervalo
        self.post(state(period=1, clock=18000, frame=31000))  # 2º tempo ainda não começou
        self.assertIn(">Intervalo<", self.cards())
        self.post(state(period=1, clock=17900, frame=32000))  # começou
        html = self.cards()
        self.assertIn("2º tempo · 45&#39;", html)
        self.assertIn("live-clock on", html)

    def test_interval_only_after_the_whistle(self):
        # Fork novo: com o relógio em 0 o jogo segue (um gol ainda pode sair); o
        # "Intervalo" só depois do apito ("over").
        self.post(state(period=0, clock=0, frame=26940, goals=(3, 2)).replace('"period"', '"over": 0, "period"'))
        html = self.cards()
        self.assertIn("1º tempo · 45+&#39;", html)
        self.assertIn("live-clock on", html)
        self.post(state(period=0, clock=0, frame=28170, goals=(3, 2)).replace('"period"', '"over": 1, "period"'))
        self.assertIn(">Intervalo<", self.cards())
        # Fim do 2º tempo empatado (prorrogação ou pênaltis a seguir).
        self.post(state(period=1, clock=0, frame=40000, goals=(3, 3)).replace('"period"', '"over": 1, "period"'))
        html = self.cards()
        self.assertIn(">Fim do tempo normal<", html)
        self.assertNotIn("live-clock on", html)

    def test_player_who_left_interrupts_the_match_at_once(self):
        self.post(state(goals=(1, 0)))
        client = self.app.test_client()
        reply = client.post("/api/live/match", headers={"Authorization": "Bearer " + self.token("TIERES")}, data={
            "state": state(goals=(1, 0)), "players": "TIERES,ANDERSHOW", "reporter": "TIERES", "left": "1",
            "room": "PCSX ReARMed: Euro vs Copa America 2004.bin"})
        self.assertEqual(reply.status_code, 200)
        row = self.rows("SELECT status, finished_at FROM live_matches")[0]
        self.assertEqual(row["status"], "abandoned")
        self.assertIsNotNone(row["finished_at"])
        self.assertIn(">Interrompido<", self.cards())
        # O PC de quem ficou (modo Server) ainda manda o fim: a partida é encerrada.
        self.post(state(goals=(2, 0), period=1, clock=0, final=True, frame=90000), reporter="ANDERSHOW")
        self.assertEqual(self.rows("SELECT status FROM live_matches")[0]["status"], "finished")

    def test_own_goal_scorer_and_penalty_on_the_card(self):
        scorers = ([["Gol contra", 17, "Z.Suzuki", "og"], ["Guesung", 28, "", "pen"]], [["D.Maeda", 8, "Y.Nagatomo"]])
        self.post(state(goals=(2, 1), scorers=scorers))
        html = self.cards()
        self.assertIn("Gol contra 17' <small>(Z.Suzuki)</small>", html)
        self.assertIn("Guesung 28' <small>(Pênalti)</small>", html)
        self.assertIn("D.Maeda 8' <small>(assist. Y.Nagatomo)</small>", html)
        # Fork antigo: gol contra sem o autor, como antes.
        self.post(state(kickoff=50000, goals=(1, 0), scorers=([["Gol contra", 17, ""]], [])))
        self.assertIn("Gol contra 17'</li>", self.cards())

    def test_championship_game_left_mid_game_counts_and_marks_who_left(self):
        from app.tournament import reports, store, xp
        self.make_league()  # mandante no campeonato = ANDERSHOW; no jogo o LOCAL é o TIERES
        self.post(state(kickoff=1000, frame=5000, goals=(0, 2)))  # ANDERSHOW ganhava 2 x 0...
        self.abandon(1000)
        self.stream([("TIERES", 5400)])  # ... e o TIERES saiu
        self.cards()
        match = self.rows("SELECT * FROM tournament_matches")[0]
        report = self.last_report()
        self.assertEqual((match["status"], match["quitter_id"], report["home_goals"], report["away_goals"]),
                         ("reported", self.tieres, 2, 0))
        self.assertIn("vale o placar de quando parou", report["comment"])
        self.assertEqual(self.rows("SELECT game_report FROM live_matches")[0]["game_report"],
                         "reportado; TIERES saiu no meio do jogo")
        page = self.app.test_client().get("/torneios/liga-1/jogos/%d" % match["id"]).get_data(as_text=True)
        self.assertIn("TIERES saiu da partida no meio do jogo", page)
        self.assertIn("perde o triplo de XP - mesmo que tenha sido queda de conexão", page)
        self.assertIn("voltem em até 20 minutos e joguem de novo", page)
        # Os dois confirmam: vale, e no XP o jogo vai com quem saiu.
        with self.app.app_context():
            db = get_db()
            tournament = store.get_tournament(db, self.rows("SELECT id FROM tournaments")[0]["id"])
            reports.confirm(db, tournament, match["id"], self.tieres)
            self.assertTrue(reports.confirm(db, tournament, match["id"], self.anders))
            db.commit()
            rows = [dict(r) for r in db.execute(xp._MATCHES)]
        self.assertEqual([(r["id"], r["quitter"]) for r in rows], [(match["id"], self.tieres)])

    def test_championship_game_finished_by_who_stayed_marks_who_left(self):
        self.make_league()
        self.post(state(kickoff=1000, frame=5000, goals=(0, 1)))
        # Modo Server: o TIERES caiu no quadro 6000 e o jogo seguiu no PC do
        # ANDERSHOW, que mandou o fim sozinho.
        self.post(state(kickoff=1000, frame=90000, goals=(0, 3), period=1, clock=0, final=True), reporter="ANDERSHOW")
        self.settle()
        self.stream([("TIERES", 6000)])
        self.cards()
        match = self.rows("SELECT * FROM tournament_matches")[0]
        report = self.last_report()
        self.assertEqual((match["status"], match["quitter_id"], report["home_goals"], report["away_goals"]),
                         ("reported", self.tieres, 3, 0))

    def test_leaving_after_the_final_whistle_is_not_quitting(self):
        self.make_league()
        self.post(state(kickoff=1000, frame=5000, goals=(1, 1)))
        self.post(state(kickoff=1000, frame=90000, goals=(1, 1), period=1, clock=0, final=True))
        self.settle()
        self.stream([("TIERES", 90400)])
        self.cards()
        match = self.rows("SELECT * FROM tournament_matches")[0]
        self.assertEqual((match["status"], match["quitter_id"]), ("reported", None))
        self.assertEqual(self.rows("SELECT game_report FROM live_matches")[0]["game_report"], "reportado")

    def test_playing_again_after_a_drop_counts_the_new_game(self):
        self.make_league()
        self.post(state(kickoff=1000, frame=5000, goals=(0, 2)))
        # O TIERES caiu; voltaram com o ANDERSHOW de host (1P) e jogaram de novo:
        # a anterior vira interrompida e a nova vai para o mesmo jogo do campeonato.
        again = dict(kickoff=3000, pads=(1, 0), goals=(1, 0))  # LOCAL = TIERES (controle 2)
        self.post(state(frame=9000, **again), players="ANDERSHOW,TIERES")
        rows = self.rows("SELECT status, tournament_match_id FROM live_matches ORDER BY id")
        self.assertEqual([r["status"] for r in rows], ["abandoned", "live"])
        self.assertEqual(rows[0]["tournament_match_id"], rows[1]["tournament_match_id"])
        self.post(state(frame=90000, period=1, clock=0, final=True, **again), players="ANDERSHOW,TIERES")
        self.settle()
        # A transmissão da 1ª sessão acabou quando ele caiu (a da nova não tem saída).
        self.stream([("TIERES", 5400)], ended=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
        self.cards()
        match = self.rows("SELECT * FROM tournament_matches")[0]
        report = self.last_report()
        self.assertEqual((match["status"], match["quitter_id"], report["home_goals"], report["away_goals"]),
                         ("reported", None, 0, 1))
        self.assertEqual([r["game_report"] for r in self.rows("SELECT game_report FROM live_matches ORDER BY id")],
                         ["jogaram de novo: vale a partida seguinte", "reportado"])

    def test_manual_report_clears_who_left(self):
        from app.tournament import reports, store
        self.make_league()
        self.post(state(kickoff=1000, frame=5000, goals=(0, 2)))
        self.abandon(1000)
        self.stream([("TIERES", 5400)])
        self.cards()
        with self.app.app_context():
            db = get_db()
            tournament = store.get_tournament(db, self.rows("SELECT id FROM tournaments")[0]["id"])
            match_id = self.rows("SELECT id FROM tournament_matches")[0]["id"]
            # Contestado e o organizador decide outro placar: sem a penalidade.
            reports.dispute(db, tournament, match_id, self.tieres, "a internet caiu, vamos jogar de novo")
            reports.resolve(db, tournament, match_id, (1, 1, None, None))
            db.commit()
        self.assertEqual(self.rows("SELECT quitter_id FROM tournament_matches")[0]["quitter_id"], None)

    def play(self, kickoff, goals, pads=(0, 1), finish=True):
        """Uma partida inteira dos dois PCs (terminada há 2 minutos)."""
        self.post(state(kickoff=kickoff, goals=goals, pads=pads))
        if finish:
            self.post(state(kickoff=kickoff, goals=goals, pads=pads, period=1, clock=0, final=True, frame=90000))
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE live_matches SET finished_at = ? WHERE kickoff_frame = ? AND finished_at IS NOT NULL",
                       ((datetime.now(timezone.utc) - timedelta(minutes=2)).replace(microsecond=0).isoformat(), kickoff))
            db.commit()

    def test_friendly_sums_the_games_of_the_session(self):
        t, group, entries = self.make_league()  # há jogo de campeonato em aberto entre os dois
        with self.app.app_context():
            db = get_db()
            fid = db.execute("INSERT INTO friendly_matches (home_id, away_id, status, created_at, updated_at) "
                             "VALUES (?, ?, 'accepted', ?, ?)", (self.tieres, self.anders, now(), now())).lastrowid
            db.commit()
        # P1 = TIERES, P2 = ANDERSHOW (controles 1 e 2): 2 x 1, 0 x 3, 1 x 3.
        self.play(1000, (2, 1))
        self.play(40000, (0, 3))
        self.play(80000, (1, 3))
        rows = self.rows("SELECT friendly_id, tournament_match_id FROM live_matches")
        self.assertEqual({(r["friendly_id"], r["tournament_match_id"]) for r in rows}, {(fid, None)})  # amistoso primeiro
        self.assertEqual(self.cards().count('<article class="live-card'), 3)  # um card por jogo
        self.cards()
        self.assertEqual(self.rows("SELECT status FROM friendly_matches WHERE id = ?", fid)[0]["status"], "accepted")
        self.end_session()
        self.cards()
        row = self.rows("SELECT * FROM friendly_matches WHERE id = ?", fid)[0]
        self.assertEqual((row["status"], row["home_goals"], row["away_goals"]), ("reported", 3, 7))
        self.assertEqual({r["game_report"] for r in self.rows("SELECT game_report FROM live_matches")},
                         {"reportado (soma de 3 jogos)"})
        # A página do amistoso lista as partidas e a soma, para conferir.
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["player_id"] = self.tieres
        page = client.get("/torneios/amistosos/%d" % fid).get_data(as_text=True)
        self.assertEqual(page.count('class="fg-row'), 3)  # uma linha por partida, inteira um link
        self.assertNotIn("<td>Soma</td>", page)
        self.assertIn('data-dialog="rejeitar-%d">Não aceitar o placar</button>' % fid, page)
        self.assertIn('name="motivo" rows="3" minlength="3" maxlength="300" required', page)
        self.assertNotIn("Não aceito este placar", page)
        for score in ("2 x 1", "0 x 3", "1 x 3"):
            self.assertIn("<strong>%s</strong>" % score, page)
        self.assertEqual(page.count('title="Ver o card no Ao vivo (quem fez os gols)"'), 3)
        self.assertEqual(page.count('class="fg-crest"'), 6)  # escudo dos dois times de cada partida
        self.assertRegex(page, r'class="fg-when"><span>\d{2}/\d{2}/\d{4}</span><span>\d{2}:\d{2}</span>')
        # Na lista de amistosos: as partidas no card e, no topo, "Esperando você".
        listing = client.get("/torneios/amistosos").get_data(as_text=True)
        self.assertIn("Esperando você (1)", listing)
        self.assertLess(listing.index("Esperando você"), listing.index("Chamar para um amistoso"))
        self.assertEqual(listing.count('class="fg-row'), 3)
        # O jogo de campeonato entre os dois continua sem placar.
        self.assertEqual(self.rows("SELECT status FROM tournament_matches WHERE status != 'confirmed'")[0]["status"],
                         "scheduled")

    def test_friendly_each_game_counts_for_xp_and_the_total_only_with_aggregate(self):
        """Cada jogo do amistoso vale sozinho no XP e no V/E/D, com o (i) do XP
        de cada um; a soma (placar agregado) só aparece com a opção do convite."""
        from unittest import mock
        from app.db import migrate_db
        from app.tournament import friendly, stats, xp
        with self.app.app_context():
            db = get_db()
            fid = db.execute("INSERT INTO friendly_matches (home_id, away_id, status, created_at, updated_at) "
                             "VALUES (?, ?, 'accepted', ?, ?)", (self.tieres, self.anders, now(), now())).lastrowid
            db.commit()
        # TIERES (mandante, P1) x ANDERSHOW: 2 x 1, 0 x 3, 1 x 3 - somados, 3 x 7.
        self.play(1000, (2, 1))
        self.play(40000, (0, 3))
        self.play(80000, (1, 3))
        self.end_session()
        self.cards()
        sql = "SELECT number, home_goals, away_goals, quit FROM friendly_games WHERE friendly_id = ? ORDER BY number"
        games = [tuple(r) for r in self.rows(sql, fid)]
        self.assertEqual(games, [(1, 2, 1, 0), (2, 0, 3, 0), (3, 1, 3, 0)])
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["player_id"] = self.tieres
            sess["csrf_token"] = CSRF
        page = client.get("/torneios/amistosos/%d" % fid).get_data(as_text=True)
        # Sem "Placar agregado": na linha, só os jogadores; os placares, na lista de jogos.
        self.assertIn('<strong class="friendly-score-text">x</strong>', page)
        self.assertNotIn("3 x 7", page)
        self.assertIn("Os 3 jogos terminaram: confira os placares na lista", page)
        self.assertIn("<li>Jogo 2: 0 x 3</li>", page)  # na janela "Não aceitar o placar"
        self.assertNotIn('data-dialog="jogo-xp-', page)  # o XP de cada jogo só depois de confirmado
        with mock.patch.object(friendly, "MIN_XP", 0):
            client.post("/torneios/amistosos/%d/confirmar" % fid, data={"csrf_token": CSRF})
            with self.app.app_context():
                db = get_db()
                friendly.confirm(db, fid, self.anders)
                db.commit()
        row = self.rows("SELECT status, ranked FROM friendly_matches WHERE id = ?", fid)[0]
        self.assertEqual((row["status"], row["ranked"]), ("confirmed", 1))
        with self.app.app_context():
            db = get_db()
            totals, deltas = xp.compute(db)
            # 1º: TIERES +20 (ANDERSHOW sem XP a perder); 2º: ANDERSHOW +20, TIERES -20; 3º: ANDERSHOW +20.
            self.assertEqual([deltas["a%d-%d" % (fid, n)] for n in (1, 2, 3)],
                             [{self.tieres: 20, self.anders: 0}, {self.tieres: -20, self.anders: 20},
                              {self.tieres: 0, self.anders: 20}])
            self.assertNotIn("a%d" % fid, deltas)  # nada do agregado
            self.assertEqual((totals[self.tieres], totals[self.anders]), (0, 40))
            record = stats.player_record(db, self.tieres)
            self.assertEqual((record["played"], record["wins"], record["losses"]), (3, 1, 2))
        page = client.get("/torneios/amistosos/%d" % fid).get_data(as_text=True)
        for n in (1, 2, 3):
            self.assertIn('data-dialog="jogo-xp-%d-%d"' % (fid, n), page)
        self.assertIn("Vale para o Ranking e o XP (soma dos 3 jogos)", page)
        dialog = page[page.index('id="jogo-xp-%d-2"' % fid):]
        dialog = dialog[:dialog.index("</dialog>")]
        self.assertIn("TIERES 0 x 3 ANDERSHOW", dialog)
        self.assertIn(">-20 XP</span>", dialog)
        self.assertIn(">+20 XP</span>", dialog)
        status = client.get("/torneios/status").get_data(as_text=True)
        self.assertIn('id="partida-a%d-3"' % fid, status)
        self.assertIn("· jogo 3", status)
        # Com "Placar agregado": a soma aparece; o XP continua jogo a jogo.
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE friendly_matches SET aggregate = 1 WHERE id = ?", (fid,))
            db.commit()
            self.assertEqual(xp.compute(db)[0][self.anders], 40)
        page = client.get("/torneios/amistosos/%d" % fid).get_data(as_text=True)
        self.assertIn(">3 x 7</strong>", page)
        self.assertIn("Placar agregado</span>", page)
        # Amistosos de antes da tabela: os jogos são refeitos das partidas no início (migrate_db).
        with self.app.app_context():
            get_db().execute("DELETE FROM friendly_games")
            get_db().commit()
            migrate_db()
        self.assertEqual([tuple(r) for r in self.rows(sql, fid)], games)

    def test_friendly_history_filter_and_limit(self):
        from tests.test_tournament_store import add_player as add
        with self.app.app_context():
            db = get_db()
            third = add(db, "RIVAL")
            for i in range(22):  # 22 amistosos encerrados, os 2 mais antigos contra o RIVAL
                away = third if i < 2 else self.anders
                at = "2026-10-%02dT12:00:00+00:00" % (i + 1)
                db.execute("INSERT INTO friendly_matches (home_id, away_id, status, home_goals, away_goals, reported_at, "
                           "confirmed_at, created_at, updated_at) VALUES (?, ?, 'confirmed', 1, 0, ?, ?, ?, ?)",
                           (self.tieres, away, at, at, at, at))
            db.commit()
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["player_id"] = self.tieres
        page = client.get("/torneios/amistosos").get_data(as_text=True)
        self.assertIn("Mostrando os 20 mais recentes de 22", page)
        self.assertEqual(page.count('class="friendly-card'), 20)
        self.assertNotIn(">RIVAL<", without_suggestions(page))  # os dois mais antigos ficaram de fora
        self.assertIn('data-nick="RIVAL"', page)  # mas o filtro "Adversário" sugere o RIVAL
        page = client.get("/torneios/amistosos?adversario=riv").get_data(as_text=True)
        self.assertEqual(page.count('class="friendly-card'), 2)
        self.assertNotIn("Mostrando os", page)
        page = client.get("/torneios/amistosos?de=2026-10-21T00:00&ate=2026-10-22T23:59").get_data(as_text=True)
        self.assertEqual(page.count('class="friendly-card'), 2)
        page = client.get("/torneios/amistosos?adversario=ninguem").get_data(as_text=True)
        self.assertIn("Nenhum amistoso com esses filtros", page)

    def test_friendly_session_ends_after_a_gap_without_games(self):
        with self.app.app_context():
            db = get_db()
            fid = db.execute("INSERT INTO friendly_matches (home_id, away_id, status, created_at, updated_at) "
                             "VALUES (?, ?, 'accepted', ?, ?)", (self.anders, self.tieres, now(), now())).lastrowid
            db.commit()
        self.play(1000, (2, 2))
        self.play(40000, (1, 0), finish=False)  # o segundo ficou pela metade: um saiu
        with self.app.app_context():
            db = get_db()
            old = (datetime.now(timezone.utc) - timedelta(minutes=25)).replace(microsecond=0).isoformat()
            db.execute("UPDATE live_matches SET status = CASE WHEN kickoff_frame = 40000 THEN 'abandoned' ELSE status END, "
                       "finished_at = ?, updated_at = ?", (old, old))
            db.commit()
        self.cards()
        # O interrompido soma com o placar de quando parou: 2x2 + (ANDERSHOW 0 x 1 TIERES) = 2 x 3.
        row = self.rows("SELECT * FROM friendly_matches WHERE id = ?", fid)[0]
        self.assertEqual((row["status"], row["home_goals"], row["away_goals"], row["quitter_id"]), ("reported", 2, 3, None))
        notes = {r["game_report"] for r in self.rows("SELECT game_report FROM live_matches")}
        self.assertEqual(notes, {"reportado (soma de 2 jogos)"})

    def test_who_left_mid_game_is_marked_and_loses_triple_xp(self):
        from app.tournament import xp
        with self.app.app_context():
            db = get_db()
            fid = db.execute("INSERT INTO friendly_matches (home_id, away_id, status, created_at, updated_at) "
                             "VALUES (?, ?, 'accepted', ?, ?)", (self.tieres, self.anders, now(), now())).lastrowid
            db.commit()
        self.play(1000, (1, 1))
        self.post(state(kickoff=40000, frame=41000, goals=(0, 2)))  # ANDERSHOW ganhava 2 x 0...
        self.abandon(40000)
        self.stream([("ANDERSHOW", 20000), ("TIERES", 41500)])  # ... e o TIERES saiu (a do 1º jogo não conta)
        self.cards()
        row = self.rows("SELECT * FROM friendly_matches WHERE id = ?", fid)[0]
        self.assertEqual((row["status"], row["home_goals"], row["away_goals"], row["quitter_id"]), ("reported", 1, 3, self.tieres))
        self.assertIn("TIERES saiu no meio do último jogo", self.rows("SELECT game_report FROM live_matches")[0]["game_report"])
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["player_id"] = self.anders
        page = client.get("/torneios/amistosos/%d" % fid).get_data(as_text=True)
        # O adversário decide, ao confirmar, se quem saiu perde o triplo de XP (caixa marcada).
        self.assertIn('name="penalizar" value="1" form="confirmar-%d" checked' % fid, page)
        self.assertIn('<input type="hidden" name="quem_saiu" value="%d">' % self.tieres, page)
        self.assertIn("TIERES saiu da partida no meio de um jogo (o placar até ali conta) e perde o triplo de XP.", page)
        self.assertIn("voltem em até 20 minutos e terminem outra partida", page)
        with client.session_transaction() as sess:
            sess["player_id"] = self.tieres
        page = client.get("/torneios/amistosos/%d" % fid).get_data(as_text=True)
        self.assertNotIn('name="penalizar"', page)  # quem saiu não decide
        self.assertIn("ANDERSHOW decide se você perde o triplo de XP", page)
        # XP: quem saiu perde o triplo; quem ganhou ganha o normal.
        start = {self.tieres: 300, self.anders: 300}
        normal = xp.replay([{"id": "a1", "home": self.tieres, "away": self.anders, "home_goals": 1, "away_goals": 3,
                             "quitter": None}], dict(start))[1]["a1"]
        quit_ = xp.replay([{"id": "a1", "home": self.tieres, "away": self.anders, "home_goals": 1, "away_goals": 3,
                            "quitter": self.tieres}], dict(start))[1]["a1"]
        self.assertEqual(quit_[self.anders], normal[self.anders])
        self.assertEqual(quit_[self.tieres], normal[self.tieres] * 3)
        # Jogo a jogo: o triplo só no jogo de que ele saiu (o 2º).
        sql = "SELECT number, home_goals, away_goals, quit FROM friendly_games WHERE friendly_id = ? ORDER BY number"
        self.assertEqual([tuple(r) for r in self.rows(sql, fid)], [(1, 1, 1, 0), (2, 0, 2, 1)])
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE friendly_matches SET status = 'confirmed', ranked = 1, confirmed_at = ? WHERE id = ?",
                       (now(), fid))
            rows = [dict(r) for r in db.execute(xp._MATCHES)]
        self.assertEqual({r["id"]: r["quitter"] for r in rows}, {"a%d-1" % fid: None, "a%d-2" % fid: self.tieres})
        tripled = set()
        deltas = xp.replay(rows, {self.tieres: 300, self.anders: 300}, tripled=tripled)[1]
        self.assertEqual(deltas["a%d-2" % fid], {self.tieres: -60, self.anders: 20})
        self.assertEqual(tripled, {"a%d-2" % fid})

    def test_friendly_game_score_confirms_by_itself_in_24h(self):
        from app.tournament import friendly
        with self.app.app_context():
            db = get_db()
            fid = db.execute("INSERT INTO friendly_matches (home_id, away_id, status, created_at, updated_at) "
                             "VALUES (?, ?, 'accepted', ?, ?)", (self.tieres, self.anders, now(), now())).lastrowid
            friendly.report_from_game(db, fid, 2, 0, now())
            db.commit()
            self.assertEqual(friendly.auto_confirm(db, at=datetime.now(timezone.utc) + timedelta(hours=23)), 0)
            self.assertEqual(friendly.auto_confirm(db, at=datetime.now(timezone.utc) + timedelta(hours=24, minutes=1)), 1)
        row = self.rows("SELECT * FROM friendly_matches WHERE id = ?", fid)[0]
        self.assertEqual((row["status"], row["home_goals"], row["away_goals"]), ("confirmed", 2, 0))

    def test_test_matches_only_for_admin(self):
        self.post(state(), test=True)
        self.assertNotIn("S.Vryzas", self.cards())
        admin = self.admin()
        self.assertIn("S.Vryzas", self.cards(admin))
        self.assertIn("Teste", self.cards(admin))
        admin.post("/ao-vivo/limpar-testes", data={"csrf_token": CSRF})
        self.assertEqual(len(self.rows("SELECT * FROM live_matches")), 0)

    def test_stale_then_reaped(self):
        self.post(state(period=1, clock=0))
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        with self.app.app_context():
            get_db().execute("UPDATE live_matches SET updated_at = ?", (old,))
            get_db().commit()
        self.assertIn("Sem sinal", self.cards())
        older = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
        with self.app.app_context():
            get_db().execute("UPDATE live_matches SET updated_at = ?", (older,))
            get_db().commit()
        self.cards()
        self.assertEqual(self.rows("SELECT status FROM live_matches")[0]["status"], "finished")
        self.assertEqual(self.cards(so_ao_vivo="1"), "")

    def test_admin_names_a_slot(self):
        self.post(state(names=("FINLAND", "CROACIA"), v=2))
        admin = self.admin()
        self.assertIn("FINLAND", admin.get("/admin/ao-vivo/times").get_data(as_text=True))
        admin.post("/admin/ao-vivo/times", data={"csrf_token": CSRF, "content_id": "BA9BDE21:1C473F60", "slot": "16",
                                                 "name": "Camarões"})
        row = self.rows("SELECT * FROM iso_team_slots WHERE slot = 16")[0]
        self.assertEqual(row["source"], "admin")
        self.assertIn("Camarões", self.cards())

    def test_leaderboard_skips_own_goals_and_ranks_ties(self):
        from app import live
        match = {"id": 7, "state_json": json.dumps({"teams": [
            {"id": 1, "goals": 4, "scorers": [["Gol contra", 5, "J.Leko", "og"], ["Gol contra", 9, ""],
                                              ["S.Vryzas", 17, "Karagounis"], ["S.Vryzas", 60, "", "pen"]]},
            {"id": 2, "goals": 2, "scorers": [["J.Leko", 20, "Modric"], ["", 30, ""]]}]}),
                 "local_players_json": '["TIERES"]', "visita_players_json": '["ANDERSHOW"]',
                 "tournament_match_id": None, "content_id": "x"}
        with self.app.app_context():
            events = live.goal_events(get_db(), match)
        self.assertEqual([(e["name"], e["assist"], e["pen"], e["nick"]) for e in events],
                         [("S.Vryzas", "Karagounis", False, "TIERES"), ("S.Vryzas", "", True, "TIERES"),
                          ("J.Leko", "Modric", False, "ANDERSHOW")])
        # O mesmo jogador do jogo com outro dono (os dois de Grécia) é outra linha.
        events.append(dict(events[0], nick="ANDERSHOW", assist="Modric", match_id=8))
        board = live.leaderboard(events)
        self.assertEqual([(r["position"], r["name"], r["nick"], r["goals"], r["pens"]) for r in board["scorers"]],
                         [(1, "S.Vryzas", "TIERES", 2, 1), (2, "J.Leko", "ANDERSHOW", 1, 0),
                          (2, "S.Vryzas", "ANDERSHOW", 1, 0)])
        self.assertEqual([(r["position"], r["name"], r["assists"], r["games"]) for r in board["assists"]],
                         [(1, "Modric", 2, 2), (2, "Karagounis", 1, 1)])

    def test_championship_scorers_only_from_confirmed_games_with_the_same_score(self):
        from app.tournament import reports
        self.make_league()
        self.finish_live(goals=(2, 1))  # TIERES (Grécia, LOCAL) 2 x 1 ANDERSHOW
        client = self.app.test_client()
        html = client.get("/torneios/liga-1?aba=artilharia").get_data(as_text=True)
        self.assertIn('class="active">Artilharia</a>', html)
        # Reportado, ainda não confirmado: os gols já somam, em laranja, "a confirmar".
        self.assertIn("Soma de 1 a confirmar do campeonato", html)
        self.assertIn('<tr class="scorer-pending">', html)
        self.assertIn('title="S.Vryzas possui 2 gols a serem confirmados">2 a confirmar</small>', html)
        self.assertIn('title="J.Leko possui 1 gol a ser confirmado">1 a confirmar</small>', html)
        self.assertIn("provisional-legend", html)
        # Página do jogo reportado: os gols em laranja, com o (i) de que faltam confirmar, e o card no Ao vivo.
        match_id = self.rows("SELECT id FROM tournament_matches")[0]["id"]
        page = client.get("/torneios/liga-1/jogos/%d" % match_id).get_data(as_text=True)
        self.assertIn('class="live-scorers match-scorers pending"', page)
        self.assertIn('data-dialog="gols-a-confirmar"', page)
        self.assertIn(">Ver card Ao Vivo</a>", page)
        self.assertNotIn(">Gols</a>", page)
        with self.app.app_context():
            reports.auto_confirm(get_db(), at=datetime.now(timezone.utc) + timedelta(hours=25))
        html = client.get("/torneios/liga-1?aba=artilharia").get_data(as_text=True)
        self.assertIn("Soma de 1 jogo confirmado do campeonato", html)
        self.assertNotIn("scorer-pending", html)
        self.assertNotIn("provisional-legend", html)
        scorers, assists = html.split("<h2>Assistências</h2>")
        self.assertIn("<strong>S.Vryzas</strong><small>TIERES</small>", scorers)
        self.assertRegex(scorers, r"(?s)S\.Vryzas.*?Grécia.*?<strong>2</strong>")
        self.assertRegex(scorers, r"(?s)J\.Leko.*?Croácia.*?<strong>1</strong>")
        self.assertRegex(assists, r"(?s)Karagounis.*?<strong>2</strong>")
        self.assertNotIn("J.Leko", assists)  # gol sem assistência
        # Página do jogo: os gols de cada lado do ponto de vista do campeonato (mandante = ANDERSHOW).
        page = client.get("/torneios/liga-1/jogos/%d" % match_id).get_data(as_text=True)
        self.assertNotIn("gols-a-confirmar", page)
        self.assertIn(">Ver card Ao Vivo</a>", page)
        home, away = page.split('class="match-ball"')
        self.assertIn("<li>J.Leko 20'</li>", home)
        self.assertEqual(away.count("<li>S.Vryzas 17' <small>(assist. Karagounis)</small></li>"), 2)
        # Placar corrigido à mão (outro que o lido do jogo): os gols lidos não valem.
        with self.app.app_context():
            get_db().execute("UPDATE tournament_matches SET home_goals = 3 WHERE status = 'confirmed'")
            get_db().commit()
        self.assertNotIn("S.Vryzas", client.get("/torneios/liga-1?aba=artilharia").get_data(as_text=True))

    def test_championship_game_page_while_live(self):
        self.make_league()
        self.post(state(goals=(1, 0)))  # rolando agora
        match_id = self.rows("SELECT id FROM tournament_matches")[0]["id"]
        page = self.app.test_client().get("/torneios/liga-1/jogos/%d" % match_id).get_data(as_text=True)
        self.assertIn(">Placar Ao Vivo</a>", page)
        self.assertIn('class="live-scorers match-scorers pending"', page)
        self.assertIn("A partida está rolando agora", page)

    def test_friendly_scorers_with_more_than_one_game(self):
        with self.app.app_context():
            db = get_db()
            fid = db.execute("INSERT INTO friendly_matches (home_id, away_id, status, created_at, updated_at) "
                             "VALUES (?, ?, 'accepted', ?, ?)", (self.tieres, self.anders, now(), now())).lastrowid
            db.commit()
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["player_id"] = self.tieres
        self.play(1000, (2, 1))
        self.end_session()
        self.cards()
        self.assertNotIn("<h2>Artilharia</h2>", client.get("/torneios/amistosos/%d" % fid).get_data(as_text=True))
        with self.app.app_context():
            db = get_db()
            fid = db.execute("INSERT INTO friendly_matches (home_id, away_id, status, created_at, updated_at) "
                             "VALUES (?, ?, 'accepted', ?, ?)", (self.tieres, self.anders, now(), now())).lastrowid
            db.execute("UPDATE live_sessions SET ended_at = ?",
                       ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"),))
            db.commit()
        self.play(40000, (1, 0))
        self.play(80000, (2, 2))
        self.end_session()
        self.cards()
        page = client.get("/torneios/amistosos/%d" % fid).get_data(as_text=True)
        self.assertIn("Soma de 2 a confirmar deste amistoso", page)  # amistoso reportado, ainda sem confirmação
        scorers, assists = page.split("<h2>Assistências</h2>")
        self.assertRegex(scorers, r"(?s)S\.Vryzas</strong><small>TIERES</small>.*?<strong>3</strong>")
        self.assertRegex(scorers, r"(?s)J\.Leko</strong><small>ANDERSHOW</small>.*?<strong>2</strong>")
        self.assertRegex(assists, r"(?s)Karagounis.*?<strong>3</strong>")


if __name__ == "__main__":
    unittest.main()
