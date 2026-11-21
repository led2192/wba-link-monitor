"""The served pages lose their comments and nothing a browser needs (build_page.py)."""
import os, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import build_page as B

try:
    import rjsmin  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False


@unittest.skipUnless(HAVE, "rjsmin is not installed")
class Compact(unittest.TestCase):
    def test_the_search_page_keeps_what_it_runs_and_loses_its_notes(self):
        src = open(os.path.join(ROOT, "site", "search.html"), encoding="utf-8").read()
        out = B.compact("search.html", src)
        self.assertLess(len(out), len(src) * 0.9)
        self.assertEqual(B.problems(src, out, ["lantern v", "rpc/link_enter", "__SUPABASE_URL__"]), [])
        self.assertNotIn("// v1", out); self.assertNotIn("<!--", out); self.assertNotIn("/*", out.split("</style>")[0])
        self.assertIn("https://lantrna.com/#k=", out)                    # "//" inside a string is not a comment
        self.assertEqual(out.count("<script"), src.count("<script")); self.assertEqual(out.count("</style>"), src.count("</style>"))

    def test_every_page_and_the_extension_compact_cleanly(self):
        for folder, names in (("site", [n for n in os.listdir(os.path.join(ROOT, "site")) if n.endswith(".html")]),
                              ("extension", [n for n in os.listdir(os.path.join(ROOT, "extension")) if n.endswith((".js", ".html"))])):
            for n in names:
                src = open(os.path.join(ROOT, folder, n), encoding="utf-8").read()
                out = B.compact(n, src)
                self.assertTrue(out.strip(), n); self.assertLessEqual(len(out), len(src), n)

    def test_small_cases(self):
        self.assertEqual(B.compact_js("// why\nconst a = 1; /* note */ const u = 'http://x//y'; const r = /\\/\\//;\n"),
                         "const a=1;const u='http://x//y';const r=/\\/\\//;")
        self.assertEqual(B.compact_css("a { color: red; } /* why */\n\n  b { }"), "a { color: red; }\nb { }")
        html = '<p>x</p>\n<!-- note -->\n<script src="a.js"></script>\n<script>\n// c\nlet a = 1;\n</script>'
        self.assertEqual(B.compact_html(html), '<p>x</p>\n<script src="a.js"></script>\n<script>let a=1;</script>')

    def test_a_result_that_lost_something_is_not_written(self):
        self.assertEqual(B.problems("abc KEEP", "ab", ["KEEP"]), ["lost 'KEEP'"])
        self.assertEqual(B.problems("abc", "abcd", []), ["larger"]); self.assertEqual(B.problems("abc", "abc", []), [])
        self.assertEqual(B.problems("abc", " ", []), ["empty"])


if __name__ == "__main__":
    unittest.main()
