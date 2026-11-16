#!/usr/bin/env bash
# The service in front of the server's database of passages (server/meaning_service.py, docs/server.md "The service").
#
#   bash server/service_install.sh check            what is there (reads only; the default)
#   bash server/service_install.sh install          the service, its user, its read-only database role, the web server;
#                                                   listens on the machine itself only; safe to run again
#   bash server/service_install.sh open HOST        opens ports 80 and 443 and gives the web server the name HOST, which
#                                                   then fetches and renews the certificate on its own
#   bash server/service_install.sh close            the way back: closes 80 and 443 and stops the web server
#   bash server/service_install.sh stop             stops the service too (deletes nothing)
#
# What is kept on the server, readable by root and the service's user only, never printed and never in the repository:
#   /etc/lantern/meaning.env       the database address of the read-only role, with a password made here
#   /etc/lantern/meaning.secrets   the secret Supabase must send (one a line, two at most)
set -euo pipefail

MODE="${1:-check}"
HOST="${2:-}"
USER_NAME=lantern-api
DIR=/opt/lantern
ETC=/etc/lantern
UNIT=lantern-meaning.service
PORT=8377
HERE="$(cd "$(dirname "$0")" && pwd)"

check() {
  echo "service: $(systemctl is-active "$UNIT" 2>/dev/null || true), starts with the machine: $(systemctl is-enabled "$UNIT" 2>/dev/null || true)"
  echo "service listens on: $(sudo ss -ltnH 2>/dev/null | awk -v p=":$PORT" '$4 ~ p"$"{print $4}' | sort -u | tr '\n' ' ')"
  echo "health, asked on the machine: $(curl -s -m 3 "http://127.0.0.1:$PORT/health" || echo 'no answer')"
  echo "without the secret: HTTP $(curl -s -m 3 -o /dev/null -w '%{http_code}' -X POST -d '{}' "http://127.0.0.1:$PORT/near" || echo '-')"
  echo "web server: $(systemctl is-active caddy 2>/dev/null || true)"
  echo "firewall lets in: $(sudo ufw status | awk '/ALLOW/{print $1}' | sort -u | tr '\n' ' ')"
  echo "secrets kept: $(sudo sh -c "[ -s $ETC/meaning.secrets ] && grep -c . $ETC/meaning.secrets || echo 0"); files readable by others: $(sudo find $ETC -type f -perm /o+r 2>/dev/null | wc -l)"
  echo "database role can write: $(sudo -u postgres psql -d lantern -Atc "select coalesce(bool_or(has_table_privilege('meaning_read', c.oid, 'insert,update,delete,truncate')), false) from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'meaning' and c.relkind = 'r'" 2>/dev/null || echo unknown)"
  bad=0
  sudo ss -ltnH | awk -v p=":$PORT" '$4 ~ p"$"{print $4}' | grep -Ev "^127\.0\.0\.1:$PORT$" && { echo "PROBLEM: the service listens beyond the machine itself"; bad=1; }
  sudo ufw status | grep -q 5432 && { echo "PROBLEM: the firewall opens the database's port"; bad=1; }
  return $bad
}

install() {
  export DEBIAN_FRONTEND=noninteractive
  id "$USER_NAME" >/dev/null 2>&1 || sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$USER_NAME"
  sudo install -d -m 755 "$DIR"
  sudo install -d -m 750 -o root -g "$USER_NAME" "$ETC"
  sudo install -m 644 "$HERE/meaning_service.py" "$DIR/meaning_service.py"
  [ -x "$DIR/venv/bin/python" ] || { sudo apt-get install -y -q python3-venv >/dev/null; sudo python3 -m venv "$DIR/venv"; }
  sudo "$DIR/venv/bin/pip" install -q "psycopg[binary]"
  # the read-only role: a new password each install, known to the env file alone
  PW=$(openssl rand -hex 24)
  sudo -u postgres psql -d lantern -Atc "select 1 from pg_roles where rolname = 'meaning_read'" | grep -q 1 || sudo -u postgres createuser meaning_read
  printf "alter role meaning_read login password '%s';\n" "$PW" | sudo -u postgres psql -d lantern -q
  sudo -u postgres psql -d lantern -q <<'SQL'
revoke all on schema meaning from meaning_read;
grant usage on schema meaning to meaning_read;
revoke all on all tables in schema meaning from meaning_read;
grant select on meaning.chunks, meaning.chunk_filters to meaning_read;
alter role meaning_read set default_transaction_read_only = on;
SQL
  printf 'MEANING_DB_URL=postgresql://meaning_read:%s@127.0.0.1:5432/lantern\nMEANING_SECRETS=%s/meaning.secrets\nMEANING_PORT=%s\n' "$PW" "$ETC" "$PORT" | sudo tee "$ETC/meaning.env" >/dev/null
  sudo sh -c "[ -s $ETC/meaning.secrets ] || openssl rand -hex 32 > $ETC/meaning.secrets"
  sudo chown root:"$USER_NAME" "$ETC/meaning.env" "$ETC/meaning.secrets"
  sudo chmod 640 "$ETC/meaning.env" "$ETC/meaning.secrets"
  sudo tee "/etc/systemd/system/$UNIT" >/dev/null <<UNITEOF
[Unit]
Description=lanterN: nearest passages by meaning (one route, read only)
After=network.target postgresql@17-main.service

[Service]
User=$USER_NAME
EnvironmentFile=$ETC/meaning.env
ExecStart=$DIR/venv/bin/python $DIR/meaning_service.py
Restart=on-failure
RestartSec=2
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
MemoryMax=1G
CPUQuota=200%

[Install]
WantedBy=multi-user.target
UNITEOF
  # the web server that will hold the certificate: installed and stopped until "open" gives it a name
  command -v caddy >/dev/null || sudo apt-get install -y -q caddy >/dev/null
  sudo systemctl disable --now caddy >/dev/null 2>&1 || true
  sudo systemctl daemon-reload
  sudo systemctl enable "$UNIT" >/dev/null 2>&1
  sudo systemctl restart "$UNIT"
  sleep 2
  echo "installed."
}

open_door() {
  echo "$HOST" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' || { echo "a host name like api.example.org"; exit 2; }
  sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDYEOF
# written by server/service_install.sh; change it there
{
	admin off
}
$HOST {
	request_body {
		max_size 64KB
	}
	header -Server
	@routes path /health /near
	handle @routes {
		reverse_proxy 127.0.0.1:$PORT
	}
	handle {
		respond 404
	}
}
CADDYEOF
  sudo ufw allow 80/tcp >/dev/null
  sudo ufw allow 443/tcp >/dev/null
  sudo systemctl enable caddy >/dev/null 2>&1
  sudo systemctl restart caddy
  echo "opened: ports 80 and 443; the web server asks for the certificate on its own (a minute or two)."
}

close_door() {
  sudo systemctl disable --now caddy >/dev/null 2>&1 || true
  sudo ufw delete allow 80/tcp >/dev/null 2>&1 || true
  sudo ufw delete allow 443/tcp >/dev/null 2>&1 || true
  echo "closed: the web server is stopped and only SSH comes in."
}

case "$MODE" in
  check)   check ;;
  install) install; check ;;
  open)    open_door; sleep 20; check; echo "from outside: HTTP $(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$HOST/health" || echo 'no answer yet')" ;;
  close)   close_door; check ;;
  stop)    close_door; sudo systemctl disable --now "$UNIT"; echo "stopped; nothing deleted." ;;
  *)       echo "check, install, open HOST, close or stop"; exit 2 ;;
esac
