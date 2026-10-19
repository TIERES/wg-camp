import hashlib
import tempfile
import unittest
import zlib
from pathlib import Path

from app import create_app
from app.db import init_db


class UpdatesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.updates_dir = root / "updates"
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": str(root / "arena17.db"),
            "DOWNLOADS_DIR": str(root / "downloads"),
            "UPLOAD_TMP_DIR": str(root / "tmp"),
            "UPDATES_DIR": str(self.updates_dir),
        })
        with self.app.app_context():
            init_db()

    def tearDown(self):
        self.temp.cleanup()

    def _publish(self, version="v.TIERES.0.15", data=b"fake-dll-bytes"):
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        (self.updates_dir / "version.txt").write_text(version, encoding="utf-8")
        (self.updates_dir / "kailleraclient-x64.dll").write_bytes(data)

    def test_latest_returns_version_size_and_crc(self):
        self._publish(data=b"hello-world")
        response = self.app.test_client().get("/updates/latest?arch=x64")
        self.assertEqual(response.status_code, 200)
        version, size, crc = response.data.decode("utf-8").strip().split("\t")
        self.assertEqual(version, "v.TIERES.0.15")
        self.assertEqual(int(size), len(b"hello-world"))
        self.assertEqual(crc, f"{zlib.crc32(b'hello-world') & 0xffffffff:08x}")

    def test_latest_404_when_nothing_published(self):
        response = self.app.test_client().get("/updates/latest?arch=x64")
        self.assertEqual(response.status_code, 404)

    def test_latest_rejects_invalid_arch(self):
        response = self.app.test_client().get("/updates/latest?arch=arm64")
        self.assertEqual(response.status_code, 400)

    def test_download_serves_dll_bytes(self):
        self._publish(data=b"dll-content")
        response = self.app.test_client().get("/updates/download/x64")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"dll-content")
        self.assertIn("filename=kailleraclient.dll", response.headers["Content-Disposition"])

    def test_download_404_for_missing_arch(self):
        response = self.app.test_client().get("/updates/download/x64")
        self.assertEqual(response.status_code, 404)

    def test_public_page_hides_kailleraclient_download_even_when_published(self):
        # Hidden since the DLL updates the whole package by itself
        # (mostrar_atualizacao_manual in public/index.html); the route stays
        # for the DLL-only self-updater.
        self._publish(version="v.TIERES.0.15")
        response = self.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"kailleraclient.dll (x64)", response.data)
        self.assertNotIn(b'/updates/download/x64', response.data)
        self.assertEqual(self.app.test_client().get("/updates/download/x64").status_code, 200)

    def test_public_page_hides_kailleraclient_download_when_not_published(self):
        response = self.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"kailleraclient.dll (x64)", response.data)

    def test_public_page_links_full_retroarch_download_and_hides_the_light(self):
        response = self.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Emulador RetroArch FFW TIERES 0.7".encode("utf-8"), response.data)
        self.assertIn(b"https://archive.org/download/one-two-iso/RetroArch-1.16.0.FFW.TIERES.0.7.zip", response.data)
        self.assertNotIn(b"RetroArch-1.16.0.FFW.TIERES.0.6.zip", response.data)
        self.assertIn("ele se atualiza sozinho".encode("utf-8"), response.data)
        self.assertNotIn(b"RetroArch-1.16.0.FFW.TIERES.0.5.zip", response.data)
        self.assertNotIn(b"RetroArch-1.16.0.FFW.TIERES.0.4.zip", response.data)
        # light card hidden (mostrar_atualizacao_manual): the DLL updates the package
        self.assertNotIn("Atualização light".encode("utf-8"), response.data)
        self.assertNotIn(b"Baixar atualiza", response.data)
        self.assertNotIn(b"RetroArch-1.16.0.FFW.TIERES.0.7.light.zip", response.data)
        self.assertNotIn("Chamada de voz no Discord.".encode("utf-8"), response.data)
        # signing up is still one click away, in the header
        self.assertIn(b'href="/conta/cadastro"', response.data)
        self.assertNotIn(b"RetroArch-1.16.0.FFW.TIERES.0.4.light.zip", response.data)
        self.assertNotIn(b"RetroArch-1.16.0.FFW.TIERES.0.3.zip", response.data)
        self.assertNotIn(b"RetroArch-1.16.0.FFW.TIERES.0.3.light.zip", response.data)
        self.assertNotIn(b"RetroArch-1.16.0.FFW.TIERES.0.2.zip", response.data)
        self.assertNotIn(b"RetroArch-TIERES-light.zip", response.data)

    def test_public_page_links_the_graphics_packs(self):
        response = self.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Pack de Gráficos".encode("utf-8"), response.data)
        self.assertIn(b"https://archive.org/download/one-two-iso/RetroArch-TIERES-Graficos-PC-Antigo.zip", response.data)
        self.assertIn(b"https://archive.org/download/one-two-iso/RetroArch-TIERES-Graficos-PC-Moderno.zip", response.data)
        self.assertIn(b"Baixar PC Antigo (VGA onboard)", response.data)
        self.assertIn(b"Baixar PC Moderno (VGA dedicada)", response.data)
        self.assertNotIn(b"Casanova", response.data)

    def test_public_page_links_the_tutorial_pdf(self):
        response = self.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Tutorial RetroArch FFW TIERES 0.2 (PDF)", response.data)
        self.assertIn(b"https://archive.org/download/one-two-iso/Tutorial_RetroArch_FFW_TIERES_0.2.pdf", response.data)

    def _admin_client(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["csrf_token"] = "test-csrf-token"
        return client

    def test_admin_shows_and_hides_the_manual_update_cards(self):
        self._publish(version="v.TIERES.0.24")
        admin = self._admin_client()
        dashboard = admin.get("/admin/").data.decode("utf-8")
        self.assertIn("<strong>ocultos</strong>", dashboard)
        self.assertIn("Mostrar os cards", dashboard)

        response = admin.post("/admin/pagina-inicial/atualizacao-manual",
                              data={"csrf_token": "test-csrf-token", "mostrar": "1"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("<strong>visíveis</strong>", response.data.decode("utf-8"))
        self.assertIn("Ocultar os cards", response.data.decode("utf-8"))
        page = self.app.test_client().get("/").data
        self.assertIn("Atualização light: TIERES 0.7".encode("utf-8"), page)
        self.assertIn(b"https://archive.org/download/one-two-iso/RetroArch-1.16.0.FFW.TIERES.0.7.light.zip", page)
        self.assertNotIn(b"RetroArch-1.16.0.FFW.TIERES.0.6.light.zip", page)
        self.assertNotIn(b"RetroArch-1.16.0.FFW.TIERES.0.5.light.zip", page)
        self.assertIn(b"kailleraclient.dll (x64)", page)
        self.assertIn(b"v.TIERES.0.24", page)
        self.assertIn(b"/updates/download/x64", page)

        admin.post("/admin/pagina-inicial/atualizacao-manual", data={"csrf_token": "test-csrf-token", "mostrar": "0"})
        page = self.app.test_client().get("/").data
        self.assertNotIn("Atualização light".encode("utf-8"), page)
        self.assertNotIn(b"kailleraclient.dll (x64)", page)

    def test_manual_update_cards_toggle_needs_admin_and_csrf(self):
        anonymous = self.app.test_client()
        response = anonymous.post("/admin/pagina-inicial/atualizacao-manual", data={"mostrar": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])
        admin = self._admin_client()
        response = admin.post("/admin/pagina-inicial/atualizacao-manual", data={"csrf_token": "errado", "mostrar": "1"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Atualização light".encode("utf-8"), self.app.test_client().get("/").data)

    def _publish_pacote(self, files, version="v.TIERES.0.25"):
        pacote = self.updates_dir.parent / "pacote"
        (pacote / "arquivos").mkdir(parents=True, exist_ok=True)
        lines = [f"versao\t{version}", "pacote\tRetroArch-1.16.0.FFW.TIERES.0.6"]
        for path, data in files.items():
            sha = hashlib.sha256(data).hexdigest()
            (pacote / "arquivos" / sha).write_bytes(data)
            lines.append(f"arquivo\t{sha}\t{len(data)}\t{path}")
        manifest = "\n".join(lines) + "\n"
        (pacote / "x64.txt").write_bytes(manifest.encode("utf-8"))
        return manifest

    def test_pacote_dir_defaults_next_to_updates_dir(self):
        # /var/www/arena17/pacote in production, which Caddy serves directly
        self.assertEqual(Path(self.app.config["PACOTE_DIR"]), self.updates_dir.parent / "pacote")

    def test_pacote_manifest_is_served_uncached(self):
        manifest = self._publish_pacote({"retroarch.exe": b"exe", "kailleraclient.dll": b"dll"})
        response = self.app.test_client().get("/updates/pacote/x64.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.decode("utf-8"), manifest)
        self.assertEqual(response.mimetype, "text/plain")
        self.assertEqual(response.headers["Cache-Control"], "no-cache")
        response.close()

    def test_pacote_manifest_404_when_not_published_or_other_arch(self):
        client = self.app.test_client()
        self.assertEqual(client.get("/updates/pacote/x64.txt").status_code, 404)
        self._publish_pacote({"retroarch.exe": b"exe"})
        self.assertEqual(client.get("/updates/pacote/x86.txt").status_code, 404)

    def test_pacote_file_is_served_by_its_sha256(self):
        self._publish_pacote({"cores/pcsx_rearmed_libretro.dll": b"core-bytes"})
        sha = hashlib.sha256(b"core-bytes").hexdigest()
        response = self.app.test_client().get(f"/updates/pacote/arquivos/{sha}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"core-bytes")
        self.assertEqual(response.mimetype, "application/octet-stream")
        response.close()

    def test_pacote_file_only_answers_sha256_names(self):
        self._publish_pacote({"retroarch.exe": b"exe"})
        client = self.app.test_client()
        sha = hashlib.sha256(b"exe").hexdigest()
        self.assertEqual(client.get(f"/updates/pacote/arquivos/{sha.upper()}").status_code, 404)
        self.assertEqual(client.get("/updates/pacote/arquivos/x64.txt").status_code, 404)
        self.assertEqual(client.get("/updates/pacote/arquivos/..%2Fx64.txt").status_code, 404)
        self.assertEqual(client.get(f"/updates/pacote/arquivos/{'0' * 64}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
