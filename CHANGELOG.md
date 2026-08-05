# Changelog

All notable changes per release. Versions follow
[semantic versioning](https://semver.org).

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
