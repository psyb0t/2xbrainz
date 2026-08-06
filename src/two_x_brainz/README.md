# Application module

This package contains the local session state machine and all external service
adapters used by the CLI. It has no database; it renders live events through
a loopback FastAPI/Svelte console and delegates
continuous ASR to Talkies.

## Contents

- [File map](#file-map)
- [Invariants](#invariants)
- [Tests](#tests)

## File map

- `contracts.py` — immutable domain enums and records shared by every boundary,
  including optional ASR timing, language, and confidence metadata.
- `config.py` — strict AIGate-only environment validation, safe defaults, and
  derivation of the Talkies proxy route from the one gateway URL.
- `capture.py` — Linux PipeWire subprocess adapter, friendly/default device
  metadata extraction, frame identity/timing, derived presentation-only level,
  Silero-driven turn segmentation, and aggregate capture-gap plus
  relative-drift diagnostics.
- `vad.py` — typed adapter that buffers 20 ms PCM capture frames into exact
  bundled-Silero inference windows and validates speech probabilities.
- `audio_selection.py` — bounded local selection-file validation and the
  candidate/controller backing the in-app first-run and hotplug-aware
  setup view for one non-monitor microphone source and one system-audio capture
  source (a monitor source or a directly capturable sink).
- `audio.py` — bounded WAV fixture loading plus in-memory PCM normalization for
  ASR evaluation; it is not the live capture path.
- `session_controls.py` — bounded local lifecycle parsing plus the
  capture-forwarding gate.
- `talkies.py` — native WebSocket protocol validation, PCM streaming, and the
  OpenAI-compatible bounded file-transcription contract.
- `benchmark.py` — finite, aggregate-only two-stream native/file contract
  evaluation for one supplied WAV fixture, with an optional concurrent,
  synthetic text-only draft probe and optional local-reference word error rates;
  it is not the live capture path.
- `docker_hosts.py` — host-side FQDN-to-IPv4 mapping helper for Docker launch;
  it reads endpoint fields only and never emits credential values.
- `transcript.py` — monotonic revision reconciliation.
- `turns.py` — partial/candidate/final turn state, reopened-turn detection,
  and role-scoped active-speech state.
- `coordinator.py` — timeline entries, reply priority, overlap suppression,
  cancellation, and stale-result rejection for drafts, commentary, and
  summaries.
- `aigate.py` — AIGate chat provider for drafts, commentary, and summaries. It
  exposes only application-owned `research_web` and bounded arithmetic to reply
  drafts when explicitly enabled. Research accepts a query or exact discovered
  URL, returns link-preserving Markdown, and allows a relevant documentation link
  to be followed in the second bounded round. Same-round calls execute
  concurrently. Production calls consume bounded OpenAI-style SSE and publish
  correlated output,
  provider-visible reasoning, and validated tool activity without claiming
  access to hidden chain-of-thought;
  `make test-real` probes its real-model prompt contracts and a four-turn
  summary-to-reply context handoff using fixed synthetic text.
- `provider_selection.py` — no-follow, bounded, exact-schema persistence for
  separate Reply, Coach, and Story AIGate model/reasoning assignments beside the
  audio-selection file, including migration from the legacy single assignment.
- `fixture_trace.py` — append-only, redacted JSONL evidence for explicit real
  fixtures; it records synthetic fixture inputs, CLI output, structured runtime
  diagnostics, and terminal outcomes without tokens or PCM data.
- `terminal.py` — bounded, control-sequence-safe presentation state with one
  temporary level probe per displayed audio candidate, selected capture labels,
  channel health, and strict local lifecycle controls. It owns no TUI.
- `web.py` — loopback FastAPI/Uvicorn adapter over the shared presentation
  state: bounded structured snapshots, same-origin start/pause controls through
  the runtime queue, compiled Svelte assets at `/`, automatically refreshed
  all-candidate audio setup, and three correlated provider-activity flows. The
  frontend owns browser-local layout
  preferences; application audio selection remains in the validated config file.
  Process shutdown remains with the owning shell so the page cannot stop its
  own server.
- `logging_config.py` — credential-redacted rotating JSON event log.
- `runtime.py` and `cli.py` — live orchestration and user-facing commands.

## Invariants

- Do not import a model SDK into the coordinator. External capabilities belong
  in a dedicated adapter.
- Treat every Talkies and AIGate payload as untrusted until its exact shape is
  checked at that boundary.
- Preserve ASR timing, language, and confidence only when a backend actually
  supplies them. Transcript bounds are derived from word offsets, never from
  an invented wall-clock estimate.
- Terminal ASR statistics are aggregate-only records; they bypass transcript
  reconciliation and cannot create turns or provider work.
- An endpoint is only a candidate end. A non-empty final is required before a
  reply draft, commentary, summary, or timeline entry can begin.
- A remote final during an active user turn is overlap. It produces a timeline
  record but cannot produce a reply draft until a later remote final arrives
  after the user turn finalizes.
- AIGate requests carry a fixed per-output token budget, and overlong provider
  text is rejected before it can become a draft, commentary, or summary.
- Provider reply drafts must be single-line plain spoken prose. The AIGate
  boundary parses CommonMark without rendering HTML, converts inline emphasis,
  code, and link labels to visible text, and rejects structural formatting or
  multiple lines before a draft reaches the terminal.
- Provider commentary and summaries may be multi-sentence but must still be
  plain prose. They use the same text-only parser boundary and reject Markdown
  structure and HTML.
- Every provider request carries the fixed 60-second application deadline.
  Deadline expiry yields an empty typed failure for the same generation and
  never prevents later ASR turns from scheduling new work.
- Docker host mappings can cover only a host-resolved fully-qualified Talkies
  or AIGate endpoint and include only a validated hostname and IPv4 address.
- Never put audio or credential values in draft payloads or logs. The rotating
  event log intentionally retains transcript, timeline, draft, commentary, and
  summary text so a local operator can reconstruct a session.
- Treat the optional session brief as trusted local framing. It is bounded,
  omitted from status and logs, and appended to every generation prompt without
  being inserted into the transcript.
- PipeWire stdout chunks are never assumed to be ASR frames; only exact 20 ms
  PCM16LE frames cross the Talkies boundary.
- Each live role owns an independent stateful Silero detector. Two consecutive
  speech-positive windows open a segment with 200 ms of pre-roll; sustained
  speech-negative windows close it. Separate start and stop thresholds provide
  hysteresis, and continuous input rotates after 60 seconds so noise cannot
  hold Talkies or downstream generation open forever.
- Audio-selection persistence contains only validated stable node names. A
  missing or malformed pair opens Sources. A device lost during a run marks
  only its channel reconnecting; its peer continues, and redetection or a new
  selection replaces only the affected route.
- Sources serializes discovery, stops presentation-only preview processes before
  each scan, and refreshes automatically while open so stale Bluetooth or USB
  nodes are removed rather than retained after a timed-out discovery call.
- System capture targets are `Audio/Source` monitor nodes or directly
  capturable `Audio/Sink` nodes. PipeWire default markers are recommendations;
  setup probes and independent live level meters reveal the two capture paths
  actually reaching the process.
- The Talkies transport races sender failure against inbound WebSocket events,
  so a capture error cannot wait indefinitely for the ASR server to close.
- A `pw-record` process that exits before producing one complete PCM frame is
  a typed capture failure, even if it exits with status zero. It cannot make a
  live session appear successful without audio.
- Every frame has a monotonic stream-local sequence and capture timestamp, but
  runtime output exposes only bounded aggregate gap and relative-drift timing
  diagnostics.
- AIGate is the sole service boundary. One validated URL, three independently
  selected flow models, and one optional
  bearer token cover text generation, model inventory, Talkies, and explicitly
  enabled allowlisted tools.
- Production AIGate completions use bounded SSE. Every provider activity record
  carries a flow identifier and output kind so Reply, Private coach, and Story
  events cannot mix. Reasoning is shown only when AIGate explicitly supplies a
  visible reasoning field; exact tool input/result text remains bounded and
  credential-redacted in the reconstruction log.
- A finalized remote turn starts reply, commentary, and summary requests
  concurrently through three independently configured AIGate clients. Each
  generation remains independently cancellable and carries an immutable
  transcript revision so a late result cannot overwrite newer state.
- Cancelling a provider generation never removes finalized transcript lines. A
  later silence boundary builds replacement requests from the complete current
  transcript and discards only unfinished provider work.
- Reply, Coach, and Story model/reasoning choices are atomically saved with
  owner-only permissions. A malformed, symlinked, oversized, or partly
  unavailable selection is ignored as one unit and the validated environment
  configuration remains active for all flows.
- Search queries reject obvious structured private identifiers before reaching
  AIGate. Page reads accept only public HTTP(S) URLs, reject credential-bearing
  and non-public destinations, disable redirects, and revalidate
  the browser's final URL. Calculation calls accept only a bounded arithmetic AST
  and send application-generated Python to Piston.
- Live startup verifies every configured AIGate flow model before it opens PipeWire
  capture or Talkies streams, preventing a capture-only session without reply
  drafts.
- Live startup verifies the selected Talkies model inventory and completes a
  serial synthetic-silence native-stream warm-up before it opens PipeWire
  capture. Warm-up sends one all-zero 20 ms frame and requires uncancelled
  one-frame terminal statistics, preventing a silent ASR-model substitution or
  a concurrent lazy-initialization race without using captured audio.
- Only fixed local lifecycle commands can alter capture. The browser's Start
  and Stop listening controls map to resume and pause; pause cancels generation
  before any later frame reaches Talkies. Process shutdown remains `Ctrl+C` in
  the owning shell.
- Reply draft records expose `running` before one terminal completed, failed,
  cancelled, or superseded state. Completed draft text is advisory display
  state, never an action or provider-context input.
- Background tasks inherit context-local logging scope and must be cancelled or
  discarded before a stale result reaches the CLI. Completed draft and insight
  queues are bounded.
- Runtime JSON records use schema version 1. The web presentation consumes them
  internally while the rotating log retains them; replay prints them. Opaque
  turn and generation IDs link related records without exposing session or
  stream identities.
- Expected PipeWire and Talkies stream failures are handled by independent
  role-scoped retry loops. Neither upstream error text nor endpoint, stream
  identity, audio, or credentials reach the browser.
- A successful rolling summary becomes the provider-context prefix and permits
  trimming only the transcript history it covers; the recent line window stays
  available for the next draft.
- Benchmark reference text is read only from a bounded regular local file and
  immediately reduced to aggregate word error rates; it never appears in CLI
  output, logs, or provider input.

## Tests

Unit tests cover configuration, protocol validation, WAV normalization,
real and deterministic Silero inference, VAD hysteresis and safety boundaries,
transcript revisions, timeline idempotency, stale-result rejection, and
priority cancellation. Integration tests start local protocol-conforming
WebSocket and HTTP servers to prove concurrent native PCM framing, terminal
statistics, and both file-transcription response shapes; fake `pw-record`
processes cover the same capture framing code without a host PipeWire socket.
Control tests cover paused startup, pause/resume gating, stop wakeup,
idempotency, removed reply-action rejection, and malformed control-line
rejection. Web-console tests cover all-candidate setup meters, audio redetection,
runtime provider settings, flat chronological provider activity, bottom-aware
autoscroll, per-flow cumulative snapshot coalescing, independently collapsed
reasoning/tool rows, a long-inventory model-picker screenshot, and separate
user/system channel state. See the
architecture overview in
[`docs/architecture.md`](../../docs/architecture.md). The opt-in
browser smoke uses a real local fake AIGate HTTP service to stream SSE reasoning,
a fragmented search tool call, an MCP result, and follow-up output through the
production client and compiled Svelte UI. It asserts chronological DOM text and
the backend/frontend DEBUG trail before cleaning up its owned containers and
test image. The opt-in
`make live-fixture` route generates temporary Talkies WAVs and drives the
production capture adapter through a controlled overlap; `make
live-interview-fixture` adds four alternating fixture turns, and `make
live-product-fixture` runs that interview with real AIGate commentary, draft,
and summary generation. Both it and `make test-real` create local reconstruction
traces for their fixed synthetic scenarios under `.testing/fixture-traces/`.
