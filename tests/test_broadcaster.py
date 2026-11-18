"""Transmissão das partidas no YouTube (app/broadcaster.py): a fila (jogos de
campeonato sozinhos, os outros pelo "Transmitir" do administrador), o que o
agente do broadcaster recebe e conta (POST /api/broadcaster/poll) e o link no card."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from app.broadcaster import LEAD_FRAMES, TAIL_FRAMES, _hash
from app.db import get_db, now, set_setting
from tests import test_live as base
from tests.test_live import CSRF, state

ROOM = "PCSX ReARMed: Euro vs Copa America 2004.bin"
TOKEN = "token-do-notebook"


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


class BroadcasterTest(unittest.TestCase):
    # Os ajudantes dos testes do Ao vivo (sem herdar os testes deles).
    tearDown = base.LiveMatchesTest.tearDown
    token = base.LiveMatchesTest.token
    post = base.LiveMatchesTest.post
    rows = base.LiveMatchesTest.rows
    admin = base.LiveMatchesTest.admin
    cards = base.LiveMatchesTest.cards
    make_tournament = base.LiveMatchesTest.make_tournament

    def setUp(self):
        base.LiveMatchesTest.setUp(self)
        self.app.config["PUBLIC_URL"] = "https://we2002.example"
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO broadcast_agents (name, token_hash, created_at) VALUES ('notebook', ?, ?)",
                       (_hash(TOKEN), now()))
            db.commit()

    # --- ajudantes ---------------------------------------------------------------

    def session(self, sid="sess1", players="TIERES, ANDERSHOW", game=ROOM, minutes=5, status="live"):
        """O Stream ao vivo da sala (começou nos menus, antes do card)."""
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO live_sessions (session_id, game_name, player_names, status, stored_name, started_at, "
                       "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (sid, game, players, status, sid + ".krec",
                                                                    _ago(minutes), now()))
            db.commit()

    def poll(self, jobs=(), token=TOKEN, slots=1, **status):
        reply = self.app.test_client().post("/api/broadcaster/poll", headers={"Authorization": "Bearer " + token},
                                            json={"version": "agent-1", "slots": slots, "status": status,
                                                  "jobs": list(jobs)})
        return reply.status_code, reply.get_json()

    def broadcast(self):
        return self.rows("SELECT * FROM broadcasts ORDER BY id")

    # --- testes ------------------------------------------------------------------

    def test_poll_needs_the_agent_token(self):
        reply = self.app.test_client().post("/api/broadcaster/poll", json={})
        self.assertEqual(reply.status_code, 401)
        self.assertEqual(self.poll(token="errado")[0], 401)
        code, data = self.poll(cpu=12)
        self.assertEqual((code, data["ok"], data["jobs"]), (200, 1, []))
        agent = self.rows("SELECT * FROM broadcast_agents")[0]
        self.assertEqual((agent["version"], json.loads(agent["status_json"])["cpu"]), ("agent-1", 12))
        self.assertIsNotNone(agent["last_seen_at"])

    def test_tournament_match_goes_live_and_stops_after_the_end(self):
        self.make_tournament()
        self.post(state(kickoff=5000, frame=8000))
        # Sem o Stream ao vivo da partida ainda: fica na fila.
        self.assertEqual(self.poll()[1]["jobs"], [])
        self.assertEqual((self.broadcast()[0]["reason"], self.broadcast()[0]["status"]), ("campeonato", "queued"))
        self.session()
        jobs = self.poll()[1]["jobs"]
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual((job["action"], job["session_id"], job["start_frame"], job["room"], job["game_file"]),
                         ("start", "sess1", 5000 - LEAD_FRAMES, ROOM, "Euro vs Copa America 2004.bin"))
        self.assertEqual(job["title"], "TIERES x ANDERSHOW | Euro WE Camp - Final: Ida | WE Camp")
        self.assertIn("RetroArch TIERES V.0.13 com Kaillera Netplay", job["description"])
        self.assertIn("https://we2002.example/ao-vivo?partida=%d" % self.rows("SELECT id FROM live_matches")[0]["id"],
                      job["description"])
        self.assertEqual(job["privacy"], "unlisted")
        self.assertIsNone(job["iso"])  # campeonato sem ISO enviada
        self.assertEqual(self.broadcast()[0]["status"], "assigned")
        # O agente ainda não confirmou: manda de novo; confirmou (starting), não manda mais.
        self.assertEqual(self.poll()[1]["jobs"][0]["action"], "start")
        bid = job["id"]
        self.assertEqual(self.poll([{"id": bid, "state": "starting", "youtube_id": "abc",
                                     "youtube_url": "https://youtu.be/abc"}])[1]["jobs"], [])
        self.assertIn("Assistir no YouTube", self.cards())
        self.poll([{"id": bid, "state": "live"}])
        row = self.broadcast()[0]
        self.assertEqual((row["status"], row["youtube_url"]), ("live", "https://youtu.be/abc"))
        self.assertIsNotNone(row["started_at"])
        # Fim da partida: para 20 s depois do último quadro informado.
        self.post(state(kickoff=5000, frame=120000, period=1, clock=0, final=True))
        stop = self.poll([{"id": bid, "state": "live"}])[1]["jobs"]
        self.assertEqual(stop, [{"id": bid, "action": "stop", "stop_frame": 120000 + TAIL_FRAMES}])
        self.poll([{"id": bid, "state": "ended"}])
        row = self.broadcast()[0]
        self.assertEqual(row["status"], "done")
        self.assertIsNotNone(row["ended_at"])
        html = self.cards()
        self.assertIn("Ver no YouTube", html)
        self.assertIn("https://youtu.be/abc", html)

    def test_only_matching_session_is_used(self):
        self.make_tournament()
        self.post(state())
        self.session("outro-jogo", game="PCSX ReARMed: Outra ISO.bin")
        self.session("outros-jogadores", players="TIERES, FULANO")
        self.session("depois", minutes=-10)  # começou depois do card: não é dela
        self.session("acabou", status="finished")
        self.assertEqual(self.poll()[1]["jobs"], [])
        self.session("certa", players="ANDERSHOW,tieres")
        self.assertEqual(self.poll()[1]["jobs"][0]["session_id"], "certa")

    def test_auto_off_and_free_matches_need_the_admin(self):
        with self.app.app_context():
            set_setting("broadcast_auto", "0")
        self.make_tournament()
        self.post(state())
        self.session()
        self.assertEqual(self.poll()[1]["jobs"], [])
        self.assertEqual(self.broadcast(), [])
        live_id = self.rows("SELECT id FROM live_matches")[0]["id"]
        admin = self.admin()
        self.assertIn(">Transmitir<", self.cards(admin))
        self.assertNotIn(">Transmitir<", self.cards())  # visitante não vê o botão
        admin.post("/ao-vivo/%d/transmitir" % live_id, data={"csrf_token": CSRF, "transmitir": "1"})
        self.assertEqual((self.broadcast()[0]["reason"], self.broadcast()[0]["status"]), ("admin", "queued"))
        self.assertIn("Parar transmissão", self.cards(admin))
        job = self.poll()[1]["jobs"][0]
        self.poll([{"id": job["id"], "state": "live", "youtube_url": "https://youtu.be/x"}])
        # Parar: o agente recebe stop no quadro 0 (para já).
        admin.post("/ao-vivo/%d/transmitir" % live_id, data={"csrf_token": CSRF, "transmitir": "0"})
        self.assertEqual(self.broadcast()[0]["status"], "cancelled")
        self.assertEqual(self.poll([{"id": job["id"], "state": "live"}])[1]["jobs"],
                         [{"id": job["id"], "action": "stop", "stop_frame": 0}])
        self.poll([{"id": job["id"], "state": "ended"}])
        row = self.broadcast()[0]
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["ended_at"])
        # Transmitir de novo (cancelada): volta para a fila.
        admin.post("/ao-vivo/%d/transmitir" % live_id, data={"csrf_token": CSRF, "transmitir": "1"})
        self.assertEqual(self.broadcast()[0]["status"], "queued")

    def test_one_slot_and_lost_jobs(self):
        self.post(state(kickoff=100))
        self.post(state(kickoff=200), players="TIERES,outro")
        self.session("s1")
        self.session("s2", players="TIERES, outro")
        admin = self.admin()
        for row in self.rows("SELECT id FROM live_matches ORDER BY id"):
            admin.post("/ao-vivo/%d/transmitir" % row["id"], data={"csrf_token": CSRF, "transmitir": "1"})
        jobs = self.poll()[1]["jobs"]
        self.assertEqual(len(jobs), 1)  # uma vaga
        self.assertEqual(jobs[0]["start_frame"], 0)  # pontapé antes dos 10 s de folga
        # O agente sumiu com ela (não fala dela há mais de 2 min): falha, e a próxima entra.
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE broadcasts SET updated_at = ? WHERE id = ?", (_ago(3), jobs[0]["id"]))
            db.commit()
        jobs2 = self.poll()[1]["jobs"]
        self.assertEqual([j["action"] for j in jobs2], ["start"])
        self.assertNotEqual(jobs2[0]["id"], jobs[0]["id"])
        failed = self.rows("SELECT * FROM broadcasts WHERE id = ?", jobs[0]["id"])[0]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("deixou de informar", failed["error"])

    def test_queued_match_that_ended_is_cancelled(self):
        self.make_tournament()
        self.post(state())
        self.poll()  # na fila, sem sessão
        self.post(state(period=1, clock=0, final=True))
        self.poll()
        row = self.broadcast()[0]
        self.assertEqual(row["status"], "cancelled")
        self.assertIn("terminou antes", row["error"])

    def test_iso_of_the_tournament_and_the_catalog_list(self):
        self.make_tournament()
        with self.app.app_context():
            db = get_db()
            tid = db.execute("SELECT id FROM tournaments").fetchone()["id"]
            db.execute("INSERT INTO tournament_isos (tournament_id, stored_name, original_filename, file_size, md5, "
                       "archive_name, archive_status, archive_url, content_id, created_at, updated_at) "
                       "VALUES (?, 'x.bin', 'Euro 2004.bin', 1234, 'ABCDEF', 'euro.bin', 'done', "
                       "'https://archive.org/download/one-two-iso/euro.bin', 'BA9BDE21:1C473F60', ?, ?)", (tid, now(), now()))
            game = db.execute("SELECT id FROM game_titles LIMIT 1").fetchone()["id"]
            db.execute("INSERT INTO catalog_isos (name, game_title_id, original_filename, file_size, md5, archive_url, "
                       "is_active, approved_at, created_at, updated_at) VALUES ('Copa 26', ?, 'Copa26.bin', 99, 'fff', "
                       "'https://archive.org/download/one-two-iso/copa26.bin', 1, ?, ?, ?)", (game, now(), now(), now()))
            db.execute("INSERT INTO catalog_isos (name, game_title_id, original_filename, file_size, md5, archive_url, "
                       "is_active, created_at, updated_at) VALUES ('Pendente', ?, 'p.bin', 1, 'eee', 'u', 1, ?, ?)",
                       (game, now(), now()))
            db.commit()
        self.post(state())
        self.session()
        iso = self.poll()[1]["jobs"][0]["iso"]
        self.assertEqual(iso, {"ref": "torneio:%d" % tid, "name": "Euro WE Camp", "file": "Euro 2004.bin",
                               "md5": "abcdef", "size": 1234, "url": "https://archive.org/download/one-two-iso/euro.bin",
                               "content_id": "BA9BDE21:1C473F60"})
        reply = self.app.test_client().get("/api/broadcaster/isos", headers={"Authorization": "Bearer " + TOKEN})
        names = sorted(item["name"] for item in reply.get_json()["isos"])
        self.assertEqual(names, ["Copa 26", "Euro WE Camp"])  # a não liberada fica de fora
        self.assertEqual(self.app.test_client().get("/api/broadcaster/isos").status_code, 401)

    def finished_session(self, sid="replay1", keep_file=True):
        """O Stream ao vivo de uma sala que já fechou (o replay no servidor ou já apagado)."""
        from pathlib import Path
        self.session(sid, minutes=60, status="finished")
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE live_sessions SET ended_at = ? WHERE session_id = ?", (now(), sid))
            db.commit()
        if keep_file:
            live_dir = Path(self.app.config["LIVE_DIR"])
            live_dir.mkdir(parents=True, exist_ok=True)
            (live_dir / (sid + ".krec")).write_bytes(b"KRC1")

    def finished_free_match(self):
        self.post(state(kickoff=5000, frame=8000))
        self.post(state(kickoff=5000, frame=120000, period=1, clock=0, final=True))
        return self.rows("SELECT id FROM live_matches")[0]["id"]

    def test_replay_of_a_finished_match(self):
        live_id = self.finished_free_match()
        admin = self.admin()
        self.assertIn(">Transmitir<", self.cards(admin))  # encerrada: o administrador pode transmitir a reprise
        # Sem o replay no servidor: não dá.
        self.finished_session("apagado", keep_file=False)
        reply = admin.post("/ao-vivo/%d/transmitir" % live_id, data={"csrf_token": CSRF, "transmitir": "1"},
                           follow_redirects=True).get_data(as_text=True)
        self.assertIn("não está no servidor", reply)
        self.assertEqual(self.broadcast(), [])
        self.finished_session()
        admin.post("/ao-vivo/%d/transmitir" % live_id, data={"csrf_token": CSRF, "transmitir": "1"})
        row = self.broadcast()[0]
        self.assertEqual((row["reason"], row["replay"], row["status"]), ("admin", 1, "queued"))
        job = self.poll()[1]["jobs"][0]  # não é cancelada por a partida já ter terminado
        self.assertEqual((job["action"], job["session_id"], job["replay"]), ("start", "replay1", True))
        # O .krec inteiro: do quadro 0, sem avanço rápido, até o fim do replay (sem quadro de parada).
        self.assertEqual((job["start_frame"], job["stop_frame"]), (0, None))
        self.assertTrue(job["title"].startswith("Reprise: TIERES x ANDERSHOW | Partida livre"))
        self.assertIn("Reprise da partida de ", job["description"])
        self.assertIn("Placar da partida: https://we2002.example/ao-vivo?partida=%d" % live_id, job["description"])
        # Rodando: nada a fazer até o replay acabar (o RetroArch sai sozinho).
        jobs = self.poll([{"id": job["id"], "state": "live", "youtube_url": "https://youtu.be/r"}])[1]["jobs"]
        self.assertEqual(jobs, [])
        self.poll([{"id": job["id"], "state": "ended"}])
        self.assertEqual(self.broadcast()[0]["status"], "done")
        html = self.cards(admin)
        self.assertIn("Ver no YouTube", html)
        self.assertNotIn(">Transmitir<", html)  # já foi ao ar
        self.assertIn("(reprise)", admin.get("/admin/transmissoes").get_data(as_text=True))

    def test_live_match_takes_the_only_slot_from_a_replay(self):
        replay_id = self.finished_free_match()
        self.finished_session()
        self.admin().post("/ao-vivo/%d/transmitir" % replay_id, data={"csrf_token": CSRF, "transmitir": "1"})
        replay = self.poll()[1]["jobs"][0]
        self.poll([{"id": replay["id"], "state": "live"}])
        # Começa um jogo de campeonato ao vivo (com o Stream ao vivo dele): a reprise para.
        self.make_tournament()
        self.post(state(kickoff=200000, frame=201000))
        self.session("ao-vivo", minutes=2)
        jobs = self.poll([{"id": replay["id"], "state": "live"}])[1]["jobs"]
        self.assertEqual(jobs, [{"id": replay["id"], "action": "stop", "stop_frame": 0}])
        row = self.rows("SELECT * FROM broadcasts WHERE id = ?", replay["id"])[0]
        self.assertEqual(row["status"], "cancelled")
        self.assertIn("partida ao vivo", row["error"])
        # A reprise parou: a vaga vai para o jogo ao vivo.
        jobs = self.poll([{"id": replay["id"], "state": "ended"}])[1]["jobs"]
        self.assertEqual([(j["action"], j["session_id"], j["replay"]) for j in jobs], [("start", "ao-vivo", False)])

    def test_two_slots_run_a_replay_and_a_live_match_together(self):
        replay_id = self.finished_free_match()
        self.finished_session()
        self.admin().post("/ao-vivo/%d/transmitir" % replay_id, data={"csrf_token": CSRF, "transmitir": "1"})
        replay = self.poll(slots=2)[1]["jobs"][0]
        self.make_tournament()
        self.post(state(kickoff=200000, frame=201000))
        self.session("ao-vivo", minutes=2)
        jobs = self.poll([{"id": replay["id"], "state": "live"}], slots=2)[1]["jobs"]
        # A reprise segue (nada para ela); a outra vaga vai para o jogo ao vivo.
        self.assertEqual([(j["action"], j["session_id"]) for j in jobs], [("start", "ao-vivo")])
        self.assertEqual(self.rows("SELECT status FROM broadcasts WHERE id = ?", replay["id"])[0]["status"], "live")

    def test_check_with_zero_slots_gets_no_job(self):
        self.make_tournament()
        self.post(state())
        self.session()
        self.assertEqual(self.poll(slots=0)[1]["jobs"], [])
        self.assertEqual(self.broadcast()[0]["status"], "queued")

    def test_admin_page_token_and_settings(self):
        admin = self.admin()
        self.assertEqual(self.app.test_client().get("/admin/transmissoes").status_code, 302)
        html = admin.get("/admin/transmissoes").get_data(as_text=True)
        self.assertIn("notebook", html)
        self.assertIn("Desconectado", html)
        reply = admin.post("/admin/transmissoes", data={"csrf_token": CSRF, "acao": "agente", "nome": "notebook"},
                           follow_redirects=True).get_data(as_text=True)
        token = reply.split("config.json do agente): ")[1].split("<")[0].strip()
        self.assertEqual(self.poll(token=token)[0], 200)
        self.assertEqual(self.poll()[0], 401)  # o token antigo deixou de valer
        admin.post("/admin/transmissoes", data={"csrf_token": CSRF, "acao": "ajustes", "privacidade": "public",
                                                "versao": "0.14"})
        with self.app.app_context():
            from app.broadcaster import settings
            self.assertEqual(settings(), {"auto": False, "privacy": "public", "version": "0.14"})
        self.assertIn('<span class="badge">Conectado</span>', admin.get("/admin/transmissoes").get_data(as_text=True))
