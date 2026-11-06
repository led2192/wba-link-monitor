import collections
import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import select_eval as S


def pool(n, cand=()):
    """n ranked pages of document D; pages in cand carry a rule candidate."""
    return [(10 - i, "D", i + 1, f"page {i + 1} text", (5.0, 1, 1, 1) if i in cand else None) for i in range(n)]


class Choose(unittest.TestCase):
    def test_base_is_todays_choice(self):
        p = pool(25, cand={20, 21})
        got = S.choose(p)
        self.assertEqual(len(got), S.N)
        self.assertIn(("D", 21), got)               # the candidate half comes first
        self.assertIn(("D", 1), got)                # then today's best ranks

    def test_a_new_order_changes_only_the_rank_half(self):
        p = pool(25, cand={20})
        order = list(reversed(range(25)))           # worst rank first
        got = S.choose(p, order)
        self.assertIn(("D", 21), got)               # candidate kept
        self.assertIn(("D", 25), got)               # the reordered half
        self.assertNotIn(("D", 1), got)

    def test_extra_pages_join_only_when_ordered_in(self):
        p = pool(10)
        extra = [(0.0, "J", 3, "決算", None)]
        self.assertNotIn(("J", 3), S.choose(p))
        self.assertIn(("J", 3), S.choose(p, [10] + list(range(10)), extra))

    def test_rrf_rewards_agreement(self):
        self.assertEqual(S.rrf([0, 1, 2], [1, 0, 2])[0] in (0, 1), True)
        self.assertEqual(S.rrf([0, 1, 2, 3], [3, 1, 2, 0])[0], 1)        # second in both beats first-and-last


class Learned(unittest.TestCase):
    def case(self, wba, fold, types, target):
        p = [(1.0, lid, i, "revenue total 4,120", None) for i, lid in enumerate(types)]
        return {"wba": wba, "attribute": "A", "fold": fold, "pool": p, "types": {lid: lid.split("-")[0] for lid in types},
                "target_idx": set(target), "target": set(target), "extra_target": set(), "extra": []}

    def test_type_lift_from_other_folds_only(self):
        cases = [self.case("X", 1, ["AR-1", "SR-1", "SR-2"], [0]), self.case("Y", 1, ["AR-2", "SR-3"], [0]),
                 self.case("Z", 0, ["SR-4", "AR-3"], [0])]          # fold 0 cites the sustainability report
        lift = S.type_lift(cases, "A", 0)
        self.assertGreater(lift["AR"], 1.0)
        self.assertLess(lift["SR"], 1.0)

    def test_hit(self):
        c = self.case("X", 0, ["AR-1", "SR-1"], [1])
        self.assertTrue(S.hit(c, [("SR-1", 1)]))
        self.assertFalse(S.hit(c, [("AR-1", 0)]))

    def test_features_shape(self):
        c = self.case("X", 0, ["AR-1", "SR-1"], [1])
        f = S.features(c, 0, {"AR": 2.0}, [1.0, 0.5], None)
        self.assertEqual(len(f), 8)


class Report(unittest.TestCase):
    def test_qualifies_by_the_worst_attribute(self):
        base = collections.Counter({"answers": 20, "hit": 10, "answers A": 10, "hit A": 5, "answers B": 10, "hit B": 5})
        new = collections.Counter({"answers": 20, "hit": 13, "answers A": 10, "hit A": 9, "answers B": 10, "hit B": 4})
        cases = [{"attribute": "A", "target_idx": {1}}] * 10 + [{"attribute": "B", "target_idx": set()}] * 10
        md = "\n".join(S.report(cases, {"base": base, "expand": new}, ["A", "B"], {"attributes": {"A": {}, "B": {}}},
                                collections.Counter(), 0.0))
        self.assertIn("expand: +15.0 points overall, worst attribute -10.0: does not qualify", md)



class Horizon(unittest.TestCase):
    def test_a_page_of_targets_is_future_heavy(self):
        targets = "Our targets: reduce Scope 1 by 50% by 2030 and reach net zero by 2050. Ambition 2035."
        actuals = "Scope 1 emissions 2024: 1,234 tCO2e (2023: 1,300). Scope 2 2024: 560."
        self.assertTrue(S.future_heavy(targets, 2024))
        self.assertFalse(S.future_heavy(actuals, 2024))

    def test_future_heavy_pages_move_down(self):
        pool = [(1, "D", 1, "target by 2030 net zero goal"), (1, "D", 2, "2024 revenue 2023"), (1, "D", 3, "2024 data")]
        o = S.horizon_order(pool, 2024, push=2)          # keys 2, 1, 2
        self.assertEqual(o, [1, 0, 2])
        self.assertEqual(S.horizon_order(pool, 2024, push=20), [1, 2, 0])

if __name__ == "__main__":
    unittest.main()
