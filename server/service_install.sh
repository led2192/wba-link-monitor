#!/usr/bin/env bash
# The service in front of the server's database of passages (server/meaning_service.py, docs/server.md "The service").
#
#   bash server/service_install.sh check            what is there (reads only; the default)
#   bash server/service_install.sh install          the service, its user, its read-only database role, the web server;
#                                                   listens on the machine itself only; safe to run again
#   bash server/service_install.sh open HOST        opens ports 80 and 443 and gives the web server the name HOST, which
#                                                   then fetches and renews the certificate on its own
#   bash server/service_install.sh site NAME        the search page under NAME (19 Nov 2026): the web server, already open,
#                                                   also serves the files of /srv/lantern/www (the workflow Search page
#                                                   copies them there); "site off" takes the page away again
#   bash server/service_install.sh close            the way back: closes 80 and 443 and stops the web server
#   bash server/service_install.sh stop             stops the service too (deletes nothing)
#   bash server/service_install.sh pause | resume   rehearsals: the service alone stopped (the web server answers 502), and started
#   bash server/service_install.sh restart-db       rehearsals: the database restarted, so its memory is cold
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
  echo "searches answered in the last hour: $(sudo journalctl -u "$UNIT" --since '-60 min' --no-pager 2>/dev/null | grep -c ' near [0-9]' || true); failed: $(sudo journalctl -u "$UNIT" --since '-60 min' --no-pager 2>/dev/null | grep -c ' near failed' || true)"
  echo "web server: $(systemctl is-active caddy 2>/dev/null || true); page served under: $(sudo cat "$ETC/site_host" 2>/dev/null || echo 'no name'), files: $(sudo find /srv/lantern/www -maxdepth 1 -type f 2>/dev/null | wc -l)"
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
  # the database library from the system's own packages, so its security updates arrive with the system's (no pip as root)
  sudo apt-get install -y -q python3-psycopg >/dev/null
  # the read-only role. Its password is made once and kept in the env file alone: running install again on a live
  # service keeps it, so no request fails in between
  sudo -u postgres psql -d lantern -Atc "select 1 from pg_roles where rolname = 'meaning_read'" | grep -q 1 || sudo -u postgres createuser meaning_read
  if ! sudo test -s "$ETC/meaning.env"; then
    PW=$(openssl rand -hex 24)
    printf "alter role meaning_read login password '%s';\n" "$PW" | sudo -u postgres psql -d lantern -q
    printf 'MEANING_DB_URL=postgresql://meaning_read:%s@127.0.0.1:5432/lantern\nMEANING_SECRETS=%s/meaning.secrets\nMEANING_PORT=%s\n' "$PW" "$ETC" "$PORT" | sudo install -m 640 -o root -g "$USER_NAME" /dev/stdin "$ETC/meaning.env"
  fi
  sudo -u postgres psql -d lantern -q <<'SQL'
revoke all on schema meaning from meaning_read;
grant usage on schema meaning to meaning_read;
revoke all on all tables in schema meaning from meaning_read;
grant select on meaning.chunks, meaning.chunk_filters to meaning_read;
alter role meaning_read set default_transaction_read_only = on;
alter role meaning_read connection limit 6;
SQL
  sudo test -s "$ETC/meaning.secrets" || openssl rand -hex 32 | sudo install -m 640 -o root -g "$USER_NAME" /dev/stdin "$ETC/meaning.secrets"
  sudo chown root:"$USER_NAME" "$ETC/meaning.env" "$ETC/meaning.secrets"
  sudo chmod 640 "$ETC/meaning.env" "$ETC/meaning.secrets"
  sudo tee "/etc/systemd/system/$UNIT" >/dev/null <<UNITEOF
[Unit]
Description=lanterN: nearest passages by meaning (one route, read only)
After=network.target postgresql@17-main.service

[Service]
User=$USER_NAME
EnvironmentFile=$ETC/meaning.env
ExecStart=/usr/bin/python3 $DIR/meaning_service.py
Restart=on-failure
RestartSec=2
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
ProtectProc=invisible
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
IPAddressDeny=any
IPAddressAllow=localhost
SystemCallFilter=@system-service
TasksMax=200
MemoryMax=1G
CPUQuota=200%

[Install]
WantedBy=multi-user.target
UNITEOF
  # the web server that will hold the certificate: installed, and stopped until "open" gives it a name. Once open
  # (the Caddyfile is the one written here), running install again leaves it running.
  command -v caddy >/dev/null || sudo apt-get install -y -q caddy >/dev/null
  sudo grep -q "written by server/service_install.sh" /etc/caddy/Caddyfile 2>/dev/null || sudo systemctl disable --now caddy >/dev/null 2>&1 || true
  sudo systemctl daemon-reload
  sudo systemctl enable "$UNIT" >/dev/null 2>&1
  sudo systemctl restart "$UNIT"
  sleep 2
  echo "installed."
}

open_door() {
  echo "$HOST" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' || { echo "a host name like api.example.org"; exit 2; }
  # the name must already point here, or the certificate is refused (and refusals are counted against the name)
  mine=$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
  theirs=$(getent ahostsv4 "$HOST" | awk 'NR==1{print $1}')
  [ -n "$mine" ] && [ "$mine" = "$theirs" ] || { echo "the name does not point to this machine yet (it resolves to '${theirs:-nothing}'). Nothing opened."; exit 1; }
  if getent ahostsv6 "$HOST" >/dev/null 2>&1 && [ -n "$(getent ahostsv6 "$HOST" | awk 'NR==1{print $1}' | grep -v '^::ffff:')" ]; then echo "the name also has an IPv6 address; remove it or point it here first. Nothing opened."; exit 1; fi
  sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDYEOF
# written by server/service_install.sh; change it there
# (admin off: there is no admin door to reload through; this script restarts the web server instead)
{
	admin off
	servers {
		timeouts {
			read_header 5s
			read_body 5s
			idle 30s
		}
	}
}
$HOST {
	request_body {
		max_size 64KB
	}
	header -Server
	# the two routes the world reaches, both behind the shared secret: the search by meaning and, since 22 Nov 2026,
	# the documentation for the owner's panel. The health route answers on the machine itself only.
	@near {
		method POST
		path /near /docs
	}
	handle @near {
		reverse_proxy 127.0.0.1:$PORT
	}
	handle {
		respond 404
	}
}
CADDYEOF
  site_block                                                 # the page's block, when the page is served from here
  sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || { echo "the web server's configuration is not valid for this version ($(caddy version | cut -d' ' -f1)). Nothing opened."; exit 1; }
  sudo ufw allow 80/tcp >/dev/null
  sudo ufw allow 443/tcp >/dev/null
  sudo systemctl enable caddy >/dev/null 2>&1
  sudo systemctl restart caddy
  echo "opened: ports 80 and 443; the web server asks for the certificate on its own (a minute or two)."
}

# The search page under the owner's own name (19 Nov 2026). The files are the ones the workflow Search page builds
# (the page holds no data and no secret: the project address and the publishable key, as on GitHub Pages). The name
# is kept in $ETC/site_host so that "open" writes the page's block again. "/" is the search page.
WWW=/srv/lantern/www
points_here() {
  mine=$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
  theirs=$(getent ahostsv4 "$1" | awk 'NR==1{print $1}')
  [ -n "$mine" ] && [ "$mine" = "$theirs" ] || return 1
  [ -z "$(getent ahostsv6 "$1" 2>/dev/null | awk 'NR==1{print $1}' | grep -v '^::ffff:' || true)" ]
}
site_block() {
  sudo test -s "$ETC/site_host" || return 0
  SITE=$(sudo head -1 "$ETC/site_host")
  sudo sed -i '/^# site begin$/,/^# site end$/d' /etc/caddy/Caddyfile
  {
    printf '# site begin\n%s {\n\theader -Server\n\theader X-Content-Type-Options nosniff\n\theader Cache-Control no-cache\n\troot * %s\n\tencode gzip\n' "$SITE" "$WWW"
    printf '\t@home path /\n\trewrite @home /search.html\n\t@get method GET HEAD\n\thandle @get {\n\t\tfile_server\n\t}\n\thandle {\n\t\trespond 405\n\t}\n}\n'
    if points_here "www.$SITE"; then printf 'www.%s {\n\theader -Server\n\tredir https://%s{uri} permanent\n}\n' "$SITE" "$SITE"; fi
    printf '# site end\n'
  } | sudo tee -a /etc/caddy/Caddyfile >/dev/null
}
site() {
  sudo grep -q "written by server/service_install.sh" /etc/caddy/Caddyfile 2>/dev/null || { echo "the web server is not open yet: 'open HOST' first. Nothing changed."; exit 1; }
  sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-site
  if [ "$HOST" = "off" ]; then
    sudo rm -f "$ETC/site_host"; sudo sed -i '/^# site begin$/,/^# site end$/d' /etc/caddy/Caddyfile
    sudo systemctl restart caddy; echo "the page is no longer served from here."; return 0
  fi
  echo "$HOST" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' || { echo "a host name like example.org"; exit 2; }
  points_here "$HOST" || { echo "the name does not point to this machine yet, or it also has an IPv6 address (it resolves to '$(getent ahosts "$HOST" | awk '{print $1}' | sort -u | tr '\n' ' ')'). Nothing changed."; exit 1; }
  sudo test -s "$WWW/search.html" || { echo "no page in $WWW yet: run the workflow Search page first. Nothing changed."; exit 1; }
  echo "$HOST" | sudo install -m 644 /dev/stdin "$ETC/site_host"
  site_block
  if ! sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    sudo cp /etc/caddy/Caddyfile.before-site /etc/caddy/Caddyfile; sudo rm -f "$ETC/site_host"
    echo "the web server's configuration with the page is not valid for this version. Put back as it was. Nothing changed."; exit 1
  fi
  sudo systemctl restart caddy
  echo "the page is served under $HOST; the certificate is asked for on its own (a minute or two)."
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
  open)    open_door; for _ in $(seq 1 18); do sleep 10; code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST -d '{}' "https://$HOST/near" || true); [ "$code" = "401" ] && break; done
           check; echo "from outside, without the secret: HTTP ${code:-no answer} (401 expected); the health route from outside: HTTP $(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$HOST/health" || true) (404 expected)" ;;
  site)    site
           if [ "$HOST" != "off" ]; then for _ in $(seq 1 18); do sleep 10; code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$HOST/" || true); [ "$code" = "200" ] && break; done
             echo "the page from outside: HTTP ${code:-no answer} (200 expected); the search by meaning's door: HTTP $(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST -d '{}' "https://$(sudo grep -m1 -E '^[a-z0-9.-]+ \{$' /etc/caddy/Caddyfile | cut -d' ' -f1)/near" || true) (401 expected)"; fi
           check ;;
  close)   close_door; check ;;
  stop)    close_door; sudo systemctl disable --now "$UNIT"; echo "stopped; nothing deleted." ;;
  pause)   sudo systemctl stop "$UNIT"; echo "paused: the service is stopped, the web server still answers."; check || true ;;
  resume)  sudo systemctl start "$UNIT"; sleep 2; echo "resumed."; check ;;
  restart-db) sudo systemctl restart postgresql@17-main.service; echo "the database was restarted at $(date -u +%H:%M:%S) UTC."; ;;
  *)       echo "check, install, open HOST, site NAME, close, stop, pause, resume or restart-db"; exit 2 ;;
esac
