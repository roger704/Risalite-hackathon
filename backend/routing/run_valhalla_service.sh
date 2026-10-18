#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <graph_dir> [port=8002]" >&2
  exit 2
fi

graph_dir="$(realpath "$1")"
port="${2:-8002}"
config="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/routing/valhalla_berlin_walking.json"
container_image="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["container_image"])' "$config")"

if [[ ! -f "$graph_dir/valhalla.json" ]]; then
  echo "Graph directory must contain valhalla.json" >&2
  exit 1
fi
if [[ ! "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
  echo "Port must be between 1 and 65535" >&2
  exit 2
fi

exec docker run --rm --name valhalla-berlin \
  -p "127.0.0.1:$port:8002" \
  --mount "type=bind,source=$graph_dir,target=/data,readonly" \
  "$container_image" valhalla_service /data/valhalla.json 1
