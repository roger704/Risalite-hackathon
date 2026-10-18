#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <work_dir> [local_pbf]" >&2
  exit 2
fi

config="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/routing/valhalla_berlin_walking.json"
work_dir="$(mkdir -p "$1" && cd "$1" && pwd)"
local_pbf="${2:-}"
read_config() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$config" "$1"
}

source_url="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["graph"]["source_url"])' "$config")"
source_sha256="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["graph"]["source_sha256"])' "$config")"
source_edition="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["graph"]["source_edition"])' "$config")"
container_image="$(read_config container_image)"
engine_version="$(read_config engine_version)"
sha12="${source_sha256:0:12}"
epoch="$(date +%s)"
build_timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
staging="$work_dir/staging-$epoch-$$"
source_file="$work_dir/.berlin-$source_edition.osm.pbf"
target="$work_dir/graphs/$source_edition-$sha12-$build_timestamp"
concurrency="${MJOLNIR_CONCURRENCY:-4}"

mkdir -p "$work_dir/graphs" "$staging"
if [[ -n "$local_pbf" ]]; then
  source_file="$(realpath "$local_pbf")"
else
  curl -fL "$source_url" -o "$source_file"
fi

actual_sha256="$(sha256sum "$source_file" | cut -d ' ' -f 1)"
if [[ "$actual_sha256" != "$source_sha256" ]]; then
  echo "Pinned PBF SHA-256 mismatch: expected $source_sha256, got $actual_sha256" >&2
  exit 1
fi
cp "$source_file" "$staging/berlin-$source_edition.osm.pbf"
if [[ "$source_file" == "$work_dir/.berlin-$source_edition.osm.pbf" ]]; then
  rm -f "$source_file"
fi

if [[ -e "$target" ]]; then
  echo "Graph output path already exists: $target" >&2
  exit 1
fi

docker run --rm --entrypoint /bin/bash \
  --mount "type=bind,source=$staging,target=/data" \
  "$container_image" -euo pipefail -c '
    pbf=/data/berlin-'"$source_edition"'.osm.pbf
    valhalla_build_config \
      --mjolnir-tile-dir /data/valhalla_tiles \
      --mjolnir-tile-extract /data/valhalla_tiles.tar \
      --mjolnir-admin /data/admins.sqlite \
      --mjolnir-concurrency '"$concurrency"' \
      --service-limits-isochrone-max-contours 4 > /data/valhalla.json
    SECONDS=0
    valhalla_build_admins -c /data/valhalla.json "$pbf"
    echo "admins_s=$SECONDS"
    SECONDS=0
    valhalla_build_tiles -c /data/valhalla.json "$pbf"
    echo "tiles_s=$SECONDS"
    SECONDS=0
    valhalla_build_extract -c /data/valhalla.json -v
    echo "extract_s=$SECONDS"
    echo "memory_peak_bytes=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo na)"
  '

tiles_sha256="$(sha256sum "$staging/valhalla_tiles.tar" | cut -d ' ' -f 1)"
tiles_size="$(stat -c %s "$staging/valhalla_tiles.tar")"
tileset_last_modified="$(stat -c %Y "$staging/valhalla_tiles.tar")"
config_sha256="$(sha256sum "$staging/valhalla.json" | cut -d ' ' -f 1)"
python3 - "$staging/graph_manifest.json" "$source_url" "$source_sha256" "$source_edition" \
  "$container_image" "$engine_version" "$config_sha256" "$tiles_sha256" "$tiles_size" \
  "$tileset_last_modified" <<'PY'
import json
import sys
from datetime import datetime, timezone

(
    target,
    source_url,
    source_sha256,
    source_edition,
    container_image,
    engine_version,
    config_sha256,
    tiles_sha256,
    tiles_size,
    tileset_last_modified,
) = sys.argv[1:]
manifest = {
    "source_url": source_url,
    "source_sha256": source_sha256,
    "source_edition": source_edition,
    "container_image": container_image,
    "engine_version": engine_version,
    "valhalla_json_sha256": config_sha256,
    "tiles_tar_sha256": tiles_sha256,
    "tiles_tar_size_bytes": int(tiles_size),
    "tileset_last_modified": int(tileset_last_modified),
    "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
with open(target, "w", encoding="utf-8") as stream:
    json.dump(manifest, stream, indent=2, sort_keys=True)
    stream.write("\n")
PY
rm -f "$staging/berlin-$source_edition.osm.pbf"
mv "$staging" "$target"
link_tmp="$work_dir/.current-$epoch-$$"
ln -s "$target" "$link_tmp"
mv -Tf "$link_tmp" "$work_dir/current"
echo "graph_dir=$target"
du -sh "$target"
