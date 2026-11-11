import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import robot_identity as R


class Identity(unittest.TestCase):
    def test_the_robot_says_who_it_is_and_where_to_read_about_it(self):
        self.assertIn("lanterN-bot/1.0", R.BOT_UA); self.assertIn("+https://", R.BOT_UA); self.assertIn("bot.html", R.BOT_UA)
        page = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site", "bot.html"), encoding="utf-8").read()
        self.assertIn("lanterN-bot", page); self.assertIn("robots.txt", page); self.assertIn("issues", page)

    def test_outcomes(self):
        self.assertEqual([R.outcome(s) for s in (200, 301, 403, 429, 404, 500, 0)], ["ok", "ok", "refused", "refused", "gone", "error", "error"])

    def test_robots_rules(self):
        rules = "User-agent: *\nDisallow: /private/\n\nUser-agent: lanterN-bot\nDisallow: /investors/\n"
        self.assertFalse(R.allowed(rules, "https://x.com/investors/reports"))
        self.assertTrue(R.allowed(rules, "https://x.com/private/a"))                 # its own group replaces the general one
        self.assertFalse(R.allowed(rules, "https://x.com/private/a", "*"))
        self.assertTrue(R.allowed("", "https://x.com/anything"))                     # no robots.txt forbids nothing
        self.assertFalse(R.allowed("User-agent: *\nDisallow: /\n", "https://x.com/reports"))

    def test_one_page_a_site_the_same_every_time(self):
        rows = [("https://a.com/1", "hub"), ("https://a.com/2", "hub"), ("https://b.com/1", "hub"), ("https://c.com/1", "hub")]
        s = R.one_per_host(rows, 2, 1)
        self.assertEqual(len(s), 2); self.assertEqual(s, R.one_per_host(rows, 2, 1))
        self.assertEqual(len({u.split("/")[2] for u, _ in s}), 2)


if __name__ == "__main__":
    unittest.main()
