#!/usr/bin/with-contenv bashio
set -euo pipefail

cd /opt/camfusion

export PANORAMA_OPTIONS_FILE="/data/options.json"
export PANORAMA_BIND_HOST="0.0.0.0"
export PANORAMA_PORT="8099"

exec python3 -m app.main
