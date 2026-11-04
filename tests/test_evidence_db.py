import unittest
from unittest import mock

import psycopg

import evidence_db as E


class Connect(unittest.TestCase):
    def test_waits_while_the_pool_is_full(self):
        full = psycopg.OperationalError("FATAL: (EMAXCONNSESSION) max clients reached in session mode")
        waits = []
        with mock.patch.object(psycopg, "connect", side_effect=[full, full, "conn"]):
            self.assertEqual(E.connect("postgres://x", sleep=waits.append), "conn")
        self.assertEqual(waits, [15, 30])

    def test_other_errors_fail_at_once(self):
        with mock.patch.object(psycopg, "connect", side_effect=psycopg.OperationalError("password authentication failed")):
            with self.assertRaises(psycopg.OperationalError):
                E.connect("postgres://x", sleep=lambda s: self.fail("should not wait"))

    def test_gives_up_after_the_wait(self):
        full = psycopg.OperationalError("max clients reached")
        with mock.patch.object(psycopg, "connect", side_effect=full):
            with self.assertRaises(psycopg.OperationalError):
                E.connect("postgres://x", wait_minutes=0, sleep=lambda s: None)


if __name__ == "__main__":
    unittest.main()
