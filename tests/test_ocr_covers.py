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


if __name__ == "__main__":
    unittest.main()
