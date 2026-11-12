"""fix_units.py: the plan, offline."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fix_units as F

FAM = {"S1": "tco2e", "REV": "currency"}


def rec(i, attr, unit, detail, printed=""):
    return {"id": f"rec{i}", "fields": {"value_id": f"v{i}", "attribute": attr, "unit": unit, "unit_printed": printed, "detail": detail}}


class Plan(unittest.TestCase):
    def test_the_scale_comes_from_the_rows_own_detail(self):
        self.assertEqual(F.scale_of('Printed as 0.39 (millions) in the column "Fiscal 2024" on page 130.'), 1e6)
        self.assertEqual(F.scale_of("Printed as 12,575,901 (thousands) on page 88 of 2024 | X | Annual Report."), 1e3)
        self.assertEqual(F.scale_of("Printed as 88,555 (units) on page 4."), 1)
        self.assertIsNone(F.scale_of("Taken from the validated table.")); self.assertIsNone(F.scale_of(None))
        self.assertIsNone(F.scale_of("Printed as 5 (x2500) on page 1."))

    def test_what_changes_and_what_is_left(self):
        todo, c = F.plan([rec(1, "S1", "Mt-CO2e", "Printed as 0.39 (millions) on page 130."),
                          rec(2, "S1", "MT CO2e", "Printed as 88,555 (units) on page 4."),             # metric tons or megatonnes: left
                          rec(3, "S1", "tonnes CO2e", "Printed as 46,086 (units) on page 9."),
                          rec(4, "REV", "RMB'000", "Printed as 12,575,901 (thousands) on page 88."),
                          rec(5, "REV", "RMB", "Printed as 12,575,901 (thousands) on page 88."),
                          rec(6, "S1", "tCO2e", "Printed as 1 (units) on page 1.", printed="Mt-CO2e"),   # done before
                          rec(7, "S1", "Mt-CO2e", "no scale here"), rec(8, "S1", "", "Printed as 1 (units) on page 1.")], FAM)
        self.assertEqual(todo, [("rec1", "v1", "Mt-CO2e", "tCO2e"), ("rec3", "v3", "tonnes CO2e", "tCO2e"), ("rec4", "v4", "RMB'000", "RMB")])
        self.assertEqual((c["right as it is, or cannot be told"], c["already corrected"], c["no scale in the detail"], c["no unit"]), (2, 1, 1, 1))

    def test_the_value_is_never_written(self):
        body = next(F.payloads([("rec1", "v1", "Mt-CO2e", "tCO2e")]))
        self.assertEqual(body, {"records": [{"id": "rec1", "fields": {"unit": "tCO2e", "unit_printed": "Mt-CO2e"}}]})


if __name__ == "__main__":
    unittest.main()
