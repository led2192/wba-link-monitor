import collections, json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import visual_eval as V  # noqa: E402


class Matches(unittest.TestCase):
    def test_stated_scale_counts_other_scale_does_not(self):
        scales = [1, 1e3, 1e6]
        self.assertEqual(V.matches(1.25, "millions", 1250000, scales), "stated")
        self.assertEqual(V.matches(1250, "units", 1250000, scales), "digits")     # only at another scale
        self.assertIsNone(V.matches(999, "units", 1250000, scales))
        self.assertIsNone(V.matches(None, "units", 1250000, scales))


class Ask(unittest.TestCase):
    def setUp(self):
        import model_reader
        self.M = model_reader
        self.saved = model_reader.call

    def tearDown(self):
        self.M.call = self.saved

    def stub(self, reply):
        seen = {}

        def call(messages, usage, sess=None, model=None, system=None, max_tokens=None):
            seen["messages"] = messages
            return reply
        self.M.call = call
        return seen

    def test_images_are_sent_and_the_reply_parsed(self):
        seen = self.stub('{"value": "1,234.5", "scale": "Millions", "page": 2, "in_a_picture": true}')
        v, sc, pic = V.ask({"name": "Revenue", "labels": ["revenue"]}, 2024, ["QUJD", "REVG"], collections.Counter())
        self.assertEqual((v, sc, pic), (1234.5, "millions", True))
        blocks = seen["messages"][0]["content"]
        self.assertEqual(sum(b["type"] == "image" for b in blocks), 2)

    def test_null_and_garbage(self):
        self.stub('{"value": null}')
        self.assertEqual(V.ask({"name": "x"}, 2024, ["QUJD"], collections.Counter()), (None, None, None))
        self.stub("no json at all")
        self.assertEqual(V.ask({"name": "x"}, 2024, ["QUJD"], collections.Counter()), (None, None, None))


class Render(unittest.TestCase):
    def test_a_page_becomes_a_jpeg(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("pymupdf not installed")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.pdf")
            doc = pymupdf.open(); doc.new_page().insert_text((72, 72), "Revenue 2024 1,234"); doc.save(p); doc.close()
            b64 = V.render(p, 1)
            self.assertTrue(b64 and b64.startswith("/9j/"))          # JPEG magic, base64
            self.assertIsNone(V.render(p, 5))                       # no such page
            self.assertIsNone(V.render(os.path.join(d, "missing.pdf"), 1))


class NoSecrets(unittest.TestCase):
    def test_nothing_runs_without_tokens(self):
        env = {k: os.environ.pop(k) for k in ("GOLD_TOKEN", "ANTHROPIC_API_KEY") if k in os.environ}
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                sys.argv = ["visual_eval.py"]
                self.assertEqual(V.main(), 0)
                self.assertIn("nothing run", open("visual_eval.md").read())
            finally:
                os.chdir(cwd); os.environ.update(env)


if __name__ == "__main__":
    unittest.main()
