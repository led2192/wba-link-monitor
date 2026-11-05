import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import plausibility as P

RATES = {"USD": 1.0, "KRW": 1400.0, "JPY": 150.0, "EUR": 0.9}


def row(wba, code, year, value, unit, validated=None, status="proposed", name=""):
    return {"id": f"rec{wba}{code}{year}", "value_id": f"{wba}|{code}|{year}", "wba_id": wba, "attribute": code,
            "attribute_name": name, "year": year, "value": value, "unit": unit, "method": "model reader",
            "status": status, "validated": validated, "matches": ""}


def universe():
    rows = []
    for i in range(60):                                   # validated revenues 1 to 60 billion USD, 2,000 to 120,000 people
        rows.append(row(f"PT_{i:05d}", P.REVENUE, 2023, None, "USD", validated=(i + 1) * 1e9))
        rows.append(row(f"PT_{i:05d}", P.EMPLOYEES, 2023, None, "people", validated=(i + 1) * 2000))
    return rows


class Plausibility(unittest.TestCase):
    def test_ecb_rates_per_usd(self):
        xml = "<Cube currency='USD' rate='1.08'/><Cube currency='KRW' rate='1512'/>"
        class R: text = xml
        r = P.usd_rates(get=lambda *a, **k: R())
        self.assertAlmostEqual(r["KRW"], 1400.0)
        self.assertAlmostEqual(r["EUR"], 1 / 1.08)

    def test_scale_slip(self):
        self.assertEqual(P.scale_slip(1e-12 * 1.02), -12)
        self.assertEqual(P.scale_slip(1e6 * 0.97), 6)
        self.assertIsNone(P.scale_slip(3.0))
        self.assertIsNone(P.scale_slip(10 ** 10.5))        # halfway between 10^9 and 10^12: no power a page prints in

    def test_a_trillion_read_as_units_is_flagged_and_rescaled(self):
        rows = universe() + [row("PT_02628", P.REVENUE, 2023, None, "KRW", validated=2.07e12),
                             row("PT_02628", P.REVENUE, 2024, 2.11, "KRW", validated=2.068e12)]
        learned, history, per_head = P.learn(rows, RATES)
        reasons, sug = P.check(rows[-1], learned, history, per_head, RATES)
        self.assertTrue(any("far below" in r for r in reasons))
        self.assertAlmostEqual(sug, 2.11e12)

    def test_a_small_headcount_is_flagged_without_a_made_up_number(self):
        rows = universe() + [row("PT_09999", P.EMPLOYEES, 2024, 120, "employees")]
        learned, history, per_head = P.learn(rows, RATES)
        reasons, sug = P.check(rows[-1], learned, history, per_head, RATES)
        self.assertTrue(any("far below" in r for r in reasons))
        self.assertIsNone(sug)                             # no other years: no reference to rescale against

    def test_a_plausible_value_is_clear(self):
        rows = universe() + [row("PT_00001", P.REVENUE, 2024, 2.2e9, "USD")]
        learned, history, per_head = P.learn(rows, RATES)
        self.assertEqual(P.check(rows[-1], learned, history, per_head, RATES), ([], None))

    def test_revenue_per_employee(self):
        rows = universe() + [row("PT_00003", P.EMPLOYEES, 2024, 4, "people"), row("PT_00003", P.REVENUE, 2024, None, "USD", validated=4e9)]
        learned, history, per_head = P.learn(rows, RATES)
        reasons, _ = P.check(rows[-2], learned, history, per_head, RATES)
        self.assertTrue(any("revenue per employee" in r for r in reasons))

    def test_too_few_validated_answers_learn_no_range(self):
        self.assertIsNone(P.bounds([1.0] * 10))


if __name__ == "__main__":
    unittest.main()
