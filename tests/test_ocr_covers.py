import os, shutil, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")
import ocr_covers as O   # noqa: E402


class Outcome(unittest.TestCase):
    def test_enough_letters_is_ok_and_cleaned(self):
        s, t = O.outcome("Rapport   Annuel 2024\n\n" + "texte " * 60)
        self.assertEqual(s, "ok")
        self.assertTrue(t.startswith("Rapport Annuel 2024 texte"))

    def test_too_little_text_is_marked_so_it_is_not_retried(self):
        self.assertEqual(O.outcome("12 3 . ."), ("no_text_ocr", ""))

    def test_cap(self):
        self.assertLessEqual(len(O.outcome("a" * 50000)[1]), O.BCT.COVER_CHARS)


@unittest.skipUnless(shutil.which("tesseract") and shutil.which("pdftoppm"), "needs tesseract and poppler")
class RealScan(unittest.TestCase):
    def test_reads_an_image_only_pdf(self):
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            self.skipTest("needs Pillow")
        import io
        img = Image.new("RGB", (1240, 1754), "white")
        d = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
        except OSError:
            self.skipTest("needs a TrueType font")
        for i, line in enumerate(["Annual Report 2024", "Example Group plc", "Strategic report"] * 4):
            d.text((120, 200 + 80 * i), line, fill="black", font=font)
        buf = io.BytesIO(); img.save(buf, "PDF")
        old = O.LANGS
        O.LANGS = "eng"
        try:
            text = O.ocr_pdf(buf.getvalue(), 2)
        finally:
            O.LANGS = old
        self.assertIn("Annual Report 2024", text)



class FreshLinks(unittest.TestCase):
    def test_rows_are_reread_in_order(self):
        calls = []

        class R:
            def __init__(self, j): self.j = j
            def json(self): return self.j

        def fake(method, url, headers, params=None):
            calls.append(dict(params)["filterByFormula"])
            return R({"records": [{"id": "rec2", "fields": {"file": [{"url": "new2"}]}},
                                  {"id": "rec1", "fields": {"file": [{"url": "new1"}]}}]})
        old = O.airtable_request
        O.airtable_request = fake
        try:
            got = O.fresh([{"id": "rec1"}, {"id": "rec2"}, {"id": "rec3"}])
        finally:
            O.airtable_request = old
        self.assertEqual([r["id"] for r in got], ["rec1", "rec2"])      # a row gone meanwhile is dropped
        self.assertIn("RECORD_ID()='rec3'", calls[0])
        self.assertEqual(O.fresh([]), [])

    def test_source_and_time_budget(self):
        self.assertEqual(O.source({"fields": {"file": [{"url": "x"}]}}), "file")
        self.assertEqual(O.source({"fields": {"document_url": "x"}}), "link")
        self.assertEqual(O.one({"id": "rec1", "fields": {}}, 4, deadline=1)[1], "not_tried")


if __name__ == "__main__":
    unittest.main()
