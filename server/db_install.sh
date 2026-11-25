#!/usr/bin/env bash
# lanterN's own database on the job machine: PostgreSQL 17 with pgvector 0.8, for the search by meaning.
# Step 2 of the plan to move the passages and their index off Supabase (docs/server.md, "The database").
#
#   bash server/db_install.sh check      what is there (reads only; the default)
#   bash server/db_install.sh install    install and configure; safe to run again
#   bash server/db_install.sh stop       the way back: stop the database and keep it from starting (deletes nothing)
#
# What install does, and no more:
#   - adds the PostgreSQL project's own package source with the script Ubuntu ships for it (postgresql-common), because
#     Ubuntu 24.04 carries PostgreSQL 16 and pgvector 0.6, which has no half-precision vectors; Supabase runs 17 and 0.8
#   - installs postgresql-17 and postgresql-17-pgvector
#   - the database listens on the machine itself only (localhost); the firewall is not touched, so its port stays shut
#   - every connection over the network interface needs a password (scram-sha-256); the only way in without one is the
#     system user postgres on the machine's own socket, for administration
#   - a ceiling on memory and processors, so the watch, the copies and the browsers of the other jobs are not starved
#   - an empty database "lantern" with the vector extension; no tables, no roles, no data
#   - security updates of these packages arrive with the system's automatic updates
# The runners on this machine can act as root, so the passwords guard against a mistake, not against a job that means
# harm: the guard for that is who may run a job here (docs/server.md).
set -euo pipefail

MODE="${1:-check}"
PG=17
NEED_FREE_GB=120          # the passages and their index need about 100 GB; below this, install refuses
MEMORY_MAX=40G            # ceiling for the database's processes together (28G until 25 Nov 2026: the index of every main report, about 21 GB, must fit; the machine has 64 GB)
CPU_QUOTA=600%            # six of the sixteen cores
CONF=/etc/postgresql/$PG/main
UNIT=postgresql@$PG-main.service

free_gb() { df --output=avail -BG / | tail -1 | tr -dc '0-9'; }

check() {
  echo "disk free: $(free_gb) GB   memory available: $(free -g | awk '/^Mem/{print $7}') GB   load: $(cut -d' ' -f1-3 /proc/loadavg)"
  if ! command -v psql >/dev/null || [ ! -d "$CONF" ]; then echo "database: not installed"; return 0; fi
  echo "server: $(sudo -u postgres psql -Atc 'show server_version' 2>/dev/null || echo 'not answering')"
  echo "service: $(systemctl is-active "$UNIT" || true), starts with the machine: $(systemctl is-enabled "$UNIT" 2>/dev/null || true)"
  echo "listens on: $(sudo ss -ltnH 2>/dev/null | awk '$4 ~ /:5432$/{print $4}' | sort -u | tr '\n' ' ')"
  echo "firewall lets in: $(sudo ufw status | awk '/ALLOW/{print $1}' | sort -u | tr '\n' ' ')"
  echo "ceilings: $(systemctl show "$UNIT" -p MemoryMax -p CPUQuotaPerSecUSec | tr '\n' ' ')"
  echo "pgvector: $(sudo -u postgres psql -d lantern -Atc "select extversion from pg_extension where extname = 'vector'" 2>/dev/null || echo none)"
  echo "half-precision vectors: $(sudo -u postgres psql -d lantern -Atc "select count(*) from pg_type where typname = 'halfvec'" 2>/dev/null || echo 0)"
  echo "databases: $(sudo -u postgres psql -Atc "select string_agg(datname || ' ' || pg_size_pretty(pg_database_size(datname)), ', ') from pg_database where not datistemplate" 2>/dev/null)"
  echo "roles that can log in: $(sudo -u postgres psql -Atc "select count(*) from pg_roles where rolcanlogin" 2>/dev/null)"
  echo "ways in without a password: $(sudo grep -Ev '^\s*(#|$)' "$CONF/pg_hba.conf" | grep -Ec 'trust|peer') (one expected: the system user postgres on the socket)"
  bad=0
  sudo ss -ltnH | awk '$4 ~ /:5432$/{print $4}' | grep -Ev '^(127\.0\.0\.1|\[::1\]):5432$' && { echo "PROBLEM: the database listens beyond the machine itself"; bad=1; }
  sudo ufw status | grep -q 5432 && { echo "PROBLEM: the firewall opens the database's port"; bad=1; }
  sudo grep -Ev '^\s*(#|$)' "$CONF/pg_hba.conf" | grep -q trust && { echo "PROBLEM: a way in with no check at all (trust)"; bad=1; }
  return $bad
}

install() {
  free=$(free_gb)
  if [ "$free" -lt "$NEED_FREE_GB" ]; then echo "only $free GB free; $NEED_FREE_GB needed. Nothing installed."; exit 1; fi
  export DEBIAN_FRONTEND=noninteractive
  sudo apt-get update -q >/dev/null
  sudo apt-get install -y -q postgresql-common ca-certificates >/dev/null
  # the package source of the PostgreSQL project, set up by the script Ubuntu's own postgresql-common ships
  [ -e /etc/apt/sources.list.d/pgdg.sources ] || [ -e /etc/apt/sources.list.d/pgdg.list ] || sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y >/dev/null
  sudo apt-get update -q >/dev/null
  sudo apt-get install -y -q "postgresql-$PG" "postgresql-$PG-pgvector" >/dev/null
  sudo install -d -o postgres -g postgres "$CONF/conf.d"
  sudo tee "$CONF/conf.d/lantern.conf" >/dev/null <<CONFEOF
# written by server/db_install.sh; change it there
listen_addresses = 'localhost'
port = 5432
password_encryption = 'scram-sha-256'
max_connections = 40
shared_buffers = '8GB'
effective_cache_size = '34GB'
work_mem = '32MB'
maintenance_work_mem = '2GB'
CONFEOF
  sudo tee "$CONF/pg_hba.conf" >/dev/null <<HBAEOF
# written by server/db_install.sh; change it there
# administration: the system user postgres, on the machine's own socket
local   all   postgres                 peer
# everything else: a password, and only from the machine itself
host    all   all       127.0.0.1/32   scram-sha-256
host    all   all       ::1/128        scram-sha-256
HBAEOF
  sudo chown postgres:postgres "$CONF/pg_hba.conf" "$CONF/conf.d/lantern.conf"
  sudo chmod 640 "$CONF/pg_hba.conf"
  sudo install -d "/etc/systemd/system/$UNIT.d"
  sudo tee "/etc/systemd/system/$UNIT.d/lantern.conf" >/dev/null <<UNITEOF
[Service]
MemoryMax=$MEMORY_MAX
CPUQuota=$CPU_QUOTA
UNITEOF
  # security updates of these packages with the system's automatic updates
  sudo tee /etc/apt/apt.conf.d/52lantern-pgdg >/dev/null <<'APTEOF'
Unattended-Upgrade::Origins-Pattern { "origin=apt.postgresql.org"; };
APTEOF
  sudo systemctl daemon-reload
  sudo systemctl enable "$UNIT" >/dev/null 2>&1 || true
  sudo systemctl restart "$UNIT"
  for _ in $(seq 1 30); do sudo -u postgres psql -Atc 'select 1' >/dev/null 2>&1 && break; sleep 1; done
  sudo -u postgres psql -Atc "select 1 from pg_database where datname = 'lantern'" | grep -q 1 || sudo -u postgres createdb lantern
  sudo -u postgres psql -d lantern -qc "create extension if not exists vector"
  echo "installed."
}

case "$MODE" in
  check)   check ;;
  install) install; check ;;
  stop)    sudo systemctl disable --now "$UNIT"; echo "stopped; nothing deleted. 'install' starts it again."; ;;
  *)       echo "check, install or stop"; exit 2 ;;
esac
