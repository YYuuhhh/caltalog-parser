#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_root"
compose=(docker compose)
run_id="$(date -u +%Y%m%dT%H%M%S.%NZ)-$$"
run_root="$project_root/logs/full-test-runs/$run_id"
parsed_run="/parsed/full-test-runs/$run_id"
mkdir -p -- "$run_root"
: > "$run_root/steps.tsv"
sampler_pid=''
cleanup() {
  if [[ -n "$sampler_pid" ]]; then
    kill "$sampler_pid" 2>/dev/null || true
    wait "$sampler_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_step() {
  local name="$1" category="$2" start="$SECONDS" result=0
  shift 2
  printf 'Full test: %s\n' "$name"
  "$@" > "$run_root/$name.stdout" 2> "$run_root/$name.stderr" || result=$?
  printf '%s\t%s\t%s\t%s\n' "$name" "$category" "$result" "$((SECONDS-start))" >> "$run_root/steps.tsv"
  if [[ "$result" -ne 0 ]]; then
    printf '  %s returned %s; evidence: %s\n' "$name" "$result" "$run_root/$name.stderr"
  fi
  return 0
}

tools=("${compose[@]}" run --rm --no-deps tools)
run_step compose operational "${compose[@]}" config --quiet
run_step baseline operational "${tools[@]}" python /scripts/full_test.py baseline --run-id "$run_id"
run_step health operational "${tools[@]}" python /scripts/full_test.py health
run_step runtime operational "${compose[@]}" exec -T mineru python -c 'import hashlib; from pathlib import Path; print(hashlib.sha256(Path("/app/app.py").read_bytes()).hexdigest())'
mapfile -t container_ids < <("${compose[@]}" ps -q)
if [[ ${#container_ids[@]} -gt 0 ]]; then
  (while true; do timeout -k 2s 10s docker stats --no-stream --format '{{json .}}' "${container_ids[@]}" >> "$run_root/resources.jsonl" 2>/dev/null || true; sleep 3; done) &
  sampler_pid=$!
fi
run_step assets operational "${tools[@]}" python /scripts/full_test.py assets
run_step regression operational "${tools[@]}" pytest -q -p no:cacheprovider /tests /mineru-src/tests
run_step inventory operational "${tools[@]}" python /scripts/catalog_registry.py verify --manifest /catalogs/manifests/guhring-production.yaml
run_step ingestion operational "${tools[@]}" python /scripts/ragflow_e2e.py /catalogs/test/guhring_catalog_test.pdf --dataset 'Catalog parser integration test'
if [[ -f logs/ragflow-e2e-latest.json ]]; then cp -- logs/ragflow-e2e-latest.json "$run_root/ingestion.json"; fi
run_step export operational "${tools[@]}" python /scripts/export_raw.py --output "$parsed_run/master_raw.xlsx"
run_step raw_validate operational "${tools[@]}" python /scripts/validate_parsing.py --workbook "$parsed_run/master_raw.xlsx" --logs-root "/logs/full-test-runs/$run_id/raw-validation"
etl=(python /scripts/catalog_etl.py --raw "$parsed_run/master_raw.xlsx" --catalog-manifest /catalogs/manifests/guhring-production.yaml --mapping /mappings/guhring.yaml --manifest-schema /schemas/catalog-manifest.schema.json --mapping-schema /schemas/field-mapping.schema.json --decision-schema /schemas/review-decision.schema.json --output-root "$parsed_run/etl" --publish-root "$parsed_run/etl/latest" --logs-root "/logs/full-test-runs/$run_id/etl")
run_step structure operational "${tools[@]}" "${etl[@]}"
run_step structure_validate operational "${tools[@]}" "${etl[@]}" --validate-only
run_step cached_structure operational "${tools[@]}" "${etl[@]}"
run_step golden quality "${tools[@]}" python /scripts/verify_guhring_test.py --workbook "$parsed_run/master_raw.xlsx" --report "/logs/full-test-runs/$run_id/golden.json"
openai=("${compose[@]}" run --rm --no-deps openai-processor --input "$parsed_run/master_raw.xlsx" --output-root "/parsed/openai/full-test-runs/$run_id" --selection-manifest /catalogs/manifests/guhring-production.yaml --dry-run)
run_step openai_rows_plan operational "${openai[@]}"
run_step openai_pages_plan operational "${openai[@]}" --mode page_completeness
run_step restart operational "${compose[@]}" restart
run_step restarted_health operational "${tools[@]}" python /scripts/full_test.py health
run_step cached_ingestion operational "${tools[@]}" python /scripts/ragflow_e2e.py /catalogs/test/guhring_catalog_test.pdf --dataset 'Catalog parser integration test'
if [[ -f logs/ragflow-e2e-latest.json ]]; then cp -- logs/ragflow-e2e-latest.json "$run_root/cached-ingestion.json"; fi
run_step restart_cache operational "${tools[@]}" python /scripts/verify_restart_safety.py /catalogs/test/guhring_catalog_test.pdf --expected-device cpu --expected-backend pipeline --output "/logs/full-test-runs/$run_id/restart-cache.json"
cleanup
sampler_pid=''
"${tools[@]}" python /scripts/full_test.py report --run-id "$run_id"
