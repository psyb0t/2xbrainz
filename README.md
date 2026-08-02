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
- **Knows when they shut up** — revisioned partial/endpoint/final events get
  reconciled into actual turns, so a draft fires on a finished thought instead
  of mid-sentence.
- **Drafts a reply, keeps its mouth shut** — no auto-send, no auto-speak, no
  injecting text into your chat window. A JSON line appears; what you do with it
  is your business.
- **Won't talk over you** — a remote turn landing while you're still speaking is
  recorded but does not trigger a draft.
- **Runs against your own gear** — [AIGate](https://github.com/psyb0t/aigate)
  with [Talkies](https://github.com/psyb0t/docker-talkies) is the default, one
  host and one token. Standalone Talkies plus any OpenAI-compatible endpoint
  works too.
- **Audio never leaves the box** — the text provider gets transcript text, never
  PCM. And it needs an explicit opt-in before even that goes anywhere remote.

## Requirements

- Docker
- Linux with PipeWire, for live capture (the replay path needs neither)
- An ASR service — [Talkies](https://github.com/psyb0t/docker-talkies)
- An OpenAI-compatible endpoint for the drafts — [AIGate](https://github.com/psyb0t/aigate),
  or OpenAI/Groq/whatever

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

If that prints JSON lines, the image works and you can go wire up the real thing.

## Quick start

**1. Write a `.env`.** Grab [`.env.example`](.env.example) for the full list. One
host, one token — AIGate keeps Talkies on a private network and serves it at
`/talkies/`, so both URLs point at the gateway's published port:

```bash
TWOXBRAINZ_AIGATE_URL=http://localhost:4000/v1
TWOXBRAINZ_AIGATE_MODEL=your-configured-model
TWOXBRAINZ_AIGATE_TOKEN=your-gateway-token
TWOXBRAINZ_TALKIES_WS_URL=ws://localhost:4000/talkies/v1/audio/transcriptions/stream
TWOXBRAINZ_TALKIES_MODEL=nemotron-3.5-asr-0.6b
```

**2. Find your audio nodes.** Same flags as the real thing, so if this works,
step 3 works:

```bash
docker run --rm --init --network host --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --user "$(id -u):$(id -g)" \
  --cap-drop ALL --security-opt no-new-privileges:true \
  -e XDG_RUNTIME_DIR=/pipewire-runtime \
  -v "$XDG_RUNTIME_DIR:/pipewire-runtime:ro" \
  psyb0t/2xbrainz devices | jq -r '.[] | "\(.media_class)\t\(.name)"' | sort
```

The `jq` is only to make it readable — drop it and you get the same data as one
line of JSON, `{"id","name","media_class"}` per node. Two of them are yours:

```
Audio/Sink      alsa_output.pci-0000_00_1f.3.analog-stereo     <- --system-node
Audio/Source    alsa_input.pci-0000_00_1f.3.analog-stereo      <- --mic-node
Audio/Sink      bluez_output.AC_12_2F_00_11_22.1
Stream/Output/Audio   Firefox
```

The rule:

- **`--mic-node`** → the **`Audio/Source`** that is your actual microphone. Ignore
  anything ending in `.monitor`; those are sinks in disguise.
- **`--system-node`** → the **`Audio/Sink`** you're currently listening through.
  You point at the *sink*, not its monitor — `pw-record --target <sink>` captures
  what that sink is playing, which is the other side of the conversation.

Pick the sink that's actually in use. If you're on headphones it's the
`bluez_output.*` or a USB one, not the built-in analog. `Stream/*` entries are
individual apps, not devices — don't use those.

Either the `name` or the numeric `id` works, so
`--system-node 51` is as valid as the full `alsa_output.…` string. Names survive
reboots; ids don't.

**3. Go.**

```bash
docker run --rm --init -i --network host \
  --memory=1g --cpus=8.0 --pids-limit=128 \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --user "$(id -u):$(id -g)" \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --env-file .env \
  -e XDG_RUNTIME_DIR=/pipewire-runtime \
  -v "$XDG_RUNTIME_DIR:/pipewire-runtime:ro" \
  psyb0t/2xbrainz live \
    --mic-node <your-mic-node> \
    --system-node <your-system-node>
```

`--network host` is the default because it just works: it reaches AIGate on the
port it already publishes, and resolves tailnet names the same way your shell
does. No hunting for which Docker network the gateway is on. If you'd rather join
one explicitly, drop `--network host`, add `--network <name>`, and swap
`localhost` for the gateway's service name in `.env`.

Swap `live` for `doctor` if something's wrong — same command, and it prints your
resolved config with the secrets masked, so it's safe to paste.

Those resource limits are a ceiling, not a requirement. This container doesn't
transcribe anything — it runs two `pw-cat` captures, normalizes the PCM, and
pushes it down a WebSocket. The transcription cost lives in Talkies, in its own
container, where these flags have exactly zero effect. If the ASR is lagging,
give **Talkies** more (or use its CUDA variant); raising this number will not
help.

## Driving it while it runs

It reads commands on stdin — that's what the `-i` is for. Type and hit enter:

| Command | What happens |
|---|---|
| `pause` / `resume` | Stop and restart capture |
| `stop` | Shut down cleanly |
| `accept` | Mark the current draft accepted — emits a JSON line, sends nothing |
| `dismiss` | Bin it |
| `regenerate` | Ask for a different draft |
| `edit <text>` | Replace the draft text |

Draft actions only apply while their transcript revision is still current. If the
conversation moved on, the action is rejected instead of acting on stale text.

## Deployment shapes

**AIGate with Talkies** (the default) — one host, one token. Both URLs point at
the gateway, and since they share a host and port the gateway token is reused for
Talkies automatically. You never set `TWOXBRAINZ_TALKIES_TOKEN`.

**Standalone Talkies, drafts from anywhere else** — point each at its own thing:

```bash
TWOXBRAINZ_AIGATE_URL=https://api.openai.com/v1     # or https://api.groq.com/openai/v1
TWOXBRAINZ_AIGATE_TOKEN=your-provider-key
TWOXBRAINZ_TALKIES_WS_URL=ws://talkies:8000/v1/audio/transcriptions/stream
TWOXBRAINZ_TALKIES_TOKEN=your-talkies-token
```

Different hosts, so nothing is shared — your provider key never gets sent to
Talkies and the Talkies token never gets sent to your provider. Shipping
transcript text to a hosted provider also needs
`TWOXBRAINZ_AIGATE_MODE=remote` **and** `TWOXBRAINZ_REMOTE_TEXT_ENABLED=true`.
Set only one and it refuses to start, on purpose, before a single word leaves the
machine.

The URL is an OpenAI-compatible API root and includes the version prefix — `/v1`
for AIGate, OpenAI and Groq alike.

Full reference: [configuration](docs/configuration.md).

## Architecture

```text
PipeWire microphone ─┐                         ┌─ text draft
                     ├─ Talkies native WS ─────┤
PipeWire system ─────┘     (same ASR model)    └─ CLI JSON lines
```

- **Talkies** is the ASR boundary. Its native WebSocket streams revisioned
  partial, endpoint, and final events.
- **The coordinator** owns reconciliation, turn state, cancellation, and throwing
  away stale results. It writes one timeline entry per finalized turn, puts the
  reply draft first, and only runs commentary or the rolling summary when nothing
  more important is pending. Every provider call has a hard 15-second deadline, so
  a wedged model can't hold the session hostage.
- **The text provider** only ever sees a minimized speaker-tagged transcript.
- **The CLI** is line-oriented and terminal-first by design. Records are
  schema-versioned with opaque turn/generation IDs that mean nothing outside your
  own session.

More detail in [docs/architecture.md](docs/architecture.md). The MVP platform and
retention boundaries are recorded in
[ADR-0001](docs/decisions/0001-mvp-launch-profile.md).

## Data handling

Raw audio is never written to disk. Logs are structured JSON and redact fields
whose names look like credentials — and the config object drops its tokens from
its own `repr`, so dumping it whole can't leak them either.

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
and audio boundaries are mocked or local protocol fixtures. The targets that do
hit real services (`make live-fixture`, `make test-real`,
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

WTFPL. See [LICENSE](LICENSE). Do what the fuck you want to.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
