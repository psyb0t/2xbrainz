SHELL := /bin/bash

DEV_IMAGE := 2xbrainz-dev:local
APP_IMAGE := 2xbrainz:local
RELEASE_IMAGE ?= psyb0t/2xbrainz
VERSION ?= $(shell awk -F'"' '/^version[[:space:]]*=[[:space:]]*"/ { print $$2; exit }' pyproject.toml)
TAG := v$(VERSION)
PROJECT_ROOT := $(CURDIR)
ENV_FILE ?= .env
FIXTURE_TRACE_DIRECTORY := $(PROJECT_ROOT)/.testing/fixture-traces
# host, so the app reaches AIGate and Talkies on the ports they already publish
# without the caller having to know which Docker network they sit on. Override
# with LIVE_NETWORK=<name> to join a specific network instead.
LIVE_NETWORK ?= host
BENCHMARK_AUDIO ?= tests/fixtures/commons-audio-cc0.wav
BENCHMARK_REFERENCE_FILE ?=
TALKIES_MODEL ?=
TALKIES_WS_URL ?=
AIGATE_URL ?=
FIXTURE_TTS_MODEL ?= kokoro-82m-nvidia
FIXTURE_TTS_VOICE ?= af_heart
FIXTURE_AIGATE_MODEL ?= pibox-zai-glm-5-turbo
FIXTURE_REAL_AIGATE ?= false
FIXTURE_AUDIO_SCENARIO ?= overlap
BENCHMARK_DRAFT_ARGUMENT ?=
RUNTIME_CPUS ?= 8.0
RUNTIME_MEMORY ?= 1g
RUNTIME_PIDS ?= 128
RUNTIME_LIMITS := --memory=$(RUNTIME_MEMORY) --cpus=$(RUNTIME_CPUS) --pids-limit=$(RUNTIME_PIDS)
BUMP_HOST := bash scripts/bump_exclude_newer.sh pyproject.toml
DOCKER_HOST_ARGUMENTS = $$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m two_x_brainz.docker_hosts \
	"$(ENV_FILE)")
FIXTURE_DOCKER_HOST_ARGUMENTS = $$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m two_x_brainz.docker_hosts \
	"$(ENV_FILE)" "$(AIGATE_URL)" "$(TALKIES_WS_URL)")
TALKIES_MODEL_ARGUMENT = $(if $(strip $(TALKIES_MODEL)),-e TWOXBRAINZ_TALKIES_MODEL="$(TALKIES_MODEL)")
TALKIES_WS_URL_ARGUMENT = $(if $(strip $(TALKIES_WS_URL)),-e TWOXBRAINZ_TALKIES_WS_URL="$(TALKIES_WS_URL)")
AIGATE_URL_ARGUMENT = $(if $(strip $(AIGATE_URL)),-e TWOXBRAINZ_AIGATE_URL="$(AIGATE_URL)")
BENCHMARK_REFERENCE_ARGUMENT = $(if $(strip $(BENCHMARK_REFERENCE_FILE)),--reference-file /fixture/reference.txt)
BENCHMARK_REFERENCE_MOUNT = $(if $(strip $(BENCHMARK_REFERENCE_FILE)),-v "$(abspath $(BENCHMARK_REFERENCE_FILE)):/fixture/reference.txt:ro")
ASR_CANDIDATE_MODELS := nemotron-3.5-asr-0.6b sherpa-zipformer-en-left-64 sherpa-zipformer-en-left-128 sherpa-zipformer-en-int8-left-64 sherpa-zipformer-en-int8-left-128 vosk-small-en-us-0.15
DEV_RUN := docker run --rm --init --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --tmpfs /work-env:rw,exec,nosuid,size=512m --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e PYRIGHT_PYTHON_CACHE_DIR=/work-env/pyright-cache -e RUFF_CACHE_DIR=/tmp/ruff-cache -e UV_CACHE_DIR=/tmp/uv-cache -e UV_PROJECT_ENVIRONMENT=/work-env/venv -v "$(PROJECT_ROOT):/workspace:ro" -w /workspace $(DEV_IMAGE)
DEV_RUN_WRITE := docker run --rm --init --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --tmpfs /work-env:rw,exec,nosuid,size=512m --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e PYRIGHT_PYTHON_CACHE_DIR=/work-env/pyright-cache -e RUFF_CACHE_DIR=/tmp/ruff-cache -e UV_CACHE_DIR=/tmp/uv-cache -e UV_PROJECT_ENVIRONMENT=/work-env/venv -v "$(PROJECT_ROOT):/workspace" -w /workspace $(DEV_IMAGE)

.DEFAULT_GOAL := help
.PHONY: help version dev-image shell dep pkg-lock pkg-add pkg-remove pkg-update pkg-upgrade lint lint-fix format test test-unit test-integration test-coverage test-real build run replay devices live live-fixture live-interview-fixture live-product-fixture benchmark benchmark-with-draft benchmark-candidates benchmark-candidates-with-draft clean

help: ## Show supported development commands.
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z0-9_-]+:.*##/ {printf "%-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

version: ## Print the release image tag derived from pyproject.toml.
	@printf '%s\n' "$(TAG)"

dev-image: ## Build the locked-down development image.
	docker build --file Dockerfile.dev --tag $(DEV_IMAGE) .

shell: dev-image ## Open a shell in the development image.
	$(DEV_RUN_WRITE) bash

dep: dev-image ## Install the locked project and development dependencies in Docker.
	$(DEV_RUN) uv sync --frozen --group dev

pkg-lock: dev-image ## Refresh uv.lock without changing dependency versions.
	$(DEV_RUN_WRITE) uv lock

pkg-add: dev-image ## Add a pinned package: make pkg-add PKG=name==version.
	@test -n "$(PKG)" || (echo "usage: make pkg-add PKG=name==version" >&2; exit 1)
	$(BUMP_HOST)
	$(DEV_RUN_WRITE) uv add --no-sync $(PKG)

pkg-remove: dev-image ## Remove a package: make pkg-remove PKG=name.
	@test -n "$(PKG)" || (echo "usage: make pkg-remove PKG=name" >&2; exit 1)
	$(BUMP_HOST)
	$(DEV_RUN_WRITE) uv remove --no-sync $(PKG)

pkg-update: dev-image ## Upgrade one package: make pkg-update PKG=name.
	@test -n "$(PKG)" || (echo "usage: make pkg-update PKG=name" >&2; exit 1)
	$(BUMP_HOST)
	$(DEV_RUN_WRITE) uv lock --upgrade-package $(PKG)

pkg-upgrade: dev-image ## Upgrade all pinned packages after reviewing the lockfile.
	$(BUMP_HOST)
	$(DEV_RUN_WRITE) uv lock --upgrade

format: dev-image ## Format Python sources inside Docker.
	$(DEV_RUN_WRITE) uv run --frozen --group dev ruff format src tests

lint: dev-image ## Run lint, format check, and strict type checking inside Docker.
	$(DEV_RUN) uv run --frozen --group dev ruff check src tests
	$(DEV_RUN) uv run --frozen --group dev ruff format --check src tests
	$(DEV_RUN) uv run --frozen --group dev pyright
	$(DEV_RUN) shellcheck scripts/bump_exclude_newer.sh

lint-fix: dev-image ## Apply safe lint fixes and formatting inside Docker.
	$(DEV_RUN_WRITE) uv run --frozen --group dev ruff check --fix src tests
	$(DEV_RUN_WRITE) uv run --frozen --group dev ruff format src tests

test: test-unit test-integration ## Run the entire test suite inside Docker.

test-unit: dev-image ## Run unit tests inside Docker.
	$(DEV_RUN) uv run --frozen --group dev pytest -o cache_dir=/tmp/pytest-cache tests/unit

test-integration: dev-image ## Run integration tests inside Docker.
	$(DEV_RUN) uv run --frozen --group dev pytest -o cache_dir=/tmp/pytest-cache tests/integration

test-coverage: dev-image ## Run tests; coverage tooling is intentionally not installed yet.
	$(DEV_RUN) uv run --frozen --group dev pytest -o cache_dir=/tmp/pytest-cache tests

test-real: build ## Check real AIGate prompts with synthetic text from the gitignored .env.
	@test -f "$(ENV_FILE)" || (echo "$(ENV_FILE) is required and must be gitignored" >&2; exit 1)
	@mkdir -p "$(FIXTURE_TRACE_DIRECTORY)"
	docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --user "$$(id -u):$$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true --network "$(LIVE_NETWORK)" $(FIXTURE_DOCKER_HOST_ARGUMENTS) --env-file "$(ENV_FILE)" $(AIGATE_URL_ARGUMENT) -e TWOXBRAINZ_AIGATE_MODEL="$(FIXTURE_AIGATE_MODEL)" -e TWOXBRAINZ_FIXTURE_TRACE_DIR=/fixture-traces -v "$(FIXTURE_TRACE_DIRECTORY):/fixture-traces:rw" -v "$(PROJECT_ROOT)/tests/integration/real_aigate_prompts.py:/fixture/real_aigate_prompts.py:ro" --entrypoint python $(APP_IMAGE) /fixture/real_aigate_prompts.py

build: ## Build and tag the production CLI image.
	docker build --file Dockerfile --tag $(APP_IMAGE) --tag $(RELEASE_IMAGE):$(TAG) --tag $(RELEASE_IMAGE):latest .

run: build ## Run the production CLI image with a read-only filesystem.
	docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --cap-drop ALL --security-opt no-new-privileges:true $(APP_IMAGE) doctor

replay: build ## Replay the bundled conversation fixture in the production image.
	docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --cap-drop ALL --security-opt no-new-privileges:true -v "$(PROJECT_ROOT)/examples:/examples:ro" $(APP_IMAGE) replay --events /examples/conversation.jsonl

devices: build ## List host PipeWire nodes from the production container.
	@test -n "$${XDG_RUNTIME_DIR:-}" || (echo "XDG_RUNTIME_DIR is required" >&2; exit 1)
	docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --user "$$(id -u):$$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true -e XDG_RUNTIME_DIR=/pipewire-runtime -v "$${XDG_RUNTIME_DIR}:/pipewire-runtime:ro" $(APP_IMAGE) devices

live: build ## Capture two PipeWire nodes; override RUNTIME_CPUS/MEMORY after measuring.
	@test -n "$(MIC_NODE)" || (echo "MIC_NODE is required" >&2; exit 1)
	@test -n "$(SYSTEM_NODE)" || (echo "SYSTEM_NODE is required" >&2; exit 1)
	@test -f "$(ENV_FILE)" || (echo "$(ENV_FILE) is required and must be gitignored" >&2; exit 1)
	@test -n "$${XDG_RUNTIME_DIR:-}" || (echo "XDG_RUNTIME_DIR is required" >&2; exit 1)
	docker run --rm --init -i $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --user "$$(id -u):$$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true --network "$(LIVE_NETWORK)" $(DOCKER_HOST_ARGUMENTS) --env-file "$(ENV_FILE)" -e XDG_RUNTIME_DIR=/pipewire-runtime -v "$${XDG_RUNTIME_DIR}:/pipewire-runtime:ro" $(APP_IMAGE) live --mic-node "$(MIC_NODE)" --system-node "$(SYSTEM_NODE)"

live-fixture: build ## Exercise overlapping Talkies TTS fixture devices; AIGate stays mocked.
	@test -f "$(ENV_FILE)" || (echo "$(ENV_FILE) is required and must be gitignored" >&2; exit 1)
	@mkdir -p "$(FIXTURE_TRACE_DIRECTORY)"
	docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --tmpfs /fixture-work:rw,exec,nosuid,size=512m --user "$$(id -u):$$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true --network "$(LIVE_NETWORK)" $(FIXTURE_DOCKER_HOST_ARGUMENTS) --env-file "$(ENV_FILE)" $(AIGATE_URL_ARGUMENT) $(TALKIES_MODEL_ARGUMENT) $(TALKIES_WS_URL_ARGUMENT) -e TWOXBRAINZ_AIGATE_MODEL="$(FIXTURE_AIGATE_MODEL)" -e TWOXBRAINZ_FIXTURE_REAL_AIGATE="$(FIXTURE_REAL_AIGATE)" -e TWOXBRAINZ_FIXTURE_AUDIO_SCENARIO="$(FIXTURE_AUDIO_SCENARIO)" -e TWOXBRAINZ_FIXTURE_TTS_MODEL="$(FIXTURE_TTS_MODEL)" -e TWOXBRAINZ_FIXTURE_TTS_VOICE="$(FIXTURE_TTS_VOICE)" -e TWOXBRAINZ_FIXTURE_WORK_DIR=/fixture-work -e TWOXBRAINZ_FIXTURE_TRACE_DIR=/fixture-traces -v "$(FIXTURE_TRACE_DIRECTORY):/fixture-traces:rw" -v "$(PROJECT_ROOT)/tests/integration/live_talkies_tts_fixture.py:/fixture/live_talkies_tts_fixture.py:ro" --entrypoint python $(APP_IMAGE) /fixture/live_talkies_tts_fixture.py

live-interview-fixture: FIXTURE_AUDIO_SCENARIO := interview
live-interview-fixture: live-fixture ## Exercise four-turn Talkies audio with deterministic AIGate.

live-product-fixture: FIXTURE_REAL_AIGATE := true
live-product-fixture: FIXTURE_AUDIO_SCENARIO := interview
live-product-fixture: live-fixture ## Exercise four-turn real Talkies capture and real AIGate story handling.

benchmark: build ## Verify one Talkies model; set TALKIES_MODEL to override .env.
	@test -f "$(BENCHMARK_AUDIO)" || (echo "BENCHMARK_AUDIO must name a WAV file" >&2; exit 1)
	@test -z "$(BENCHMARK_REFERENCE_FILE)" || test -f "$(BENCHMARK_REFERENCE_FILE)" || (echo "BENCHMARK_REFERENCE_FILE must name a text file" >&2; exit 1)
	@test -f "$(ENV_FILE)" || (echo "$(ENV_FILE) is required and must be gitignored" >&2; exit 1)
	docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --cap-drop ALL --security-opt no-new-privileges:true --network "$(LIVE_NETWORK)" $(DOCKER_HOST_ARGUMENTS) --env-file "$(ENV_FILE)" $(TALKIES_MODEL_ARGUMENT) -v "$(abspath $(BENCHMARK_AUDIO)):/fixture/audio.wav:ro" $(BENCHMARK_REFERENCE_MOUNT) $(APP_IMAGE) benchmark --audio /fixture/audio.wav $(BENCHMARK_REFERENCE_ARGUMENT) $(BENCHMARK_DRAFT_ARGUMENT)

benchmark-with-draft: BENCHMARK_DRAFT_ARGUMENT := --with-draft
benchmark-with-draft: benchmark ## Verify one Talkies model with a concurrent synthetic AIGate draft.

benchmark-candidates: ## Benchmark all supported native candidates sequentially.
	@for model in $(ASR_CANDIDATE_MODELS); do \
		$(MAKE) --no-print-directory benchmark \
			ENV_FILE="$(ENV_FILE)" \
			LIVE_NETWORK="$(LIVE_NETWORK)" \
			BENCHMARK_AUDIO="$(BENCHMARK_AUDIO)" \
			BENCHMARK_REFERENCE_FILE="$(BENCHMARK_REFERENCE_FILE)" \
			RUNTIME_CPUS="$(RUNTIME_CPUS)" \
			RUNTIME_MEMORY="$(RUNTIME_MEMORY)" \
			RUNTIME_PIDS="$(RUNTIME_PIDS)" \
			BENCHMARK_DRAFT_ARGUMENT="$(BENCHMARK_DRAFT_ARGUMENT)" \
			TALKIES_MODEL="$$model" || exit $$?; \
	done

benchmark-candidates-with-draft: BENCHMARK_DRAFT_ARGUMENT := --with-draft
benchmark-candidates-with-draft: benchmark-candidates ## Benchmark all candidates with a concurrent synthetic AIGate draft.

clean: ## Remove only this project's locally built Docker images.
	docker image rm $(DEV_IMAGE) $(APP_IMAGE)
