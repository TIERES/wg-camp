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

    def test_public_page_shows_kailleraclient_download_when_published(self):
        self._publish(version="v.TIERES.0.15")
        response = self.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"kailleraclient.dll (x64)", response.data)
        self.assertIn(b"v.TIERES.0.15", response.data)
        self.assertIn(b'/updates/download/x64', response.data)

    def test_public_page_hides_kailleraclient_download_when_not_published(self):
        response = self.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"kailleraclient.dll (x64)", response.data)

    def test_public_page_links_full_and_light_retroarch_downloads(self):
        response = self.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Emulador RetroArch FFW TIERES 0.2".encode("utf-8"), response.data)
        self.assertIn(b"https://archive.org/download/one-two-iso/RetroArch-1.16.0.FFW.TIERES.0.2.zip", response.data)
        self.assertIn("Atualização light".encode("utf-8"), response.data)
        self.assertIn(b"https://archive.org/download/one-two-iso/RetroArch-TIERES-light.zip", response.data)
        self.assertNotIn(b"RetroArch-1.16.0.FFW.TIERES.0.1.zip", response.data)

    def test_public_page_links_the_tutorial_pdf(self):
        response = self.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Tutorial RetroArch FFW TIERES 0.2 (PDF)", response.data)
        self.assertIn(b"https://archive.org/download/one-two-iso/Tutorial_RetroArch_FFW_TIERES_0.2.pdf", response.data)


if __name__ == "__main__":
    unittest.main()
