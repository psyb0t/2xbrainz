# Changelog

All notable changes per release. Versions follow
[semantic versioning](https://semver.org).

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
