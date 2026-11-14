import os, re, subprocess, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "server", "setup.sh")


class ServerSetup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = open(PATH, encoding="utf-8").read()

    def test_the_script_parses(self):
        self.assertEqual(subprocess.run(["bash", "-n", PATH]).returncode, 0)

    def test_stops_on_the_first_error_and_never_echoes_commands(self):
        self.assertIn("set -euo pipefail", self.text)
        self.assertNotRegex(self.text, r"set -[a-z]*x")                 # -x would print the token

    def test_the_token_is_asked_for_and_never_written(self):
        self.assertIn("read -r TOKEN < /dev/tty", self.text)            # works when the script is piped too
        self.assertIn('TOKEN="${RUNNER_TOKEN:-}"', self.text)           # or given by the caller, nothing asked
        self.assertNotRegex(self.text, r"TOKEN[^\n]*>\s*[\w/.]")        # never redirected into a file
        self.assertNotRegex(self.text, r"gh[pousr]_[A-Za-z0-9]{20,}|[A-Z0-9]{29}")   # no token pasted in

    def test_the_firewall_is_up_before_any_runner_starts(self):
        self.assertLess(self.text.index("ufw --force enable"), self.text.index("config.sh"))
        self.assertIn("ufw allow 22/tcp", self.text)                    # SSH stays open: enabling must not lock us out
        self.assertLess(self.text.index("ufw allow 22/tcp"), self.text.index("ufw --force enable"))

    def test_running_it_again_registers_nothing_twice(self):
        self.assertIn('if [ -f "$D/.runner" ]; then echo "runner $i: already registered"; continue; fi', self.text)

    def test_only_this_repository(self):
        urls = set(re.findall(r'REPO_URL="([^"]+)"', self.text))
        self.assertEqual(urls, {"https://github.com/led2192/wba-link-monitor"})


class FirstBoot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = os.path.join(ROOT, "server", "first_boot.sh")
        cls.text = open(cls.path, encoding="utf-8").read()

    def test_the_script_parses_and_fits_the_installer(self):
        self.assertEqual(subprocess.run(["bash", "-n", self.path]).returncode, 0)
        self.assertTrue(self.text.startswith("#!/bin/bash"))            # the installer asks for a shebang
        self.assertLess(len(self.text), 10000)                          # and takes 10,000 characters at most

    def test_the_file_holds_a_placeholder_never_a_token(self):
        self.assertEqual(re.findall(r'^TOKEN="([^"]*)"', self.text, re.M), ["PASTE_THE_TOKEN_HERE"])

    def test_the_token_is_readable_by_root_only_and_deleted_after_use(self):
        self.assertLess(self.text.index("umask 077"), self.text.index("> /root/.lantern-token"))
        self.assertLess(self.text.index("bash /root/setup.sh"), self.text.index("rm -f /root/.lantern-token"))

    def test_it_runs_once(self):
        self.assertIn("ConditionPathExists=!/root/lantern-setup.done", self.text)
        self.assertIn("touch /root/lantern-setup.done", self.text)


if __name__ == "__main__":
    unittest.main()
