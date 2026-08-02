# ASR evaluation

The native streaming path and OpenAI-compatible file-transcription route must
be compared using the same audio source. This project provides a small,
versioned CC0 speech fixture for deterministic transport and schema checks; it
does not claim that a short clip measures production transcription quality.

## Fixture

[`tests/fixtures/commons-audio-cc0.wav`](../tests/fixtures/commons-audio-cc0.wav)
is a 2.4-second speech recording from
[Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Audio.wav), released
by its uploader under CC0-1.0. Its expected SHA-256 is documented next to the
fixture and enforced by a unit test.

The evaluation loader accepts a bounded uncompressed PCM WAV with one or two
channels. It holds the original WAV for the file route and makes a separate
in-memory 16 kHz mono PCM16LE representation for the native stream. The loader
rejects symlinks, empty files, oversized files, unsupported channels, compressed
audio, and invalid WAV structure before it opens a network connection.

## What remains a release gate

Use a larger, consented or separately licensed corpus on the target machine to
choose the production model. Measure each candidate alone, with two same-model
streams, and with the configured drafting path active. Record capture-to-
partial, endpoint, and final latencies as well as sustained CPU and memory use.

For the full design and the distinction between transport verification and a
model-quality decision, see [the architecture overview](architecture.md) and
[the launch profile](decisions/0001-mvp-launch-profile.md).

## Contract command

After Talkies is reachable on the selected Docker network and `.env` contains
the required Talkies settings, run:

```bash
make benchmark LIVE_NETWORK=<network>
```

Set `TALKIES_MODEL=<slug>` to override the model in `.env` for one run. The
following native candidates are available in the accompanying Talkies release:

- `nemotron-3.5-asr-0.6b`
- `sherpa-zipformer-en-left-64`
- `sherpa-zipformer-en-left-128`
- `sherpa-zipformer-en-int8-left-64`
- `sherpa-zipformer-en-int8-left-128`
- `vosk-small-en-us-0.15`

Before the fixture is streamed, the command requests Talkies'
OpenAI-compatible `GET /v1/models` inventory. The configured model must be
present there; otherwise the command stops before opening native streams or
uploading the WAV. This makes an incomplete model deployment an explicit
configuration failure rather than a partial benchmark.

To run all six sequentially, start Talkies with those model slugs enabled and
run `make benchmark-candidates LIVE_NETWORK=<network>`. This is a transport and
resource measurement loop, not an automatic model-selection decision.

To add the configured drafting path to the same concurrent check, set
`TWOXBRAINZ_AIGATE_MODEL` and run:

```bash
make benchmark-with-draft LIVE_NETWORK=<network>
make benchmark-candidates-with-draft LIVE_NETWORK=<network>
```

Those targets send one fixed synthetic text-only draft request while the two
native streams are active. They do not send fixture audio or ASR transcript text
to AIGate. The report exposes only the aggregate `draft_elapsed_seconds` timing;
it does not print the request or provider response.

Set `BENCHMARK_AUDIO=<path-to-wav>` to use another bounded PCM WAV. The command
streams the normalized 16 kHz mono representation concurrently through
role-labelled `user` and `remote` native WebSocket streams, then submits the
original WAV to the OpenAI-compatible file endpoint using both `json` and
`verbose_json`. Each stream requires a final event and terminal statistics with
an exact sent-frame count; the file responses require `{ "text": ... }` and a
fully typed verbose response. The terminal report contains timing and count
metadata only; it does not print transcription text or fixture bytes.

For a quality measurement, set `BENCHMARK_REFERENCE_FILE=<path-to-utf8-text>`.
The local file is mounted read-only and compared with the final native event and
both file-route results after Unicode, case, and punctuation normalization. The
report exposes only aggregate word error rates (`word_error_rate` per native
stream, plus `batch_json_word_error_rate` and
`batch_verbose_json_word_error_rate`); it never exposes the reference path,
reference text, or recognized text. The reference must be a regular non-empty
UTF-8 file no larger than 64 KiB, and each evaluated transcript is limited to
2,048 normalized words.
