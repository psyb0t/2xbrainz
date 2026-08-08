SHELL := /bin/bash
comma := ,

DEV_IMAGE := 2xbrainz-dev:local
APP_IMAGE := 2xbrainz:local
REAL_TEST_IMAGE := 2xbrainz-test-real:local
BROWSER_TEST_APP_IMAGE := 2xbrainz-browser-test:local
BROWSER_TEST_IMAGE := psyb0t/stealthy-auto-browse@sha256:0dab459e28c8872ea2f54048f91c2b3aae10b43b7e1ec44d9c56aca8d456169d
WEB_TOOL_IMAGE := node:24-bookworm-slim@sha256:cd84903a12dbd26b46f1f3b8144a2568c41c5d37ddd0c7a80a34c7a19786b35f
RELEASE_IMAGE ?= psyb0t/2xbrainz
VERSION ?= $(shell awk -F'"' '/^version[[:space:]]*=[[:space:]]*"/ { print $$2; exit }' pyproject.toml)
TAG := v$(VERSION)
PROJECT_ROOT := $(CURDIR)
ENV_FILE ?= .env
LOG_DIRECTORY ?= $(PROJECT_ROOT)/logs
LIVE_LOG_DIRECTORY := /logs
LOG_TAIL_LINES ?= 200
LOG_FILE ?=
WEB_PORT ?= 7860
RUN_ARGUMENTS ?= --web-port $(WEB_PORT)
FIXTURE_TRACE_DIRECTORY := $(PROJECT_ROOT)/.testing/fixture-traces
# host, so the app reaches AIGate and Talkies on the ports they already publish
# without the caller having to know which Docker network they sit on. Override
# with LIVE_NETWORK=<name> to join a specific network instead.
LIVE_NETWORK ?= host
BENCHMARK_AUDIO ?= tests/fixtures/commons-audio-cc0.wav
BENCHMARK_REFERENCE_FILE ?=
TALKIES_MODEL ?=
AIGATE_URL ?=
FIXTURE_TTS_MODEL ?= kokoro-82m-nvidia
FIXTURE_TTS_VOICE ?= af_heart
FIXTURE_AIGATE_MODEL ?= groq-gpt-oss-120b
FIXTURE_AIGATE_DRAFT_MODEL ?= claudebox-sonnet
FIXTURE_AIGATE_COMMENTARY_MODEL ?= pibox-zai-glm-5-turbo
FIXTURE_AIGATE_SUMMARY_MODEL ?= groq-gpt-oss-120b
FIXTURE_REAL_AIGATE ?= false
FIXTURE_AUDIO_SCENARIO ?= overlap
EVALUATION_SCENARIO ?= tests/fixtures/slang-interrupted-project-chat.json
EVALUATION_RUN ?=
EVALUATION_USER_VOICE ?= af_heart
EVALUATION_REMOTE_VOICE ?= am_michael
EVALUATION_REPEATS ?= 3
BENCHMARK_DRAFT_ARGUMENT ?=
RUNTIME_CPUS ?= 8.0
RUNTIME_MEMORY ?= 1g
RUNTIME_PIDS ?= 128
RUNTIME_LIMITS := --memory=$(RUNTIME_MEMORY) --cpus=$(RUNTIME_CPUS) --pids-limit=$(RUNTIME_PIDS)
BUMP_HOST := bash scripts/bump_exclude_newer.sh pyproject.toml
DOCKER_HOST_ARGUMENTS = $(if $(filter host,$(LIVE_NETWORK)),,$$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m two_x_brainz.docker_hosts \
	"$(ENV_FILE)"))
FIXTURE_DOCKER_HOST_ARGUMENTS = $(if $(filter host,$(LIVE_NETWORK)),,$$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m two_x_brainz.docker_hosts \
	"$(ENV_FILE)" "$(AIGATE_URL)"))
FIXTURE_TALKIES_MODEL_ARGUMENT = $(if $(strip $(TALKIES_MODEL)),-e TWOXBRAINZ_FIXTURE_TALKIES_MODEL="$(TALKIES_MODEL)")
BENCHMARK_MODEL_ARGUMENT = $(if $(strip $(TALKIES_MODEL)),--model "$(TALKIES_MODEL)")
AIGATE_URL_ARGUMENT = $(if $(strip $(AIGATE_URL)),-e TWOXBRAINZ_AIGATE_URL="$(AIGATE_URL)")
BENCHMARK_REFERENCE_ARGUMENT = $(if $(strip $(BENCHMARK_REFERENCE_FILE)),--reference-file /fixture/reference.txt)
BENCHMARK_REFERENCE_MOUNT = $(if $(strip $(BENCHMARK_REFERENCE_FILE)),-v "$(abspath $(BENCHMARK_REFERENCE_FILE)):/fixture/reference.txt:ro")
ASR_CANDIDATE_MODELS := nemotron-3.5-asr-0.6b sherpa-zipformer-en-left-64 sherpa-zipformer-en-left-128 sherpa-zipformer-en-int8-left-64 sherpa-zipformer-en-int8-left-128 vosk-small-en-us-0.15
BUILD_DEV_IMAGE = docker build --file Dockerfile.dev --tag $(DEV_IMAGE) .
BUILD_APP_IMAGES = docker build --file Dockerfile --tag $(APP_IMAGE) --tag $(RELEASE_IMAGE):$(TAG) --tag $(RELEASE_IMAGE):latest .
BUILD_REAL_TEST_IMAGE = docker build --file Dockerfile --tag $(REAL_TEST_IMAGE) .
DEV_RUN := docker run --rm --init --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --tmpfs /work-env:rw,exec,nosuid,size=1g --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e PYRIGHT_PYTHON_CACHE_DIR=/work-env/pyright-cache -e RUFF_CACHE_DIR=/tmp/ruff-cache -e UV_CACHE_DIR=/tmp/uv-cache -e UV_PROJECT_ENVIRONMENT=/work-env/venv -v "$(PROJECT_ROOT):/workspace:ro" -w /workspace $(DEV_IMAGE)
DEV_RUN_WRITE := docker run --rm --init --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --tmpfs /work-env:rw,exec,nosuid,size=1g --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e PYRIGHT_PYTHON_CACHE_DIR=/work-env/pyright-cache -e RUFF_CACHE_DIR=/tmp/ruff-cache -e UV_CACHE_DIR=/tmp/uv-cache -e UV_PROJECT_ENVIRONMENT=/work-env/venv -v "$(PROJECT_ROOT):/workspace" -w /workspace $(DEV_IMAGE)
WEB_LOCK_RUN := docker run --rm --init --read-only --tmpfs /tmp:rw,exec,nosuid,size=1g --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e COREPACK_HOME=/tmp/corepack -e PNPM_HOME=/tmp/pnpm -v "$(PROJECT_ROOT)/web:/web:rw" -w /web $(WEB_TOOL_IMAGE)
WEB_CHECK_RUN := docker run --rm --init --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --tmpfs /opt/web/node_modules/.vite:rw,exec,nosuid,size=32m --tmpfs /opt/web/node_modules/.vite-temp:rw,exec,nosuid,size=32m --tmpfs /opt/web/dist:rw,noexec,nosuid,size=32m $(DEV_IMAGE) sh -c 'cd /opt/web && pnpm check && pnpm test && pnpm build'

define RUN_WITH_IMAGE_CLEANUP
	@status=0; \
	cleanup_images() { \
		cleanup_status=0; \
		for image in $(2); do \
			if docker image inspect "$$image" >/dev/null 2>&1; then \
				docker image rm "$$image" || cleanup_status=$$?; \
			fi; \
		done; \
		return "$$cleanup_status"; \
	}; \
	trap 'status=130; cleanup_images; exit "$$status"' INT TERM; \
	$(1) || status=$$?; \
	trap - INT TERM; \
	cleanup_status=0; \
	cleanup_images || cleanup_status=$$?; \
	if [ "$$status" -eq 0 ] && [ "$$cleanup_status" -ne 0 ]; then \
		status=$$cleanup_status; \
	fi; \
	exit "$$status"
endef

.DEFAULT_GOAL := help
.PHONY: help version dev-image shell dep pkg-lock pkg-add pkg-remove pkg-update pkg-upgrade web-pkg-lock web-check web-format web-build lint lint-fix format test test-unit test-integration test-coverage test-browser test-real test-real-talkies test-real-evaluation evaluation-report build run validate-web-network logs doctor replay devices live-fixture live-interview-fixture live-product-fixture benchmark benchmark-with-draft benchmark-candidates benchmark-candidates-with-draft clean

help: ## Show supported development commands.
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z0-9_-]+:.*##/ {printf "%-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

version: ## Print the release image tag derived from pyproject.toml.
	@printf '%s\n' "$(TAG)"

dev-image: ## Build the locked-down development image.
	$(BUILD_DEV_IMAGE)

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

web-pkg-lock: ## Refresh the age-gated Svelte lockfile in a Node container.
	$(WEB_LOCK_RUN) corepack pnpm install --lockfile-only --ignore-scripts

web-check: dev-image ## Type-check, test, format-check, and build the Svelte console.
	$(WEB_CHECK_RUN)

web-format: dev-image ## Format Svelte and TypeScript sources in Docker.
	$(DEV_RUN_WRITE) sh -c 'cd /opt/web && pnpm exec prettier --config /opt/web/prettier.config.js --write /workspace/web'

web-build: dev-image ## Compile the static Svelte bundle in Docker.
	docker run --rm --init --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --tmpfs /opt/web/node_modules/.vite-temp:rw,exec,nosuid,size=32m --tmpfs /opt/web/dist:rw,noexec,nosuid,size=32m $(DEV_IMAGE) sh -c 'cd /opt/web && pnpm build'

format: web-format ## Format Python and web sources inside Docker.
	$(DEV_RUN_WRITE) uv run --frozen --group dev ruff format src tests

lint: ## Run lint, format check, and strict type checking, then remove its image.
	$(call RUN_WITH_IMAGE_CLEANUP,$(BUILD_DEV_IMAGE) && $(WEB_CHECK_RUN) && $(DEV_RUN) uv run --frozen --group dev ruff check src tests && $(DEV_RUN) uv run --frozen --group dev ruff format --check src tests && $(DEV_RUN) uv run --frozen --group dev pyright && $(DEV_RUN) shellcheck scripts/bump_exclude_newer.sh scripts/browser_smoke.sh,$(DEV_IMAGE))

lint-fix: dev-image ## Apply safe lint fixes and formatting inside Docker.
	$(DEV_RUN_WRITE) uv run --frozen --group dev ruff check --fix src tests
	$(DEV_RUN_WRITE) uv run --frozen --group dev ruff format src tests

test: ## Run the entire test suite inside Docker, then remove its image.
	$(call RUN_WITH_IMAGE_CLEANUP,$(BUILD_DEV_IMAGE) && $(DEV_RUN) uv run --frozen --group dev pytest -o cache_dir=/tmp/pytest-cache tests,$(DEV_IMAGE))

test-unit: ## Run unit tests inside Docker, then remove its image.
	$(call RUN_WITH_IMAGE_CLEANUP,$(BUILD_DEV_IMAGE) && $(DEV_RUN) uv run --frozen --group dev pytest -o cache_dir=/tmp/pytest-cache tests/unit,$(DEV_IMAGE))

test-integration: ## Run integration tests inside Docker, then remove its image.
	$(call RUN_WITH_IMAGE_CLEANUP,$(BUILD_DEV_IMAGE) && $(DEV_RUN) uv run --frozen --group dev pytest -o cache_dir=/tmp/pytest-cache tests/integration,$(DEV_IMAGE))

test-coverage: ## Run tests and remove the image; coverage is not installed yet.
	$(call RUN_WITH_IMAGE_CLEANUP,$(BUILD_DEV_IMAGE) && $(DEV_RUN) uv run --frozen --group dev pytest -o cache_dir=/tmp/pytest-cache tests,$(DEV_IMAGE))

test-browser: ## Run a real-browser console smoke and remove its containers and image.
	BROWSER_TEST_PROJECT_ROOT="$(PROJECT_ROOT)" \
	BROWSER_TEST_APP_IMAGE="$(BROWSER_TEST_APP_IMAGE)" \
	BROWSER_TEST_IMAGE="$(BROWSER_TEST_IMAGE)" \
	./scripts/browser_smoke.sh

test-real: ## Check real AIGate prompts and two-stream Talkies concurrency.
	$(call RUN_WITH_IMAGE_CLEANUP,(test -f "$(ENV_FILE)" || { echo "$(ENV_FILE) is required and must be gitignored" >&2; exit 1; }) && (test -f "$(BENCHMARK_AUDIO)" || { echo "BENCHMARK_AUDIO must name a WAV file" >&2; exit 1; }) && mkdir -p "$(FIXTURE_TRACE_DIRECTORY)" && $(BUILD_REAL_TEST_IMAGE) && docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw$(comma)noexec$(comma)nosuid$(comma)size=512m --user "$$(id -u):$$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true --network "$(LIVE_NETWORK)" $(FIXTURE_DOCKER_HOST_ARGUMENTS) --env-file "$(ENV_FILE)" $(AIGATE_URL_ARGUMENT) -e TWOXBRAINZ_FIXTURE_DRAFT_MODEL="$(FIXTURE_AIGATE_DRAFT_MODEL)" -e TWOXBRAINZ_FIXTURE_COMMENTARY_MODEL="$(FIXTURE_AIGATE_COMMENTARY_MODEL)" -e TWOXBRAINZ_FIXTURE_SUMMARY_MODEL="$(FIXTURE_AIGATE_SUMMARY_MODEL)" -e TWOXBRAINZ_FIXTURE_TRACE_DIR=/fixture-traces -v "$(FIXTURE_TRACE_DIRECTORY):/fixture-traces:rw" -v "$(PROJECT_ROOT)/tests/integration/real_aigate_prompts.py:/fixture/real_aigate_prompts.py:ro" --entrypoint python $(REAL_TEST_IMAGE) /fixture/real_aigate_prompts.py && docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw$(comma)noexec$(comma)nosuid$(comma)size=512m --user "$$(id -u):$$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true --network "$(LIVE_NETWORK)" $(FIXTURE_DOCKER_HOST_ARGUMENTS) --env-file "$(ENV_FILE)" $(AIGATE_URL_ARGUMENT) $(FIXTURE_TALKIES_MODEL_ARGUMENT) -e TWOXBRAINZ_FIXTURE_TRACE_DIR=/fixture-traces -e TWOXBRAINZ_CONCURRENCY_AUDIO=/fixture/audio.wav -v "$(FIXTURE_TRACE_DIRECTORY):/fixture-traces:rw" -v "$(abspath $(BENCHMARK_AUDIO)):/fixture/audio.wav:ro" -v "$(PROJECT_ROOT)/tests/integration/real_talkies_concurrency.py:/fixture/real_talkies_concurrency.py:ro" --entrypoint python $(REAL_TEST_IMAGE) /fixture/real_talkies_concurrency.py,$(REAL_TEST_IMAGE))

test-real-talkies: ## Prove two concurrent Talkies streams through real AIGate.
	$(call RUN_WITH_IMAGE_CLEANUP,(test -f "$(ENV_FILE)" || { echo "$(ENV_FILE) is required and must be gitignored" >&2; exit 1; }) && (test -f "$(BENCHMARK_AUDIO)" || { echo "BENCHMARK_AUDIO must name a WAV file" >&2; exit 1; }) && mkdir -p "$(FIXTURE_TRACE_DIRECTORY)" && $(BUILD_REAL_TEST_IMAGE) && docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw$(comma)noexec$(comma)nosuid$(comma)size=512m --user "$$(id -u):$$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true --network "$(LIVE_NETWORK)" $(FIXTURE_DOCKER_HOST_ARGUMENTS) --env-file "$(ENV_FILE)" $(AIGATE_URL_ARGUMENT) $(FIXTURE_TALKIES_MODEL_ARGUMENT) -e TWOXBRAINZ_FIXTURE_TRACE_DIR=/fixture-traces -e TWOXBRAINZ_CONCURRENCY_AUDIO=/fixture/audio.wav -v "$(FIXTURE_TRACE_DIRECTORY):/fixture-traces:rw" -v "$(abspath $(BENCHMARK_AUDIO)):/fixture/audio.wav:ro" -v "$(PROJECT_ROOT)/tests/integration/real_talkies_concurrency.py:/fixture/real_talkies_concurrency.py:ro" --entrypoint python $(REAL_TEST_IMAGE) /fixture/real_talkies_concurrency.py,$(REAL_TEST_IMAGE))

test-real-evaluation: ## Score a generated slang conversation through real Talkies and AIGate.
	$(call RUN_WITH_IMAGE_CLEANUP,(test -f "$(ENV_FILE)" || { echo "$(ENV_FILE) is required and must be gitignored" >&2; exit 1; }) && (test -f "$(EVALUATION_SCENARIO)" || { echo "EVALUATION_SCENARIO must name a JSON file" >&2; exit 1; }) && mkdir -p "$(FIXTURE_TRACE_DIRECTORY)" && $(BUILD_REAL_TEST_IMAGE) && docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw$(comma)noexec$(comma)nosuid$(comma)size=512m --tmpfs /fixture-work:rw$(comma)exec$(comma)nosuid$(comma)size=512m --user "$$(id -u):$$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true --network "$(LIVE_NETWORK)" $(FIXTURE_DOCKER_HOST_ARGUMENTS) --env-file "$(ENV_FILE)" $(AIGATE_URL_ARGUMENT) $(FIXTURE_TALKIES_MODEL_ARGUMENT) -e TWOXBRAINZ_FIXTURE_DRAFT_MODEL="$(FIXTURE_AIGATE_DRAFT_MODEL)" -e TWOXBRAINZ_FIXTURE_COMMENTARY_MODEL="$(FIXTURE_AIGATE_COMMENTARY_MODEL)" -e TWOXBRAINZ_FIXTURE_SUMMARY_MODEL="$(FIXTURE_AIGATE_SUMMARY_MODEL)" -e TWOXBRAINZ_EVALUATION_SCENARIO=/fixture/scenario.json -e TWOXBRAINZ_EVALUATION_USER_VOICE="$(EVALUATION_USER_VOICE)" -e TWOXBRAINZ_EVALUATION_REMOTE_VOICE="$(EVALUATION_REMOTE_VOICE)" -e TWOXBRAINZ_EVALUATION_REPEATS="$(EVALUATION_REPEATS)" -e TWOXBRAINZ_FIXTURE_TTS_MODEL="$(FIXTURE_TTS_MODEL)" -e TWOXBRAINZ_FIXTURE_WORK_DIR=/fixture-work -e TWOXBRAINZ_FIXTURE_TRACE_DIR=/fixture-traces -v "$(FIXTURE_TRACE_DIRECTORY):/fixture-traces:rw" -v "$(abspath $(EVALUATION_SCENARIO)):/fixture/scenario.json:ro" -v "$(PROJECT_ROOT)/tests/integration/live_talkies_tts_fixture.py:/fixture/live_talkies_tts_fixture.py:ro" -v "$(PROJECT_ROOT)/tests/integration/real_conversation_evaluation.py:/fixture/real_conversation_evaluation.py:ro" --entrypoint python $(REAL_TEST_IMAGE) /fixture/real_conversation_evaluation.py,$(REAL_TEST_IMAGE))

evaluation-report: ## Regenerate one real-evaluation suite report from local artifacts.
	@test -n "$(EVALUATION_RUN)" || (echo "usage: make evaluation-report EVALUATION_RUN=<suite-directory>" >&2; exit 1)
	$(call RUN_WITH_IMAGE_CLEANUP,$(BUILD_DEV_IMAGE) && docker run --rm --init --read-only --tmpfs /tmp:rw$(comma)noexec$(comma)nosuid$(comma)size=512m --tmpfs /work-env:rw$(comma)exec$(comma)nosuid$(comma)size=1g --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv-cache -e UV_PROJECT_ENVIRONMENT=/work-env/venv -e TWOXBRAINZ_EVALUATION_RUN="$(EVALUATION_RUN)" -v "$(PROJECT_ROOT):/workspace:ro" -v "$(FIXTURE_TRACE_DIRECTORY):/workspace/.testing/fixture-traces:rw" -w /workspace $(DEV_IMAGE) uv run --frozen --group dev python -m two_x_brainz.evaluation_report,$(DEV_IMAGE))

build: ## Build and tag the production web application image.
	$(BUILD_APP_IMAGES)

run: validate-web-network build ## Start the local Svelte console at http://127.0.0.1:<port>.
	@test -f "$(ENV_FILE)" || (echo "$(ENV_FILE) is required and must be gitignored" >&2; exit 1)
	@test -n "$${XDG_RUNTIME_DIR:-}" || (echo "XDG_RUNTIME_DIR is required" >&2; exit 1)
	@install -d -m 700 "$(LOG_DIRECTORY)"
	docker run --rm --init -it $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --user "$$(id -u):$$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true --network "$(LIVE_NETWORK)" $(DOCKER_HOST_ARGUMENTS) --env-file "$(ENV_FILE)" -e XDG_RUNTIME_DIR=/pipewire-runtime -e TWOXBRAINZ_LOG_DIRECTORY="$(LIVE_LOG_DIRECTORY)" -v "$${XDG_RUNTIME_DIR}:/pipewire-runtime:ro" -v "$(LOG_DIRECTORY):$(LIVE_LOG_DIRECTORY):rw" $(APP_IMAGE) live $(RUN_ARGUMENTS)

validate-web-network:
	@test "$(LIVE_NETWORK)" = "host" || (echo "run requires LIVE_NETWORK=host because the web server binds only to loopback" >&2; exit 1)

logs: build ## Follow the newest session log; set LOG_FILE to select one explicitly.
	docker run --rm --init --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --user "$$(id -u):$$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true -e TWOXBRAINZ_LOG_NAME="$(LOG_FILE)" -v "$(LOG_DIRECTORY):$(LIVE_LOG_DIRECTORY):ro" --entrypoint /bin/sh $(APP_IMAGE) -c 'set -eu; selected_log="$${TWOXBRAINZ_LOG_NAME}"; if [ -z "$$selected_log" ]; then selected_log="$$(find "$(LIVE_LOG_DIRECTORY)" -maxdepth 1 -type f -name "*_2xbrainz*.log" -printf "%T@ %f\n" | sort -nr | sed -n "1{s/^[^ ]* //;p;}")"; fi; case "$$selected_log" in ""|*/*|*..*|*[!0-9TZ_.-]*) echo "invalid session log name" >&2; exit 1;; esac; case "$$selected_log" in *_2xbrainz.log|*_2xbrainz-[0-9]*.log) ;; *) echo "invalid session log name" >&2; exit 1;; esac; test -f "$(LIVE_LOG_DIRECTORY)/$$selected_log" || { echo "no live log yet: run make run first" >&2; exit 1; }; exec tail -n "$(LOG_TAIL_LINES)" -F "$(LIVE_LOG_DIRECTORY)/$$selected_log"'

doctor: build ## Print a sanitized resolved configuration for troubleshooting.
	@test -f "$(ENV_FILE)" || (echo "$(ENV_FILE) is required and must be gitignored" >&2; exit 1)
	docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --cap-drop ALL --security-opt no-new-privileges:true --env-file "$(ENV_FILE)" $(APP_IMAGE) doctor

replay: build ## Replay the bundled conversation fixture in the production image.
	docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --cap-drop ALL --security-opt no-new-privileges:true -v "$(PROJECT_ROOT)/examples:/examples:ro" $(APP_IMAGE) replay --events /examples/conversation.jsonl

devices: build ## List host PipeWire nodes from the production container.
	@test -n "$${XDG_RUNTIME_DIR:-}" || (echo "XDG_RUNTIME_DIR is required" >&2; exit 1)
	docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --user "$$(id -u):$$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true -e XDG_RUNTIME_DIR=/pipewire-runtime -v "$${XDG_RUNTIME_DIR}:/pipewire-runtime:ro" $(APP_IMAGE) devices

live-fixture: build ## Exercise overlapping Talkies TTS fixture devices; AIGate stays mocked.
	@test -f "$(ENV_FILE)" || (echo "$(ENV_FILE) is required and must be gitignored" >&2; exit 1)
	@mkdir -p "$(FIXTURE_TRACE_DIRECTORY)"
	docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --tmpfs /fixture-work:rw,exec,nosuid,size=512m --user "$$(id -u):$$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true --network "$(LIVE_NETWORK)" $(FIXTURE_DOCKER_HOST_ARGUMENTS) --env-file "$(ENV_FILE)" $(AIGATE_URL_ARGUMENT) $(FIXTURE_TALKIES_MODEL_ARGUMENT) -e TWOXBRAINZ_FIXTURE_AIGATE_MODEL="$(FIXTURE_AIGATE_MODEL)" -e TWOXBRAINZ_FIXTURE_REAL_AIGATE="$(FIXTURE_REAL_AIGATE)" -e TWOXBRAINZ_FIXTURE_AUDIO_SCENARIO="$(FIXTURE_AUDIO_SCENARIO)" -e TWOXBRAINZ_FIXTURE_TTS_MODEL="$(FIXTURE_TTS_MODEL)" -e TWOXBRAINZ_FIXTURE_TTS_VOICE="$(FIXTURE_TTS_VOICE)" -e TWOXBRAINZ_FIXTURE_WORK_DIR=/fixture-work -e TWOXBRAINZ_FIXTURE_TRACE_DIR=/fixture-traces -v "$(FIXTURE_TRACE_DIRECTORY):/fixture-traces:rw" -v "$(PROJECT_ROOT)/tests/integration/live_talkies_tts_fixture.py:/fixture/live_talkies_tts_fixture.py:ro" --entrypoint python $(APP_IMAGE) /fixture/live_talkies_tts_fixture.py

live-interview-fixture: FIXTURE_AUDIO_SCENARIO := interview
live-interview-fixture: live-fixture ## Exercise four-turn Talkies audio with deterministic AIGate.

live-product-fixture: FIXTURE_REAL_AIGATE := true
live-product-fixture: FIXTURE_AUDIO_SCENARIO := interview
live-product-fixture: live-fixture ## Exercise four-turn real Talkies capture and real AIGate story handling.

benchmark: build ## Verify one Talkies model; set TALKIES_MODEL to override .env.
	@test -f "$(BENCHMARK_AUDIO)" || (echo "BENCHMARK_AUDIO must name a WAV file" >&2; exit 1)
	@test -z "$(BENCHMARK_REFERENCE_FILE)" || test -f "$(BENCHMARK_REFERENCE_FILE)" || (echo "BENCHMARK_REFERENCE_FILE must name a text file" >&2; exit 1)
	@test -f "$(ENV_FILE)" || (echo "$(ENV_FILE) is required and must be gitignored" >&2; exit 1)
	docker run --rm --init $(RUNTIME_LIMITS) --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --cap-drop ALL --security-opt no-new-privileges:true --network "$(LIVE_NETWORK)" $(DOCKER_HOST_ARGUMENTS) --env-file "$(ENV_FILE)" -v "$(abspath $(BENCHMARK_AUDIO)):/fixture/audio.wav:ro" $(BENCHMARK_REFERENCE_MOUNT) $(APP_IMAGE) benchmark --audio /fixture/audio.wav $(BENCHMARK_MODEL_ARGUMENT) $(BENCHMARK_REFERENCE_ARGUMENT) $(BENCHMARK_DRAFT_ARGUMENT)

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
