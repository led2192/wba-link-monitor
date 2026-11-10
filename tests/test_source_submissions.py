import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import source_submissions as S  # noqa: E402


class Plan(unittest.TestCase):
    def test_links_are_pasted_after_what_the_box_holds_once_each(self):
        rows = [(1, "PT_1", "https://a.example/r.pdf"), (2, "PT_1", "https://a.example/s.pdf"), (3, "PT_1", "https://a.example/r.pdf")]
        pastes, waiting, orphans = S.plan(rows, {"PT_1": ("recA", "https://a.example/old.pdf\n")})
        self.assertEqual(pastes, {"recA": ("PT_1", "https://a.example/old.pdf\nhttps://a.example/r.pdf\nhttps://a.example/s.pdf", [1, 2])})
        self.assertEqual(waiting, [3]); self.assertEqual(orphans, [])

    def test_a_link_already_waiting_is_not_pasted_again(self):
        pastes, waiting, orphans = S.plan([(7, "PT_1", "https://a.example/r.pdf")], {"PT_1": ("recA", "https://a.example/r.pdf")})
        self.assertEqual(pastes, {}); self.assertEqual(waiting, [7])

    def test_a_company_not_in_the_base_is_an_orphan_not_a_new_row(self):
        pastes, waiting, orphans = S.plan([(9, "PT_404", "https://a.example/r.pdf")], {})
        self.assertEqual(pastes, {}); self.assertEqual(orphans, [(9, "PT_404")])

    def test_box_lines(self):
        self.assertEqual(S.lines_of(" a \n\n b\n"), ["a", "b"]); self.assertEqual(S.lines_of(None), [])

    def test_nothing_is_written_to_the_library_or_typecast(self):
        src = open(S.__file__, encoding="utf-8").read()
        self.assertNotIn("typecast", src); self.assertNotIn('"report_library"', src)


if __name__ == "__main__":
    unittest.main()
