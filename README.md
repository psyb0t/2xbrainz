# 2xbrainz

[![CI](https://github.com/psyb0t/2xbrainz/actions/workflows/pipeline.yml/badge.svg?branch=main)](https://github.com/psyb0t/2xbrainz/actions/workflows/pipeline.yml)
[![version](https://raw.githubusercontent.com/psyb0t/2xbrainz/badges/version.svg)](https://github.com/psyb0t/2xbrainz/releases)
[![license](https://raw.githubusercontent.com/psyb0t/2xbrainz/badges/license.svg)](LICENSE)
[![Docker Pulls](https://img.shields.io/docker/pulls/psyb0t/2xbrainz?style=flat-square)](https://hub.docker.com/r/psyb0t/2xbrainz)

A second brain for conversations you're already having. It listens to your mic
and your speakers as two separate streams, transcribes both live, works out when
the other side actually stopped talking, and hands you a reply draft before you've
finished panicking about what to say.

Nothing is sent for you. Nothing is spoken for you. It drafts, you decide.

## Contents

- [What this fucker does](#what-this-fucker-does)
- [Requirements](#requirements)
- [Try it with zero setup](#try-it-with-zero-setup)
- [Quick start](#quick-start)
- [Driving it while it runs](#driving-it-while-it-runs)
- [Deployment shapes](#deployment-shapes)
- [Architecture](#architecture)
- [Data handling](#data-handling)
- [Development](#development)
- [Project layout](#project-layout)
- [License](#license)
- [Changelog](#changelog)

> [!WARNING]
> This thing listens to both sides of a conversation. Recording people has rules
> and they are not the same everywhere. Getting consent is your job, not the
> software's. It also ships as alpha — live capture is Linux/PipeWire only.

## What this fucker does

- **Two streams, not one blob** — your mic and your system audio go to the ASR
  separately, so it always knows who said what without guessing from a mixdown.
- **Knows when they shut up** — a bundled Silero neural VAD independently
  bounds each live audio stream before Talkies finalizes it. Revisioned ASR
  events are then reconciled into actual turns, so a draft fires on a finished
  thought instead of background noise or a permanently open microphone.
- **Drafts a reply, keeps its mouth shut** — no auto-send, no auto-speak, no
  injecting text into your chat window. The live dashboard shows the suggestion;
  what you do with it is your business.
- **Won't talk over you** — a remote turn landing while you're still speaking is
  recorded but does not trigger a draft.
- **Runs against your own gear** — [AIGate](https://github.com/psyb0t/aigate)
  with [Talkies](https://github.com/psyb0t/docker-talkies): one host and one
  token for ASR, drafts, search, and bounded calculations.
- **Audio never leaves the box** — 2xbrainz streams PCM only to Talkies behind
  your AIGate. AIGate's provider and privacy policy determine where any
  transcript-derived text is sent.

## Requirements

- Docker
- Linux with PipeWire, for live capture (the replay path needs neither)
- [AIGate](https://github.com/psyb0t/aigate) with Talkies enabled. It is the
  only service 2xbrainz connects to.

## Try it with zero setup

No microphone, no services, no `.env`. This replays a bundled synthetic
conversation through the real image and prints the timeline, the draft, and the
summary it would have produced:

```bash
git clone https://github.com/psyb0t/2xbrainz.git

docker run --rm --init --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --cap-drop ALL --security-opt no-new-privileges:true \
  -v "$PWD/2xbrainz/examples:/examples:ro" \
  psyb0t/2xbrainz replay --events /examples/conversation.jsonl
```

If that prints replay records, the image works and you can go wire up the real
thing.

## Quick start

**1. Set up AIGate, then write a `.env`.** Grab [`.env.example`](.env.example)
for the full list. AIGate keeps Talkies on a private network and serves it at
`/talkies/`; 2xbrainz derives that path from the one AIGate API root:

```bash
TWOXBRAINZ_AIGATE_URL=http://localhost:4000/v1
TWOXBRAINZ_AIGATE_MODEL=your-configured-model
TWOXBRAINZ_AIGATE_TOKEN=your-gateway-token
TWOXBRAINZ_TALKIES_MODEL=nemotron-3.5-asr-0.6b
```

**2. Inspect the audio nodes if you want to.** `make run` and `make run-web` present the same
compatible microphone and system-output nodes in its in-app Audio Setup view,
so this is only for checking what PipeWire exposes:

```bash
make devices
```

It prints JSON objects with `id`, `name`, and `media_class`, plus an optional
friendly `description` and PipeWire `default_role`. Audio Setup shows only
capture-safe choices:

```
Audio/Source    alsa_input.pci-0000_00_1f.3.analog-stereo      <- microphone
Audio/Source    alsa_output.pci-0000_00_1f.3.analog-stereo.monitor <- system audio
Audio/Sink      bluez_output.AC_12_2F_00_11_22.1
Stream/Output/Audio   Firefox
```

Audio Setup accepts one non-monitor **`Audio/Source`** microphone and one system
audio capture source: either an **`Audio/Source`** monitor (`*.monitor`) or a
directly capturable **`Audio/Sink`**. It excludes `Stream/*` application entries.
The PipeWire configured default microphone and default output are marked
`[DEFAULT]` and sorted first. That is a recommendation, not proof of
per-application routing. Audio Setup meters **every displayed candidate at the
same time**: speak into the mic and play system audio, then select the two rows
whose independent meters react. The chosen stable node names are saved to
`$XDG_CONFIG_HOME/2xbrainz/audio-selection.json` (or
`~/.config/2xbrainz/audio-selection.json`) with mode `0600`. If either saved
node is unavailable later, it asks again.

**3. Go.**

```bash
make run
```

On the first launch, the Textual app opens Audio Setup: select the microphone,
then the system audio source with the arrow keys, Enter, or the mouse. On later
launches it reuses the saved pair and opens the dashboard directly. Press `F3`
at any time to return to Audio Setup; a change made while a call is running is
saved for the next live session so current capture is not interrupted. The web
console exposes the same controls in its **Audio setup** panel. A missing or
stale saved pair opens setup automatically.

`--network host` is the default because it just works: it reaches AIGate on the
port it already publishes, and resolves tailnet names the same way your shell
does. No hunting for which Docker network the gateway is on. If you'd rather join
one explicitly for the TUI or benchmark targets, set `LIVE_NETWORK=<name>` and
swap `localhost` for the gateway's service name in `.env`. Named-network mode
uses the repository's small host-side Python helper to validate an optional
hostname mapping. The loopback-only web console intentionally requires the
default host network.

Run `make doctor` if something's wrong — it prints the resolved configuration with
secrets masked, so it is safe to paste.

Those resource limits are a ceiling, not a requirement. This container doesn't
transcribe anything — it runs two `pw-record` captures, normalizes the PCM, and
pushes it down a WebSocket. The transcription cost lives in Talkies, in its own
container, where these flags have exactly zero effect. If the ASR is lagging,
give **Talkies** more (or use its CUDA variant); raising this number will not
help.

## Driving it while it runs

`make run` opens an interactive Textual operator console. `make run-web` opens
the same live session in a responsive Svelte browser console at
`http://127.0.0.1:7860` (override with `WEB_PORT=9000 make run-web`). A compact status bar
shows the session state, active operation (for example, `Calling reply LLM`),
and elapsed timers. Below it, MIC INPUT and SYSTEM AUDIO show the selected
friendly names and independent live levels. The browser console keeps
Conversation, Reply suggestion, Private coach, and Story so far in separate
scrollable, collapsible, resizable panels instead of collapsing them into one
feed. Panel state and split sizes persist in browser-local storage. Source
selection lives in a modal with a live meter beside every microphone and
system-audio candidate. Sink candidates are captured through PipeWire monitor
ports, so their meters represent playback routed to that output rather than a
fallback microphone. The browser exposes Sources, Pause, and Resume; session
shutdown remains in the owning terminal so stopping the process cannot strand a
broken browser page. The terminal conversation and guidance panes retain independent
scrolling and focused views; click a pane or move focus to it, then use the
mouse wheel or Up/Down, Page Up/Down, Home, and End to read it. New content
auto-follows only while that pane remains at its bottom. Press `F2` to cycle
split, full-conversation, and full-guidance views, or `F3` to change the saved
audio pair. Type a command in the input area and press Enter:

| Command | What happens |
|---|---|
| `pause` / `resume` | Stop and restart capture |
| `stop` | Shut down the terminal session cleanly |

`Ctrl+P`, `Ctrl+R`, `Ctrl+X`, and `Ctrl+Q` pause, resume, stop, and quit
cleanly; `Ctrl+C` follows the same clean stop path while the terminal UI has
focus. Guidance is advisory: it is never sent or spoken, requires no
accept/dismiss step, and is not supplied back to the model. Each new generation
uses the current transcript and rolling story only.

The dashboard stays clean. Every runtime event goes to a rotating local JSON log
instead, which you can follow in another terminal:

```bash
make logs
```

See [Data handling](#data-handling) before sharing that file.

## AIGate integration

2xbrainz is intentionally AIGate-only. `TWOXBRAINZ_AIGATE_TOKEN` is the sole
credential and is sent to AIGate's chat, Talkies proxy, model inventory, and
allowlisted MCP endpoints. The application derives
`ws(s)://<aigate-host>[/prefix]/talkies/v1/audio/transcriptions/stream` from
`TWOXBRAINZ_AIGATE_URL`, which must end in `/v1`.

For optional current-context help, set `TWOXBRAINZ_WEB_RESEARCH_ENABLED=true`
and enable AIGate's SearXNG MCP service. The model sees only `search_web` and a
bounded arithmetic-only `execute_code` tool; it never receives AIGate's full
tool catalog. It may make up to three independent tool calls concurrently, then
uses the returned bounded results to produce its final spoken draft. AIGate's
SearXNG configuration controls which public search engines receive a query.
Obvious structured private identifiers are rejected before a query leaves the
app; do not enable research for conversations whose unfamiliar terms are
themselves private.

Set `TWOXBRAINZ_SESSION_BRIEF` to optional trusted context such as the purpose
of the call and the local user's role. The bounded brief frames replies,
coaching, and the running story without becoming transcript or log content.

Full reference: [configuration](docs/configuration.md).

## Architecture

```text
PipeWire microphone ─┐                         ┌─ text draft
                     ├─ Talkies native WS ─────┤
PipeWire system ─────┘     (same ASR model)    └─ TUI or local web console
                                                   └─ rotating JSON log
```

- **Talkies** is the ASR boundary. Its native WebSocket streams revisioned
  partial, endpoint, and final events.
- **The coordinator** owns reconciliation, turn state, cancellation, and throwing
  away stale results. It writes one timeline entry per finalized turn, puts the
  reply draft first, and only runs commentary or the rolling summary when nothing
  more important is pending. Every provider call has a hard 60-second deadline, so
  a wedged model can't hold the session hostage.
- **The text provider** only ever sees a minimized speaker-tagged transcript.
- **The CLI** is terminal-first by design. Its interactive dashboard stays
  human-readable; the parallel reconstruction log carries schema-versioned
  records with opaque turn/generation IDs.

More detail in [docs/architecture.md](docs/architecture.md). The MVP platform
boundary is recorded in [ADR-0001](docs/decisions/0001-mvp-launch-profile.md);
the bounded reconstruction-log retention decision is in
[ADR-0002](docs/decisions/0002-persistent-reconstruction-log.md).

## Data handling

Raw audio is never written to disk. Each `make run` session writes a structured
JSON event log such as [`logs/20260804T211408123456Z_2xbrainz.log`](logs/.gitkeep)
beside the directory where you ran it, so a session can be reconstructed after
it stops. Each session file rotates at 5 MB and retains at most three numbered
backups. `make logs` follows the newest session; set `LOG_FILE=<filename>` to
select another one. Use
`LOG_DIRECTORY=/absolute/host/path make run` to mount another host directory.

That log contains transcript text, timeline entries, reply drafts, commentary,
summaries, and runtime diagnostics. Treat it as sensitive conversation data;
do not paste it into tickets or share it casually. It never contains raw PCM.
Credential-shaped fields are redacted, and the config object drops its tokens
and session brief from its own `repr`. `make run` forces the mounted `logs/`
directory to mode `0700`, and active plus rotated log files are mode `0600`.

The one thing that does get written is a redacted trace from the real-service test
fixtures, under the gitignored `.testing/fixture-traces/`. It holds fixed
synthetic text and the resulting records — never PCM, never host device names,
never tokens.

## Development

Everything runs in containers; you don't need Python on your host.

```bash
make help          # every target
make lint          # ruff + pyright + shellcheck
make test          # unit + integration, offline and deterministic
make build         # production image
make replay        # the bundled fixture
```

`make test` never loads `.env` and never calls Talkies or AIGate — the providers
and audio boundaries are mocked or local protocol fixtures. Every `test*`
target removes the exact local Docker image tags it builds, on success or
failure. The targets that do hit real services (`make live-fixture`, `make test-real`,
`make live-product-fixture`, `make benchmark`) are deliberately separate, need a
real `.env`, and are not part of `make test` or CI.

Picking an ASR model? [docs/asr-evaluation.md](docs/asr-evaluation.md) covers the
benchmark targets and the fixture's provenance.

`make pkg-add`, `make pkg-update`, `make pkg-remove` and `make pkg-upgrade` are
the only supported ways to touch dependencies — they refresh the supply-chain age
gate before the lockfile moves.

## Project layout

```text
src/two_x_brainz/  — application modules and CLI
tests/             — unit and integration tests
examples/          — synthetic replay fixtures
docs/              — architecture, configuration, ASR evaluation
scripts/           — development tooling
```

## License

WTFPL. See [LICENSE](LICENSE). Do what the fuck you want to. Runtime and
frontend dependency notices are documented in [THIRD_PARTY.md](THIRD_PARTY.md).

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
