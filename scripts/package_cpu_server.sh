#!/usr/bin/env bash
# Build a transferable CPU-only inference image; never read local model weights.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
package_dir="$project_dir/dist/catalogflow-large-cpu"
mkdir -p "$package_dir"
docker compose -f "$project_dir/deploy/large-cpu/compose.yml" build
cp "$project_dir/deploy/large-cpu/compose.yml" \
   "$project_dir/deploy/large-cpu/Dockerfile" \
   "$project_dir/deploy/large-cpu/.dockerignore" \
   "$project_dir/deploy/large-cpu/.env.example" \
   "$project_dir/deploy/large-cpu/README.md" "$package_dir/"
docker image save catalogflow-large-cpu:0.32.5 | gzip -1 > "$package_dir/catalogflow-large-cpu-image.tar.gz.tmp"
mv "$package_dir/catalogflow-large-cpu-image.tar.gz.tmp" "$package_dir/catalogflow-large-cpu-image.tar.gz"
(
    cd "$package_dir"
    sha256sum compose.yml Dockerfile .dockerignore .env.example README.md \
        catalogflow-large-cpu-image.tar.gz > SHA256SUMS
)
printf 'CPU server package: %s\n' "$package_dir"
