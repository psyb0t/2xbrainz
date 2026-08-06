# Configuration

All runtime configuration is environment-based and begins with
`TWOXBRAINZ_`. The CLI rejects malformed URLs, URL credentials, query strings,
and relative log paths before it opens any network connection.

Use [`.env.example`](../.env.example) as the field reference. Keep the actual
`.env` file outside version control and provide it only to explicit real
targets such as `make run`, `make benchmark`, `make test-real`, or a live
fixture. `TWOXBRAINZ_AIGATE_MODEL` is additionally required by the optional
`make benchmark-with-draft` target.

## Contents

- [Audio device selection](#audio-device-selection)
- [Web console and persistent log](#web-console-and-persistent-log)
- [AIGate-only deployment](#aigate-only-deployment)
- [Talkies TTS fixture capture](#talkies-tts-fixture-capture)
- [Real provider test tiers](#real-provider-test-tiers)
- [Data boundary and optional tools](#data-boundary-and-optional-tools)
- [Network and PipeWire boundary](#network-and-pipewire-boundary)
- [Live-session controls](#live-session-controls)
- [Health checks](#health-checks)

`make run` validates its configured AIGate model against AIGate's model
inventory before it opens PipeWire capture or Talkies streams. A missing or
unavailable model therefore fails before it can produce a capture-only session
that cannot draft replies. It also verifies that the selected Talkies model is
currently exposed before it opens PipeWire capture, so an unavailable ASR
model cannot silently alter a session's configured transcription path.
After inventory verification, it sends one synthetic all-zero 20 ms PCM frame
through a serial Talkies warm-up stream and requires uncancelled one-frame
terminal statistics. This materializes lazy ASR backends before either
captured stream begins; no captured audio is used for the warm-up.

| Variable | Required for live capture | Purpose |
|---|---:|---|
| `TWOXBRAINZ_TALKIES_MODEL` | yes | One Talkies streaming model slug shared by both streams. |
| `TWOXBRAINZ_AIGATE_URL` | yes | AIGate API root using `http` or `https` and ending in `/v1`. 2xbrainz derives the Talkies WebSocket proxy route from it. |
| `TWOXBRAINZ_AIGATE_MODEL` | conditionally | Legacy first-run fallback for any flow without an explicit model below. A saved browser selection replaces environment defaults on later runs when every saved model remains available. |
| `TWOXBRAINZ_AIGATE_REPLY_MODEL` | conditionally | First-run Reply model. This is the only flow with research and arithmetic tools, so select a fast model with reliable tool calling. |
| `TWOXBRAINZ_AIGATE_COACH_MODEL` | conditionally | First-run Private coach model. Tools remain disabled. |
| `TWOXBRAINZ_AIGATE_SUMMARY_MODEL` | conditionally | First-run Story-so-far model. Tools remain disabled. |
| `TWOXBRAINZ_AIGATE_REASONING_EFFORT` | no | Legacy first-run reasoning fallback for any flow without a dedicated value below. |
| `TWOXBRAINZ_AIGATE_REPLY_REASONING_EFFORT` | no | First-run Reply reasoning: `none`, `minimal`, `low`, `medium`, or `high`. |
| `TWOXBRAINZ_AIGATE_COACH_REASONING_EFFORT` | no | First-run Private coach reasoning, using the same allowed values. |
| `TWOXBRAINZ_AIGATE_SUMMARY_REASONING_EFFORT` | no | First-run Story-so-far reasoning, using the same allowed values. The browser persists every flow's model/effort pair independently. |
| `TWOXBRAINZ_AIGATE_TOKEN` | if AIGate requires it | The single bearer token used for AIGate chat, Talkies proxy, model inventory, and the three allowlisted MCP tools. |
| `TWOXBRAINZ_SESSION_BRIEF` | no | Trusted local context, up to 4000 characters, appended to every generation prompt to frame the call. It is neither transcript data nor status/log output. |
| `TWOXBRAINZ_WEB_RESEARCH_ENABLED` | no | Exactly `true` enables reply-draft search, application-owned public-page reading, and bounded arithmetic. It requires AIGate `SEARXNG=1`; arithmetic additionally requires `PISTON=1`. |
| `TWOXBRAINZ_LOG_LEVEL` | no | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `TWOXBRAINZ_LOG_DIRECTORY` | no | Absolute in-container directory for UTC-prefixed rotating session logs. It wins over `TWOXBRAINZ_LOG_FILE`; direct Docker users must mount it themselves. |
| `TWOXBRAINZ_LOG_FILE` | no | Absolute base JSON log filename for direct Docker use when `TWOXBRAINZ_LOG_DIRECTORY` is unset. `live` prefixes its basename with a UTC session timestamp. |
| `TWOXBRAINZ_AUDIO_CONFIG_FILE` | no | Absolute in-container path for the local microphone/system-output selection. `make run` mounts this at `/audio-config/audio-selection.json`. |

2xbrainz derives `ws(s)://<aigate-host>[/prefix]/talkies/v1/audio/transcriptions/stream`
from `TWOXBRAINZ_AIGATE_URL`. It does not support a separate Talkies endpoint or
credential.

## Audio device selection

`make run` mounts the desktop PipeWire runtime directory read-only and serves
the Svelte Sources view at `http://127.0.0.1:7860`. It offers
non-monitor `Audio/Source` microphone nodes and a system-audio source: either
an `Audio/Source` monitor node (`*.monitor`) or a directly capturable
`Audio/Sink`; application-stream nodes are excluded. Audio Setup shows a
friendly PipeWire description when available, marks the configured default
source and default output as `[DEFAULT]`, and sorts them first. Default is a
fallback recommendation rather than proof of a particular application's current
route. Audio Setup starts one temporary, presentation-only probe for **every
displayed candidate**. Speak and play audio while the list is visible; each
candidate row has its own meter, so the active microphone and system-audio
route can be identified before selection. The dashboard then keeps its own live
MIC INPUT and SYSTEM AUDIO meters for the active capture pair. It
saves stable node names—not ephemeral numeric IDs—to the host's
`$XDG_CONFIG_HOME/2xbrainz/audio-selection.json` (or
`~/.config/2xbrainz/audio-selection.json`) with mode `0600`.

The stored pair is checked against the currently visible PipeWire nodes before
each session. A missing or malformed file opens Sources. A temporarily absent
device leaves only its channel waiting and retrying while the other channel
continues. The open Sources modal automatically refreshes discovery every three
seconds; **Redetect devices** requests the same refresh immediately. Preview
capture stops before discovery so a busy probe cannot leave disconnected
Bluetooth or USB nodes stuck in the list. Saving a new pair switches each
changed channel independently and immediately without restarting the web
session.
The selection file contains only the two node names; it has no audio, transcript,
endpoint, or credential data. Runtime model and reasoning choices are stored in
the same host directory as `provider-selection.json`, also mode `0600`. That
exact-schema file contains separate model and reasoning assignments for Reply,
Private coach, and Story so far. A missing, malformed, symlinked, oversized, or
partly unavailable saved selection falls back as one unit to the environment
flow assignments. Each model and reasoning effort has a dedicated first-run
variable and falls back independently to the corresponding legacy shared value.
Version 1 single-model files migrate by assigning the saved pair to all three
flows.

## Web console and persistent log

`make run` serves the compiled Svelte console through FastAPI/Uvicorn at
`127.0.0.1` only; `make run-web` is a compatibility alias. Its same-origin
`/ws` connection carries bounded snapshots and strict start, pause,
audio-metering, audio-redetection, audio-selection, model, and reasoning-effort
commands. The app opens idle and does not start either PipeWire capture until
**Start listening** is pressed. **Stop listening** pauses capture while keeping
the page and session state alive; process shutdown remains `Ctrl+C` in the
owning terminal.
It does not enable sharing, monitoring, MCP, public APIs, or arbitrary file
paths.
Its fixed status bar shows capture/session state, the active operation, and both
session and operation elapsed timers. A source strip shows selected friendly
capture labels and two derived presentation-only PCM level meters. System-output
meters explicitly capture PipeWire sink monitor ports; a sink target is never
allowed to fall back to the default microphone. In the
browser, Conversation, Reply suggestion, Private coach, and Story so far are
separate scrollable, collapsible, and resizable panels. Expanded guidance
panels consume the full height released by collapsed siblings. Their presentation
preferences are validated and stored in browser-local storage. Source settings
use a modal with a live meter for every candidate; selected source identity is
kept in the application audio-selection file. Panels scroll independently and
auto-follow only when already at their own bottom. Level updates are never
written to the reconstruction log and retain no PCM. The provider-routing panel
has one searchable model picker and reasoning selector for each flow. Every
picker shows an explicit result count, readable fixed-height rows, a visible
scrollbar, and opens around its selected inventory item. Changes affect future
requests for that flow only and persist in the application config directory.
Reply, Private coach, and Story-so-far generation have independent clients and
bounded activity histories. A remote final starts all three concurrently. The
browser renders
incomplete Markdown with Streamdown and keeps one chronological activity history
in each scrollable feed. Status, visible reasoning, tool calls/results, and output
sit inline in arrival order; each reasoning or tool row starts independently
collapsed. Cumulative reasoning/output snapshots coalesce independently per flow
through interleaved parallel generations; tool and lifecycle events remain real
chronological boundaries. The feed auto-follows only while already at the bottom. Prompts,
credentials, and hidden chain-of-thought are never exposed. These activity events are included in the
reconstruction log because tool context can explain a result.

Each `make run` session writes runtime events to a separate file below
`./logs/`, named `<UTC timestamp>_2xbrainz.log`. The session file rolls at 5 MB
and retains at most three numbered backups. Follow the newest session with:

```bash
make logs
```

Set `LOG_TAIL_LINES=<count>` when invoking that target to change its initial
tail length, or `LOG_FILE=<filename>` to follow a specific session. Set
`LOG_DIRECTORY=/absolute/host/path` on both `make run` and `make logs` to use a
different mounted host directory. The log records transcript text, timelines, drafts, commentary,
summaries, lifecycle actions, and diagnostics to make a completed session
reconstructable. It is local but sensitive conversation data. It never records
raw PCM or credential values; credential-shaped fields are redacted.
`make run` forces its host log directory to mode `0700`; direct Docker callers
must supply a mode-`0700` mount. Active and rotated log files are always forced
to mode `0600`, even when the caller has a permissive umask.

At `TWOXBRAINZ_LOG_LEVEL=DEBUG`, the same file includes bounded observability for
every stream boundary: AIGate SSE open/event/close counts, provider activity
retention and per-flow cumulative-chunk coalescing, WebSocket snapshot revisions, and
strict browser acknowledgments for validated snapshots and rendered feeds.
These diagnostics contain finite identifiers and byte/item/character counts,
not authorization headers, prompts, transcript bodies, or arbitrary browser
strings. INFO retains terminal provider results and lifecycle events, not every
full cumulative token snapshot.

## AIGate-only deployment

AIGate keeps Talkies on its private network with no host port binding and
publishes it at `/talkies/`. 2xbrainz uses one gateway URL and one token:

```
TWOXBRAINZ_AIGATE_URL=http://localhost:4000/v1
TWOXBRAINZ_AIGATE_TOKEN=<gateway token>
```

That token is reused by all application requests. Provider selection, remote
access, and any further privacy boundary are AIGate configuration, not a second
2xbrainz provider mode.

`localhost` here assumes the default `LIVE_NETWORK=host`, which reaches the
gateway on the port it already publishes and resolves tailnet names exactly as
the host shell does. Set `LIVE_NETWORK=<name>` for benchmark or fixture
targets to join a specific Docker network, and replace `localhost` with the
gateway's service name on that network. This optional mode uses a host-side
`python3` helper from the repository to validate a hostname mapping. `make
run-web` is loopback-only and therefore requires `LIVE_NETWORK=host`.

When `TWOXBRAINZ_WEB_RESEARCH_ENABLED=true`, the reply path exposes only two
application-owned tools to the model: `research_web` and `execute_code`. The app
permits two tool rounds with at most three calls per round; calls in one round
execute concurrently. Commentary and rolling summaries are transcript-only and
never receive tools. `research_web` requires AIGate `SEARXNG=1`; it accepts a
focused query or an exact discovered URL, downloads public pages in 2xbrainz,
and returns bounded link-preserving Markdown extracted with Trafilatura. Tables,
lists, prose links, raw Markdown, and resolved relative URLs let the reply model
follow a relevant documentation page in its second tool round. It does not
require AIGate's browser MCP. Redirects, credential-bearing URLs, non-public DNS
destinations, unsupported content, and oversized bodies are rejected.
`execute_code` accepts only application-validated bounded numeric arithmetic and
requires AIGate `PISTON=1`. Tool failures return a structured bounded reason and
remain visible in the Reply activity stream; the draft may still complete.
Before a search leaves the process, the application rejects obvious structured
private identifiers including email addresses, URLs, social handles, and
phone/account-like digit runs. Operators must still avoid placing private names
or other unstructured personal details in search-enabled conversations.

## Talkies TTS fixture capture

`make live-fixture` is an explicit external integration target. It derives
the direct Talkies TTS route from the configured AIGate URL, creates two
ephemeral WAV fixtures with `kokoro-82m-nvidia` by default, and runs the real
`live` command against two harness-owned `pw-record` fixture devices. It does
not mount the host PipeWire runtime directory, require audio hardware, retain
the generated WAVs, or print their speech or transcripts. Its executable
fixture files live only on a dedicated ephemeral executable tmpfs; the normal
`/tmp` mount remains `noexec`. The target writes one durable JSONL
reconstruction trace below `.testing/fixture-traces/`; it contains only the
fixed synthetic test text, model output derived from it, lifecycle events, and
the production CLI's JSON records and structured runtime diagnostics. It never
contains audio bytes, host PipeWire node names, or credential values; the
harness-owned fixture node labels may appear so the playback path is auditable.

Fixture WAV synthesis has its own bounded 60-second startup allowance because
Talkies may need to materialize a cold TTS backend before the actual capture
test starts. The live application's 60-second text-generation deadline and
15-second ASR/network deadlines are not changed by this harness allowance.

Talkies may return HTTP 409 while handing a TTS model between requests. The
fixture retries that status twice with a short bounded delay; it does not retry
other HTTP failures.

The default `FIXTURE_AUDIO_SCENARIO=overlap` starts the local-user WAV first,
then begins shorter remote audio while local speech remains active. It requires
both final transcripts and timeline entries, positive capture-timing
comparisons, completed commentary and summary, and no reply draft. This tests
the overlap rule through the production `PipeWireSource`, two native Talkies
streams, and coordinator. The target uses an in-process protocol-conforming
AIGate fixture so that audio timing and the reply-suppression assertion remain
deterministic.

Set `FIXTURE_AUDIO_SCENARIO=interview`, or run `make
live-interview-fixture`, to drive four alternating turns: initial local
commitment and risk, interviewer follow-up, local mitigation, and final
interviewer verification question. The fixture emits silence between generated
WAV utterances so native streaming ASR must finalize each turn. It requires the
four finalized timelines in role order, a draft before and after the mitigation,
the final draft tied to the final interviewer turn, and a final summary that
retains the commitment, deadline, risk, mitigation, staging evidence, and
unresolved question. When a selected native ASR backend produces a final only
after `end`, the runtime keeps PipeWire capture open but rotates its Talkies
segment after the local Silero detector observes sustained speech followed by
sustained silence. Hysteresis rejects transient background noise, and a
60-second maximum rotates a segment even when silence never arrives. Each new
segment waits for neural speech detection and has a distinct logical ASR
identity, so the configured Nemotron backend can produce independent
multi-turn finals without holding an idle connection or recreating either
capture process.

The target requires `TWOXBRAINZ_AIGATE_URL`, `TWOXBRAINZ_TALKIES_MODEL`, and
the single `TWOXBRAINZ_AIGATE_TOKEN`.
`FIXTURE_TTS_MODEL` and `FIXTURE_TTS_VOICE` are Make variables, not application
configuration; use them only to select an available Talkies TTS model and voice
for this test. Because it reaches the configured Talkies service,
`live-fixture` is intentionally excluded from the ordinary offline test suite.
For a one-off direct AIGate test without editing `.env`, pass only the AIGate
base URL as a Make variable:

```bash
make live-fixture LIVE_NETWORK=<network> \
  AIGATE_URL=http://aigate.example.test/v1
```

The target validates and maps each fully-qualified override through the same
host-only Docker mapping helper used for configured endpoints.

## Real provider test tiers

`make test` is fully deterministic and never reads `.env` or contacts a
provider. The explicit `make test-real` target loads the gitignored `.env`,
validates three distinct models, and sends fixed synthetic text concurrently to
the production Reply, Coach, and Story paths. Its defaults are
`FIXTURE_AIGATE_DRAFT_MODEL=cerebras-glm-4.7`,
`FIXTURE_AIGATE_COMMENTARY_MODEL=claudebox-sonnet`, and
`FIXTURE_AIGATE_SUMMARY_MODEL=pibox-zai-glm-5-turbo`; each Make variable is
independently overridable. It asserts each non-empty result is completed and
plain prose. The Reply model must also autonomously call `research_web` for a
synthetic public-documentation question, consume the fetched Markdown, and
complete its spoken reply. The fixture then drives a four-turn interview through the production
coordinator with the same assignments. The interview requires the running
summary to retain a commitment, risk, mitigation, and unresolved interviewer
question; it also requires the final reply request to contain the accepted
mitigation summary and address the final question.

After the prompt contract passes, the same target queries AIGate's Talkies
`/v1/models` proxy for the selected `TWOXBRAINZ_TALKIES_MODEL`. The model must
advertise an integer `max_concurrency` of at least two. The test then opens two
native Talkies WebSockets with distinct stream identities and waits until both
servers have replied `ready` before either stream sends audio. Both connections
receive the bundled bounded CC0 WAV in real time and must return a non-empty
final transcript plus exactly one non-cancelled terminal statistics event with
the expected frame count. A fixed barrier and total deadline make serialized or
stalled handling fail instead of hanging or reporting false concurrency.
Run `make test-real-talkies` to execute this focused ASR concurrency proof
without running the separate LLM prompt fixture first.

`make live-product-fixture` runs the four-turn synthetic-audio interview with
real AIGate rather than the fixture gateway. It releases each opposing turn
only after the required preceding provider result completes, so the production
coordinator must preserve the whole story, supersede the first draft after the
local mitigation, and create a final current draft and summary. It uses the
same default PIBOX GLM model and accepts the same `FIXTURE_AIGATE_MODEL`
override. It is a compatibility gate: a backend must produce a terminal final
when the runtime closes a bounded utterance segment; otherwise the retained
trace records the failure instead of claiming a completed interview.

Both targets require a gitignored `.env` containing
`TWOXBRAINZ_AIGATE_TOKEN`, plus a reachable AIGate endpoint. A token-only
`.env` can be used by passing the AIGate URL as a Make variable:

```bash
make test-real LIVE_NETWORK=<network> \
  AIGATE_URL=http://aigate.example.test/v1

make live-product-fixture LIVE_NETWORK=<network> \
  AIGATE_URL=http://aigate.example.test/v1
```

Every real fixture creates a new append-only JSONL trace in the gitignored
`.testing/fixture-traces/` directory. The trace records ordered elapsed timing,
fixture actions, TTS attempts, production CLI events, provider request context,
provider results, structured runtime diagnostics, and assertion outcomes. On
failure it also records the redacted terminal error type and message. It
redacts fields named like a token, secret, API key, authorization value,
password, or cookie, and replaces configured credential values appearing in
text. It is required for a successful real fixture run, so a missing or
unwritable trace directory fails the target rather than producing unverifiable
success.

## Data boundary and optional tools

2xbrainz sends speaker-tagged transcript text, never raw audio, to its one
configured AIGate. `2xbrainz doctor`, `2xbrainz status`, and the initial
live-session record report the AIGate model but never token values. Configure
the upstream model provider and transcript egress policy in AIGate itself.

Reply drafts, commentary, and summaries use fixed application-owned token and
text-length budgets. AIGate content that exceeds the matching budget is
rejected before the web console can render it; the budgets are not provider-controlled
configuration. The reply budget is 1,024 completion tokens so reasoning-capable
models have room to return visible spoken text. Commentary and summary also
receive 1,024 completion tokens so hidden reasoning does not consume their whole
budget; separate character limits still bound what the application accepts.

Reply-draft content is also a strict display boundary: it must be one line of
plain spoken prose. The provider boundary parses CommonMark without rendering
HTML, converts inline emphasis, code, and link labels to visible text, and
discards link destinations. It rejects headings, lists, block quotes, fenced
code, HTML, and line breaks rather than presenting them as a draft. A rejected
reply becomes a failed draft record with no provider text rendered. Commentary
and summaries may contain multiple sentences, but they use the same text-only
parser boundary before entering CLI state.

Each text-generation job also has a fixed 60-second application deadline. The
shorter outbound HTTP timeout remains 15 seconds for non-generation requests.
A generation deadline expiry produces an empty failed result
for the current transcript revision and leaves capture plus later turns
running. The deadline is not an environment setting: changing it requires a
code change and matching operational measurement.

Reply guidance may offer a relevant mechanism as a clearly tentative proposal.
It cannot describe that proposal as implemented, tested, or committed, and it
cannot invent a date, deadline, evidence, result, or status.

Some reasoning providers occasionally return a successful completion with no
visible text. The client retries that narrow condition once within the same
generation deadline. A second blank completion, malformed response, or
transport failure remains a typed failure; tool execution is never repeated.

## Network and PipeWire boundary

`make run` does not publish any TCP port. It joins the `LIVE_NETWORK` chosen
by the operator so the app can resolve Talkies and AIGate by their service
names. The container drops all Linux capabilities, runs read-only apart from a
temporary filesystem, defaults to an eight-CPU, 1 GiB, 128-process cgroup
budget, and mounts the host PipeWire runtime directory read-only.

That budget is a ceiling, not a measured requirement. No transcription runs in
this container: it starts two `pw-record` captures, normalizes the PCM, and writes
it to a WebSocket, so `RUNTIME_CPUS`, `RUNTIME_MEMORY`, and `RUNTIME_PIDS` have
no bearing on ASR latency. The transcription cost belongs to Talkies, in its own
container, and is where `make benchmark` measurements apply.

For a fully-qualified AIGate or Talkies hostname that resolves on the host but
not inside Docker, such as a tailnet-only name, `make run` and `make benchmark`
resolve its IPv4 address on the host and pass one validated Docker host mapping.
That helper derives the mapping from the endpoint hostname only; it never reads
token settings or emits URL paths. If the hostname cannot be resolved by the
host, attach the CLI to an explicit bridge network with the required DNS
instead.

Under the default `LIVE_NETWORK=host`, no mapping helper or host Python is used:
the container shares the host's resolver, so tailnet and other host-only names
already resolve. The validated mapping helper runs only when a named Docker
network is explicitly selected.

The mounted PipeWire socket is only usable when the container runs with the
same UID as the host desktop session. `make run` does that automatically.

## Live-session controls

`make run` exposes lifecycle controls through the browser:

- **Stop listening** — blocks future audio frames before the Talkies boundary and cancels
  active drafting, commentary, and summary work.
- **Start listening** — starts or resumes the two independently supervised
  capture channels.

The page cannot terminate its own server. Use `Ctrl+C` in the owning shell to
end the process.

Each lifecycle change updates the dashboard and writes a structured `session`
record with `state`, `action`, and `changed` to the rotating log. Guidance is
display-only: it is never sent or spoken, has no accept/dismiss/edit/regenerate
state, and is never included in later provider requests. Invalid or oversized
lines show fixed control help, log a fixed `control_error` record, and do not
echo the input. EOF alone leaves the session running.

The ordinary `draft` record is a lifecycle record. A remote final first emits
`status: "running"` with empty text, followed by exactly one terminal status:
`completed` with text, or `failed`, `cancelled`, or `superseded` with empty
text. A completed draft remains visible only until a later current-state draft
replaces it.

When a capture stream closes, the CLI emits one `capture_stats` record with its
speaker role, frame count, accepted PCM duration, and aggregate gap count/max.
It never includes frame bytes, a PipeWire node identifier, session ID, or
transcript text. Talkies terminal `asr_stats` records similarly contain only
role, selected model, accepted audio duration, frame count, and cancellation
state.

At live-session shutdown, the CLI also emits one `capture_drift` record. It
contains only the count of matching user/remote frame-sequence comparisons,
the largest startup-offset-adjusted absolute drift, and a bounded unmatched
frame count. It never includes frame bytes, stream IDs, session IDs, or device
metadata.

## Health checks

`make doctor` executes `2xbrainz doctor`, which prints a sanitized JSON view of
the selected AIGate model, configured endpoints, and whether credentials are
present. It never prints token values. `2xbrainz status` produces the same safe
configuration view.

The replay route needs no external service:

```bash
make replay
```

It is the appropriate clean-machine smoke test for the container image and
coordination state machine. Live Talkies and AIGate connectivity are validated
separately by the operator's selected Docker network and model configuration.
