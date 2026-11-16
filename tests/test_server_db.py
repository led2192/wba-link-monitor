import os, re, unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = open(os.path.join(HERE, "server", "db_install.sh"), encoding="utf-8").read()
FLOW = open(os.path.join(HERE, ".github", "workflows", "server_db.yml"), encoding="utf-8").read()


class ServerDatabase(unittest.TestCase):
    def test_closed_to_the_world(self):
        self.assertIn("listen_addresses = 'localhost'", SCRIPT)
        self.assertNotRegex(SCRIPT, r"ufw\s+(allow|delete|disable|reset)")                # the firewall is read, never changed
        self.assertNotRegex(SCRIPT, r"listen_addresses\s*=\s*'\*'")
        hba = SCRIPT.split("<<HBAEOF")[1].split("HBAEOF")[0]
        lines = [l.split() for l in hba.splitlines() if l.strip() and not l.strip().startswith("#")]
        self.assertEqual([l[-1] for l in lines], ["peer", "scram-sha-256", "scram-sha-256"])
        self.assertEqual(lines[0][:3], ["local", "all", "postgres"])                     # the one way in without a password
        self.assertTrue(all(l[3] in ("127.0.0.1/32", "::1/128") for l in lines[1:]))     # passwords, and from the machine itself only
        self.assertNotIn("trust", hba)

    def test_it_deletes_nothing_and_holds_no_secret(self):
        for word in ("dropdb", "drop database", "pg_dropcluster", "purge", "rm -rf", "password '", "PGPASSWORD"):
            self.assertNotIn(word, SCRIPT)
        self.assertNotIn("secrets.", FLOW)
        self.assertIn("disable --now", SCRIPT)                                           # the way back stops, it does not remove

    def test_the_same_versions_as_supabase_a_ceiling_and_a_disk_guard(self):
        self.assertIn("PG=17", SCRIPT)
        self.assertIn('"postgresql-$PG-pgvector"', SCRIPT)
        self.assertIn("apt.postgresql.org.sh", SCRIPT)                                   # the source set up by Ubuntu's own script
        self.assertRegex(SCRIPT, r"MemoryMax=\$MEMORY_MAX")
        self.assertRegex(SCRIPT, r'if \[ "\$free" -lt "\$NEED_FREE_GB" \]')
        self.assertIn("origin=apt.postgresql.org", SCRIPT)                               # security updates arrive on their own

    def test_manual_only_on_the_server_and_check_by_default(self):
        self.assertNotIn("schedule:", FLOW)
        self.assertNotIn("pull_request", FLOW)
        self.assertIn("runs-on: [self-hosted, lantern]", FLOW)
        self.assertIn("default: check", FLOW)
        self.assertIn('MODE="${1:-check}"', SCRIPT)


if __name__ == "__main__":
    unittest.main()
