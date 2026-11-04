import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import dup_audit as D

TEXT = " ".join(f"word{i}" for i in range(60))


class Fingerprints(unittest.TestCase):
    def test_spacing_case_and_punctuation_do_not_matter(self):
        self.assertEqual(D.fingerprint(TEXT), D.fingerprint(TEXT.upper().replace(" ", " ,\n ")))
        self.assertIsNone(D.fingerprint("Annual Report 2024"))           # a cover is too short to count

    def test_a_changed_figure_is_a_different_page(self):
        self.assertNotEqual(D.fingerprint(TEXT + " 4120"), D.fingerprint(TEXT + " 4210"))


class Relations(unittest.TestCase):
    def test_relations(self):
        self.assertEqual(D.relation(100, 98, 97), "same")
        self.assertEqual(D.relation(300, 40, 36), "contained")          # a sustainability statement inside the annual report
        self.assertEqual(D.relation(100, 60, 30), "overlap")
        self.assertIsNone(D.relation(100, 60, 5))
        self.assertIsNone(D.relation(0, 60, 0))

    def test_pairs_only_for_documents_sharing_pages(self):
        prints = {"A": {"p1", "p2", "p3"}, "B": {"p1", "p2", "p3"}, "C": {"x"}}
        self.assertEqual(D.pairs_of(prints), [("A", "B", 3)])

    def test_boilerplate_page_in_many_documents_is_ignored(self):
        prints = {f"D{i}": {"disclaimer"} for i in range(60)}
        self.assertEqual(D.pairs_of(prints), [])

    def test_keep_the_copy_with_an_address(self):
        a = {"library_id": "a", "url": "", "pages": 120}
        b = {"library_id": "b", "url": "https://x.com/ar.pdf", "pages": 118}
        self.assertIs(D.keep_which(a, b), b)


if __name__ == "__main__":
    unittest.main()
