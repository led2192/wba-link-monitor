import unittest

import resolve_doc_conflicts as R


class KeepBoth(unittest.TestCase):
    def test_changelog_lines_from_both_sides(self):
        text = ("## October 2026\n<<<<<<< HEAD\n- 2026-10-26 one (#77)\n=======\n- 2026-10-26 two (#76)\n"
                ">>>>>>> origin/main\n- 2026-10-25 older\n")
        out = R.keep_both("docs/CHANGELOG.md", text)
        self.assertEqual(out, "## October 2026\n- 2026-10-26 one (#77)\n- 2026-10-26 two (#76)\n- 2026-10-25 older\n")

    def test_appended_sections_stay_apart(self):
        text = "x\n<<<<<<< HEAD\n## A\nfirst\n=======\n## B\nsecond\n>>>>>>> main\n"
        self.assertEqual(R.keep_both("docs/pitfalls.md", text), "x\n## A\nfirst\n\n## B\nsecond\n")

    def test_workflow_list_keeps_each_name_once(self):
        text = ('      - "Company coverage"\n<<<<<<< HEAD\n      - "Key reports"\n      - "Evidence passages"\n'
                '=======\n      - "Key reports"\n      - "Publication dates"\n>>>>>>> main\n')
        out = R.keep_both(".github/workflows/rerun_failed.yml", text)
        self.assertEqual(out.count('"Key reports"'), 1)
        self.assertIn('"Publication dates"', out)

    def test_only_documentation_is_resolved(self):
        self.assertTrue(R.resolvable(["docs/CHANGELOG.md", "docs/guide.md", ".github/workflows/rerun_failed.yml"]))
        self.assertFalse(R.resolvable(["docs/CHANGELOG.md", "qa_sync.py"]))
        self.assertFalse(R.resolvable([]))


if __name__ == "__main__":
    unittest.main()
