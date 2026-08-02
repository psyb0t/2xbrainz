# ADR-0001: Linux PipeWire, CPU ASR, and ephemeral sessions

**Status:** accepted
**Date:** 2026-07-31

## Context

2xbrainz continuously captures two audio streams. The launch configuration
must protect that latency-critical path from draft-model contention and make
the conversation-data boundary explicit.

The current launch workspace exposes a 16-thread x86_64 CPU (AMD Ryzen 7
3700X) and no NVIDIA runtime. The application already uses Linux PipeWire
nodes, Talkies native streaming ASR, Docker cgroup limits, and a text-only
AIGate boundary.

## Decision

The MVP supports a Linux x86_64 desktop session with PipeWire. It uses one
same-model, CPU-native Talkies stream for the microphone and one for the mixed
system-output stream. The production container defaults to an eight-CPU,
1 GiB, 128-process budget; operators tune those values only after measuring
their selected Talkies model with both streams active.

Draft generation is independent of the ASR CPU budget. `local` AIGate mode is
the default. A remote text provider is permitted only when
`TWOXBRAINZ_AIGATE_MODE=remote` and
`TWOXBRAINZ_REMOTE_TEXT_ENABLED=true`; it receives minimized transcript text,
never audio.

Sessions are ephemeral. The runtime writes no raw audio, transcript, summary,
timeline, or draft history to disk. Export and deletion commands are therefore
not part of this mode: stopping the process removes its in-memory state.

The supported call-application boundary is deliberately capability-based:
2xbrainz supports applications that expose a microphone node and a separate
PipeWire system-output monitor node. It does not claim compatibility with a
specific calling application until that application has passed the live test.

## Consequences

- Windows and macOS capture are outside the MVP.
- A configured streaming model must be benchmarked on two concurrent streams
  before it becomes the supported default; the current model slug is only a
  configuration default, not a completed performance decision.
- A host without PipeWire-visible microphone and output-monitor nodes cannot
  run `live`, but can use Dockerized replay and diagnostics.
- Local history, encrypted persistence, export, and deletion require a future
  retention-mode decision and separate implementation.

## Verification

Run the Dockerized replay and diagnostics path:

```bash
make lint
make test
make replay
make run
```

For the hardware-specific live gate, run `make devices`, choose the two
PipeWire nodes, then run `make live` with a measured resource budget. See
[`docs/configuration.md`](../configuration.md) and the module-level
[`src/two_x_brainz/README.md`](../../src/two_x_brainz/README.md).
