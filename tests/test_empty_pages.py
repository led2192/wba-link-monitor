import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import empty_pages as E  # noqa: E402


class Classify(unittest.TestCase):
    def test_a_document_with_no_text(self):
        c = E.classify([(1, 0), (2, 3), (3, 0)])
        self.assertEqual((c["documents with no text"], c["pages of documents with no text"], c["documents with text"]), (1, 3, 0))

    def test_covers_are_apart_from_middle_pages(self):
        c = E.classify([(1, 0), (2, 900), (3, 0), (4, 800), (5, 10)])
        self.assertEqual(c["empty first or last page"], 2)
        self.assertEqual(c["empty middle pages"], 1)
        self.assertEqual(c["documents with an empty middle page"], 1)
        self.assertEqual(c["documents with 10% or more of their middle pages empty"], 1)    # 1 of 3

    def test_order_of_rows_does_not_matter(self):
        rows = [(3, 0), (1, 500), (2, 500), (4, 500)]
        self.assertEqual(E.classify(rows), E.classify(sorted(rows)))

    def test_clean_document(self):
        c = E.classify([(p, 1000) for p in range(1, 30)])
        self.assertEqual(c["empty middle pages"] + c["empty first or last page"], 0)


if __name__ == "__main__":
    unittest.main()
