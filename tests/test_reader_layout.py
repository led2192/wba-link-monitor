"""The reader's layout of a page (page v113, 19 Nov 2026): the lines of a page as the library holds them, turned into
tables, paragraphs and headings by the page's own readerParts(), run in node. Nothing may be added or dropped."""
import json, os, shutil, subprocess, unittest

PAGE = open(os.path.join(os.path.dirname(__file__), "..", "site", "search.html"), encoding="utf-8").read()

TABLE = """Emissions Indicator Name
2025 Reported Values in Metric
Scope 1 emissions 38,379
Scope 2, Location-based method (LBM) emissions 41,284
Category 3: Activities Not Included in Scope 1 or Scope 2 20,487
Category 8: Upstream Leased Assets 65
Category 9: Downstream Transportation and Distribution Not calculated; not relevant
Category 10: Processing of Sold Products Not calculated; not relevant
Category 11: Use of Sold Products 1,186
Carbon Credits4 1,087,936
Reporting Boundary:
The company has selected an organizational boundary for the subject matter based on the company's operational
control. All direct and indirect emissions from owned and leased corporate facilities are included in the Scope 1
and Scope 2 emissions reported.
A second paragraph begins here and it is long enough to be read as a line that the column of the file cut off
before its end."""


def parts(text):
    code = PAGE[PAGE.index("  const NUM_CELL = "):PAGE.index("  // Draws the parts into the reader")]
    out = subprocess.run(["node", "-e", code + "\nconsole.log(JSON.stringify(readerParts(%s)))" % json.dumps(text)], capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise AssertionError(out.stderr[-1500:])
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ReaderLayout(unittest.TestCase):
    def test_lines_that_end_in_figures_become_a_table_and_cut_lines_a_paragraph(self):
        got = parts(TABLE)
        self.assertEqual([p["t"] for p in got], ["l", "l", "table", "h", "p", "p"])
        rows = got[2]["rows"]
        self.assertEqual(rows[0], {"label": "Scope 1 emissions", "cells": ["38,379"]})
        self.assertEqual(rows[2], {"label": "Category 3: Activities Not Included in Scope 1 or Scope 2", "cells": ["20,487"]})   # the 2 belongs to the label
        self.assertEqual(rows[3], {"label": "Category 8: Upstream Leased Assets", "cells": ["65"]})
        self.assertEqual(rows[4]["cells"], [])                                  # a line with no figure stays in the table, across it
        self.assertEqual(rows[-1], {"label": "Carbon Credits4", "cells": ["1,087,936"]})
        self.assertTrue(got[4]["s"].endswith("emissions reported.") and got[4]["s"].startswith("The company has selected"))
        self.assertTrue(got[5]["s"].startswith("A second paragraph"))

    def test_nothing_is_added_or_dropped(self):
        words = lambda t: " ".join(t.split())
        flat = []
        for p in parts(TABLE):
            flat += [" ".join([r["label"]] + r["cells"]).strip() for r in p["rows"]] if p["t"] == "table" else [p["s"]]
        self.assertEqual(words(" ".join(flat)), words(TABLE))

    def test_several_columns_two_lines_with_a_figure_and_bullets(self):
        got = parts("Revenue 1,200 1,100 (3.5%)\nCost (800) (700) n/a\nProfit 400 400 -\n\nFounded in 1998\nOffices in 12\n\n• first point\n• second point\nSUMMARY OF RESULTS")
        self.assertEqual(got[0]["rows"][0], {"label": "Revenue", "cells": ["1,200", "1,100", "(3.5%)"]})
        self.assertEqual(got[0]["rows"][2]["cells"], ["400", "400", "-"])
        self.assertEqual([p["t"] for p in got[1:]], ["l", "l", "li", "li", "h"])      # two lines ending in a figure are not a table
        self.assertEqual(got[3]["s"], "first point")

    def test_a_document_with_no_address_opens_in_the_reader_not_in_a_new_tab_of_lantern(self):
        self.assertIn("function docLink(a, s, find) {", PAGE)
        self.assertIn("docLink(a, s); li.appendChild(a);", PAGE)                       # the answer's list of sources
        self.assertIn('data-cite="${n}"', PAGE)                                        # its [n] citations
        self.assertIn('ev.preventDefault(); openReader(r.library_id, r.page, "");', PAGE)   # a result's title

    def test_a_figure_of_the_answer_that_the_robot_holds_is_not_listed_twice(self):
        self.assertIn("heldValues.some(v => Math.abs(Number(v.value)) === Math.abs(Number(f.value)) && yearOf(f).includes(Number(v.year)))", PAGE)
        self.assertIn("&& !held(f));", PAGE)

if __name__ == "__main__":
    unittest.main()
