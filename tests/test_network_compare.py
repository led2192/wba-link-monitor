import unittest

import network_compare as N


class Letters(unittest.TestCase):
    def test_one_letter_a_read(self):
        self.assertEqual([N.letter(status=s) for s in (200, 403, 429, 404, 500, 301)], ["o", "r", "r", "g", "e", "e"])
        self.assertEqual([N.letter(error=e) for e in ("timeout", "connection", "ssl")], ["t", "c", "x"])


class Sample(unittest.TestCase):
    def test_the_same_pages_whatever_the_order_they_come_in(self):
        urls = [f"https://site{i:03}.example/reports" for i in range(100)]
        a = N.sample(urls, 10)
        b = N.sample(list(reversed(urls)) + ["", None, "not an address"], 10)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 10)
        self.assertEqual(a[0], "https://site000.example/reports")

    def test_a_small_list_is_read_whole(self):
        self.assertEqual(N.sample(["https://b.example", "https://a.example", "https://a.example"], 400),
                         ["https://a.example", "https://b.example"])


class Reading(unittest.TestCase):
    def test_every_page_once_in_order_with_no_network(self):
        urls = ["https://a.example/1", "https://a.example/2", "https://b.example/1"]
        seen = []

        def reader(session, url):
            seen.append(url)
            return "r" if url.endswith("/2") else "o"

        self.assertEqual(N.read_all(urls, workers=3, reader=reader), "oro")
        self.assertEqual(sorted(seen), sorted(urls))


class Table(unittest.TestCase):
    def test_counts_what_one_machine_reads_and_the_other_does_not(self):
        lines, lost, won = N.table("ooorrt", "oorrot")
        self.assertEqual((lost, won), (1, 1))
        self.assertIn("pages compared: 6; the same answer on both machines: 4 (66%)", lines[0])
        self.assertIn("  A answered, B refused: 1", lines)
        self.assertIn("  A refused, B answered: 1", lines)

    def test_no_address_is_ever_printed(self):
        lines, _, _ = N.table("or", "ro")
        self.assertFalse(any("http" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
