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


class Ambiguous(unittest.TestCase):
    def row(self, i, unit, value, attr="S1", lid="PT_1-a", page=4, detail="Printed as 1 (units) on page 4.", printed=""):
        return {"id": f"rec{i}", "fields": {"value_id": f"v{i}", "attribute": attr, "unit": unit, "unit_printed": printed, "detail": detail,
                                            "value": value, "library_id": lid, "page": page}}

    def test_which_units_are_ambiguous(self):
        for u in ("MT CO2e", "mt CO2e", "Mt-CO2e", "MT", "MtCO2eq", "mtCO₂e"):
            self.assertTrue(F.is_ambiguous(u), u)
        for u in ("tCO2e", "ktCO2e", "metric tons CO2e", "MWh", "", "Mt CO2e per MWh"):
            self.assertFalse(F.is_ambiguous(u), u)

    def test_the_page_or_what_is_possible_settles_it_and_nothing_else_does(self):
        self.assertEqual(F.settle("S1", 88555, "Scope 1 (metric tons CO2e) 88,555")[0], "tonnes")
        self.assertEqual(F.settle("S1", 0.39, "Emissions (Mt-CO2e), million tonnes of CO2 equivalent 0.39")[0], "megatonnes")
        self.assertEqual(F.settle("S1", 88555, None), ("tonnes", "more megatonnes than is possible"))
        self.assertEqual(F.settle("S1", 1500, None)[0], None)                       # 1,500 Mt is not impossible enough to decide
        self.assertEqual(F.settle("S3", 30000, "nothing about units", )[0], "tonnes")
        self.assertEqual(F.settle("GMC_00005.1", 30000, None)[0], None)             # Scope 3: only above the world's emissions
        self.assertEqual(F.settle("S1", 12, "metric tons and million tonnes on one page"), (None, "the page says both"))

    def test_the_plan(self):
        fam = {"S1": "tco2e", "REV": "currency"}
        recs = [self.row(1, "MT CO2e", 88555), self.row(2, "Mt-CO2e", 0.39, lid="PT_2-b", page=130), self.row(3, "MT CO2e", 12, lid="PT_3-c"),
                self.row(4, "MT CO2e", 5, detail="Printed as 5 (millions) on page 4."),      # a scale was applied: the first pass's business
                self.row(5, "MT", 9, attr="REV"), self.row(6, "MT CO2e", 7, printed="x")]
        bodies = {("PT_1-a", 4): "all figures in metric tons CO2e", ("PT_2-b", 130): "Emissions in million tonnes (Mt-CO2e)"}
        todo, c, left = F.plan_ambiguous(recs, fam, bodies)
        self.assertEqual(todo, [("rec1", "v1", "MT CO2e", "tCO2e")])
        self.assertEqual(left, [("v3", "MT CO2e", 12, "neither the page nor the size settles it")])
        self.assertEqual(c["megatonnes: the page spells out million tonnes"], 1)

    def test_one_document_means_one_thing_by_a_unit(self):
        fam = {"S1": "tco2e"}
        recs = [self.row(1, "mtCO2e", 1676, lid="PT_9-doc"), self.row(2, "mtCO2e", 250000, lid="PT_9-doc"),      # the second is settled by the bound
                self.row(3, "mtCO2e", 900, lid="PT_8-other"), self.row(4, "MT CO2", 88555, lid="PT_7-x")]
        todo, c, left = F.plan_ambiguous(recs, fam, {})
        self.assertEqual(sorted(t[1] for t in todo), ["v1", "v2", "v4"])
        self.assertEqual(dict((t[1], t[3]) for t in todo)["v4"], "tCO2")                  # no "e" printed, none added
        self.assertEqual([l[0] for l in left], ["v3"])
        self.assertEqual(c["tonnes: the same document's other figures are in metric tons"], 1)


if __name__ == "__main__":
    unittest.main()
