import unittest

import file_space as F


class Whole(unittest.TestCase):
    def test_totals_repeats_and_bands(self):
        rows = [(2.0, "h1", 2024, "annual_report"), (2.0, "h1", 2024, "annual_report"), (30.0, "h2", 2010, "sustainability_report"),
                (0.5, "unavailable", None, None), (0.5, "unavailable", None, None), (None, "h9", 2024, "annual_report")]
        w = F.whole(rows)
        self.assertEqual((w["documents"], w["with_size"]), (6, 5))
        self.assertEqual(w["total_mb"], 35.0)
        self.assertEqual((w["repeat_files"], w["repeat_mb"]), (1, 2.0))             # 'unavailable' is not a hash: never a repeat
        self.assertEqual(w["n_band"]["20 to 50 MB"], 1); self.assertEqual(w["by_band"]["1 to 5 MB"], 4.0)
        self.assertEqual(w["by_year"]["before 2015"], 30.0); self.assertEqual(w["by_year"]["no year"], 1.0)
        lines = F.whole_lines(w)
        self.assertIn("the same file stored again (same hash): 1 files, 0.0 GB (5.7% of the bytes)", lines)

    def test_bands_cover_every_size(self):
        self.assertEqual([F.band(x) for x in (0.2, 1, 19.9, 20, 400)], ["under 1 MB", "1 to 5 MB", "5 to 20 MB", "20 to 50 MB", "50 MB and over"])


class Sample(unittest.TestCase):
    def test_a_rewrite_that_is_not_smaller_keeps_the_original(self):
        results = [{"bytes": 10_000_000, "zstd": 9_000_000, "gs": 2_000_000, "gs_pages_same": True, "gs_text_kept": True},
                   {"bytes": 1_000_000, "zstd": 950_000, "gs": 1_500_000, "gs_pages_same": True, "gs_text_kept": False},
                   {"bytes": 500_000, "zstd": None, "gs": None, "gs_pages_same": None, "gs_text_kept": None}]
        lines = F.sample_lines(results, 4)
        self.assertIn("sample: 3 of 4 files downloaded and measured", lines[0])
        self.assertIn("compressed whole without loss (zstd 19): 9.5% smaller (2 files)", lines[1])
        self.assertIn("72.7% smaller over 2 files", lines[2])                       # (2 + 1) of 11 MB kept
        self.assertIn("1 files at least 10% smaller, 1 came out larger; 1 could not be rewritten", lines[2])
        self.assertIn("kept its pages in 2 of 2 and its text (95% of the characters of the first 30 pages) in 1 of 2", lines[3])

    def test_says_so_when_a_measure_could_not_be_made(self):
        lines = F.sample_lines([{"bytes": 10, "zstd": None, "gs": None, "gs_pages_same": None, "gs_text_kept": None}], 1)
        self.assertIn("not measured (zstandard is not installed)", lines[1]); self.assertIn("not measured (gs is not installed)", lines[2])
        self.assertEqual(F.sample_lines([], 5), ["sample: none of the 5 files could be downloaded"])

    def test_no_address_or_name_can_be_printed(self):
        self.assertNotIn("http", " ".join(F.sample_lines([{"bytes": 10, "zstd": 5, "gs": 4, "gs_pages_same": True, "gs_text_kept": True}], 1)))
        self.assertEqual(F.gs_cmd("a.pdf", "b.pdf")[-2:], ["-sOutputFile=b.pdf", "a.pdf"])


if __name__ == "__main__":
    unittest.main()
