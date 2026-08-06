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
TWOXBRAINZ_AIGATE_REPLY_MODEL=cerebras-glm-4.7
TWOXBRAINZ_AIGATE_REPLY_REASONING_EFFORT=none
TWOXBRAINZ_AIGATE_COACH_MODEL=your-coaching-model
TWOXBRAINZ_AIGATE_COACH_REASONING_EFFORT=none
TWOXBRAINZ_AIGATE_SUMMARY_MODEL=your-summary-model
TWOXBRAINZ_AIGATE_SUMMARY_REASONING_EFFORT=none
TWOXBRAINZ_AIGATE_TOKEN=your-gateway-token
TWOXBRAINZ_TALKIES_MODEL=nemotron-3.5-asr-0.6b
```

**2. Inspect the audio nodes if you want to.** `make run` presents the
compatible microphone and system-output nodes in its in-app Sources view,
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
node is unavailable later, its channel waits and retries while the other
channel keeps running.

**3. Go.**

```bash
make run
```

Open `http://127.0.0.1:7860`. The browser starts idle and opens Sources when no
usable pair is saved. Select a microphone and system-audio source, then press
**Start listening**. The saved pair is reused on later runs. Sources remains
available throughout the session; redetection and changes apply to the affected
capture channel without restarting the page or discarding the conversation.

`--network host` is the default because it just works: it reaches AIGate on the
port it already publishes, and resolves tailnet names the same way your shell
does. No hunting for which Docker network the gateway is on. If you'd rather join
one explicitly for benchmark or fixture targets, set `LIVE_NETWORK=<name>` and
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

`make run` builds the production image and serves the Svelte operator console at
`http://127.0.0.1:7860` (override with `WEB_PORT=9000 make run`).

The app opens **idle**. It does not open either PipeWire capture process or send
audio to Talkies until you press **Start listening**. **Stop listening** pauses
both capture gates but keeps the browser, transcript, story, and guidance alive.

The top bar provides:

- three searchable AIGate model pickers—one each for Reply, Private coach, and
  Story so far—with result counts, readable scrolling inventories, visible
  current-model markers, and automatic positioning on each selected model.
  Every flow also has its own reasoning effort (`Default`, `Minimal`, `Low`,
  `Medium`, or `High`); all six choices persist across runs; and
- separate live Reply, Private coach, and Story-so-far generation flows. Each is
  one continuous chronological stream: status, visible reasoning, tool activity,
  and streamed Markdown appear inline in arrival order. Every reasoning and tool
  row starts independently collapsed; there are no per-generation cards or
  grouped trace boxes. Cumulative reasoning and output snapshots coalesce per
  flow even while Reply, Coach, and Story run concurrently, so token updates do
  not become duplicate `Thinking` rows. It never fabricates or claims access to
  hidden chain-of-thought.

Conversation, Reply, Private coach, and Story so far remain separate scrollable,
collapsible, resizable panels. Expanded panels consume all height released by
collapsed siblings, and their layout persists in browser-local storage. Each feed
auto-follows new events only while it is already at the bottom, so scrolling back
through the full activity history is not interrupted.

**Sources** opens audio settings. Every visible microphone and system-audio
candidate has its own live meter. While the modal is open, the inventory
automatically refreshes; **Redetect devices** requests an immediate refresh.
Disconnected Bluetooth or USB nodes disappear and returning nodes reappear.
Saving a new pair applies it immediately. Each capture side is supervised
independently: if the microphone disappears, system audio keeps transcribing
while the microphone retries; reconnecting the same node or selecting a
replacement restores only that side. The source strip shows each channel's
`idle`, `ready`, `switching`, or `reconnecting` state.

Guidance is advisory: it is never sent or spoken, has no accept/dismiss state,
and is not supplied back to the model. Each generation uses the current
transcript and rolling story.

Every runtime event goes to a rotating local JSON log:

```bash
make logs
```

Set `TWOXBRAINZ_LOG_LEVEL=DEBUG` to trace the full streaming handoff without
recording credentials or raw provider payloads: AIGate SSE connection/events,
activity retention or coalescing, WebSocket snapshot delivery, browser receipt,
and each Reply/Coach/Story feed render. Browser diagnostics are a strict finite
schema of event names and numeric counts sent back over the same-origin
WebSocket; browser code cannot choose arbitrary log messages or fields.
High-frequency cumulative snapshots are logged as character counts at DEBUG;
the final reasoning/output and lifecycle records remain in the reconstruction
log without copying every growing snapshot into INFO.

See [Data handling](#data-handling) before sharing that file.

## AIGate integration

2xbrainz is intentionally AIGate-only. `TWOXBRAINZ_AIGATE_TOKEN` is the sole
credential and is sent to AIGate's chat, Talkies proxy, model inventory, and
allowlisted MCP endpoints. The application derives
`ws(s)://<aigate-host>[/prefix]/talkies/v1/audio/transcriptions/stream` from
`TWOXBRAINZ_AIGATE_URL`, which must end in `/v1`.

For optional current-context help, set `TWOXBRAINZ_WEB_RESEARCH_ENABLED=true`
and enable AIGate with `SEARXNG=1` (`PISTON=1` additionally enables arithmetic).
The reply model sees only `research_web` and bounded arithmetic-only
`execute_code`; it never receives AIGate's full tool catalog. `research_web`
accepts either a focused query or an exact discovered URL. It searches through
AIGate, selects a clearly matching public result, downloads that page locally,
and returns bounded, link-preserving Markdown extracted with Trafilatura. HTML
tables, lists, and prose retain their links; raw Markdown pages are supported;
and relative links are resolved so the model can follow relevant `docs/*.md`
pages in the next tool round. It does not depend on AIGate's browser MCP.
Redirects, credentials, unsupported content, oversized pages, and non-public DNS
destinations are rejected. The model may issue up to three distinct research or
calculation calls concurrently in each of two bounded rounds. Tool and provider
failures appear with a bounded reason in the relevant Reply activity stream.
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
PipeWire system ─────┘     (same ASR model)    └─ local web console
                                                   └─ rotating JSON log
```

- **Talkies** is the ASR boundary. Its native WebSocket streams revisioned
  partial, endpoint, and final events.
- **The coordinator** owns reconciliation, turn state, cancellation, and throwing
  away stale results. It writes one timeline entry per finalized turn and starts
  reply, private coaching, and rolling-story work concurrently after a remote
  final. Every provider call has a hard 60-second deadline, so a wedged model
  cannot hold the session hostage. Cancelling an unfinished provider request
  never removes finalized transcript state; the next silence-triggered request
  is rebuilt from the complete current transcript. Only unfinished model output,
  reasoning, and tool work are discarded.
- **The text provider** only ever sees a minimized speaker-tagged transcript.
- **The web console** is the sole live operator surface. Its loopback-only
  WebSocket carries controls and SSE-style incremental activity in one
  bidirectional channel. The parallel reconstruction log carries
  schema-versioned records with opaque turn/generation IDs.

More detail in [docs/architecture.md](docs/architecture.md).

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
make test-browser  # compiled UI in a real browser; self-cleaning containers
make build         # production image
make replay        # the bundled fixture
```

`make test` never loads `.env` and never calls Talkies or AIGate — the providers
and audio boundaries are mocked or local protocol fixtures. Every `test*`
target removes the exact local Docker image tags it builds, on success or
failure. `make test-browser` starts uniquely named `--rm` fixture and browser
containers, verifies the compiled console through the digest-pinned stealth
browser, and stops both from an exit trap even when an assertion or interrupt
fails. The targets that do hit real services (`make live-fixture`,
`make test-real`, `make live-product-fixture`, `make benchmark`) are deliberately
separate, need a real `.env`, and are not part of `make test` or CI.
`make test-real` sends Reply, Coach, and Story prompt checks concurrently through
three distinct defaults (`cerebras-glm-4.7`, `claudebox-sonnet`, and
`pibox-zai-glm-5-turbo`). It also verifies that AIGate advertises at least two
requests for the selected Talkies model and completes two native ASR streams
concurrently against the bundled CC0 WAV. Use `make test-real-talkies` to run
only that provider check. The Reply check requires the model itself to recognize
that public documentation is needed, call `research_web`, consume the fetched
page, and finish a one-line spoken response. See
[docs/configuration.md](docs/configuration.md#real-provider-test-tiers) for the
full contract.

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
