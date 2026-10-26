import unittest

import supabase_check as S

REF = "abcdefghijklmnopqrst"
GOOD = f"postgresql://postgres.{REF}:Abc123xyz@aws-1-eu-central-1.pooler.supabase.com:5432/postgres"


class DbUrl(unittest.TestCase):
    def test_good_session_pooler_address(self):
        self.assertEqual(S.check_db_url(GOOD), ([], REF))

    def test_placeholder_left_in(self):
        p, _ = S.check_db_url(GOOD.replace("Abc123xyz", "[YOUR-PASSWORD]"))
        self.assertTrue(any("placeholder" in x for x in p))

    def test_direct_connection_and_transaction_port(self):
        p, _ = S.check_db_url(f"postgresql://postgres:pw@db.{REF}.supabase.co:5432/postgres")
        self.assertTrue(any("session pooler" in x for x in p))
        p, _ = S.check_db_url(GOOD.replace(":5432", ":6543"))
        self.assertTrue(any("6543" in x for x in p))

    def test_trailing_newline_is_reported(self):
        p, ref = S.check_db_url(GOOD + "\n")
        self.assertEqual(ref, REF)
        self.assertTrue(any("line break" in x for x in p))

    def test_missing(self):
        self.assertEqual(S.check_db_url("")[1], None)


class ApiUrlAndKey(unittest.TestCase):
    def test_api_url(self):
        self.assertEqual(S.check_api_url(f"https://{REF}.supabase.co"), ([], REF))
        self.assertTrue(S.check_api_url("https://supabase.com/dashboard/project/x")[0])
        p, ref = S.check_api_url(f"https://{REF}.supabase.co/rest/v1/")
        self.assertEqual(ref, REF)
        self.assertIn("path", p[0])
        p, _ = S.check_api_url(f"{REF}.supabase.co")
        self.assertIn("https://", p[0])

    def test_key_kinds(self):
        self.assertEqual(S.key_kind("sb_secret_abc"), "secret")
        self.assertEqual(S.key_kind("sb_publishable_abc"), "publishable")
        self.assertEqual(S.key_kind("eyJa.eyJb.c"), "legacy JWT")
        self.assertEqual(S.key_kind(""), "missing")


if __name__ == "__main__":
    unittest.main()
