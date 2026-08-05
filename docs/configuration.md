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
| `TWOXBRAINZ_AIGATE_MODEL` | yes | Model name configured by the AIGate gateway. |
| `TWOXBRAINZ_AIGATE_REASONING_EFFORT` | no | Initial reasoning effort: `none`, `minimal`, `low`, `medium`, or `high`. It can be changed in the browser for future requests. |
| `TWOXBRAINZ_AIGATE_TOKEN` | if AIGate requires it | The single bearer token used for AIGate chat, Talkies proxy, model inventory, and the two allowlisted MCP tools. |
| `TWOXBRAINZ_SESSION_BRIEF` | no | Trusted local context, up to 4000 characters, appended to every generation prompt to frame the call. It is neither transcript data nor status/log output. |
| `TWOXBRAINZ_WEB_RESEARCH_ENABLED` | no | Exactly `true` enables reply-draft web search and bounded arithmetic through AIGate MCP. It requires AIGate SearXNG; calculation additionally requires Piston. |
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
continues. **Redetect devices** refreshes discovery after Bluetooth or USB
changes. Saving a new pair switches each changed channel independently and
immediately without restarting the web session.
The selection file contains only the two node names; it has no audio, transcript,
endpoint, or credential data.

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
separate scrollable, collapsible, and resizable panels. Their presentation
preferences are validated and stored in browser-local storage. Source settings
use a modal with a live meter for every candidate; selected source identity is
kept in the application audio-selection file. Panels scroll independently and
auto-follow only when already at their own bottom. Level updates are never
written to the reconstruction log and retain no PCM. The model selector and
reasoning selector affect future requests only. The bounded provider activity
trail reports request and allowlisted-tool phases without prompts, tool payloads,
results, credentials, or private hidden reasoning.

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
application-owned tools to the model: `search_web` and `execute_code`. The app
executes at most three requested calls concurrently through AIGate MCP, bounds
the returned data, then calls the LLM once more for the spoken draft. Commentary
and rolling summaries are transcript-only and never receive tools. `search_web`
requires AIGate SearXNG; `execute_code` accepts only application-validated,
bounded numeric arithmetic and requires the AIGate Piston service. If either
MCP service is unavailable, the model gets a generic
tool-unavailable result and the draft still completes without it.
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
uses `FIXTURE_AIGATE_MODEL` (default: `pibox-zai-glm-5-turbo`), validates the
configured model inventory, and sends only fixed synthetic text to the reply,
commentary, and summary prompts. It asserts each non-empty result is completed
and plain prose, then drives a four-turn interview through the production
coordinator. The interview requires the running summary to retain a commitment,
risk, mitigation, and unresolved interviewer question; it also requires the
final reply request to contain the accepted mitigation summary and address the
final question. It does not contact Talkies.

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
