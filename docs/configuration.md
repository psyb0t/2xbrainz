# Configuration

All runtime configuration is environment-based and begins with
`TWOXBRAINZ_`. The CLI rejects malformed URLs, URL credentials, query strings,
and relative log paths before it opens any network connection.

Use [`.env.example`](../.env.example) as the field reference. Keep the actual
`.env` file outside version control and provide it only to explicit real
targets such as `make live`, `make benchmark`, `make test-real`, or a live
fixture. `TWOXBRAINZ_AIGATE_MODEL` is additionally required by the optional
`make benchmark-with-draft` target.

`make live` validates its configured AIGate model against AIGate's model
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
| `TWOXBRAINZ_TALKIES_WS_URL` | yes | Native Talkies stream URL using `ws` or `wss`. |
| `TWOXBRAINZ_TALKIES_MODEL` | yes | One Talkies streaming model slug shared by both streams. |
| `TWOXBRAINZ_TALKIES_TOKEN` | if Talkies auth is enabled | Dedicated bearer token sent to Talkies' native stream, model inventory, and file-transcription routes. When omitted, the app reuses `TWOXBRAINZ_AIGATE_TOKEN` only if the Talkies and AIGate URLs have the same host and effective port. |
| `TWOXBRAINZ_AIGATE_URL` | yes | OpenAI-compatible API root using `http` or `https`, including the provider's version prefix (`/v1` for AIGate, OpenAI and Groq). |
| `TWOXBRAINZ_AIGATE_MODE` | no | `local` (default) or `remote`; remote requires an explicit opt-in. |
| `TWOXBRAINZ_AIGATE_MODEL` | yes | Model name configured by the AIGate gateway. |
| `TWOXBRAINZ_AIGATE_TOKEN` | if AIGate requires it | Bearer token used only for draft requests. |
| `TWOXBRAINZ_REMOTE_TEXT_ENABLED` | when mode is `remote` | Must be exactly `true` before transcript text can reach a remote provider. |
| `TWOXBRAINZ_LOG_LEVEL` | no | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `TWOXBRAINZ_LOG_FILE` | no | Absolute temporary log file path. |

`TWOXBRAINZ_TALKIES_WS_URL` must end in
`/v1/audio/transcriptions/stream`. The CLI derives the matching model
inventory and file-transcription routes from that URL while retaining any
path prefix.

## Deployment shapes

**AIGate with Talkies enabled** — the default. AIGate keeps Talkies on its
private network with no host port binding and publishes it at `/talkies/`, so
both URLs point at the gateway and `talkies:8000` is not reachable from outside
the compose stack:

```
TWOXBRAINZ_AIGATE_URL=http://aigate:4000/v1
TWOXBRAINZ_TALKIES_WS_URL=ws://aigate:4000/talkies/v1/audio/transcriptions/stream
TWOXBRAINZ_AIGATE_TOKEN=<gateway token>
```

Both carry the same host and effective port, so the gateway token is reused for
Talkies and `TWOXBRAINZ_TALKIES_TOKEN` can stay unset.

**Standalone Talkies with text generation elsewhere** — point each at its own
provider. Any OpenAI-compatible endpoint works for drafts:

```
TWOXBRAINZ_AIGATE_URL=https://api.openai.com/v1
TWOXBRAINZ_TALKIES_WS_URL=ws://talkies:8000/v1/audio/transcriptions/stream
TWOXBRAINZ_AIGATE_TOKEN=<provider key>
TWOXBRAINZ_TALKIES_TOKEN=<talkies token>
```

The authorities differ here, so nothing is shared: the provider key is never
sent to Talkies and the Talkies token is never sent to the provider. Sending
transcript text to a hosted provider also requires
`TWOXBRAINZ_AIGATE_MODE=remote` and `TWOXBRAINZ_REMOTE_TEXT_ENABLED=true`.

## Talkies TTS fixture capture

`make live-fixture` is an explicit external integration target. It derives
the direct Talkies TTS route from `TWOXBRAINZ_TALKIES_WS_URL`, creates two
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
test starts. The live application's 15-second AIGate and ASR deadlines are not
changed by this harness allowance.

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
segment after detected speech is followed by bounded silence. Each new segment
waits for its first audible frame and has a distinct logical ASR identity, so
the configured Nemotron backend can produce independent multi-turn finals
without holding an idle connection or recreating either capture process.

The target requires `TWOXBRAINZ_TALKIES_WS_URL`,
`TWOXBRAINZ_TALKIES_MODEL`, and the shared `TWOXBRAINZ_AIGATE_TOKEN` on the
same gateway host and port. A separate `TWOXBRAINZ_TALKIES_TOKEN` remains an
optional override only for deployments that intentionally use different
provider authorities.
`FIXTURE_TTS_MODEL` and `FIXTURE_TTS_VOICE` are Make variables, not application
configuration; use them only to select an available Talkies TTS model and voice
for this test. Because it reaches the configured Talkies service,
`live-fixture` is intentionally excluded from the ordinary offline test suite.
For a one-off direct AIGate test without editing `.env`, pass the AIGate base
URL and native Talkies stream URL as Make variables. Their host and effective
port must match for AIGate-token reuse to apply:

```bash
make live-fixture LIVE_NETWORK=<network> \
  AIGATE_URL=http://aigate.example.test \
  TALKIES_WS_URL=ws://aigate.example.test/talkies/v1/audio/transcriptions/stream
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
`TWOXBRAINZ_AIGATE_TOKEN`, plus reachable endpoints. A token-only `.env` can
be used by passing the AIGate and Talkies URLs as Make variables; token reuse
for Talkies is still limited to matching endpoint authority:

```bash
make test-real LIVE_NETWORK=<network> \
  AIGATE_URL=http://aigate.example.test

make live-product-fixture LIVE_NETWORK=<network> \
  AIGATE_URL=http://aigate.example.test \
  TALKIES_WS_URL=ws://aigate.example.test/talkies/v1/audio/transcriptions/stream
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

## Local and remote text generation

`TWOXBRAINZ_AIGATE_MODE=local` is the default. It is intended for an AIGate
endpoint running on the operator-controlled machine or network. Remote mode is
an explicit privacy boundary: it requires both values below at process start.

```dotenv
TWOXBRAINZ_AIGATE_MODE=remote
TWOXBRAINZ_REMOTE_TEXT_ENABLED=true
```

Remote mode sends speaker-tagged transcript text to the configured AIGate
endpoint; it never sends raw audio. `2xbrainz doctor`, `2xbrainz status`, and
the initial live-session JSON record report the selected mode and model, but
never print token values.

Reply drafts, commentary, and summaries use fixed application-owned token and
text-length budgets. AIGate content that exceeds the matching budget is
rejected before the CLI can render it; the budgets are not provider-controlled
configuration. The reply budget is 512 completion tokens so reasoning-capable
models have room to return visible spoken text; the separate character limit
still bounds what the CLI accepts.

Reply-draft content is also a strict display boundary: it must be one line of
plain spoken prose. The provider boundary parses CommonMark without rendering
HTML, converts inline emphasis, code, and link labels to visible text, and
discards link destinations. It rejects headings, lists, block quotes, fenced
code, HTML, and line breaks rather than presenting them as a draft. A rejected
reply becomes a failed draft record with no provider text rendered. Commentary
and summaries may contain multiple sentences, but they use the same text-only
parser boundary before entering CLI state.

Each provider job also has a fixed 15-second application deadline, matching
the outbound HTTP timeout. A deadline expiry produces an empty failed result
for the current transcript revision and leaves capture plus later turns
running. The deadline is not an environment setting: changing it requires a
code change and matching operational measurement.

## Network and PipeWire boundary

`make live` does not publish any TCP port. It joins the `LIVE_NETWORK` chosen
by the operator so the app can resolve Talkies and AIGate by their service
names. The container drops all Linux capabilities, runs read-only apart from a
temporary filesystem, defaults to an eight-CPU, 1 GiB, 128-process cgroup
budget, and mounts the host PipeWire runtime directory read-only. Override
`RUNTIME_CPUS`, `RUNTIME_MEMORY`, or `RUNTIME_PIDS` only after measuring the
selected Talkies model with both streams active.

For a fully-qualified AIGate or Talkies hostname that resolves on the host but
not inside Docker, such as a tailnet-only name, `make live` and `make benchmark`
resolve its IPv4 address on the host and pass one validated Docker host mapping.
That helper derives the mapping from the endpoint hostname only; it never reads
token settings or emits URL paths, and does not use host-network mode. If the
hostname cannot be resolved by the host, attach the CLI to an explicit bridge
network with the required DNS instead.

The mounted PipeWire socket is only usable when the container runs with the
same UID as the host desktop session. `make live` does that automatically. Use
`make devices` before choosing `MIC_NODE` and `SYSTEM_NODE`.

## Live-session controls

`make live` keeps standard input open for exact, case-insensitive lifecycle
and draft-control lines:

- `pause` — blocks future audio frames before the Talkies boundary and cancels
  active drafting, commentary, and summary work.
- `resume` — resumes forwarding frames to the existing Talkies streams.
- `stop` — cancels generation, ends both capture streams, and exits the CLI.
- `accept` or `dismiss` — consume the current completed reply draft without
  sending, speaking, or persisting it.
- `edit <replacement text>` — replace the current completed reply text locally.
- `regenerate` — request a fresh reply only when the previous draft still has
  the current transcript revision.

Each action produces a `session` JSON record with `state`, `action`, and
`changed` fields or a `draft_action` JSON record with `action`, `changed`,
`outcome`, `generation_id`, and `context_revision` fields. Outcome identifiers
are `null` for edits, regeneration, failed actions, and non-outcome actions.
Accepted and dismissed drafts are retained only in a bounded in-memory record
until the CLI exits. Invalid or oversized lines produce a fixed `control_error`
record and do not echo the input. EOF alone leaves the session running.

The ordinary `draft` record is a lifecycle record. A remote final first emits
`status: "running"` with empty text, followed by exactly one terminal status:
`completed` with text, or `failed`, `cancelled`, or `superseded` with empty
text. Only a completed draft can be accepted, dismissed, edited, or
regenerated.

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

`make run` executes `2xbrainz doctor`, which prints a sanitized JSON view of
the selected AIGate mode/model, configured endpoints, and whether credentials
are present. It never prints token values. `2xbrainz status` produces the same
safe configuration view.

The replay route needs no external service:

```bash
make replay
```

It is the appropriate clean-machine smoke test for the container image and
coordination state machine. Live Talkies and AIGate connectivity are validated
separately by the operator's selected Docker network and model configuration.
