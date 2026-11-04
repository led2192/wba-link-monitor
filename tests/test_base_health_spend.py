"""Base health adds up the model spend the jobs print (3 Nov 2026: the prepaid credit ran out unseen)."""
import io, os, sys, unittest, zipfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import base_health as B  # noqa: E402


class Resp:
    def __init__(self, json=None, content=b"", status=200):
        self._j, self.content, self.status_code = json, content, status
    def json(self): return self._j
    def raise_for_status(self): pass


def logs(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, t in files.items():
            z.writestr(n, t)
    return buf.getvalue()


class Spend(unittest.TestCase):
    def test_estimates_from_the_three_report_shapes(self):
        t = ("| model reader: cost in USD (estimate) | 0.63 |\n| judge cost in USD (estimate) | 0.024 |\n"
             "Model calls: 240 (0 failed); cost about $0.31 ($1.29 per 1,000 answers).")
        self.assertAlmostEqual(B.spend_in_log(t), 0.964)

    def test_runs_of_the_week_summed_per_job_without_the_step_copies(self):
        now = datetime(2026, 11, 3, 22, tzinfo=timezone.utc)
        runs = {"propose_values.yml": [{"id": 1, "status": "completed", "name": "Propose values", "created_at": "2026-11-03T07:33:00Z"}],
                "extract_eval.yml": [{"id": 2, "status": "completed", "name": "Generic reader", "created_at": "2026-10-30T10:00:00Z"},
                                     {"id": 3, "status": "in_progress", "name": "Generic reader", "created_at": "2026-11-03T21:00:00Z"}],
                "web_search.yml": []}
        zips = {1: logs({"0_propose (1).txt": "| model reader: cost in USD (estimate) | 0.63 |",
                         "1_propose (2).txt": "| model reader: cost in USD (estimate) | 0.60 |",
                         "propose (1)/3_Run.txt": "| model reader: cost in USD (estimate) | 0.63 |"}),
                2: logs({"0_evaluate.txt": "cost about $0.31 ($1.29 per 1,000 answers)"})}
        def get(url, **kw):
            if url.endswith("/workflows/narrative_eval.yml/runs"):
                return Resp(status=404)
            for wf, rs in runs.items():
                if url.endswith(f"/workflows/{wf}/runs"):
                    return Resp({"workflow_runs": rs})
            return Resp(content=zips[int(url.rstrip("/").split("/")[-2])])
        os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"] = "t", "o/r"
        got = B.model_spend(now, get=get)
        self.assertAlmostEqual(got["7d"], 1.54); self.assertAlmostEqual(got["24h"], 1.23)
        self.assertAlmostEqual(got["by"]["Propose values"], 1.23)


if __name__ == "__main__":
    unittest.main()
