#!/bin/sh
set -e

CONFIG="${MS_CONFIG:-/config/config.yaml}"

# On first run there is no config yet — seed one from the bundled example so the
# container boots and the dashboard/Settings page is reachable to finish setup.
if [ ! -f "$CONFIG" ]; then
    echo "[mediaspektor] No config at $CONFIG — seeding from config.yaml.example"
    mkdir -p "$(dirname "$CONFIG")"
    cp /app/config.yaml.example "$CONFIG"
fi

exec python3 mediaspektor.py --host 0.0.0.0 --port 5000 --config "$CONFIG"
