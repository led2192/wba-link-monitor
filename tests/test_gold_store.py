import os, unittest

import gold_store as G
import import_source_library as SL


class Formulas(unittest.TestCase):
    A = {"Metadata ID": "PT_1-GMC_00048.1-2024", "Selected Value": "5", "Data Reviewed": True, "Source Link": ["rec1"],
         "GMC Type": ["Boolean"], "RP Metadata Status": "QC Complete", "Evidence": "page 3"}
    B = {"Metadata ID": "PT_1-GMC_9-2024", "Evidence": "x"}

    def keep(self, formula, fields):
        return G.holds(G.parse(formula), fields)

    def test_the_shapes_the_scripts_send(self):
        for formula, a, b in [
                ("AND({Data Reviewed}, {Selected Value}!='')", True, False),
                ("AND({Data Reviewed}, {Source Link}&''!='')", True, False),
                ("AND({Data Reviewed}, {Selected Value}!='', OR(FIND('GMC_00048.1', {Metadata ID}),FIND('GMC_00001.1', {Metadata ID})))", True, False),
                ("AND({Data Reviewed}, OR(FIND('Boolean', {GMC Type}&''), FIND('Select', {GMC Type}&'')))", True, False),
                ("OR({Data Reviewed}, {RP Metadata Status}='QC Complete')", True, False),
                ("AND({RP Metadata Status}='QC Complete', OR(FIND('-GMC_9-', {Metadata ID})))", False, False),
                ("{Evidence}!=''", True, True),
                ("NOT({Data Reviewed})", False, True)]:
            self.assertEqual((self.keep(formula, self.A), self.keep(formula, self.B)), (a, b), formula)

    def test_a_formula_it_does_not_read_is_refused_not_guessed(self):
        for formula in ("LEN({x})>3", "AND({a}", "{a} {b}"):
            with self.assertRaises((ValueError, IndexError)):
                G.parse(formula)

    def test_a_cell_as_text(self):
        self.assertEqual([G.text_of(v) for v in (None, False, True, 5, ["a", "b"], {"name": "QC Complete"})], ["", "", "1", "5", "a, b", "QC Complete"])


class Serving(unittest.TestCase):
    def setUp(self):
        self.keep = (G._HELD, dict(G._ROWS), os.environ.get("GOLD_COPY"), os.environ.get("SUPABASE_DB_URL"))
        G._HELD = {("appGOLD", "Metadata")}
        G._ROWS.clear()
        G._ROWS[("appGOLD", "Metadata")] = [("rec1", dict(Formulas.A)), ("rec2", dict(Formulas.B))]
        os.environ["SUPABASE_DB_URL"] = "postgresql://example"

    def tearDown(self):
        G._HELD = self.keep[0]; G._ROWS.clear(); G._ROWS.update(self.keep[1])
        for k, v in (("GOLD_COPY", self.keep[2]), ("SUPABASE_DB_URL", self.keep[3])):
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

    def test_the_copy_answers_unless_switched_off(self):
        os.environ["GOLD_COPY"] = "off"
        self.assertFalse(G.serves("appGOLD", "Metadata"))                     # the way back to Airtable
        self.assertTrue(G.serves("appGOLD", "Metadata", force=True))          # --verify reads it all the same
        os.environ.pop("GOLD_COPY", None)
        self.assertTrue(G.serves("appGOLD", "Metadata"))                      # the default since 14 Nov 2026
        self.assertFalse(G.serves("appGOLD", "Sources"))                      # a table the copy does not hold
        self.assertFalse(G.serves("appOTHER", "Metadata"))
        self.assertFalse(G.serves("appGOLD", "report_library"))               # never one of ours

    def test_get_all_is_answered_by_the_copy_in_airtables_shape(self):
        os.environ["GOLD_COPY"] = "on"
        got = SL.get_all("no token needed", "appGOLD", "Metadata", ["Metadata ID", "Selected Value"], "AND({Data Reviewed}, {Selected Value}!='')")
        self.assertEqual(got, [{"id": "rec1", "fields": {"Metadata ID": "PT_1-GMC_00048.1-2024", "Selected Value": "5"}}])
        with self.assertRaises(ValueError):
            G.serve("appGOLD", "Metadata", ["A field nobody copied"])          # refused; get_all then goes to Airtable


if __name__ == "__main__":
    unittest.main()
