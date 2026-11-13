import unittest

import vault_secret as V


class Cur:
    def __init__(self, existing=None, reply=None):
        self.sql, self.existing, self.reply, self.last = [], existing, reply, None

    def execute(self, sql, params=()):
        self.sql.append((sql, params)); self.last = sql

    def fetchone(self):
        if "from vault.secrets" in self.last:
            return (self.existing,) if self.existing else None
        if "query_vector_made" in self.last:
            return (self.reply,)
        return None


class Conn:
    def __init__(self, cur): self.cur = cur
    def cursor(self): return self.cur
    def __enter__(self): return self
    def __exit__(self, *a): return False


class VaultSecret(unittest.TestCase):
    def test_dry_run_writes_nothing_and_never_prints_the_value(self):
        lines = []
        self.assertEqual(V.main(["--name", "hf_token", "--env", "HF_TOKEN"], connect=lambda: self.fail("connected"), env={"HF_TOKEN": "hf_secretvalue"}, out=lines.append), 0)
        self.assertIn("14 characters", lines[0]); self.assertNotIn("hf_secretvalue", " ".join(lines))

    def test_the_value_travels_as_a_parameter_created_once_then_replaced(self):
        for existing, word in ((None, "created"), ("id-1", "replaced")):
            cur, lines = Cur(existing, {"vec": [0.0] * 384}), []
            self.assertEqual(V.main(["--name", "hf_token", "--env", "HF_TOKEN", "--commit", "--try-query"], connect=lambda: Conn(cur), env={"HF_TOKEN": "hf_secretvalue"}, out=lines.append), 0)
            self.assertIn(word, lines[0])
            self.assertTrue(all("hf_secretvalue" not in sql for sql, _ in cur.sql))          # never inside a statement's text
            self.assertTrue(any("hf_secretvalue" in params for _, params in cur.sql))
            self.assertNotIn("hf_secretvalue", " ".join(lines))
            self.assertIn("a vector of 384 numbers", lines[1])

    def test_only_the_listed_name_from_its_own_variable_and_never_an_empty_value(self):
        lines = []
        self.assertEqual(V.main(["--name", "anthropic_api_key", "--env", "HF_TOKEN", "--commit"], connect=lambda: self.fail("connected"), env={"HF_TOKEN": "x"}, out=lines.append), 1)
        self.assertEqual(V.main(["--name", "hf_token", "--env", "HF_TOKEN", "--commit"], connect=lambda: self.fail("connected"), env={}, out=lines.append), 1)

    def test_a_refusal_is_said_in_the_functions_words(self):
        what, _ = V.try_vector(Cur(reply={"error": "the service answered HTTP 402"}))
        self.assertEqual(what, "no vector: the service answered HTTP 402")


if __name__ == "__main__":
    unittest.main()
