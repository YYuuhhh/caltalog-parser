SHELL := /bin/bash
COMPOSE := docker compose
LEGACY_COMPOSE := docker compose -f docker-compose.yml -f docker-compose.legacy.yml
TOOLS_IMAGE := guhring-parser-tools:3
MODEL_RAM ?= 32
RUNTIME_BUNDLE ?= $(CURDIR)/artifacts/runtime
WORK_REVIEW_WORKER ?= 0
TOOLS_RUN := $(COMPOSE) run --rm --no-deps --name CatalogFlow_tools_$$(date +%s%N) tools
OPENAI_RUN := $(LEGACY_COMPOSE) run --rm --no-deps --name CatalogFlow_openai-processor_$$(date +%s%N) openai-processor
ETL_RUN := $(COMPOSE) run --rm --no-deps --name CatalogFlow_catalog-etl_$$(date +%s%N) catalog-etl
CPU_PROFILES := cpu,elasticsearch,metadata-mysql,tei-cpu
GPU_PROFILES := gpu,elasticsearch,metadata-mysql,tei-cpu
CATALOG_MANIFEST ?= /catalogs/manifests/guhring-production.yaml
FIELD_MAPPING ?= /config/mappings/guhring.yaml
ETL_ARGS ?=
OPENAI_ARGS ?=
REGISTRY_ARGS ?=
AUDIT_ARGS ?=

.PHONY: build start stop restart status logs update ensure-tools ensure-etl-dirs prepare-test prepare-vlm-test parse-test parse-test-openai parse-test-structure parse-test-openai-structure gpu-parse-test parse-folder export validate verify-test verify-vlm openai-process openai-test structure structure-reviewed validate-structure structure-aliases test full-test legacy-full-test review-worker legacy-start mineru-ui gpu-build gpu-start backup register-catalog verify-inventory bind-raw parse-manifest openai-completeness structure-complete compare-revisions audit

build:
	python3 scripts/runtime_bundle.py build --bundle "$(RUNTIME_BUNDLE)"

.PHONY: rebuild deps-prepare deps-verify deps-restore
rebuild:
	python3 scripts/runtime_bundle.py rebuild --bundle "$(RUNTIME_BUNDLE)"

deps-prepare:
	python3 scripts/runtime_bundle.py prepare --bundle "$(RUNTIME_BUNDLE)" $(DEPS_ARGS)

deps-verify:
	python3 scripts/runtime_bundle.py verify --bundle "$(RUNTIME_BUNDLE)"

deps-restore:
	python3 scripts/runtime_bundle.py restore --bundle "$(RUNTIME_BUNDLE)"

.PHONY: local-models
local-models:
	$(COMPOSE) up -d local-llm
	python3 scripts/pull_local_models.py --ram $(MODEL_RAM)

.PHONY: cpu-server-package
cpu-server-package:
	bash scripts/package_cpu_server.sh

start: ensure-workbench-dirs build
	$(COMPOSE) up -d
	@if [ "$(WORK_REVIEW_WORKER)" = "1" ]; then python3 scripts/review_worker_service.py start; fi

stop:
	$(COMPOSE) down

restart:
	@if [ "$(WORK_REVIEW_WORKER)" = "1" ]; then python3 scripts/review_worker_service.py stop; fi
	$(COMPOSE) restart
	@if [ "$(WORK_REVIEW_WORKER)" = "1" ]; then python3 scripts/review_worker_service.py start; fi

status:
	$(COMPOSE) ps
	python3 scripts/review_worker_service.py status

logs:
	$(COMPOSE) logs --tail=200 -f

update:
	$(MAKE) build
	$(COMPOSE) up -d
	@if [ "$(WORK_REVIEW_WORKER)" = "1" ]; then python3 scripts/review_worker_service.py restart; fi

ensure-tools:
	@docker image inspect $(TOOLS_IMAGE) >/dev/null 2>&1 || $(MAKE) build

ensure-etl-dirs:
	mkdir -p parsed/etl logs/etl

prepare-test: ensure-tools
	$(TOOLS_RUN) python /scripts/prepare_test_catalog.py

prepare-vlm-test: ensure-tools
	$(TOOLS_RUN) python /scripts/prepare_test_catalog.py --pages 1128 --output /catalogs/test/guhring_vlm_test.pdf

parse-test: prepare-test
	$(TOOLS_RUN) python /scripts/ragflow_e2e.py /catalogs/test/guhring_catalog_test.pdf
	$(TOOLS_RUN) python /scripts/export_raw.py
	$(TOOLS_RUN) python /scripts/validate_parsing.py
	$(TOOLS_RUN) python /scripts/verify_guhring_test.py --report-only

parse-test-openai: parse-test openai-test

parse-test-structure: parse-test
	$(MAKE) structure

parse-test-openai-structure: parse-test-openai
	$(MAKE) structure-reviewed

gpu-parse-test: prepare-vlm-test
	COMPOSE_PROFILES=$(GPU_PROFILES) MINERU_BACKEND=vlm-transformers MINERU_DEVICE_MODE=cuda $(LEGACY_COMPOSE) run --rm --no-deps --name CatalogFlow_tools_$$(date +%s%N) tools python /scripts/ragflow_e2e.py /catalogs/test/guhring_vlm_test.pdf
	$(TOOLS_RUN) python /scripts/export_raw.py
	$(TOOLS_RUN) python /scripts/validate_parsing.py
	$(TOOLS_RUN) python /scripts/verify_vlm_accuracy.py

parse-folder: ensure-tools
	$(TOOLS_RUN) python /scripts/parse_catalogs.py /catalogs/input

export: ensure-tools
	$(TOOLS_RUN) python /scripts/export_raw.py

validate: ensure-tools
	$(TOOLS_RUN) python /scripts/validate_parsing.py

verify-test: ensure-tools
	$(TOOLS_RUN) python /scripts/verify_guhring_test.py

verify-vlm: ensure-tools
	$(TOOLS_RUN) python /scripts/verify_vlm_accuracy.py

openai-process: ensure-tools
	$(OPENAI_RUN) --selection-manifest $(CATALOG_MANIFEST) $(OPENAI_ARGS)

openai-test: ensure-tools
	$(OPENAI_RUN) --source-pdf /catalogs/test/guhring_catalog_test.pdf

structure: ensure-tools ensure-etl-dirs
	$(ETL_RUN) --catalog-manifest $(CATALOG_MANIFEST) --mapping $(FIELD_MAPPING) $(ETL_ARGS)
	$(MAKE) structure-aliases

structure-reviewed: ensure-tools ensure-etl-dirs
	$(ETL_RUN) --use-openai --catalog-manifest $(CATALOG_MANIFEST) --mapping $(FIELD_MAPPING) $(ETL_ARGS)
	$(MAKE) structure-aliases

validate-structure: ensure-tools ensure-etl-dirs
	$(ETL_RUN) --validate-only

structure-aliases:
	@test -f parsed/etl/latest/master_reviewed.xlsx
	@ln -sfn etl/latest/master_reviewed.xlsx parsed/master_reviewed.xlsx
	@test -f parsed/etl/latest/master_canonical.xlsx
	@ln -sfn etl/latest/master_canonical.xlsx parsed/master_canonical.xlsx
	@test -f parsed/etl/latest/master_structured.xlsx
	@ln -sfn etl/latest/master_structured.xlsx parsed/master_structured.xlsx
	@test -f parsed/etl/latest/master_matcher.xlsx
	@ln -sfn etl/latest/master_matcher.xlsx parsed/master_matcher.xlsx

test: ensure-tools
	$(TOOLS_RUN) pytest -q -p no:cacheprovider /tests /mineru-src/tests

full-test: ensure-tools
	python3 scripts/workbench_full_test.py $(TEST_ARGS)

legacy-full-test: ensure-tools
	COMPOSE_FILE=docker-compose.yml:docker-compose.legacy.yml COMPOSE_PROFILES=$(CPU_PROFILES) bash scripts/full_test.sh

.PHONY: ui pipeline-ui

ui: pipeline-ui

pipeline-ui: ensure-workbench-dirs build
	$(COMPOSE) up -d mineru local-llm pipeline-ui
	@if [ "$(WORK_REVIEW_WORKER)" = "1" ]; then python3 scripts/review_worker_service.py start; fi

mineru-ui: build
	COMPOSE_PROFILES=$(CPU_PROFILES),mineru-ui $(LEGACY_COMPOSE) up -d mineru-ui

gpu-build:
	COMPOSE_PROFILES=$(GPU_PROFILES) $(LEGACY_COMPOSE) build mineru-gpu

gpu-start:
	$(LEGACY_COMPOSE) down
	COMPOSE_PROFILES=$(GPU_PROFILES) MINERU_BACKEND=vlm-transformers MINERU_DEVICE_MODE=cuda $(LEGACY_COMPOSE) up -d --build

backup:
	mkdir -p backups
	docker run --rm --name CatalogFlow_backup_$$(date +%s%N) -v "$(CURDIR):/project:ro" -v "$(CURDIR)/backups:/backup" alpine:3.22.1 sh -ec 'tar -czf "/backup/guhring-parser-$$(date -u +%Y%m%dT%H%M%SZ).tar.gz" -C /project data parsed logs catalogs schemas mappings'

register-catalog: ensure-tools
	$(TOOLS_RUN) python /scripts/catalog_registry.py register --manifest $(CATALOG_MANIFEST) $(REGISTRY_ARGS)

verify-inventory: ensure-tools
	$(TOOLS_RUN) python /scripts/catalog_registry.py verify --manifest $(CATALOG_MANIFEST)

bind-raw: ensure-tools
	$(TOOLS_RUN) python /scripts/catalog_registry.py bind-raw --manifest $(CATALOG_MANIFEST)

parse-manifest: ensure-tools
	$(TOOLS_RUN) python /scripts/parse_catalogs.py --manifest $(CATALOG_MANIFEST)

openai-completeness: ensure-tools
	$(OPENAI_RUN) --mode page_completeness --selection-manifest $(CATALOG_MANIFEST) $(OPENAI_ARGS)

structure-complete: ensure-tools ensure-etl-dirs
	$(ETL_RUN) --use-page-completeness --catalog-manifest $(CATALOG_MANIFEST) --mapping $(FIELD_MAPPING) $(ETL_ARGS)
	$(MAKE) structure-aliases

compare-revisions: ensure-tools
	@test -n "$(OLD)" -a -n "$(NEW)" || (echo 'Set OLD and NEW to canonical workbook paths inside /parsed.' >&2; exit 2)
	$(TOOLS_RUN) python /scripts/compare_catalogs.py "$(OLD)" "$(NEW)" $(DIFF_ARGS)

audit: ensure-tools
	$(TOOLS_RUN) python /scripts/audit_catalog.py $(AUDIT_ARGS)

.PHONY: review-worker review-worker-stop review-worker-status review-worker-logs legacy-start refresh-models
review-worker:
	python3 scripts/review_worker_service.py start

review-worker-stop:
	python3 scripts/review_worker_service.py stop

review-worker-status:
	python3 scripts/review_worker_service.py status

review-worker-logs:
	python3 scripts/review_worker_service.py logs

refresh-models:
	python3 scripts/refresh_models.py $(MODEL_ARGS)

legacy-start:
	COMPOSE_PROFILES=cpu,elasticsearch,metadata-mysql,tei-cpu $(LEGACY_COMPOSE) up -d

.PHONY: ensure-workbench-dirs
ensure-workbench-dirs:
	mkdir -p catalogs/input catalogs/uploads catalogs/imports/guhring catalogs/library parsed logs
