# Application module

This package contains the local session state machine and all external service
adapters used by the CLI. The package has no web server and no database; it
prints terminal events and delegates continuous ASR to Talkies.

## File map

- `contracts.py` — immutable domain enums and records shared by every boundary,
  including optional ASR timing, language, and confidence metadata.
- `config.py` — strict environment validation, including remote-text opt-in,
  safe defaults, and constrained AIGate-token reuse for same-authority Talkies
  routes.
- `capture.py` — Linux PipeWire subprocess adapter, frame identity/timing, and
  aggregate capture-gap plus relative-drift diagnostics.
- `audio.py` — bounded WAV fixture loading plus in-memory PCM normalization for
  ASR evaluation; it is not the live capture path.
- `session_controls.py` — bounded local lifecycle and human-gate action parsing
  plus the capture-forwarding gate.
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
- `aigate.py` — text-only OpenAI-compatible provider for drafts, commentary,
  and summaries; `make test-real` probes its three real-model prompt contracts
  and a four-turn summary-to-reply context handoff using fixed synthetic text.
- `fixture_trace.py` — append-only, redacted JSONL evidence for explicit real
  fixtures; it records synthetic fixture inputs, CLI output, structured runtime
  diagnostics, and terminal outcomes without tokens or PCM data.
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
- Every provider request carries the fixed 15-second application deadline.
  Deadline expiry yields an empty typed failure for the same generation and
  never prevents later ASR turns from scheduling new work.
- Docker host mappings can cover only a host-resolved fully-qualified Talkies
  or AIGate endpoint and include only a validated hostname and IPv4 address.
- Never put audio or credential values in draft payloads or logs.
- PipeWire stdout chunks are never assumed to be ASR frames; only exact 20 ms
  PCM16LE frames cross the Talkies boundary.
- The Talkies transport races sender failure against inbound WebSocket events,
  so a capture error cannot wait indefinitely for the ASR server to close.
- A `pw-record` process that exits before producing one complete PCM frame is
  a typed capture failure, even if it exits with status zero. It cannot make a
  live session appear successful without audio.
- Every frame has a monotonic stream-local sequence and capture timestamp, but
  runtime output exposes only bounded aggregate gap and relative-drift timing
  diagnostics.
- Local AIGate mode is the default. Remote text mode cannot start unless its
  separate affirmative environment setting is exactly `true`.
- Live startup verifies the configured AIGate model before it opens PipeWire
  capture or Talkies streams, preventing a capture-only session without reply
  drafts.
- Live startup verifies the selected Talkies model inventory and completes a
  serial synthetic-silence native-stream warm-up before it opens PipeWire
  capture. Warm-up sends one all-zero 20 ms frame and requires uncancelled
  one-frame terminal statistics, preventing a silent ASR-model substitution or
  a concurrent lazy-initialization race without using captured audio.
- Only fixed local control words can alter a live session. Pause and stop
  cancel generation before any later frame reaches Talkies; draft actions are
  rejected after their transcript revision becomes stale.
- Reply draft records expose `running` before one terminal completed, failed,
  cancelled, or superseded state. Only completed draft text remains actionable.
- Accepted and dismissed drafts produce bounded, in-memory outcome records for
  the active process. They are never sent, spoken, or persisted.
- Background tasks inherit context-local logging scope and must be cancelled or
  discarded before a stale result reaches the CLI. Completed draft and insight
  queues are bounded.
- CLI JSON records use schema version 1. Opaque turn and generation IDs link
  related records without exposing session or stream identities.
- Expected PipeWire and Talkies stream failures emit one fixed
  `session_error` reason before the live command terminates non-zero; no error
  message, endpoint, stream identity, audio, or credential crosses that JSON
  boundary. Simultaneous stream failures are fanned in before that single
  record is emitted.
- A successful rolling summary becomes the provider-context prefix and permits
  trimming only the transcript history it covers; the recent line window stays
  available for the next draft.
- Benchmark reference text is read only from a bounded regular local file and
  immediately reduced to aggregate word error rates; it never appears in CLI
  output, logs, or provider input.

## Tests

Unit tests cover configuration, protocol validation, WAV normalization,
transcript revisions, timeline idempotency, stale-result rejection, and
priority cancellation. Integration tests start local protocol-conforming
WebSocket and HTTP servers to prove concurrent native PCM framing, terminal
statistics, and both file-transcription response shapes; fake `pw-record`
processes cover the same capture framing code without a host PipeWire socket.
Control tests cover pause/resume gating, stop wakeup, idempotency, draft
actions, and malformed control-line rejection. See the architecture overview in
[`docs/architecture.md`](../../docs/architecture.md). The opt-in
`make live-fixture` route generates temporary Talkies WAVs and drives the
production capture adapter through a controlled overlap; `make
live-interview-fixture` adds four alternating fixture turns, and `make
live-product-fixture` runs that interview with real AIGate commentary, draft,
and summary generation. Both it and `make test-real` create local reconstruction
traces for their fixed synthetic scenarios under `.testing/fixture-traces/`.
