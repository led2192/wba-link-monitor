"""Offline test for base_health's AI typing check. Real case, 9 Nov 2026: the Airtable AI fields
(source_type_check, source_title) stopped answering around 20 Oct; of the English documents that
arrived after 25 Oct about 2% were typed, and 35,000 waited for three weeks while base_health said OK."""
import os, sys, unittest
from datetime import datetime, timezone
from unittest import mock

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import base_health as B  # noqa: E402

EMPTY = {"state": "empty", "value": None, "isStale": True}
ERROR = {"state": "error", "errorType": "emptyDependency", "value": None, "isStale": False}


def row(stc=None, title=None):
    f = {}
    if stc is not None:
        f["source_type_check"] = stc
    if title is not None:
        f["source_title"] = title
    return {"id": "rec", "fields": f}


class AiTyping(unittest.TestCase):
    def test_ai_states_without_a_value_are_untyped(self):
        rows = [row("Annual Report"), row(title={"state": "generated", "value": "Annual Report 2025"}),
                row(title=EMPTY), row(title=ERROR), row()]
        self.assertEqual(B.ai_typing_share(rows), (2, 3))

    def test_stall_turns_check(self):
        # the week of 2 Nov 2026: 27 typed, 15,788 waiting
        rows = [row("Sustainability Report")] * 27 + [row(title=EMPTY)] * 15788
        ai = B.ai_typing(datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc), rows=rows)
        self.assertEqual((ai["typed"], ai["untyped"]), (27, 15788))
        line = B.ai_typing_attention(ai)
        self.assertIn("0% of the English documents", line)
        self.assertIn("15,788 wait", line)

    def test_normal_day_is_quiet(self):
        # 19 Oct 2026, before the stall: 1,884 typed, 505 waiting (79%)
        rows = [row("Annual Report")] * 1884 + [row()] * 505
        self.assertIsNone(B.ai_typing_attention(B.ai_typing(datetime(2026, 10, 21, tzinfo=timezone.utc), rows=rows)))

    def test_too_few_rows_to_judge(self):
        self.assertIsNone(B.ai_typing_attention({"typed": 0, "untyped": 40, "share": 0.0}))
        self.assertIsNone(B.ai_typing_attention(None))
        self.assertIsNone(B.ai_typing_attention({"typed": 0, "untyped": 0, "share": None}))

    def test_window_formula(self):
        seen = {}
        def fake_sweep(table, fields, formula=None):
            seen.update(table=table, fields=fields, formula=formula)
            return iter([])
        with mock.patch.object(B, "sweep", fake_sweep):
            ai = B.ai_typing(datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc))
        self.assertEqual(seen["table"], "report_library")
        self.assertIn("IS_AFTER(CREATED_TIME(), '2026-10-31')", seen["formula"])
        self.assertIn("IS_BEFORE(CREATED_TIME(), '2026-11-07')", seen["formula"])
        self.assertIn("{classify_input}='run'", seen["formula"])
        self.assertIsNone(ai["share"])


if __name__ == "__main__":
    unittest.main()
