#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: render-guizang-container.sh PACKAGE_JSON OUTPUT_DIR" >&2
  exit 64
fi

package_path=$1
output_dir=$2
input_dir=$(dirname "$package_path")

mkdir -p "$output_dir"

exec docker run --rm \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 128 \
  --memory 768m \
  --cpus 1.0 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=128m \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --mount "type=bind,src=$input_dir,dst=/input,readonly" \
  --mount "type=bind,src=$output_dir,dst=/output" \
  --entrypoint node \
  act-guizang-brief-renderer:1.0.0 \
  /app/render_guizang_brief.mjs \
  /input/$(basename "$package_path") \
  /app/template-swiss-card.html \
  /output
