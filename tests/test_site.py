"""Static checks on the search page (site/index.html): no browser, no network.

Run:  python3 -m unittest tests/test_site.py -v

Two mistakes from 27 Oct 2026 that no other test could catch:
- two functions with the same name in one script keep only the second (#108 named the sign-in
  renderer `render`, the name of the result renderer, and the app never appeared);
- calling getSession() inside the onAuthStateChange callback deadlocks the library's start-up.
"""
import os, re, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def page_script():
    html = open(os.path.join(ROOT, "site", "index.html"), encoding="utf-8").read()
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert scripts, "no inline script in site/index.html"
    return scripts[-1]


class SitePage(unittest.TestCase):
    def test_function_names_are_unique(self):
        names = re.findall(r"^\s*(?:async\s+)?function\s+(\w+)\s*\(", page_script(), flags=re.M)
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(dupes, [], f"duplicate function names: {dupes}")

    def test_auth_callback_does_not_read_the_session(self):
        m = re.search(r"onAuthStateChange\((.*?)\);\s*$", page_script(), flags=re.M | re.S)
        self.assertIsNotNone(m, "onAuthStateChange not found")
        self.assertNotIn("getSession", m.group(1))
        self.assertNotIn("show()", m.group(1))

    def test_session_renderer_is_named_apart_from_the_result_renderer(self):
        s = page_script()
        self.assertIn("function showSession(session)", s)
        self.assertIn("function render(r)", s)
        self.assertNotIn("render(session)", s)
        self.assertNotIn("render(data.session)", s)


if __name__ == "__main__":
    unittest.main()
