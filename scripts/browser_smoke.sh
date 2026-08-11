#!/bin/bash
set -euo pipefail

usage() {
	printf 'Usage: make test-browser\n'
}

if (($# > 0)); then
	if [[ "$1" == "--help" && $# -eq 1 ]]; then
		usage
		exit 0
	fi
	usage >&2
	exit 2
fi

log() {
	local level="$1"
	shift
	printf '{"time":"%s","level":"%s","file":"%s","line":%d,"func":"%s","msg":"%s"}\n' \
		"$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
		"$level" \
		"${BASH_SOURCE[1]##*/}" \
		"${BASH_LINENO[0]}" \
		"${FUNCNAME[1]:-main}" \
		"$*" >&2
}

trap 'status=$?; log ERROR "browser smoke command failed exit=$status"' ERR

project_root="${BROWSER_TEST_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
run_id="${BROWSER_TEST_RUN_ID:-$(date -u '+%Y%m%dT%H%M%SZ')-$$}"
app_image="${BROWSER_TEST_APP_IMAGE:-2xbrainz-browser-test:local}"
browser_image="${BROWSER_TEST_IMAGE:-psyb0t/stealthy-auto-browse@sha256:0dab459e28c8872ea2f54048f91c2b3aae10b43b7e1ec44d9c56aca8d456169d}"
fixture_port="${BROWSER_TEST_FIXTURE_PORT:-17860}"
browser_api_port="${BROWSER_TEST_API_PORT:-18080}"
browser_vnc_port="${BROWSER_TEST_VNC_PORT:-15900}"
fixture_container="2xbrainz-browser-fixture-${run_id}"
browser_container="2xbrainz-browser-check-${run_id}"
log_file="${BROWSER_TEST_LOG_FILE:-/tmp/2xbrainz-browser-smoke-${run_id}.log}"
fixture_log_directory="${BROWSER_TEST_FIXTURE_LOG_DIRECTORY:-$project_root/.testing/browser-smoke/$run_id}"
fixture_log_file="$fixture_log_directory/stream-observability.jsonl"
picker_screenshot_file="$fixture_log_directory/model-picker-open.png"
feed_screenshot_file="$fixture_log_directory/provider-feed.png"
fixture_script="$project_root/tests/integration/browser_console_fixture.py"
fixture_url="http://127.0.0.1:${fixture_port}/"
browser_api_url="http://127.0.0.1:${browser_api_port}"

exec > >(tee -a "$log_file") 2>&1

validate_port() {
	local name="$1"
	local value="$2"
	if [[ ! "$value" =~ ^[0-9]+$ ]] || ((value < 1024 || value > 65535)); then
		log ERROR "$name must be an integer from 1024 through 65535"
		return 1
	fi
}

require_command() {
	local command_name="$1"
	if ! command -v "$command_name" >/dev/null 2>&1; then
		log ERROR "required command is unavailable: $command_name"
		return 1
	fi
}

stop_owned_container() {
	local container_name="$1"
	local expected_image="$2"
	local actual_image
	if ! actual_image="$(docker container inspect --format '{{.Config.Image}}' "$container_name" 2>/dev/null)"; then
		return 0
	fi
	if [[ "$actual_image" != "$expected_image" ]]; then
		log ERROR "refusing cleanup for $container_name because its image is not owned by this run"
		return 1
	fi
	if ! docker stop --time 5 "$container_name" >/dev/null; then
		log ERROR "failed to stop owned container $container_name"
		return 1
	fi
	log INFO "stopped owned container $container_name"
}

remove_test_image() {
	if ! docker image inspect "$app_image" >/dev/null 2>&1; then
		return 0
	fi
	if ! docker image rm "$app_image" >/dev/null; then
		log ERROR "failed to remove browser fixture image $app_image"
		return 1
	fi
	log INFO "removed browser fixture image $app_image"
}

cleanup() {
	local status=$?
	local cleanup_status=0
	trap - EXIT INT TERM
	stop_owned_container "$browser_container" "$browser_image" || cleanup_status=$?
	stop_owned_container "$fixture_container" "$app_image" || cleanup_status=$?
	remove_test_image || cleanup_status=$?
	if ((status == 0 && cleanup_status != 0)); then
		status=$cleanup_status
	fi
	exit "$status"
}

validate_fixture_log() {
	python3 - "$fixture_log_file" <<'PY'
import json
import sys

records = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
messages = [record["msg"] for record in records]
required = {
    "fake AIGate SSE response started",
    "AIGate SSE event received",
    "provider activity emitted",
    "provider activity retained",
    "web console snapshot streamed",
    "frontend stream diagnostic received",
    "fake AIGate browser flow completed",
    "manual browser dispatch accepted",
}
missing = required.difference(messages)
assert not missing, f"fixture log is missing stream diagnostics: {sorted(missing)}"
frontend_events = {
    record.get("frontend_event")
    for record in records
    if record["msg"] == "frontend stream diagnostic received"
}
assert {"snapshot_received", "provider_feed_rendered"}.issubset(frontend_events)
phases = [
    record.get("phase")
    for record in records
    if record["msg"] == "provider activity emitted"
]
for phase in (
    "reasoning_streaming",
    "tool_started",
    "tool_completed",
    "output_streaming",
    "request_completed",
):
    assert phase in phases
serialized = "\n".join(json.dumps(record) for record in records)
assert "<unk>" not in serialized
assert "WeWe" not in serialized
PY
}

handle_signal() {
	log WARN "browser smoke interrupted"
	exit 130
}

container_http_request() {
	local network_owner="$1"
	local method="$2"
	local url="$3"
	local timeout_seconds="$4"
	docker run --rm --init --interactive \
		--network "container:$network_owner" \
		--read-only \
		--tmpfs /tmp:rw,noexec,nosuid,size=16m \
		--user "$(id -u):$(id -g)" \
		--cap-drop ALL \
		--security-opt no-new-privileges:true \
		--memory 128m \
		--cpus 0.5 \
		--pids-limit 64 \
		--entrypoint python \
		"$app_image" \
		-c '
import sys
from urllib.request import Request, urlopen

method, url, timeout_text = sys.argv[1:]
body = sys.stdin.buffer.read() if method == "POST" else None
headers = {"Content-Type": "application/json"} if body is not None else {}
request = Request(url, data=body, headers=headers, method=method)
with urlopen(request, timeout=float(timeout_text)) as response:
    sys.stdout.buffer.write(response.read())
' "$method" "$url" "$timeout_seconds"
}

wait_for_url() {
	local network_owner="$1"
	local url="$2"
	local attempts="$3"
	local attempt
	for ((attempt = 1; attempt <= attempts; attempt++)); do
		if container_http_request "$network_owner" GET "$url" 3 </dev/null >/dev/null 2>&1; then
			return 0
		fi
		sleep 1
	done
	log ERROR "service did not become ready at $url"
	return 1
}

validate_response() {
	python3 -c '
import json
import sys

response = json.load(sys.stdin)
assert response.get("success") is True, f"browser request failed: {response!r}"
data = response.get("data")
assert isinstance(data, dict), f"browser response data is invalid: {response!r}"
failure = data.get("step_results", data)
assert data.get("success") is True, f"browser script failed: {failure!r}"
outputs = data.get("outputs")
assert isinstance(outputs, dict), f"browser script returned no outputs: {data!r}"
result = outputs["ui"]["result"]
assert result["appShell"] is True
assert result["connected"] is True
assert result["providerFeeds"] == 4
assert result["providerAssignments"] == 4
assert result["modelFilter"] is True
assert result["settingsModalBounded"] is True
assert result["settingsTabs"] == 3
assert result["modelListScrollable"] is True
assert result["modelOptionReadable"] is True
assert result["modelSelectedInView"] is True
assert result["modelResultCount"] == "120 of 120"
assert result["persistedDraftModel"] == "claudebox-provider-example-model-001", result
assert result["persistedDraftReasoning"] == "minimal", result
assert result["selectedDraftModel"] == "claudebox-provider-example-model-001", result
assert result["generationCards"] == 0
assert result["replyItems"] == 6
assert result["collapsedTraceRows"] is True
assert result["replyText"] == "Start at the gateway, then follow validation and routing."
assert result["allFeedsIdle"] is True
assert result["coachCancelled"] is True
assert result["storyFailed"] is True
assert result["failureReasonVisible"] is True
assert result["storyResponses"] == 2
assert result["researchCompleted"] is True, result
assert result["manualSendEnabled"] is True, result
assert result["streamOrder"] == [
    "stream-status",
    "stream-event",
    "stream-event",
    "stream-status",
    "stream-event",
    "stream-response",
]
assert result["cleanText"] is True
'
}

validate_console_response() {
	python3 -c '
import json
import sys

response = json.load(sys.stdin)
assert response["success"] is True
entries = response["data"]["log"]
errors = [entry for entry in entries if entry.get("type") == "error"]
assert not errors, f"browser console errors: {errors}"
'
}

validate_reset_response() {
	python3 -c '
import json
import sys

response = json.load(sys.stdin)
assert response.get("success") is True, f"browser request failed: {response!r}"
data = response.get("data")
assert isinstance(data, dict), f"browser response data is invalid: {response!r}"
failure = data.get("step_results", data)
assert data.get("success") is True, f"browser reset script failed: {failure!r}"
outputs = data.get("outputs")
assert isinstance(outputs, dict), f"browser reset returned no outputs: {data!r}"
result = outputs["reset"]["result"]
assert result["settingsCleared"] is True
'
}

validate_png() {
	python3 - "$1" <<'PY'
import sys

with open(sys.argv[1], "rb") as screenshot:
    assert screenshot.read(8) == b"\x89PNG\r\n\x1a\n"
PY
}

require_command docker
require_command python3
[[ "$run_id" =~ ^[A-Za-z0-9._-]+$ ]] || {
	log ERROR "BROWSER_TEST_RUN_ID contains unsupported characters"
	exit 2
}
[[ -f "$fixture_script" ]] || {
	log ERROR "browser fixture script is missing"
	exit 2
}
validate_port BROWSER_TEST_FIXTURE_PORT "$fixture_port"
validate_port BROWSER_TEST_API_PORT "$browser_api_port"
validate_port BROWSER_TEST_VNC_PORT "$browser_vnc_port"
if [[ "$fixture_port" == "$browser_api_port" || "$fixture_port" == "$browser_vnc_port" || "$browser_api_port" == "$browser_vnc_port" ]]; then
	log ERROR "browser smoke ports must be distinct"
	exit 2
fi
install -d -m 700 "$fixture_log_directory"

trap cleanup EXIT
trap handle_signal INT TERM

docker build --file "$project_root/Dockerfile" --tag "$app_image" "$project_root"
docker run --detach --rm --init \
	--name "$fixture_container" \
	--read-only \
	--tmpfs /tmp:rw,noexec,nosuid,size=64m \
	--cap-drop ALL \
	--security-opt no-new-privileges:true \
	--memory 512m \
	--cpus 1.0 \
	--pids-limit 128 \
	--volume "$fixture_script:/fixture/browser_console_fixture.py:ro" \
	--volume "$fixture_log_directory:/fixture-logs:rw" \
	--user "$(id -u):$(id -g)" \
	--entrypoint python \
	"$app_image" \
	/fixture/browser_console_fixture.py \
	--port "$fixture_port" \
	--log-file /fixture-logs/stream-observability.jsonl >/dev/null
wait_for_url "$fixture_container" "$fixture_url" 20

docker run --detach --rm --init \
	--name "$browser_container" \
	--network "container:$fixture_container" \
	--security-opt no-new-privileges:true \
	--memory 3g \
	--cpus 2.0 \
	--pids-limit 512 \
	--env "HTTP_LISTEN_HOST=127.0.0.1" \
	--env "HTTP_LISTEN_PORT=$browser_api_port" \
	--env "VNC_LISTEN_HOST=127.0.0.1" \
	--env "VNC_LISTEN_PORT=$browser_vnc_port" \
	--env "USE_VIEWPORT=true" \
	--env "XVFB_RESOLUTION=1440x820" \
	"$browser_image" >/dev/null
wait_for_url "$browser_container" "$browser_api_url/health" 60

printf '%s' '{"action":"enable_console_log"}' |
	container_http_request "$browser_container" POST "$browser_api_url" 30 >/dev/null

payload="$(python3 -c '
import json
import sys

target = sys.argv[1]
print(json.dumps({
    "action": "run_script",
    "name": "2xbrainz-browser-smoke",
    "steps": [
        {"action": "goto", "url": target, "wait_until": "domcontentloaded"},
        {
            "action": "eval",
            "expression": "(() => { localStorage.setItem(\"2xbrainz.web.settings.v1\", JSON.stringify({ schemaVersion: 2, providers: { draft: { model: \"claudebox-provider-example-model-087\", reasoningEffort: \"minimal\" }, commentary: { model: \"claudebox-provider-example-model-087\", reasoningEffort: \"medium\" }, summary: { model: \"claudebox-provider-example-model-087\", reasoningEffort: \"medium\" }, research: { model: \"claudebox-provider-example-model-087\", reasoningEffort: \"high\" } }, talkiesModel: \"fixture-asr\", sessionBrief: \"\", webResearchEnabled: true, autoDispatchEnabled: false, microphoneNode: \"fixture-mic\", systemNode: \"fixture-system\" })); return true; })()"
        },
        {"action": "goto", "url": target, "wait_until": "domcontentloaded"},
        {"action": "sleep", "duration": 2},
        {"action": "click", "selector": ".research-card .collapse-button"},
        {"action": "click", "selector": ".header-actions .send-button"},
        {"action": "click", "selector": ".header-actions .quiet-button"},
        {"action": "click", "selector": ".settings-tabs button:nth-child(3)"},
        {"action": "click", "selector": ".settings-tabs button:nth-child(1)"},
        {"action": "click", "selector": ".settings-tabs button:nth-child(2)"},
        {"action": "click", "selector": ".provider-assignment .model-picker-trigger"},
        {"action": "eval", "expression": "(() => { const input = document.querySelector(\"input[aria-label=\\\"Filter models\\\"]\"); if (!(input instanceof HTMLInputElement)) throw new Error(\"model filter missing\"); input.value = \"model-001\"; input.dispatchEvent(new Event(\"input\", { bubbles: true })); return true; })()"},
        {"action": "sleep", "duration": 0.5},
        {"action": "click", "selector": ".model-options button"},
        {"action": "click", "selector": ".modal-footer .primary-button"},
        {"action": "goto", "url": target, "wait_until": "domcontentloaded"},
        {"action": "sleep", "duration": 2},
        {"action": "click", "selector": ".header-actions .quiet-button"},
        {"action": "click", "selector": ".settings-tabs button:nth-child(2)"},
        {"action": "click", "selector": ".provider-assignment .model-picker-trigger"},
        {"action": "sleep", "duration": 0.5},
        {
            "action": "eval",
            "expression": "(() => { const modal = document.querySelector(\".settings-modal\"); const rect = modal?.getBoundingClientRect(); const list = document.querySelector(\".model-options\"); const listRect = list?.getBoundingClientRect(); const listStyle = list ? getComputedStyle(list) : null; const option = document.querySelector(\".model-options button\"); const optionRect = option?.getBoundingClientRect(); const optionStyle = option ? getComputedStyle(option) : null; const selected = document.querySelector(\".model-options button.selected\"); const selectedRect = selected?.getBoundingClientRect(); const feed = document.querySelector(\".reply-card .provider-feed\"); const rows = [...document.querySelectorAll(\".reply-card .stream-event\")]; const output = document.querySelector(\".reply-card .stream-response\")?.textContent?.trim() ?? \"\"; const text = feed?.textContent ?? \"\"; const saved = JSON.parse(localStorage.getItem(\"2xbrainz.web.settings.v2\") ?? \"null\"); const research = document.querySelector(\".research-card\")?.textContent ?? \"\"; return { appShell: !!document.querySelector(\".app-shell\"), connected: !!document.querySelector(\".connection-dot.online\"), providerFeeds: document.querySelectorAll(\".provider-feed\").length, providerAssignments: document.querySelectorAll(\".provider-assignment\").length, settingsTabs: document.querySelectorAll(\".settings-tabs button\").length, modelFilter: !!document.querySelector(\"input[aria-label=\\\"Filter models\\\"]\"), settingsModalBounded: !!rect && rect.width < innerWidth && rect.height < innerHeight, modelListScrollable: !!list && !!listStyle && list.scrollHeight > list.clientHeight && listStyle.overflowY === \"scroll\", modelOptionReadable: !!optionRect && !!optionStyle && optionRect.height >= 36 && parseFloat(optionStyle.fontSize) >= 14, modelSelectedInView: !!selectedRect && !!listRect && selectedRect.top >= listRect.top && selectedRect.bottom <= listRect.bottom, modelResultCount: document.querySelector(\".model-search .model-picker-heading span\")?.textContent?.trim() ?? \"\", persistedDraftModel: saved?.providers?.draft?.model ?? \"\", persistedDraftReasoning: saved?.providers?.draft?.reasoningEffort ?? \"\", selectedDraftModel: document.querySelector(\".provider-assignment .model-picker-trigger\")?.textContent?.trim() ?? \"\", generationCards: document.querySelectorAll(\".generation-entry\").length, replyItems: document.querySelectorAll(\".reply-card .stream-item\").length, collapsedTraceRows: rows.length === 3 && rows.every((row) => !row.open), replyText: output, allFeedsIdle: document.querySelectorAll(\".provider-feed .streaming\").length === 0, coachCancelled: !!document.querySelector(\".coach-card .stream-status.cancelled\"), storyFailed: !!document.querySelector(\".story-card .stream-status.failed\"), failureReasonVisible: document.querySelector(\".story-card\")?.textContent?.includes(\"fixture provider deadline exceeded\") === true, storyResponses: document.querySelectorAll(\".story-card .stream-response\").length, researchCompleted: research.includes(\"Verified the gateway flow\") && !!document.querySelector(\".research-card .stream-tool\"), manualSendEnabled: !document.querySelector(\".header-actions .send-button\")?.disabled, streamOrder: [...(feed?.children ?? [])].map((item) => item.classList[0]), cleanText: !text.includes(\"WeWe\") && !text.includes(\"<unk>\") }; })()",
            "output_id": "ui",
        },
    ],
}))
' "$fixture_url")"
response="$(printf '%s' "$payload" |
	container_http_request "$browser_container" POST "$browser_api_url" 60)"
validate_response <<<"$response"
container_http_request \
	"$browser_container" \
	GET \
	"$browser_api_url/screenshot/browser?whLargest=512" \
	30 \
	</dev/null >"$picker_screenshot_file"
validate_png "$picker_screenshot_file"
reset_payload="$(python3 -c '
import json

print(json.dumps({
    "action": "run_script",
    "name": "2xbrainz-browser-reset-smoke",
    "steps": [
        {"action": "click", "selector": ".model-options button.selected"},
        {"action": "click", "selector": ".modal-footer .quiet-button"},
        {"action": "click", "selector": "button[aria-label=\"Close settings\"]"},
        {
            "action": "eval",
            "expression": "(() => ({ settingsCleared: localStorage.getItem(\"2xbrainz.web.settings.v1\") === null }))()",
            "output_id": "reset",
        },
    ],
}))
')"
reset_response="$(printf '%s' "$reset_payload" |
	container_http_request "$browser_container" POST "$browser_api_url" 30)"
validate_reset_response <<<"$reset_response"
container_http_request \
	"$browser_container" \
	GET \
	"$browser_api_url/screenshot/browser?whLargest=512" \
	30 \
	</dev/null >"$feed_screenshot_file"
validate_png "$feed_screenshot_file"
console_response="$(printf '%s' '{"action":"get_console_log"}' |
	container_http_request "$browser_container" POST "$browser_api_url" 30)"
validate_console_response <<<"$console_response"
validate_fixture_log
log INFO "browser smoke passed picker_screenshot=$picker_screenshot_file feed_screenshot=$feed_screenshot_file"
