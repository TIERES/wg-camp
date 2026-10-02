import unittest

from app.timeutil import format_local


class FormatLocalTests(unittest.TestCase):
    def test_converts_stored_utc_to_sao_paulo(self):
        self.assertEqual(format_local("2026-10-02T01:30:00+00:00"), "01-10-2026 22:30")

    def test_naive_value_is_treated_as_utc(self):
        self.assertEqual(format_local("2026-10-02T12:00:00", "%Y-%m-%d %H:%M"), "2026-10-02 09:00")

    def test_empty_or_invalid_value_passes_through(self):
        self.assertEqual(format_local(None), "")
        self.assertEqual(format_local("lixo"), "lixo")


if __name__ == "__main__":
    unittest.main()
