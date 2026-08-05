# ADR-0002: Bounded persistent reconstruction log

**Status:** accepted
**Date:** 2026-08-04

## Context

The live command originally wrote machine-readable events into the same
terminal used by the operator. That made an active call difficult to follow and
left no durable account of timing, transcript revisions, drafts, summaries, or
control actions once the short-lived container exited.

Operators need a readable live surface and enough local evidence to reconstruct
a completed or failed session. Raw audio and credentials must remain outside
that evidence.

## Decision

`make run` renders a human-readable terminal dashboard. It writes the same
structured runtime events to a unique UTC-prefixed
`./logs/<timestamp>_2xbrainz.log` file through a rotating JSON file handler.
`LOG_DIRECTORY` selects another mounted host directory for `make run`. Each
session log rolls at 5 MB and keeps at most three numbered backups.

The log includes transcript text, timeline entries, reply drafts, commentary,
summaries, actions, and capture/ASR diagnostics. It excludes raw PCM and
credential values; the formatter redacts credential-shaped fields. The existing
in-memory coordinator state still ends when the process exits.

## Consequences

- The local log is sensitive conversation data and must be protected like any
  other local transcript history.
- Rotation bounds disk use but does not provide encrypted retention, export, or
  deletion workflows.
- Replay remains a JSON-record command for deterministic automation; only live
  capture uses the terminal dashboard.
- This supersedes ADR-0001's session-retention statement only for the bounded
  structured event log. It does not change the no-audio policy.
