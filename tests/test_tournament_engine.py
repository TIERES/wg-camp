import random
import unittest
from collections import Counter
from itertools import combinations

from app.tournament import engine, formats
from app.tournament.engine import Result
from app.wallet import format_cents, parse_cents


class RoundRobinTest(unittest.TestCase):
    def check(self, n, legs):
        fixtures = engine.round_robin(range(n), legs)
        pairs = Counter(frozenset((home, away)) for _, home, away in fixtures)
        self.assertEqual(set(pairs), {frozenset(p) for p in combinations(range(n), 2)})
        self.assertTrue(all(count == legs for count in pairs.values()))
        by_round = {}
        for rnd, home, away in fixtures:
            players = by_round.setdefault(rnd, set())
            self.assertNotIn(home, players)
            self.assertNotIn(away, players)
            players.update((home, away))
        if legs == 2:
            ordered = Counter((home, away) for _, home, away in fixtures)
            self.assertTrue(all(count == 1 for count in ordered.values()), "returno inverte o mando")
        homes = Counter(home for _, home, _ in fixtures)
        self.assertLessEqual(max(homes.values()) - min(homes[p] for p in range(n)), 1 if legs == 1 else 2)

    def test_even_odd_and_two_legs(self):
        for n in (2, 3, 4, 5, 8, 9, 20):
            for legs in (1, 2):
                self.check(n, legs)


class StandingsTest(unittest.TestCase):
    def test_points_then_head_to_head(self):
        results = [Result(1, 2, 2, 0), Result(2, 3, 1, 0), Result(3, 1, 1, 0), Result(4, 1, 0, 0),
                   Result(4, 2, 0, 0), Result(4, 3, 0, 0)]
        table = engine.standings([1, 2, 3, 4], results)
        # 1, 2 e 3: 4 pts, 1 V. Saldos: 1 => +1, 3 => 0, 2 => -1
        self.assertEqual([row["entry_id"] for row in table], [1, 3, 2, 4])
        self.assertEqual(table[0]["points"], 4)
        self.assertEqual(table[0]["form"], ["V", "D", "E"])

    def test_head_to_head_breaks_full_tie(self):
        # 1 e 2 empatam em tudo, mas 2 venceu o confronto direto.
        results = [Result(2, 1, 1, 0), Result(1, 3, 2, 0), Result(1, 4, 1, 0), Result(3, 2, 1, 0),
                   Result(2, 4, 2, 0), Result(3, 4, 0, 0)]
        table = engine.standings([1, 2, 3, 4], results)
        self.assertEqual(table[0]["points"], table[1]["points"])
        self.assertEqual([table[0]["entry_id"], table[1]["entry_id"]], [2, 1])

    def test_draw_order_last(self):
        self.assertEqual([row["entry_id"] for row in engine.standings([7, 3, 5], [])], [7, 3, 5])


class SwissTest(unittest.TestCase):
    def check(self, n, pots, per_pot, seed):
        entries = list(range(n))
        size = n // pots
        pot_lists = [entries[i * size:(i + 1) * size] for i in range(pots)]
        pot_of = {e: i for i, pot in enumerate(pot_lists) for e in pot}
        fixtures = engine.swiss_fixtures(pot_lists, per_pot, random.Random(seed))
        games = Counter()
        vs_pot = Counter()
        homes = Counter()
        pairs = Counter()
        for _, home, away in fixtures:
            self.assertNotEqual(home, away)
            games[home] += 1
            games[away] += 1
            homes[home] += 1
            vs_pot[(home, pot_of[away])] += 1
            vs_pot[(away, pot_of[home])] += 1
            pairs[frozenset((home, away))] += 1
        for e in entries:
            self.assertEqual(games[e], pots * per_pot)
            for p in range(pots):
                self.assertEqual(vs_pot[(e, p)], per_pot)
            if per_pot == 2:
                self.assertEqual(homes[e], pots)
        self.assertTrue(all(count == 1 for count in pairs.values()), "ninguém se enfrenta duas vezes")
        rounds = {}
        for rnd, home, away in fixtures:
            players = rounds.setdefault(rnd, set())
            self.assertFalse({home, away} & players)
            players.update((home, away))

    def test_official_shape_and_small_leagues(self):
        for seed in range(5):
            self.check(36, 4, 2, seed)
            self.check(16, 4, 1, seed)
            self.check(12, 4, 2, seed)
            self.check(8, 4, 1, seed)
            self.check(18, 3, 2, seed)


class KnockoutTest(unittest.TestCase):
    def test_seed_positions(self):
        self.assertEqual(engine.seed_positions(8), [1, 8, 4, 5, 2, 7, 3, 6])

    def test_byes_go_to_top_seeds(self):
        pairs = engine.seeded_pairs(list("ABCDEF"))
        self.assertEqual(pairs, [("A", None), ("D", "E"), ("B", None), ("C", "F")])

    def test_ucl_playoff_shape(self):
        # 24 classificados (8 direto + 16 no playoff): 9 x 24 ... 16 x 17.
        pairs = engine.seeded_pairs(list(range(1, 25)))
        self.assertEqual(len(pairs), 16)
        byes = [a for a, b in pairs if b is None]
        self.assertEqual(sorted(byes), list(range(1, 9)))
        self.assertIn((16, 17), pairs)
        self.assertIn((9, 24), pairs)
        # 1º encara o vencedor de 16 x 17
        self.assertEqual(pairs[0], (1, None))
        self.assertEqual(pairs[1], (16, 17))

    def test_cross_pairs(self):
        tables = [["1A", "2A"], ["1B", "2B"], ["1C", "2C"], ["1D", "2D"]]
        self.assertEqual(engine.cross_pairs(tables),
                         [("1A", "2B"), ("1C", "2D"), ("1B", "2A"), ("1D", "2C")])

    def test_tie_outcome(self):
        one = [Result("a", "b", 1, 1)]
        self.assertIsNone(engine.tie_outcome("a", "b", 1, one))
        self.assertEqual(engine.tie_outcome("a", "b", 1, one, (5, 4)), "a")
        two = [Result("b", "a", 2, 1), Result("a", "b", 1, 0)]
        self.assertIsNone(engine.tie_outcome("a", "b", 2, two))
        self.assertEqual(engine.tie_outcome("a", "b", 2, two, (3, 4)), "b")
        self.assertEqual(engine.tie_outcome("a", "b", 2, two, away_goals=True), "a")
        self.assertIsNone(engine.tie_outcome("a", "b", 2, two[:1]))


class PrizeAndMoneyTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(engine.split_prize(1001, [60, 30, 10], 3), [601, 300, 100])
        self.assertEqual(engine.split_prize(1000, [60, 30, 10], 2), [700, 300])
        self.assertEqual(engine.split_prize(0, [60, 30, 10], 3), [])
        with self.assertRaises(ValueError):
            engine.parse_split("50,30,10")
        with self.assertRaises(ValueError):
            engine.parse_split("10,30,60")

    def test_money(self):
        self.assertEqual(format_cents(100000), "W$1.000,00")
        self.assertEqual(format_cents(-50), "-W$0,50")
        self.assertEqual(parse_cents("1.000,50"), 100050)
        self.assertEqual(parse_cents("50"), 5000)
        # Como a máscara dos campos de W$ (static/js/wcash-mask.js) envia.
        self.assertEqual(parse_cents("W$ 2,00"), 200)
        self.assertEqual(parse_cents("W$ 1.234,56"), 123456)
        self.assertEqual(parse_cents("W$ 0,00"), 0)
        self.assertEqual(parse_cents("0,5"), 50)
        self.assertEqual(parse_cents("12.5"), 1250)
        self.assertEqual(parse_cents("1.000"), 100000)
        for bad in ("", "-1", "abc", "1,234"):
            with self.assertRaises(ValueError):
                parse_cents(bad)


class FormatsTest(unittest.TestCase):
    def test_config_errors(self):
        ucl = formats.normalize("ucl", {})
        self.assertIsNone(formats.config_error("ucl", ucl, 8))
        self.assertIn("múltiplo", formats.config_error("ucl", ucl, 10))
        self.assertIsNone(formats.config_error("ucl", dict(ucl, pots=4, per_pot=2, direct=8), 36))
        self.assertIn("playoff", formats.config_error("ucl", dict(ucl, direct=4), 8))
        wc = formats.normalize("world_cup", {"groups": "4"})
        self.assertIsNone(formats.config_error("world_cup", wc, 16))
        self.assertIsNotNone(formats.config_error("world_cup", wc, 6))
        self.assertIsNotNone(formats.config_error("league", formats.normalize("league", {}), 1))

    def test_unchecked_box_from_form_is_false(self):
        self.assertFalse(formats.normalize("knockout", {"_form": "1"})["third_place"])
        self.assertTrue(formats.normalize("knockout", {"_form": "1", "third_place": "on"})["third_place"])


if __name__ == "__main__":
    unittest.main()
