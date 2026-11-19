#!/bin/sh
# Install an outbound connector under the same unprivileged user as BiomeRTE.
set -eu
if [ "$#" -ne 1 ]; then echo "Usage: $0 https://gateway.example" >&2; exit 2; fi
origin=$1
root=$(CDPATH= cd -P "$(dirname "$0")/.." && pwd)
owner=$(id -un)
case "$root:$HOME:$origin" in *[!a-zA-Z0-9_./:%~-]*) echo 'Use plain absolute paths and an HTTPS origin.' >&2; exit 2;; esac
case "$origin" in https://*) ;; *) exit 2;; esac
umask 077
mkdir -p "$HOME/.local/share/biomerte-gateway"
key="$HOME/.local/share/biomerte-gateway/bridge.key"
if [ ! -e "$key" ]; then
  (set -C; openssl rand -hex 32 | tr -d '\n' > "$key")
fi
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT
cat > "$temp/biomerte.service" <<UNIT
[Unit]
Description=BiomeRTE controller
After=network.target
[Service]
User=$owner
Environment=HOME=$HOME
WorkingDirectory=$root
ExecStart=$root/biome serve
Restart=on-failure
RestartSec=3
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
cat > "$temp/biomerte-gateway.service" <<UNIT
[Unit]
Description=BiomeRTE outbound MCP connector
After=network.target biomerte.service
Wants=biomerte.service
[Service]
User=$owner
Environment=HOME=$HOME
WorkingDirectory=$root
ExecStart=/usr/bin/python3 $root/deploy/gateway-agent.py --origin $origin
Restart=on-failure
RestartSec=4
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
sudo -n install -m 0644 "$temp/biomerte.service" /etc/systemd/system/biomerte.service
sudo -n install -m 0644 "$temp/biomerte-gateway.service" /etc/systemd/system/biomerte-gateway.service
sudo -n systemctl daemon-reload
sudo -n systemctl enable biomerte.service biomerte-gateway.service
sudo -n systemctl restart biomerte.service
sudo -n systemctl restart biomerte-gateway.service
