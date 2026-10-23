import glob, os, re, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows")


def names():
    out = {}
    for p in glob.glob(os.path.join(WF, "*.yml")):
        m = re.search(r"^name:\s*(.+)$", open(p).read(), re.M)
        out[os.path.basename(p)] = m.group(1).strip().strip('"') if m else ""
    return out


class RerunWorkflow(unittest.TestCase):
    def test_every_listed_workflow_exists(self):
        text = open(os.path.join(WF, "rerun_failed.yml")).read()
        listed = re.findall(r'^\s+- "(.+)"$', text, re.M)
        self.assertGreater(len(listed), 20)
        known = set(names().values())
        self.assertEqual([n for n in listed if n not in known], [])

    def test_retries_only_first_attempt_failures(self):
        text = open(os.path.join(WF, "rerun_failed.yml")).read()
        self.assertIn("run_attempt == 1", text)
        self.assertIn("conclusion == 'failure'", text)
        for excluded in ("Scan now", "QA archive", "Purge discarded rows"):
            self.assertNotIn(f'"{excluded}"', text)


if __name__ == "__main__":
    unittest.main()
