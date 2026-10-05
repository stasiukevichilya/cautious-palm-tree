#!/bin/bash
# Prepares the persistent /data layout, then execs the PI WEB process given as CMD.
set -euo pipefail

mkdir -p "$HOME" "$(dirname "$PI_WEB_CONFIG")" "$PI_WEB_DATA_DIR" "$PI_CODING_AGENT_DIR"

# The repo's models.json is the source of truth for local endpoints; refresh it on every start.
if [[ -f /etc/pi-web/models.json ]]; then
  cp /etc/pi-web/models.json "$PI_CODING_AGENT_DIR/models.json"
fi

# PI WEB config is editable from Settings, so only seed it once.
if [[ ! -f $PI_WEB_CONFIG ]]; then
  jq -n --arg projects "${PIWEB_PROJECTS:-}" '{
    host: "0.0.0.0",
    port: 8504,
    pathAccess: {allowedPaths: (["/workspace"] + (if $projects == "" then [] else [$projects] end))}
  }' > "$PI_WEB_CONFIG"
fi

exec "$@"
