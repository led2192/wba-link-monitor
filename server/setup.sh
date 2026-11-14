#!/usr/bin/env bash
# server/setup.sh: prepares a fresh Ubuntu 24.04 server as lanterN's own job machine and connects it to
# this repository as self-hosted GitHub Actions runners. Run once, as root, on the server:
#
#   curl -fsSL https://raw.githubusercontent.com/led2192/wba-link-monitor/main/server/setup.sh -o setup.sh && bash setup.sh
#
# What it does, in order (safe to run again: every step checks what is already there):
#   1. system updates, automatic security updates, firewall (only SSH comes in), fail2ban against password guessing
#   2. a user "runner" that the jobs run as
#   3. RUNNERS copies of the GitHub Actions runner (default 6), each a service that starts with the machine,
#      registered with the label "lantern"
# It asks once for the registration token (GitHub: Settings > Actions > Runners > New self-hosted runner; the
# value after --token; it expires after one hour and is not stored). No secret lives in this file or on the
# server: the jobs get their secrets from GitHub at run time, as they do on GitHub's own machines.
#
#   RUNNERS=8 bash setup.sh        another number of runners
#   bash setup.sh --no-runners     steps 1 and 2 only
set -euo pipefail

REPO_URL="https://github.com/led2192/wba-link-monitor"
RUNNERS="${RUNNERS:-6}"
LABEL="lantern"
WITH_RUNNERS=1
[ "${1:-}" = "--no-runners" ] && WITH_RUNNERS=0

say() { printf '\n>>> %s\n' "$*"; }
die() { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run this as root"
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || die "this script is written for Ubuntu (found: ${PRETTY_NAME:-unknown})"
case "$RUNNERS" in ''|*[!0-9]*) die "RUNNERS must be a number";; esac

say "1/3 system: updates, automatic security updates, firewall, fail2ban"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get -y -q -o Dpkg::Options::=--force-confold upgrade
apt-get -y -q install curl tar git jq ufw fail2ban unattended-upgrades ca-certificates build-essential
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
ufw allow 22/tcp >/dev/null
ufw --force enable >/dev/null
systemctl enable --now fail2ban >/dev/null 2>&1 || true
ufw status | sed -n '1,8p'

say "2/3 the user the jobs run as"
if ! id runner >/dev/null 2>&1; then useradd -m -s /bin/bash runner; fi
# The jobs install system packages (browsers for the page reader), as they do on GitHub's machines.
echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner
chmod 440 /etc/sudoers.d/runner
id runner

if [ "$WITH_RUNNERS" = "0" ]; then say "done (no runners asked for)"; exit 0; fi

say "3/3 GitHub Actions runners: $RUNNERS, label \"$LABEL\""
need=0
for i in $(seq 1 "$RUNNERS"); do [ -f "/home/runner/actions-runner-$i/.runner" ] || need=1; done
TOKEN=""
if [ "$need" = "1" ]; then
  printf 'Registration token (GitHub: Settings > Actions > Runners > New self-hosted runner, the value after --token): '
  read -r TOKEN < /dev/tty
  [ -n "$TOKEN" ] || die "no token given"
  V="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')"
  case "$V" in ''|null) die "could not read the runner version from GitHub";; esac
  TGZ="/tmp/actions-runner-linux-x64-$V.tar.gz"
  [ -f "$TGZ" ] || curl -fsSL -o "$TGZ" "https://github.com/actions/runner/releases/download/v$V/actions-runner-linux-x64-$V.tar.gz"
fi
for i in $(seq 1 "$RUNNERS"); do
  D="/home/runner/actions-runner-$i"
  if [ -f "$D/.runner" ]; then echo "runner $i: already registered"; continue; fi
  mkdir -p "$D"; tar -xzf "$TGZ" -C "$D"; chown -R runner:runner "$D"
  [ "$i" = "1" ] && "$D/bin/installdependencies.sh" >/dev/null
  sudo -u runner bash -c "cd '$D' && ./config.sh --unattended --url '$REPO_URL' --token '$TOKEN' --name 'lantern-$i' --labels '$LABEL' --work _work --replace" | tail -n 3
  ( cd "$D" && ./svc.sh install runner >/dev/null && ./svc.sh start >/dev/null )
  echo "runner $i: registered and started"
done
TOKEN=""

say "done"
systemctl list-units 'actions.runner.*' --no-legend | awk '{print $1, $3, $4}'
echo "cores: $(nproc)   memory: $(free -g | awk '/^Mem/{print $2}') GB   disk free: $(df -h / | awk 'NR==2{print $4}')"
