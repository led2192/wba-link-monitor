#!/bin/bash
# server/first_boot.sh: the "custom script" pasted into the provider's image installer, so that a fresh Ubuntu
# sets itself up with nobody at the keyboard. The owner replaces PASTE_THE_TOKEN_HERE with the registration token
# (GitHub: Settings > Actions > Runners > New self-hosted runner, the value after --token; it works for one hour)
# in the installer's form, never in this file.
#
# It only plants a one-time service: the real work (server/setup.sh) runs when the installed system is up and has
# network, whether the installer runs this script before the first boot or after it. The token is deleted from
# the server as soon as setup.sh has finished; what happened is in /root/lantern-setup.log.
TOKEN="PASTE_THE_TOKEN_HERE"

umask 077
printf '%s' "$TOKEN" > /root/.lantern-token
cat > /root/lantern-setup.sh <<'RUN'
#!/bin/bash
exec >> /root/lantern-setup.log 2>&1
echo "=== $(date -u +%FT%TZ) first-boot setup"
for i in $(seq 1 30); do getent hosts raw.githubusercontent.com >/dev/null && break; sleep 5; done
export DEBIAN_FRONTEND=noninteractive
command -v curl >/dev/null || { apt-get update -q; apt-get -y -q install curl ca-certificates; }
curl -fsSL https://raw.githubusercontent.com/led2192/wba-link-monitor/main/server/setup.sh -o /root/setup.sh || exit 1
RUNNER_TOKEN="$(cat /root/.lantern-token)" bash /root/setup.sh
code=$?
rm -f /root/.lantern-token
[ "$code" = "0" ] && touch /root/lantern-setup.done && systemctl disable lantern-setup.service
echo "=== $(date -u +%FT%TZ) finished with code $code"
RUN
chmod 700 /root/lantern-setup.sh
cat > /etc/systemd/system/lantern-setup.service <<'UNIT'
[Unit]
Description=lanterN first-boot setup (once)
After=network-online.target
Wants=network-online.target
ConditionPathExists=!/root/lantern-setup.done

[Service]
Type=oneshot
ExecStart=/root/lantern-setup.sh
TimeoutStartSec=3600

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable lantern-setup.service
systemctl start --no-block lantern-setup.service 2>/dev/null || true
exit 0
