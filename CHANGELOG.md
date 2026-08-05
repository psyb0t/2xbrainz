# Changelog

All notable changes per release. Versions follow
[semantic versioning](https://semver.org).

## v1.0.0 — 2026-08-05

Make the loopback Svelte console the sole live operator surface and keep live
sessions usable across audio-device failures.

- **Breaking.** Remove the Textual interface and the `live --output tui|web`
  selector. `make run` now starts the web console; direct callers use
  `live --web-port <port>`.
- Open live sessions idle and require **Start listening** before either PipeWire
  source produces frames. **Stop listening** pauses capture without terminating
  the browser or losing the conversation state.
- Add runtime AIGate model and reasoning-effort selection plus a bounded activity
  trail for request and allowlisted tool phases. The trail excludes prompts,
  tool payloads and results, credentials, and private hidden reasoning.
- Supervise microphone and system audio independently so a disconnected channel
  retries or switches routes without cancelling its peer, the web server, or the
  accumulated transcript and story.
- Add in-app PipeWire redetection and apply persisted source changes immediately
  to the affected channel.
- Remove Textual and its transitive runtime dependencies, synchronize the
  third-party inventory, and expand frontend, WebSocket, routing, configuration,
  and failure-recovery tests.

## v0.2.0 — 2026-08-05

- Add responsive Textual and loopback Svelte operator consoles with independent,
  scrollable conversation, reply, coaching, and story views; persisted browser
  layout preferences; lifecycle status and timers; and clean terminal shutdown.
- Move PipeWire source selection into both consoles, persist validated stable node
  names, expose friendly/default device metadata, and meter every compatible
  microphone and system-audio candidate before selection.
- Add per-stream Silero voice activity detection with pre-roll, hysteresis,
  silence-bounded Talkies segments, and a maximum continuous-segment limit.
- Consolidate ASR, model inventory, drafting, and optional bounded web-search and
  application-validated arithmetic tools behind one AIGate URL and bearer token.
- Give reasoning-capable models bounded generation headroom, retry one blank
  completion, and permit useful mechanisms only as explicitly tentative proposals.
- Write each live session to a UTC-prefixed, credential-redacted rotating JSON log
  while keeping raw PCM out of persistent storage.
- Ship runtime dependency license families and the Svelte MIT notice with the
  production image.
- Gate fixed High-severity image findings and record reviewed, code-path-specific
  CPython non-impact assessments as OpenVEX.
- Expand deterministic unit and integration coverage for audio selection,
  metering, VAD, console controls, browser contracts, logging, and Docker target
  cleanup.

## v0.1.0 — 2026-08-01

Initial alpha release of the Docker-first continuous conversation copilot.

- Capture separate Linux PipeWire microphone and system-audio streams and send
  them to Talkies native streaming ASR.
- Reconcile revisioned transcripts into remote turns, human-gated reply drafts,
  private commentary, and a rolling conversation summary through AIGate.
- Provide deterministic containerized tests, real-provider fixtures with
  redacted reconstruction traces, and model benchmarking for native Talkies
  backends.
- Publish a hardened Docker runtime and configuration documentation for local
  and explicitly enabled remote text-provider operation.
