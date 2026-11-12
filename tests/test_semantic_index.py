import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import semantic_index as S  # noqa: E402


def schema_text():
    return open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "supabase", "schema.sql"), encoding="utf-8").read()


class Arguments(unittest.TestCase):
    def test_partition(self):
        self.assertEqual(S.parse_partition(""), (0, 1)); self.assertEqual(S.parse_partition("2/4"), (2, 4))
        with self.assertRaises(ValueError):
            S.parse_partition("4/4")

    def test_years(self):
        self.assertEqual(S.parse_years("2024-"), (2024, None)); self.assertEqual(S.parse_years("2023-2024"), (2023, 2024))
        self.assertEqual(S.parse_years("2025"), (2025, 2025)); self.assertEqual(S.parse_years(None), (S.YEAR_MIN, None))


class Chunks(unittest.TestCase):
    def test_pages_become_passages_that_say_something(self):
        long = " ".join(f"Sentence number {i} says that scope one emissions fell by three percent this year." for i in range(30))
        out = S.chunk_pages([(1, long), (2, "12"), (3, "")])
        self.assertTrue(out and all(p == 1 for p, _, _ in out))             # page 2 is a stub, page 3 empty
        self.assertEqual([n for _, n, _ in out], list(range(len(out))))
        self.assertTrue(all(8 <= len(t.split()) <= 200 for _, _, t in out)); self.assertEqual(len(out), 4)

    def test_vector_literal(self):
        self.assertEqual(S.vector_literal([0.5, -1, 0.123456789]), "[0.500000,-1.000000,0.123457]")


class Writes(unittest.TestCase):
    def test_one_document_is_one_transaction_of_rows_then_its_marker(self):
        calls = []

        class Cur:
            def executemany(self, sql, rows): calls.append(("many", sql[:40], len(rows)))
            def execute(self, sql, args=None): calls.append(("one", sql[:40], args))
        chunks = [(1, 0, "a"), (1, 1, "b"), (2, 0, "c")]
        S.write_document(Cur(), "PT_1-abc", chunks, [[0.0] * 3] * 3, "m")
        self.assertEqual(calls[0][0], "many"); self.assertEqual(calls[0][2], 3)
        self.assertEqual(calls[-1][0], "one"); self.assertIn("chunk_docs", calls[-1][1]); self.assertEqual(calls[-1][2], ("PT_1-abc", 3, "m"))


class Pairs(unittest.TestCase):
    def test_neighbouring_words_become_lexemes(self):
        lex = S.pair_lexemes("ACT Core methodology; Scope 1 emissions. ACT Core")
        self.assertEqual(lex, ["1_emissions", "act_core", "core_methodology", "emissions_act", "methodology_scope", "scope_1"])
        self.assertEqual(S.pair_lexemes("alone"), []); self.assertEqual(S.pair_lexemes(None), [])

    def test_other_scripts_and_broken_words(self):
        self.assertIn("développement_durable", S.pair_lexemes("Développement durable"))
        self.assertEqual(S.pair_lexemes("x" * 41 + " word"), [])

    def test_rows_leave_out_empty_pages_and_quote_lexemes(self):
        self.assertEqual(S.pair_rows([(1, "just transition"), (2, "one"), (3, "")]), [(1, "'just_transition'")])
        self.assertEqual(S.pairs_literal(["a_b", "o'x_y"]), "'a_b' 'ox_y'")

    def test_pairs_are_written_before_the_marker(self):
        calls = []

        class Cur:
            def executemany(self, sql, rows): calls.append(("many", sql[:40], len(rows)))
            def execute(self, sql, args=None): calls.append(("one", sql[:40], args))
        S.write_document(Cur(), "PT_1-abc", [(1, 0, "a")], [[0.0] * 3], "m", [(1, "'a_b'"), (2, "'c_d'")])
        self.assertIn("page_pairs", calls[0][1]); self.assertEqual(calls[0][2], 2)
        self.assertIn("chunk_docs", calls[-1][1])


class Reconnect(unittest.TestCase):
    def test_a_lost_connection_is_replaced_not_fatal(self):
        import os
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "semantic_index.py"), encoding="utf-8").read()
        self.assertIn('if not gone or lost >= 5:', src)
        self.assertIn('WRITE_S["pairs"] += time.time() - t', src); self.assertIn("Minutes writing:", src)   # where the time goes is printed, not guessed (12 Nov 2026)
        self.assertIn('prs if (has_pairs and not a.no_pairs) else None', src)
        self.assertIn('FILL_INDEXES = ("chunks_embedding", "page_pairs_pairs")', src)           # the fill writes with no index on the embeddings nor on the pairs
        self.assertIn('cur.execute(f"drop index if exists public.{name}")', src)
        self.assertNotIn("create index if not exists page_pairs_pairs", schema_text())
        self.assertIn("set lock_timeout = '30s'", src)                                          # and never waits long for the lock to drop it
        schema = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "supabase", "schema.sql"), encoding="utf-8").read()
        self.assertNotIn("create index if not exists chunks_embedding", schema)                 # a load must not build it back in the middle of the fill
        self.assertIn("--build-index", src)      # five times, then the run fails as before
        self.assertIn('getattr(conn, "broken", False)', src)                                 # a connection the driver gave up on is lost too
        self.assertIn("conn = evidence_db.connect(); cur = conn.cursor()", src)


if __name__ == "__main__":
    unittest.main()
