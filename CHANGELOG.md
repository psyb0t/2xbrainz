# Changelog

All notable changes per release. Versions follow
[semantic versioning](https://semver.org).

## v3.3.0 — 2026-08-21

- Add a second reply lane. A fast, no-thinking model drafts an instant reply
  suggestion in about a second while the considered Reply model keeps thinking,
  so a first draft appears without waiting for the slower answer. Both lanes run
  on the same finished turn and share supersede, cancellation, and the
  local-speech suppression that blocks a reply while you are still talking.
- Default the fast lane to `groq-gpt-oss-120b` with reasoning off. Make the
  considered Reply lane think, defaulting it to `cerebras-glm-4.7` at medium
  reasoning instead of no reasoning.
- Add a Fast reply model picker and its own live feed panel to the web console.
  Migrate saved browser settings from schema 2 to schema 3 by back-filling the
  new lane with its default model and reasoning.
- Add a GitHub sponsors funding link.
- Move CI checks to the shared code-workflow and grant it `security-events`
  write access.

## v3.2.0 — 2026-08-10

- Add automatic/manual provider dispatch. Manual mode keeps ASR live, exposes
  unsent transcript state, and lets Send atomically supersede current work and
  dispatch the newest complete conversation exactly once.
- Split fast Reply, Private coach, Story so far, and agentic Research into four
  independently configured concurrent flows. Completed current research becomes
  bounded evidence for later flows; failed, stale, cancelled, and no-op research
  is excluded.
- Add the fourth compact streamed browser feed, persisted model/reasoning and
  dispatch settings, strict WebSocket contracts, interruption recovery, and
  realistic generated-audio quality/latency evaluation.
- Suppress late activity from detached superseded Claudebox operations while
  allowing the replacement to reuse the same workspace.

## v3.1.1 — 2026-08-10

Repair browser settings saved before Claudebox Reply reasoning became strict.

- Preserve a saved, available Reply model while replacing an unsupported
  `none` or `minimal` reasoning value with the backend Reply default before the
  browser sends its first runtime-settings message.
- Cover the migration through unit, WebSocket-message, and compiled-browser
  regressions seeded with legacy browser-local settings.
- Document the distinct Reply, Private coach, and Story reasoning defaults and
  supported choices consistently across the README and reference docs.

## v3.1.0 — 2026-08-09

Add persistent Claudebox repository research with interruption recovery, while
keeping streamed guidance responsive and browser regressions visible.

- Fix duplicate keyed response rows when terminal output follows intervening
  reasoning or tool activity, preventing the Svelte console crash that froze
  the live transcript and guidance feeds.
- Settle or remove unfinished response rows on cancellation and failure instead
  of leaving an empty `Generating…` entry active indefinitely, while preserving
  any partial output already received.
- Expand the deterministic compiled-browser fixture across successful,
  cancelled, failed, and interleaved streams; fail on browser console errors;
  and retain screenshots of the model picker and provider feeds.
- Stream Reply through AIGate's OpenAI-compatible endpoint in one persistent
  Claudebox workspace. Omit OpenAI tool declarations and explicitly enable
  Claude Code's internal tools so the agent can clone and inspect repositories
  while incremental spoken output remains visible in the provider feed.
- Preserve Claude Code's native agent prompt by appending 2xbrainz instructions
  through the Claudebox header instead of replacing it with an OpenAI system
  message. Buffer explicit repository-URL research, recover incomplete ordinary
  streams once, and verify live research against the checkout's Git remote.
- Recognize spoken GitHub/GitLab repository references, keep superseded accepted
  native research from publishing stale guidance, and continue its replacement
  from the complete transcript in the same Claudebox workspace.
- Add a real generated-audio interruption gate through CUDA Nemotron and direct
  Claudebox research, including exact checkout proof and elapsed lifecycle trace;
  remove the stale fixture targets that still passed deleted CLI audio flags.

## v3.0.0 — 2026-08-08

Move all non-secret live configuration into the web console and add a
repeatable, end-to-end conversation quality gate.

- **Breaking.** Remove the live model, reasoning, ASR, session-context,
  research, and audio-source environment variables. Configure those settings
  in the browser; only credentials, service connectivity, logging, and server
  settings remain environment-based.
- **Breaking.** Remove the `--mic-node` and `--system-node` live-command
  options and the host audio-selection file. The browser settings modal now
  owns source selection and persists safe settings in local storage.
- Add strict runtime-settings validation and independent in-session model and
  reasoning controls for Reply, Private coach, and Story so far, backed by
  code-defined defaults.
- Default streaming ASR to the exact CUDA Nemotron model, validate its reported
  device and concurrency before real evaluation, and keep independently
  supervised audio channels recoverable across hot-plug events.
- Harden provider streaming and research-tool recovery with bounded retries,
  URL validation, parallel tool execution, and clearer failure events.
- Add an eight-turn slang-heavy, interrupted conversation evaluation that
  generates speech, exercises two concurrent ASR streams and three routed LLM
  flows, and gates transcript accuracy, latency, interruption recovery,
  research behavior, and final conversation understanding.
- Improve the web console's streamed histories, settings layout, searchable
  model controls, diagnostics, and automated browser coverage.

## v2.0.0 — 2026-08-06

Remove pre-release compatibility layers and require explicit configuration for
each live guidance flow.

- **Breaking.** Replace `TWOXBRAINZ_AIGATE_MODEL` with all three explicit
  `TWOXBRAINZ_AIGATE_REPLY_MODEL`, `TWOXBRAINZ_AIGATE_COACH_MODEL`, and
  `TWOXBRAINZ_AIGATE_SUMMARY_MODEL` variables. First-run startup now rejects
  incomplete flow assignments instead of expanding a shared model.
- **Breaking.** Replace `TWOXBRAINZ_AIGATE_REASONING_EFFORT` with the dedicated
  `TWOXBRAINZ_AIGATE_REPLY_REASONING_EFFORT`,
  `TWOXBRAINZ_AIGATE_COACH_REASONING_EFFORT`, and
  `TWOXBRAINZ_AIGATE_SUMMARY_REASONING_EFFORT` variables.
- **Breaking.** Stop migrating schema-v1 `provider-selection.json` files. Open
  Settings and save the three flow assignments again to write the schema-v2
  format.
- **Breaking.** Remove the `make run-web` alias; use `make run`.
- Remove the launch-profile decision records now that the current runtime and
  configuration documentation describe the supported application directly.

## v1.1.0 — 2026-08-06

Make live guidance faster, observable, independently routed, and resilient to
provider and audio-device failures.

- Add searchable, independently persisted AIGate model and reasoning-effort
  assignments for Reply, Private coach, and Story so far. Their separate
  histories render incremental Streamdown output, preserve prior generations,
  follow only while at the bottom, and collapse provider-visible reasoning plus
  tool events by default. Dedicated first-run environment values configure each
  flow's model and reasoning effort independently, with shared legacy fallbacks.
- Run reply, coaching, and story generation concurrently. Normalize cumulative
  provider snapshots and model sentinel tokens so streamed reasoning and output
  no longer duplicate text.
- Exercise the three LLM paths concurrently against distinct Cerebras,
  Claudebox, and PIBOX models in the opt-in real-provider fixture. Require the
  Reply model itself to complete a real research tool round, not merely validate
  that the underlying search/fetch implementation is reachable.
- Add two bounded tool rounds and an application-owned `research_web` tool that
  searches through AIGate or reads an exact discovered URL, extracts
  link-preserving Markdown with Trafilatura, and lets the model follow relevant
  documentation links. Keep the full AIGate catalog out of model context and
  expose bounded tool/provider failure reasons separately from cancellation.
- Supervise microphone and system audio independently so a disconnected channel
  retries or switches routes without cancelling its peer, the web server, or the
  accumulated transcript and story.
- Add automatic and operator-triggered in-app PipeWire redetection, quiesce audio
  previews before discovery, remove disconnected nodes from the picker, and
  apply persisted source changes immediately to the affected channel.
- Reallocate released guidance height to expanded panels when any sibling is
  collapsed, keeping the full browser viewport useful.
- Add deterministic browser-stream observability tests, real autonomous research
  qualification, and a two-stream Talkies concurrency proof. Synchronize the
  locked dependencies and third-party inventory.

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
