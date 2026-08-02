# 2xbrainz

[![CI](https://github.com/psyb0t/2xbrainz/actions/workflows/pipeline.yml/badge.svg?branch=main)](https://github.com/psyb0t/2xbrainz/actions/workflows/pipeline.yml)
[![version](https://raw.githubusercontent.com/psyb0t/2xbrainz/badges/version.svg)](https://github.com/psyb0t/2xbrainz/releases)
[![license](https://raw.githubusercontent.com/psyb0t/2xbrainz/badges/license.svg)](LICENSE)
[![Docker Pulls](https://img.shields.io/docker/pulls/psyb0t/2xbrainz?style=flat-square)](https://hub.docker.com/r/psyb0t/2xbrainz)

Docker-first, local-first conversation copilot: it captures separate microphone
and system-audio streams, transcribes them through Talkies, detects remote turn
ends, and produces a human-gated reply draft through AIGate.

## Contents

- [Quick start](#quick-start)
- [Live Linux/PipeWire use](#live-linuxpipewire-use)
- [Architecture](#architecture)
- [Development workflow](#development-workflow)
- [Project layout](#project-layout)
- [Data handling](#data-handling)
- [License](#license)
- [Changelog](#changelog)

**Status:** alpha. The replay workflow and Docker runtime are implemented. Live
capture currently targets Linux PipeWire and requires running Talkies and
AIGate services on a Docker network selected by the operator.

## Quick start

The replay path is self-contained and proves the CLI image, transcript state,
turn detection, priority scheduling, and JSON-line rendering without audio
hardware or external models. It emits `timeline`, `draft`, and `summary`
records for the synthetic remote turn.

```bash
make pkg-lock
make replay
```

Run the complete validation suite in containers:

```bash
make lint
make test
make build
make run
```

`make test` is deterministic and offline: its providers and audio boundaries
are mocked or local protocol fixtures. It never loads `.env` or calls AIGate
or Talkies.

## Live Linux/PipeWire use

First list the PipeWire nodes available through the host runtime socket:

```bash
make devices
```

Create a gitignored `.env` based on [`.env.example`](.env.example), then
set the Talkies and AIGate service URLs, configured model slugs, and only the
tokens those services require. Start the two-stream session by naming the
Docker network where those services are reachable:

```bash
make live MIC_NODE=<microphone-node> SYSTEM_NODE=<system-node> LIVE_NETWORK=<network>
```

The live runtime defaults to eight CPUs, 1 GiB memory, and 128 processes so
two CPU-ASR streams are not artificially pinned to one core. Tune only after
benchmarking the selected Talkies model on the target host:

```bash
make live MIC_NODE=<microphone-node> SYSTEM_NODE=<system-node> \
  RUNTIME_CPUS=<measured-cpu-budget> RUNTIME_MEMORY=<measured-memory-budget>
```

Before selecting a streaming model, exercise the configured Talkies service
with the bundled CC0 speech fixture through two concurrent native streams and
both file routes:

```bash
make benchmark LIVE_NETWORK=<network>
```

To exercise the full `live` orchestration without a physical microphone, host
PipeWire socket, or audio hardware, generate two temporary known-speech WAVs
through the configured direct Talkies TTS route and present them as bounded
fixture capture devices. The default fixture deliberately overlaps the streams:

```bash
make live-fixture LIVE_NETWORK=<network>
```

For a token-only `.env`, pass the same-authority AIGate and native Talkies URLs
as target variables; see [configuration](docs/configuration.md#talkies-tts-fixture-capture).

This is an explicit real Talkies TTS/ASR check, not part of `make test`. It
asserts two final timelines, capture timing comparison, commentary, and the
absence of a reply draft while the remote turn overlaps active local speech.
The AIGate boundary is mocked for this target. It requires
`TWOXBRAINZ_TALKIES_WS_URL`, `TWOXBRAINZ_TALKIES_MODEL`, and the shared
`TWOXBRAINZ_AIGATE_TOKEN` in the gitignored environment file. It defaults to
the fast `kokoro-82m-nvidia` TTS model; set `FIXTURE_TTS_MODEL` or
`FIXTURE_TTS_VOICE` to choose another configured Talkies TTS model or voice.
The harness never persists generated audio or credentials. Each real fixture
does retain one redacted reconstruction trace below
`.testing/fixture-traces/`; it contains fixed synthetic fixture text and the
resulting CLI/model records, structured runtime logs, and its terminal
assertion outcome. It never contains PCM bytes, host device names, or tokens.

To test the configured PIBOX GLM model without requiring Talkies, run the
synthetic-text prompt and interview-story check. It calls AIGate model
inventory, reply, commentary, and summary routes, verifies that a multi-turn
running summary retains a commitment, risk, and interviewer question, then
proves the next reply request receives that summary:

```bash
make test-real LIVE_NETWORK=<network>
```

`test-real` defaults to `pibox-zai-glm-5-turbo`; override
`FIXTURE_AIGATE_MODEL=<configured-model>` when comparing a different model.
Its JSON result prints the container-side trace path; the corresponding host
file is below `.testing/fixture-traces/`.
To exercise full story reconstruction with deterministic local AIGate, run the
four-turn interview fixture. It alternates local commitment, interviewer
question, local mitigation, and final verification question:

```bash
make live-interview-fixture LIVE_NETWORK=<network>
```

To prove that same flow against real Talkies TTS/ASR and real AIGate generation,
run:

```bash
make live-product-fixture LIVE_NETWORK=<network>
```

Both interview targets retain a redacted JSONL trace with synthesized inputs,
playback releases, CLI records, runtime diagnostics, provider results, and
final assertions. A provider deadline is a failed fixture result, not a silent
pass. When a native ASR backend finalizes only after `end`, the runtime keeps
capture open and rotates only that ASR segment after detected speech is followed
by bounded silence. The next segment opens only on the next audible frame, so
the configured Nemotron backend is not left with an idle socket while the LLM
processes a prior turn. This supports the multi-turn fixture without recreating
either capture process.

To compare the built-in native candidates sequentially, first configure the
Talkies service with all six model slugs enabled, then run:

```bash
make benchmark-candidates LIVE_NETWORK=<network>
```

To include a concurrent AIGate draft-path check, configure
`TWOXBRAINZ_AIGATE_MODEL` and run either `make benchmark-with-draft` or
`make benchmark-candidates-with-draft` with the same `LIVE_NETWORK`. The extra
request has fixed synthetic text; no fixture audio or transcription text reaches
AIGate.

The command emits timing and contract metadata only—never audio bytes or
transcript text. See [ASR evaluation](docs/asr-evaluation.md) for the fixture
license and the larger target-machine benchmark still required for model choice.
Set `BENCHMARK_REFERENCE_FILE=<path-to-utf8-text>` to add aggregate local word
error rates without emitting transcript or reference text.

The command runs the application as the invoking host user, mounts only the
host PipeWire runtime directory read-only, sends both streams to Talkies using
the same configured streaming model, and sends only transcript text to AIGate.
While it runs, enter `pause`, `resume`, `stop`, `accept`, `dismiss`,
`regenerate`, or `edit <replacement text>` on standard input. Draft actions
only apply while their transcript revision is current; the CLI emits a
fixed-shape action JSON line and never sends or speaks an accepted draft.

`TWOXBRAINZ_AIGATE_MODE` defaults to `local`. To allow a remote text provider,
set it to `remote` and explicitly set `TWOXBRAINZ_REMOTE_TEXT_ENABLED=true`.
Without that second value, startup fails before any transcript leaves the
machine.

## Architecture

```text
PipeWire microphone ─┐                         ┌─ AIGate text draft
                     ├─ Talkies native WS ────┤
PipeWire system ─────┘     (same ASR model)    └─ CLI JSON lines
```

- `Talkies` is the continuous ASR boundary. Its native WebSocket delivers
  revisioned partial, endpoint, and final events.
- The coordinator owns reconciliation, turn state, cancellation, and stale
  result rejection. It writes one finalized-turn timeline entry, prioritizes a
  remote reply draft, and runs commentary or a rolling summary only when
  higher-priority work is no longer active. Provider work has a fixed 15-second
  deadline, so an unavailable model cannot hold the session open. A remote
  final received during active local speech is retained in the timeline but
  does not start a reply draft.
- `AIGate` is text-only: it receives the minimized speaker-tagged transcript,
  never PCM audio.
- The CLI is intentionally line-oriented and terminal-first; it does not
  auto-speak, send, inject a draft into another application, or provide a UI.
  Its JSON records are schema-versioned and use opaque turn/generation IDs for
  local correlation only.

See [docs/architecture.md](docs/architecture.md) and
[docs/configuration.md](docs/configuration.md) for the full operating model.
See [docs/asr-evaluation.md](docs/asr-evaluation.md) for fixture provenance and
the model-selection measurement gate.
The selected MVP platform, compute, and retention boundaries are recorded in
[ADR-0001](docs/decisions/0001-mvp-launch-profile.md).

## Development workflow

All supported development commands run inside the development container.
That image pins CPython 3.14.6.

```bash
make help
make format
make lint-fix
make test-unit
make test-integration
make test-real LIVE_NETWORK=<network>
```

`make version` prints the Docker release tag derived from `pyproject.toml`.
`make build` applies that tag, `latest`, and the local development tag to the
same image.

`make pkg-add`, `make pkg-update`, `make pkg-remove`, and `make pkg-upgrade`
are the only supported dependency-mutation paths. They refresh the fixed
supply-chain age gate before changing the lockfile.

## Project layout

```text
src/two_x_brainz/  — typed application modules and CLI
tests/             — unit and WebSocket integration tests
examples/          — synthetic replay fixtures
docs/              — architecture and configuration guides
scripts/           — safe development tooling
```

## Data handling

Raw audio is not persisted by this implementation. Logs are structured and
redact values whose field names look like credentials. Real fixture traces are
an intentional exception for fixed synthetic test transcripts and outputs only;
they are written below the gitignored `.testing/fixture-traces/` directory.
Remote draft mode sends only text to the configured AIGate endpoint. Operators
remain responsible for obtaining every participant's required recording consent.

## License

MIT. See [LICENSE](LICENSE).

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release notes.
