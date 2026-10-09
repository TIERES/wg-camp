import unittest
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

from app import discord
from app.db import get_db
from tests.test_players_memcards import PlayersTestBase

GUILD = "1558204659556950156"
CATEGORY = "1558205982511865927"
BOT_ID = "999"


class FakeDiscord:
    """Só o pedaço da API REST do Discord que app/discord.py usa."""

    def __init__(self):
        self.calls = []
        self.channels = {}       # id -> body do POST
        self.voice = {}          # discord_id -> channel_id em que está
        self.members = set()     # membros do servidor
        self.users = {}          # access_token -> usuário
        self.next_id = 5000
        self.fail_permissions = True

    def __call__(self, method, path, body=None, auth="bot", form=None, basic=None):
        self.calls.append((method, path, body))
        if path == "/users/@me" and auth == "bot":
            return {"id": BOT_ID}
        if path == "/users/@me":
            return self.users[auth]
        if path == "/oauth2/token":
            return {"access_token": form["code"]}
        if method == "PUT" and path.startswith(f"/guilds/{GUILD}/members/"):
            self.members.add(path.rsplit("/", 1)[1])
            return None
        if method == "POST" and path == f"/guilds/{GUILD}/channels":
            self.next_id += 1
            self.channels[str(self.next_id)] = body
            return {"id": str(self.next_id)}
        if method == "GET" and path.startswith("/channels/"):
            if path.rsplit("/", 1)[1] not in self.channels:
                raise discord.DiscordError(404, discord.UNKNOWN_CHANNEL)
            return {}
        if method == "PUT" and "/permissions/" in path:
            if self.fail_permissions:
                raise discord.DiscordError(403, 50013, "Missing Permissions")
            return None
        if method == "DELETE" and path.startswith("/channels/"):
            self.channels.pop(path.rsplit("/", 1)[1], None)
            return None
        if method == "PATCH" and path.startswith(f"/guilds/{GUILD}/members/"):
            user = path.rsplit("/", 1)[1]
            if user not in self.voice:
                raise discord.DiscordError(400, discord.NOT_IN_VOICE)
            self.voice[user] = body["channel_id"]
            return None
        if method == "GET" and path.startswith(f"/guilds/{GUILD}/voice-states/"):
            user = path.rsplit("/", 1)[1]
            if user not in self.voice:
                raise discord.DiscordError(404, discord.UNKNOWN_VOICE_STATE)
            return {"channel_id": self.voice[user]}
        raise AssertionError(f"chamada inesperada: {method} {path}")


class DiscordVoiceTest(PlayersTestBase):
    def setUp(self):
        super().setUp()
        self.app.config.update(DISCORD_BOT_TOKEN="bot-token", DISCORD_CLIENT_SECRET="secret",
                               DISCORD_GUILD_ID=GUILD, DISCORD_CATEGORY_ID=CATEGORY)
        self.fake = FakeDiscord()
        patcher = mock.patch.object(discord, "http", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        discord._bot_user_id[0] = None

    def api_token(self, username, password="segredo123"):
        response = self.client.post("/api/mc/login", data={"username": username, "password": password})
        return dict(line.split("=", 1) for line in response.get_data(as_text=True).splitlines())["token"]

    def link_discord(self, username, discord_id, name=None):
        self.web_login(username)
        response = self.client.post("/conta/discord/vincular", data={"csrf_token": "csrf"})
        state = parse_qs(urlparse(response.headers["Location"]).query)["state"][0]
        self.fake.users[f"code-{discord_id}"] = {"id": discord_id, "global_name": name or username}
        return self.client.get(f"/discord/callback?code=code-{discord_id}&state={state}")

    def join(self, token, players):
        response = self.client.post("/api/voice/join", data={"players": players},
                                    headers={"Authorization": f"Bearer {token}"})
        return response, dict(line.split("=", 1) for line in response.get_data(as_text=True).splitlines())

    def two_linked_players(self):
        self.register("Pele", "pele@example.com")
        self.register("Zico", "zico@example.com")
        self.link_discord("Pele", "111")
        self.link_discord("Zico", "222")
        return self.api_token("Pele"), self.api_token("Zico")

    def test_link_puts_player_in_guild_and_shows_on_account(self):
        self.register()
        response = self.link_discord("Pele", "111", "Pelé")
        self.assertEqual(response.status_code, 302)
        self.assertIn("111", self.fake.members)
        with self.app.app_context():
            row = get_db().execute("SELECT discord_id, discord_name FROM players WHERE username = 'Pele'").fetchone()
        self.assertEqual((row["discord_id"], row["discord_name"]), ("111", "Pelé"))
        page = self.client.get("/conta/").get_data(as_text=True)
        self.assertIn("Desvincular Discord", page)
        self.client.post("/conta/discord/desvincular", data={"csrf_token": "csrf"})
        self.assertIn("Vincular Discord", self.client.get("/conta/").get_data(as_text=True))

    def test_login_and_whoami_tell_the_dll_about_discord(self):
        self.register()
        response = self.client.post("/api/mc/login", data={"username": "pele@example.com", "password": "segredo123"})
        self.assertIn("discord_linked=0", response.get_data(as_text=True))
        self.link_discord("Pele", "111", "Pelé\nRei")
        token = self.api_token("Pele")
        reply = self.client.get("/api/mc/whoami", headers={"Authorization": f"Bearer {token}"}).get_data(as_text=True)
        self.assertIn("discord_linked=1\n", reply)
        self.assertIn("discord_name=Pelé Rei\n", reply)

    def test_callback_rejects_wrong_state(self):
        self.register()
        self.web_login()
        self.client.post("/conta/discord/vincular", data={"csrf_token": "csrf"})
        self.fake.users["code-1"] = {"id": "1"}
        self.client.get("/discord/callback?code=code-1&state=forged")
        with self.app.app_context():
            self.assertIsNone(get_db().execute("SELECT discord_id FROM players").fetchone()["discord_id"])

    def test_same_discord_cannot_link_two_accounts(self):
        self.register("Pele", "pele@example.com")
        self.register("Zico", "zico@example.com")
        self.link_discord("Pele", "111")
        self.link_discord("Zico", "111")
        with self.app.app_context():
            row = get_db().execute("SELECT discord_id FROM players WHERE username = 'Zico'").fetchone()
        self.assertIsNone(row["discord_id"])

    def start_channel(self, first, second, players="Pele,Zico"):
        """Os dois pedem: o primeiro espera, o segundo cria o canal."""
        _, waiting = self.join(first, players)
        self.assertEqual(waiting["status"], "waiting")
        _, reply = self.join(second, players)
        return reply

    def test_channel_needs_two_players_and_moves_both(self):
        pele, zico = self.two_linked_players()
        self.fake.voice = {"111": "lobby", "222": "lobby"}
        response, reply = self.join(pele, "Pele,Zico")
        self.assertEqual(response.status_code, 200, reply)
        self.assertEqual(reply, {"ok": "1", "status": "waiting"})
        self.assertEqual(self.fake.channels, {})
        self.assertEqual(self.fake.voice, {"111": "lobby", "222": "lobby"})

        response, reply = self.join(zico, "Pele,Zico")
        self.assertEqual(reply["status"], "moved")
        channel = reply["channel_id"]
        self.assertEqual(reply["url"], f"https://discord.com/channels/{GUILD}/{channel}")
        self.assertEqual(reply["app_url"], f"discord://-/channels/{GUILD}/{channel}")
        body = self.fake.channels[channel]
        self.assertEqual(body["parent_id"], CATEGORY)
        self.assertEqual(body["type"], 2)
        allowed = {o["id"] for o in body["permission_overwrites"] if o["allow"] != "0"}
        self.assertEqual(allowed, {BOT_ID, "111", "222"})
        everyone = next(o for o in body["permission_overwrites"] if o["id"] == GUILD)
        self.assertEqual(int(everyone["deny"]), discord.VIEW_CHANNEL | discord.CONNECT)
        # Quem esperava também pediu para entrar: os dois foram movidos.
        self.assertEqual(self.fake.voice, {"111": channel, "222": channel})

        # A próxima pergunta do DLL do primeiro já encontra o canal.
        _, reply = self.join(pele, "Pele,Zico")
        self.assertEqual((reply["status"], reply["channel_id"]), ("moved", channel))
        self.assertEqual(len(self.fake.channels), 1)

    def test_one_player_alone_never_gets_a_channel(self):
        pele, zico = self.two_linked_players()
        for _ in range(3):
            _, reply = self.join(pele, "Pele,Zico")
            self.assertEqual(reply["status"], "waiting")
        self.assertEqual(self.fake.channels, {})
        # Um pedido antigo (de outra partida) não conta mais.
        with self.app.app_context():
            old = (discord.utcnow() - timedelta(minutes=10)).isoformat()
            get_db().execute("UPDATE voice_requests SET requested_at = ?", (old,))
            get_db().commit()
        _, reply = self.join(zico, "Pele,Zico")
        self.assertEqual(reply["status"], "waiting")
        self.assertEqual(self.fake.channels, {})

    def test_player_who_is_not_asking_is_not_moved(self):
        self.register("Pele", "pele@example.com")
        self.register("Zico", "zico@example.com")
        self.register("Romario", "romario@example.com")
        for name, discord_id in (("Pele", "111"), ("Zico", "222"), ("Romario", "333")):
            self.link_discord(name, discord_id)
        self.fake.voice = {"111": "lobby", "222": "lobby", "333": "lobby"}
        reply = self.start_channel(self.api_token("Pele"), self.api_token("Zico"), "Pele,Zico,Romario")
        channel = reply["channel_id"]
        # Romario está na sala mas desmarcou a chamada (o DLL dele não pede).
        self.assertEqual(self.fake.voice, {"111": channel, "222": channel, "333": "lobby"})

    def test_rematch_in_another_room_order_reuses_channel(self):
        pele, zico = self.two_linked_players()
        first = self.start_channel(pele, zico)
        _, second = self.join(zico, "zico,PELE")
        self.assertEqual(first["channel_id"], second["channel_id"])

    def test_not_in_voice_gets_link(self):
        pele, zico = self.two_linked_players()
        reply = self.start_channel(pele, zico)
        self.assertEqual(reply["status"], "link")

    def test_deleted_channel_is_recreated(self):
        pele, zico = self.two_linked_players()
        first = self.start_channel(pele, zico)
        self.fake.channels.clear()
        _, second = self.join(pele, "Pele,Zico")
        self.assertNotEqual(first["channel_id"], second["channel_id"])
        self.assertIn(second["channel_id"], self.fake.channels)

    def test_unlinked_or_unlisted_or_solo_is_refused(self):
        self.register("Pele", "pele@example.com")
        self.register("Zico", "zico@example.com")
        pele = self.api_token("Pele")
        response, reply = self.join(pele, "Pele,Zico")
        self.assertEqual((response.status_code, reply["error"]), (409, "not_linked"))
        self.link_discord("Pele", "111")
        response, reply = self.join(pele, "Zico,Romario")
        self.assertEqual(reply["error"], "not_in_players")
        response, reply = self.join(pele, "Pele")
        self.assertEqual(reply["error"], "solo")
        response, reply = self.join("bad-token", "Pele,Zico")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.fake.channels, {})

    def test_disabled_without_bot_token(self):
        pele, _ = self.two_linked_players()
        self.app.config["DISCORD_BOT_TOKEN"] = ""
        response, reply = self.join(pele, "Pele,Zico")
        self.assertEqual((response.status_code, reply["error"]), (503, "disabled"))

    def test_late_linked_player_is_still_moved(self):
        self.register("Pele", "pele@example.com")
        self.register("Zico", "zico@example.com")
        self.register("Romario", "romario@example.com")
        self.link_discord("Pele", "111")
        self.link_discord("Romario", "333")
        first = self.start_channel(self.api_token("Pele"), self.api_token("Romario"), "Pele,Zico,Romario")
        self.link_discord("Zico", "222")
        self.fake.voice = {"222": "lobby"}
        _, reply = self.join(self.api_token("Zico"), "Pele,Zico,Romario")
        self.assertEqual(reply["status"], "moved")
        self.assertEqual(self.fake.voice["222"], first["channel_id"])

    def age_channels(self, minutes):
        with self.app.app_context():
            old = (discord.utcnow() - timedelta(minutes=minutes)).isoformat()
            get_db().execute("UPDATE voice_channels SET created_at = ?, last_join_at = ?", (old, old))
            get_db().commit()

    def reap(self):
        with self.app.app_context():
            return discord.reap_voice_channels(get_db())

    def test_reaper_keeps_new_and_occupied_channels_and_removes_empty(self):
        pele, zico = self.two_linked_players()
        self.fake.voice = {"111": "lobby"}
        channel = self.start_channel(pele, zico)["channel_id"]
        self.fake.voice["111"] = channel
        self.assertEqual(self.reap(), 0)          # ainda no tempo de tolerância
        self.age_channels(10)
        self.assertEqual(self.reap(), 0)          # Pele está no canal
        self.fake.voice["111"] = "lobby"
        self.assertEqual(self.reap(), 1)
        self.assertNotIn(channel, self.fake.channels)
        with self.app.app_context():
            self.assertIsNone(get_db().execute("SELECT 1 FROM voice_channels").fetchone())

    def test_reaper_removes_very_old_channel_even_if_occupied(self):
        pele, zico = self.two_linked_players()
        self.fake.voice = {"111": "lobby"}
        self.start_channel(pele, zico)
        self.age_channels(13 * 60)
        self.assertEqual(self.reap(), 1)


if __name__ == "__main__":
    unittest.main()
