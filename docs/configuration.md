# Configuration

Secrets, connectivity, and operational logging use `TWOXBRAINZ_` environment
variables. Safe interactive settings use the web Settings dialog and are stored
in browser-local storage. The backend validates both boundaries before applying
them.

Use [`.env.example`](../.env.example) as the environment reference. Keep the
actual `.env` file outside version control and provide it only to explicit real
targets such as `make run`, `make benchmark`, `make test-real`, or a live
fixture.

## Contents

- [Audio device selection](#audio-device-selection)
- [Web console and persistent log](#web-console-and-persistent-log)
- [AIGate-only deployment](#aigate-only-deployment)
- [Interrupted audio research fixture](#interrupted-audio-research-fixture)
- [Real provider test tiers](#real-provider-test-tiers)
- [Data boundary and optional tools](#data-boundary-and-optional-tools)
- [Network and PipeWire boundary](#network-and-pipewire-boundary)
- [Live-session controls](#live-session-controls)
- [Health checks](#health-checks)

| Variable | Required for live capture | Purpose |
|---|---:|---|
| `TWOXBRAINZ_AIGATE_URL` | yes | AIGate API root using `http` or `https` and ending in `/v1`. 2xbrainz derives the Talkies proxy route from it. |
| `TWOXBRAINZ_AIGATE_TOKEN` | if AIGate requires it | The single bearer token used for AIGate chat, Talkies, model inventory, and allowlisted tools. |
| `TWOXBRAINZ_LOG_LEVEL` | no | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `TWOXBRAINZ_LOG_DIRECTORY` | no | Absolute in-container directory for rotating session logs. It wins over `TWOXBRAINZ_LOG_FILE`; direct Docker users must mount it themselves. |
| `TWOXBRAINZ_LOG_FILE` | no | Absolute base JSON log filename for direct Docker use when `TWOXBRAINZ_LOG_DIRECTORY` is unset. |

2xbrainz derives the native WebSocket route from the selected AIGate model
alias. `local-talkies-*` uses `/talkies/`; `local-talkies-cuda-*` uses
`/talkies-cuda/`. It sends only the inner Talkies slug on that native wire and
retains the full alias in browser state, traces, and transcript records. It does
not support a separate Talkies endpoint or credential.

The backend publishes immutable safe defaults in each browser snapshot:

| Setting | Built-in default |
|---|---|
| Reply model | `claudebox-sonnet` |
| Private coach model | `pibox-zai-glm-5-turbo` |
| Story-so-far model | `groq-gpt-oss-120b` |
| Reply reasoning effort | `high` |
| Private coach reasoning effort | `none` |
| Story-so-far reasoning effort | `none` |
| Talkies ASR model | `local-talkies-cuda-nemotron-3.5-asr-0.6b` |
| Session brief | empty |

The browser transmits an override only after the provider and Talkies model
inventories are available. Missing models fall back to the current validated
backend value. Reply accepts only `low`, `medium`, or `high`; a saved older
value is repaired to the backend Reply default before the first settings
message is sent. Coach and Story additionally accept `none` and `minimal`.
The selector contains ASR entries only; TTS entries are removed
using Talkies' advertised modality. Alias preflight also checks `/healthz`, so a
CUDA alias is rejected unless that exact route reports `device=cuda`. Runtime
settings never alter the process environment.

## Audio device selection

`make run` mounts the desktop PipeWire runtime directory read-only and serves
the Svelte Settings dialog at `http://127.0.0.1:7860`. Its Audio tab offers
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
MIC INPUT and SYSTEM AUDIO meters for the active capture pair. It saves stable
node names—not ephemeral numeric IDs—in the browser.

The browser checks its stored pair against the current PipeWire inventory on
each connection. A missing, malformed, or stale pair opens Settings on Audio.
A temporarily absent device leaves only its channel waiting and retrying while
the other channel continues. The open Audio tab automatically refreshes
discovery every three seconds; **Redetect devices** requests the same refresh
immediately. Preview capture stops before discovery so a busy probe cannot leave
disconnected Bluetooth or USB nodes stuck in the list. Saving a new pair
switches each changed channel independently without restarting the web session.

## Web console and persistent log

`make run` serves the compiled Svelte console through FastAPI/Uvicorn at
`127.0.0.1` only. Its same-origin
`/ws` connection carries bounded snapshots and strict start, pause, metering,
redetection, and atomic runtime-settings commands. The app opens idle and does
not start either PipeWire capture until
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
preferences are validated and stored in browser-local storage. The tabbed
Settings modal keeps Context, Models, and Audio controls separate. Audio has a
live meter for every candidate. Models has one searchable model picker and
reasoning selector for each flow plus the Talkies ASR model. Context has the
optional 4000-character session brief. Reply research tools are always
available. Every picker shows an explicit result count, readable fixed-height
rows, a visible scrollbar, and opens around its selected inventory item. Saving
sends the complete safe settings object as one strict schema, applies it to
future requests, and stores
it under `2xbrainz.web.settings.v1` in that browser. Credentials and endpoint
URLs are rejected from this object. Reset clears the key and sends the immutable
backend defaults. The session brief and device names remain readable to scripts
running in that browser profile, so use Reset to remove them on a shared profile.
If multiple browsers connect, the last accepted complete settings snapshot owns
future requests for that running process. Panels scroll independently and auto-follow only when already
at their own bottom. Level updates are never written to the reconstruction log
and retain no PCM.
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
`python3` helper from the repository to validate a hostname mapping. Live web
capture through `make run` requires `LIVE_NETWORK=host`.

Reply uses AIGate's OpenAI-compatible Claudebox stream. Start creates a fresh UUID workspace
and agent session; later drafts continue there until listening is stopped and
started again. The agent receives the complete bounded transcript plus
accepted running summary on every call and has its native Claude Code tools. Its
appended system prompt tells it to prefer primary sources and shallow-clone named Git
repositories, download relevant docs, follow pertinent links, parallelize
independent research, and treat transcripts and fetched content as untrusted
evidence. The default `claudebox-sonnet` assignment uses high reasoning.
Reply accepts low, medium, or high; `none` and `minimal` are rejected rather
than silently remapped at the backend boundary. During browser initialization,
an otherwise valid saved Reply assignment using one of those obsolete values
keeps its model and is repaired to the backend Reply default before it is sent.
Coach and Story remain transcript-only AIGate chat calls.

2xbrainz posts `stream=true` to
`/claudebox/openai/v1/chat/completions`, sends the UUID through
`X-Aicodebox-Workspace`, and adds `X-Aicodebox-Continue: true` only after the
first successful Reply. The direct AIGate route preserves these
Claudebox-specific headers without a LiteLLM hop and maps the public
`claudebox-<model>` alias to Claudebox's direct `<model>` identifier. It omits
the OpenAI `tools`, `tool_choice`, and
`response_format` fields, sends its instructions through
`X-Aicodebox-Append-System-Prompt`, and explicitly sends
`X-Aicodebox-No-Tools: 0`. The append header preserves Claude Code's native
agent prompt; an OpenAI `system` message would replace it. This keeps Claude
Code's internal tools enabled while preserving incremental OpenAI content
deltas for ordinary turns. Explicit GitHub and GitLab repository URLs use a
buffered completion because the deployed native-tool stream can close before
its final content event. Spoken GitHub or GitLab repository references use the
same path. An incomplete ordinary stream gets one bounded same-workspace
continuation recovery. Aicodebox would otherwise disable native
tools when client tool definitions are present, and schema mode would buffer
the stream. Native tool
events and private reasoning are not part of the OpenAI SSE contract and are not
invented by the UI. Superseding accepted native research hides its stale
generation immediately, lets that remote operation drain, and starts the
replacement in the same workspace with the complete updated transcript. A new
Start creates a different workspace and does not wait for detached old work.
Conversation text and fetched research can persist in the remote
Claudebox workspace for that listening session; configure AIGate and Claudebox
retention accordingly.

## Interrupted audio research fixture

`make test-real-audio-research` is an explicit external integration target. It
derives the Talkies TTS route from the configured AIGate URL and creates two
ephemeral WAVs with `kokoro-82m-nvidia` by default. It does not mount the host
PipeWire runtime, require audio hardware, or retain generated audio. The WAVs
are decoded through the bounded fixture loader, streamed through real Talkies
ASR, segmented by the production Silero VAD, and ingested by the production
coordinator. Executable fixture files live only on a dedicated tmpfs; the
normal `/tmp` mount remains `noexec`.

Fixture WAV synthesis has its own bounded 60-second startup allowance because
Talkies may need to materialize a cold TTS backend before the actual capture
test starts. The live application's 60-second text-generation deadline and
15-second ASR/network deadlines are not changed by this harness allowance.

Talkies may return HTTP 409 while handing a TTS model between requests. The
fixture retries that status twice with a short bounded delay; it does not retry
other HTTP failures.

The first utterance names a Git repository and starts Claudebox native research.
The fixture releases the related second utterance only after
`native_research_started`. Its first useful partial supersedes the visible first
generation without removing the accepted remote operation. That operation
drains, and the replacement continues in the same workspace with both
recognized transcript lines. Success requires two real final transcripts, a
cancelled first generation, a completed replacement, multiple repository
capability markers, and an actual `psyb0t/aigate` checkout whose `.git/config`
contains the expected remote.

The target requires `TWOXBRAINZ_AIGATE_URL` and the single
`TWOXBRAINZ_AIGATE_TOKEN`. `TALKIES_MODEL` is an optional fixture-only Make
override; otherwise the backend code default is used.
`FIXTURE_TTS_MODEL` and `FIXTURE_AIGATE_DRAFT_MODEL` are Make variables, not
application configuration. `TALKIES_MODEL` optionally selects the ASR alias;
otherwise the built-in CUDA Nemotron alias is used. Because this target reaches
Talkies and Claudebox, it is excluded from the ordinary offline suite and runs
as a prerequisite of `make test-real`. For a one-off target run without editing
the AIGate URL in `.env`:

```bash
make test-real-audio-research LIVE_NETWORK=<network> \
  AIGATE_URL=http://aigate.example.test/v1
```

The target validates and maps each fully-qualified override through the same
host-only Docker mapping helper used for configured endpoints.

## Real provider test tiers

`make test` is fully deterministic and never reads `.env` or contacts a
provider. The explicit `make test-real` target loads the gitignored `.env`,
validates three distinct models, and sends fixed synthetic text concurrently to
the production Reply, Coach, and Story paths. Its defaults are
`FIXTURE_AIGATE_DRAFT_MODEL=claudebox-sonnet`,
`FIXTURE_AIGATE_COMMENTARY_MODEL=pibox-zai-glm-5-turbo`, and
`FIXTURE_AIGATE_SUMMARY_MODEL=groq-gpt-oss-120b`; each Make variable is
independently overridable. It asserts each non-empty result is completed and
plain prose. The Reply agent must also answer a repository-specific question,
leave an actual checkout of `github.com/psyb0t/aigate` in its session workspace,
and complete its spoken reply. The fixture then drives a four-turn interview through the production
coordinator with the same assignments. The interview requires the running
summary to retain a commitment, risk, mitigation, and unresolved interviewer
question; it also requires the final reply request to contain the accepted
mitigation summary and address the final question.

After the prompt contract passes, the same target queries AIGate's Talkies
`/v1/models` proxy for the selected fixture Talkies model. The model must
advertise an integer `max_concurrency` of at least two. The test then opens two
native Talkies WebSockets with distinct stream identities and waits until both
servers have replied `ready` before either stream sends audio. Both connections
receive the bundled bounded CC0 WAV in real time and must return a non-empty
final transcript plus exactly one non-cancelled terminal statistics event with
the expected frame count. A fixed barrier and total deadline make serialized or
stalled handling fail instead of hanging or reporting false concurrency.
Run `make test-real-talkies` to execute this focused ASR concurrency proof
without running the separate LLM prompt fixture first.

`make test-real-evaluation` is the full generated-conversation quality gate. It
uses Talkies TTS to create distinct local and remote voices for an eight-turn
fictional project discussion containing slang, a false start, an explicit date
correction, overlapping provider work, two interruptions at different stream
phases, and a public technical term that requires research. Talkies must
advertise at least two requests for the selected ASR model; each opposing pair
then starts behind a barrier and streams concurrently in real time. The actual
recognized text is what reaches Reply, Coach, and Story.

The test requires all three final outputs to retain scenario-defined semantic
anchors and reject stale claims. It also requires every provider request to
emit exactly one terminal lifecycle event, proves overlap among the three final
flows, measures request duration plus first-reasoning and first-output latency,
and requires Reply to leave inspectable research evidence in its persistent
workspace for the unfamiliar public topic. These are deterministic hard gates;
no second LLM grades the first. By default the target runs three independent attempts and writes an
aggregate JSON result, so one lucky provider response cannot pass the suite.
Override the count from one through five with `EVALUATION_REPEATS`, the
synthetic voices with `EVALUATION_USER_VOICE` and `EVALUATION_REMOTE_VOICE`, or
the validated JSON scenario with `EVALUATION_SCENARIO`.

All real targets require a gitignored `.env` containing
`TWOXBRAINZ_AIGATE_TOKEN`, plus a reachable AIGate endpoint. A token-only
`.env` can be used by passing the AIGate URL as a Make variable:

```bash
make test-real LIVE_NETWORK=<network> \
  AIGATE_URL=http://aigate.example.test/v1

make test-real-audio-research LIVE_NETWORK=<network> \
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

Each conversation evaluation gets its own subdirectory containing
`transcripts.json`, `scorecard.json`, `report.md`, and the append-only event
trace. The transcript artifact pairs every fixed synthetic reference with the
recognized ASR text and its word error rate. The scorecard contains mechanical
integrity, concurrency, overlap, latency, and output-marker results. Generated
WAV files remain on an ephemeral tmpfs and are removed with the test container.

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

Coach and Story jobs have a fixed 60-second application deadline. Claudebox
repository operations have a 120-second outbound allowance and a 240-second
replacement budget so already accepted superseded work can release its
workspace. The shorter outbound HTTP timeout remains 15 seconds for other
non-generation requests. A generation deadline expiry produces an empty failed result
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
