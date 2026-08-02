# Architecture

2xbrainz is a single-user, Docker-run CLI. It deliberately keeps audio capture,
live ASR, turn state, and text drafting as replaceable boundaries rather than
creating a distributed control plane.

The current MVP launch profile is Linux PipeWire with CPU-native ASR and
ephemeral session state. See
[ADR-0001](decisions/0001-mvp-launch-profile.md) for the exact platform,
resource, provider, and retention boundaries.

## Runtime flow

```text
host PipeWire socket (read-only mount)
  ├─ microphone node ─┐
  └─ system-output node ─┼─> `pw-record` PCM16LE frames
                         │
                         ├─> two Talkies WebSocket connections
                         │      same configured native ASR model
                         │      partial / endpoint / final revisions
                         │
                         └─> transcript store -> turn manager -> coordinator
                                                            │
                                                            ├─> remote reply draft
                                                            └─> user commentary / rolling summary
                                                                 via AIGate /chat/completions
                                                                 with text-only context
```

`live` opens one Talkies stream per capture node. Before it constructs either
PipeWire source, both `live` and `benchmark` verify the selected model inventory
and serially open a synthetic-silence stream. The warm-up must receive `ready`,
send exactly one all-zero 20 ms PCM frame, then `end`, and receive uncancelled
one-frame terminal statistics. This materializes lazy backend initialization
before the two audio streams start together without using captured audio.
Talkies reserves one model slug while streams are active, so the CLI
intentionally configures the same model for microphone and system audio. A
mixed-model installation needs separate Talkies processes and a measured
resource budget.

`make live-fixture` exercises the same production `live` process without the
host PipeWire boundary. It asks the configured direct Talkies API to synthesize
two known, ephemeral WAV fixtures, then supplies their normalized PCM through
two harness-owned `pw-record` device processes. Its default overlap scenario
starts short remote speech after local speech is active and proves that a
remote final remains in the timeline but cannot start a reply draft. It checks
real Talkies TTS, native ASR, capture timing, turn records, commentary, and
summary while using a deterministic local AIGate protocol fixture.

`make test-real` separately exercises the configured real AIGate model using
fixed synthetic text only. It drives a four-turn interview through the same
coordinator used by `live`: commitment and risk, an interviewer question, a
specific mitigation, and a final verification question must remain coherent in
the running summary and final reply request. `make live-interview-fixture`
performs that schedule with real Talkies TTS/ASR and a deterministic AIGate
boundary. `make live-product-fixture` substitutes real AIGate and holds each
opposing WAV release until the preceding required generation is complete. Each
explicit real fixture leaves an ordered, redacted JSONL reconstruction trace under
`.testing/fixture-traces/`, including provider context and every emitted CLI
record plus structured runtime diagnostics and terminal assertion outcome for
its synthetic session. This makes prompt/provider failures distinct from
capture/ASR timing failures while keeping both scenarios fully reproducible
without host audio hardware. The scenario also exercises the native-ASR
compatibility path: when a backend finalizes only after `end`, the runtime keeps
PipeWire capture open but ends the current Talkies segment after detected speech
is followed by a bounded silence interval. It waits for the next audible frame
before opening a new segment with a new logical ASR identity, avoiding an idle
backend connection while another turn's LLM work is in flight. This lets
Nemotron produce independent multi-turn finals without recreating either
capture process.

## State invariants

- Capture stream identity establishes `user` versus `remote`; no diarization
  or cross-stream speaker classifier is used for the initial two-party case.
- ASR revisions are monotonic per stream. A stale revision cannot replace the
  latest transcript text.
- Every finalized turn produces exactly one timeline record. Duplicate ASR
  finals do not add another entry.
- A remote endpoint with non-empty text marks only a candidate end. Its stable
  final starts one reply job, and a completed reply schedules a rolling
  summary.
- A remote final received while the local-user stream is `speaking` or at a
  `candidate_end` is overlap, not a reply opportunity. It keeps its timeline
  entry but suppresses the draft until a later remote final follows local-user
  finalization.
- A finalized user turn starts private commentary; completed commentary
  schedules a rolling summary.
- Remote speech supersedes the active reply and cancels commentary and summary
  work. User speech cancels an active reply.
- A result is visible only when its generation ID and transcript revision still
  match the coordinator's active state; a changed revision discards stale
  background output.
- Every provider job carries the fixed 15-second application deadline. An
  expired draft, commentary, or summary becomes a typed failed result and
  cannot hold capture or later turns hostage.
- An expected PipeWire or Talkies stream failure emits one versioned
  `session_error` record with a fixed reason (`capture_unavailable`,
  `asr_unavailable`, or `asr_protocol_error`) before ending the session with a
  non-zero exit. The record and CLI message never include the upstream error,
  endpoint, stream identity, audio, or credentials. Unknown failures retain
  ordinary crash behavior for diagnosis. The runtime fans in simultaneous
  expected stream failures before writing that one public record.
- A `pw-record` subprocess that exits without one complete PCM frame is a
  capture failure even when its exit status is zero. A silent capture-only
  session is never a successful live session.
- A completed rolling summary is a bounded prefix for later provider contexts.
  It can trim only transcript lines that it covers; newer lines remain visible.
- A rolling summary treats transcript text as untrusted ASR output. It retains
  an established fact across conflicting later wording unless a speaker
  explicitly corrects it, and it does not infer unsupported qualifiers.
- The AIGate boundary receives transcript text only. It never receives raw
  frames, PipeWire node identifiers, or Talkies credentials.

## Components

- [`capture.py`](../src/two_x_brainz/capture.py) launches `pw-record` without a
  shell, validates a short PipeWire node identifier, normalizes arbitrary
  stdout reads into exact bounded 20 ms PCM frames, assigns immutable stream
  identity and monotonic timing, and reports aggregate capture gaps plus
  bounded relative drift between matching frame sequences. It rejects a clean
  subprocess exit that did not yield a complete frame. It never retains PCM or
  device identifiers for diagnostics.
- [`talkies.py`](../src/two_x_brainz/talkies.py) validates the Talkies protocol
  at the boundary, maps native WebSocket transcripts plus terminal stream
  statistics into typed contracts, preserves optional word offsets and
  confidence, derives transcript timing bounds from actual word offsets only,
  serially warms the selected backend with one synthetic-silence frame before
  any concurrent traffic, stops receive processing promptly when its PCM sender
  fails, and validates the two OpenAI-compatible file-transcription response
  shapes used by the finite benchmark command. The JSON-line transcript record emits
  the selected ASR model, accepted audio duration, optional utterance timing,
  language, confidence, and per-word metadata without session or stream IDs.
- [`benchmark.py`](../src/two_x_brainz/benchmark.py) feeds one bounded WAV to
  two concurrent role-labelled native streams plus both Talkies file routes and
  emits aggregate timing/count data only. It is a transport-contract check,
  not a model-quality claim.
- [`docker_hosts.py`](../src/two_x_brainz/docker_hosts.py) allows the Docker
  launch targets to map a host-resolved fully-qualified provider hostname to a
  validated IPv4 address without using host networking or reading credentials.
- [`transcript.py`](../src/two_x_brainz/transcript.py) performs revision-safe
  transcript reconciliation and retains a bounded recent window after a
  newer rolling summary is accepted.
- [`turns.py`](../src/two_x_brainz/turns.py) is ASR-signal-first: an endpoint
  creates a candidate end, and only a stable final completes a turn; optional
  VAD can only become an earlier timing hint in a later capture implementation.
  It also exposes per-role active speech so overlap cannot be mistaken for a
  completed reply opportunity.
- [`coordinator.py`](../src/two_x_brainz/coordinator.py) owns timeline entries,
  reply priority, overlap suppression, cancellation, provider deadlines,
  stale-result rejection, and bounded completed result queues.
- [`aigate.py`](../src/two_x_brainz/aigate.py) is a narrow implementation of
  the OpenAI-compatible chat-completions contract used by AIGate. It applies
  application-owned token and text-length limits before provider output enters
  CLI state, then parses CommonMark into a text-only subset. Safe inline
  presentation becomes visible text; structural Markdown and HTML are rejected
  without rendering provider-controlled markup.

For module-level invariants and file ownership, see
[`src/two_x_brainz/README.md`](../src/two_x_brainz/README.md).

## Deliberate limits

The runtime emits JSON lines in the terminal rather than a graphical overlay
or terminal dashboard. It never speaks or sends a draft automatically, stores
no audio, and does not attempt multi-speaker diarization on the mixed system
stream. Those boundaries prevent the always-on assistant from becoming an
invisible actor.

Live and replay use the same JSON-line record shapes for transcript, turn,
timeline, draft, commentary, and summary output. This lets a local consumer
validate its parser with the deterministic replay fixture before attaching to
live audio. Every record carries `schema_version: 1`. Turn and timeline records
share an opaque `turn_id`; generated records carry opaque `generation_id` and
`trigger_turn_id` fields. These support local correlation without exposing a
session ID, stream ID, audio bytes, or credentials.
