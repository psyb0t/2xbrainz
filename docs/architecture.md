# Architecture

2xbrainz is a single-user, Docker-run web application. It deliberately keeps
audio capture,
live ASR, turn state, and text drafting as replaceable boundaries rather than
creating a distributed control plane.

The current launch profile is Linux PipeWire with CPU-native ASR, ephemeral
in-memory session state, and bounded local event-log retention.

## Contents

- [Runtime flow](#runtime-flow)
- [State invariants](#state-invariants)
- [Components](#components)
- [Deliberate limits](#deliberate-limits)

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
                                                            ├─> local Svelte console
                                                            ├─> rotating local event log
                                                            ├─> remote reply draft
                                                            └─> user commentary / rolling summary
                                                                 via AIGate /chat/completions
                                                                 with text-only context
```

At startup, `live` reads the visible PipeWire nodes through the existing
read-only runtime mount and opens the browser idle. The browser sends its
validated local settings when available; otherwise Settings opens on Audio and
asks for one non-monitor microphone source and one system-audio capture source:
a monitor source when available, or a directly capturable sink. The browser
persists those stable names with the other safe runtime settings. Settings
remains available throughout the run. While Audio is open, redetection serially
stops the temporary meter probes, refreshes candidates
every three seconds, and restarts probes for the new inventory. A manual refresh
uses the same path. A changed pair restarts only the affected capture channel,
and a missing channel retries independently while its peer continues.
The
PipeWire configured default source and default output are a first-choice
recommendation, not a claim about an application's current route. While Audio
Setup is visible, one short-lived meter probe runs for every displayed candidate
and the UI renders each independent level, so the operator can identify active
routes before persisting a pair. The dashboard then meters only its two selected
capture paths. No audio or PipeWire socket is written to that file.

`live` opens one Talkies stream per capture node. Before it constructs either
PipeWire source, both `live` and `benchmark` verify the selected model inventory
and serially open a synthetic-silence stream. AIGate model aliases choose the
native proxy: `local-talkies-*` maps to `/talkies/`, while
`local-talkies-cuda-*` maps to `/talkies-cuda/`; only the inner model slug is
sent to Talkies. Alias preflight checks the selected service health response and
rejects a CPU/CUDA mismatch. The warm-up must receive `ready`,
send exactly one all-zero 20 ms PCM frame, then `end`, and receive uncancelled
one-frame terminal statistics. This materializes lazy backend initialization
before the two audio streams start together without using captured audio.
Talkies reserves one model slug while streams are active, so the CLI
intentionally configures the same model for microphone and system audio. A
mixed-model installation needs separate Talkies processes and a measured
resource budget.

`make test-real` separately exercises the configured real AIGate model using
fixed synthetic text only. It drives a four-turn interview through the same
coordinator used by `live`: commitment and risk, an interviewer question, a
specific mitigation, and a final verification question must remain coherent in
the running summary and final reply request. `make test-real-audio-research`
adds the complete audio path: it synthesizes two related utterances, streams
them through real Talkies ASR and the production VAD/coordinator, and releases
the second only after the first Claudebox repository investigation starts. The
first generation is superseded; its already accepted native operation drains,
then the replacement continues in the same workspace with both recognized
turns and must leave a verified `psyb0t/aigate` checkout. Each explicit real
fixture leaves an ordered, redacted JSONL reconstruction trace under
`.testing/fixture-traces/`, including provider context and every emitted CLI
record plus structured runtime diagnostics and terminal assertion outcome for
its synthetic session. This makes prompt/provider failures distinct from
capture/ASR timing failures while keeping both scenarios fully reproducible
without host audio hardware. The scenario also exercises backend finalization
after `end`: the runtime keeps
PipeWire capture open while a local Silero model independently bounds each
Talkies segment. Speech start requires consecutive positive model windows;
speech end uses a lower probability threshold and sustained silence, providing
hysteresis against noisy microphones. A short pre-roll preserves initial
phonemes, and a 60-second safety boundary rotates continuous audio even if
silence never arrives. The runtime waits for the next detected speech region
before opening a new logical ASR identity, avoiding an idle backend connection
while another turn's LLM work is in flight. This lets Nemotron produce
independent multi-turn finals without recreating either capture process.

## State invariants

- Capture stream identity establishes `user` versus `remote`; no diarization
  or cross-stream speaker classifier is used for the initial two-party case.
- ASR revisions are monotonic per stream. A stale revision cannot replace the
  latest transcript text.
- Every finalized turn produces exactly one timeline record. Duplicate ASR
  finals do not add another entry.
- A remote endpoint with non-empty text marks only a candidate end. Its stable
  final starts reply, private commentary, and rolling-summary jobs concurrently
  through three independently configured AIGate clients.
- A remote final received while the local-user stream is `speaking` or at a
  `candidate_end` is overlap, not a reply opportunity. It keeps its timeline
  entry but suppresses the draft until a later remote final follows local-user
  finalization.
- A finalized user turn starts private commentary and a rolling summary.
- Remote speech supersedes the active reply and cancels commentary and summary
  work. User speech cancels an active reply.
- Cancellation discards only unfinished provider output, reasoning, and tool
  work and closes the active SSE response so superseded upstream work does not
  keep occupying provider capacity. Finalized transcript lines remain in
  coordinator state, so the next silence-triggered request contains both earlier
  and newly finalized speech.
- A result is visible only when its generation ID and transcript revision still
  match the coordinator's active state; a changed revision discards stale
  background output.
- Coach and Story jobs carry a fixed 60-second application deadline. Claudebox
  repository calls allow 120 seconds per outbound operation and 240 seconds for
  a replacement to wait for an accepted superseded run before continuing in
  the same workspace. Expiry becomes a typed failed result and cannot stop
  capture or later turns.
- An expected PipeWire or Talkies stream failure marks only its audio channel
  `reconnecting`, clears that channel's meter, and retries it. The peer channel,
  web server, transcript, and provider state remain alive. Switching a route
  cancels and replaces only that channel. Error details stay in the structured
  diagnostic log and never enter browser snapshots.
- A `pw-record` subprocess that exits without one complete PCM frame is a
  capture failure even when its exit status is zero. A silent capture-only
  session is never a successful live session.
- A completed rolling summary is a bounded prefix for later provider contexts.
  It can trim only transcript lines that it covers; newer lines remain visible.
- A rolling summary treats transcript text as untrusted ASR output. It retains
  an established fact across conflicting later wording unless a speaker
  explicitly corrects it, closes a prior question when a speaker directly
  answers it even hesitantly or outside the requested format, and does not infer
  unsupported qualifiers.
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
- [`vad.py`](../src/two_x_brainz/vad.py) adapts arbitrary 20 ms capture frames
  to the bundled Silero model's exact inference window, validates every speech
  probability, and converts model failures into typed capture failures. Each
  role owns a separate stateful detector.
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
  validated IPv4 address without reading credentials. It runs only when a named
  Docker network is selected; under the default `LIVE_NETWORK=host` the
  container already shares the host's resolver and needs no host-side Python.
- [`transcript.py`](../src/two_x_brainz/transcript.py) performs revision-safe
  transcript reconciliation and retains a bounded recent window after a
  newer rolling summary is accepted.
- [`turns.py`](../src/two_x_brainz/turns.py) remains ASR-signal-first after the
  local VAD closes a transport: an endpoint creates a candidate end, and only a
  stable final completes a turn. It also exposes per-role active speech so
  overlap cannot be mistaken for a completed reply opportunity.
- [`coordinator.py`](../src/two_x_brainz/coordinator.py) owns timeline entries,
  reply priority, overlap suppression, cancellation, provider deadlines,
  stale-result rejection, and bounded completed result queues. Reply guidance
  is display-only: it never becomes an input to a later provider request.
- [`claudebox.py`](../src/two_x_brainz/claudebox.py) runs Reply through
  AIGate's direct OpenAI-compatible Claudebox stream. Each Start creates a UUID
  workspace and fresh Claude Code session; later drafts continue in that workspace. Every
  call resends the complete bounded transcript plus accepted running summary.
  The agent may use its native tools to shallow-clone named repositories, fetch
  documentation, and follow relevant links. Requests omit OpenAI client tools,
  explicitly set `X-Aicodebox-No-Tools: 0`, and append instructions with
  `X-Aicodebox-Append-System-Prompt` rather than replacing Claude Code's native
  agent prompt with an OpenAI system message. Plain OpenAI SSE content deltas
  update the Reply feed immediately. Explicit or spoken repository research is
  buffered around the deployed Claudebox tool-stream limitation, and an
  incomplete ordinary stream gets one bounded same-workspace recovery. Native
  tool and private reasoning events remain internal to Claudebox. Superseded
  accepted native work drains without becoming visible; its replacement waits
  and continues in the same workspace. Starting a new listening session detaches
  any old workspace task and never waits for it.
- [`aigate.py`](../src/two_x_brainz/aigate.py) is the OpenAI-compatible
  chat-completions provider used by Coach and Story. It applies
  application-owned token and text-length limits before provider output enters
  application state, then parses CommonMark into a text-only subset. Production
  requests consume bounded SSE and publish correlated activity for Reply,
  Private coach, and Story-so-far flows: streamed output, explicitly exposed
  provider reasoning and bounded activity records.
  Safe inline presentation becomes visible text; structural Markdown and HTML
  are rejected without rendering provider-controlled markup. Providers that do
  not send a visible reasoning field are reported as such; private hidden
  chain-of-thought is neither requested nor fabricated.
- [`terminal.py`](../src/two_x_brainz/terminal.py) retains the bounded,
  control-sequence-safe presentation state shared with the browser. It owns no
  terminal interface. Transcript and provider text remain literal, and audio
  levels are presentation-only.
- [`web.py`](../src/two_x_brainz/web.py) adapts that exact terminal state for a
  loopback FastAPI/Svelte console. FastAPI serves the compiled assets and a
  same-origin WebSocket. Browser pause and resume controls feed the existing
  strict control queue; process shutdown remains with the owning terminal so
  the web console cannot terminate the server that serves its own page.
  Bounded structured snapshots update separate conversation, reply,
  private-coach, story, and all-candidate setup-meter panels. The bidirectional
  WebSocket carries browser controls plus SSE-style append-only activity.
  Streamdown incrementally renders incomplete provider Markdown in one flat
  chronological feed per output kind. Status, visible reasoning, tool activity,
  and output retain arrival order; every reasoning or tool row starts
  independently collapsed rather than living in a grouped generation card.
  Cumulative reasoning and output snapshots coalesce independently within each
  flow across interleaved concurrent flows and across each other; a same-flow
  tool or lifecycle event is the boundary that starts a new chronological row.
  Svelte flexes expanded guidance into height released by collapsed panels and
  owns browser-local layout plus safe runtime settings. Reply, Coach, and Story
  model/reasoning assignments, the Talkies model, session brief, enabled research
  policy, and audio names are sent as one validated settings snapshot. Saved
  Reply reasoning is normalized to Claudebox's supported values before that
  snapshot is sent. Credentials and endpoints remain environment-only.
- [`logging_config.py`](../src/two_x_brainz/logging_config.py) writes every
  runtime event to a credential-redacted rotating JSON log. It is the durable
  reconstruction surface; it retains text events but never PCM. DEBUG mode also
  traces the provider SSE parser, activity retention/coalescing, snapshot
  delivery, frontend snapshot receipt, and feed rendering with bounded metadata.
  Per-token cumulative snapshots use counts at DEBUG rather than full raw INFO
  records; terminal stream completion retains the final reasoning and output.
  The browser returns only allowlisted diagnostic event names and numeric counts
  through the existing same-origin WebSocket.

For module-level invariants and file ownership, see
[`src/two_x_brainz/README.md`](../src/two_x_brainz/README.md).

## Deliberate limits

The runtime uses the local Svelte console for `live`, not a graphical overlay,
terminal dashboard, or raw JSON event stream. It never speaks or sends a draft
automatically, stores no audio, and does not attempt multi-speaker diarization
on the mixed system stream. Those boundaries prevent the always-on assistant
from becoming an invisible actor.

Live and replay use the same JSON record shapes for transcript, turn, timeline,
draft, commentary, and summary events. Replay prints them for deterministic
test consumers; live writes them to the rotating log while rendering the
dashboard. Every record carries `schema_version: 1`. Turn and timeline records
share an opaque `turn_id`; generated records carry opaque `generation_id` and
`trigger_turn_id` fields. These support local correlation without exposing a
session ID, stream ID, audio bytes, or credentials.
