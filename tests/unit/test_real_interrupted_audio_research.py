from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any

from two_x_brainz.contracts import (
    DraftRequest,
    DraftResult,
    GenerationStatus,
    SpeakerRole,
    TranscriptLine,
    TranscriptSnapshot,
)

_SCRIPT = Path("tests/integration/real_interrupted_audio_research.py")


def _load_module() -> Any:
    integration_directory = str(_SCRIPT.parent.resolve())
    sys.path.insert(0, integration_directory)
    try:
        specification = importlib.util.spec_from_file_location(
            "real_interrupted_audio_research",
            _SCRIPT,
        )
        assert specification is not None
        assert specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(integration_directory)


_FIXTURE = _load_module()


class InterruptedResearchContractTests(unittest.TestCase):
    def test_accepts_cancelled_then_completed_research_in_one_workspace(self) -> None:
        _FIXTURE._assert_contract(
            activities=_activities("workspace-1"),
            requests=_requests(),
            records=[
                {
                    "kind": "transcript",
                    "speaker_role": "remote",
                    "type": "partial",
                }
            ],
            final_draft=_completed_draft(),
            workspace_session_id="workspace-1",
        )

    def test_rejects_missing_cancellation_or_workspace_change(self) -> None:
        missing_cancel = [
            activity
            for activity in _activities("workspace-1")
            if activity["phase"] != "request_cancelled"
        ]
        with self.assertRaisesRegex(
            _FIXTURE.InterruptedAudioResearchError,
            "lifecycle is incomplete",
        ):
            _FIXTURE._assert_activity_lifecycle(missing_cancel, "workspace-1")

        changed_workspace = _activities("workspace-1")
        changed_workspace[-1]["workspace_session_id"] = "workspace-2"
        with self.assertRaisesRegex(
            _FIXTURE.InterruptedAudioResearchError,
            "one Claudebox workspace",
        ):
            _FIXTURE._assert_activity_lifecycle(changed_workspace, "workspace-1")

    def test_rejects_replacement_request_that_loses_prior_context(self) -> None:
        requests = _requests()
        requests[-1] = _request(
            "generation-2",
            "And continue by checking its actual files for the main capabilities.",
        )
        with self.assertRaisesRegex(
            _FIXTURE.InterruptedAudioResearchError,
            "final transcript is missing markers",
        ):
            _FIXTURE._assert_contract(
                activities=_activities("workspace-1"),
                requests=requests,
                records=[
                    {
                        "kind": "transcript",
                        "speaker_role": "remote",
                        "type": "partial",
                    }
                ],
                final_draft=_completed_draft(),
                workspace_session_id="workspace-1",
            )

    def test_rejects_missing_partial_or_ungrounded_reply(self) -> None:
        with self.assertRaisesRegex(
            _FIXTURE.InterruptedAudioResearchError,
            "no partial transcript",
        ):
            _FIXTURE._assert_contract(
                activities=_activities("workspace-1"),
                requests=_requests(),
                records=[],
                final_draft=_completed_draft(),
                workspace_session_id="workspace-1",
            )

        ungrounded = DraftResult(
            generation_id="generation-2",
            trigger_turn_id="turn-2",
            context_revision=2,
            status=GenerationStatus.COMPLETED,
            text="I would explain it clearly.",
        )
        with self.assertRaisesRegex(
            _FIXTURE.InterruptedAudioResearchError,
            "capability markers",
        ):
            _FIXTURE._assert_contract(
                activities=_activities("workspace-1"),
                requests=_requests(),
                records=[
                    {
                        "kind": "transcript",
                        "speaker_role": "remote",
                        "type": "partial",
                    }
                ],
                final_draft=ungrounded,
                workspace_session_id="workspace-1",
            )


def _activities(workspace: str) -> list[dict[str, object]]:
    phases = (
        ("request_started", "generation-1"),
        ("native_research_started", "generation-1"),
        ("request_cancelled", "generation-1"),
        ("request_started", "generation-2"),
        ("native_research_started", "generation-2"),
        ("native_research_completed", "generation-2"),
        ("request_completed", "generation-2"),
    )
    return [
        {
            "phase": phase,
            "generation_id": generation_id,
            "workspace_session_id": workspace,
        }
        for phase, generation_id in phases
    ]


def _requests() -> list[DraftRequest]:
    first = (
        "Research the GitHub repository called AI Gate and tell me what problem "
        "it solves."
    )
    second = (
        f"{first}\nThe exact link is GitHub dot com slash P S Y B zero T slash "
        "AI Gate. Check its actual files and main capabilities."
    )
    return [_request("generation-1", first), _request("generation-2", second)]


def _request(generation_id: str, text: str) -> DraftRequest:
    return DraftRequest(
        generation_id=generation_id,
        trigger_turn_id=f"turn-{generation_id[-1]}",
        context_revision=int(generation_id[-1]),
        transcript=TranscriptSnapshot(
            revision=int(generation_id[-1]),
            lines=(
                TranscriptLine(
                    stream_id=f"remote-{generation_id[-1]}",
                    speaker_role=SpeakerRole.REMOTE,
                    revision=1,
                    text=text,
                    is_final=True,
                ),
            ),
        ),
        deadline_seconds=60,
    )


def _completed_draft() -> DraftResult:
    return DraftResult(
        generation_id="generation-2",
        trigger_turn_id="turn-2",
        context_revision=2,
        status=GenerationStatus.COMPLETED,
        text="AIGate is an API gateway that routes requests across model providers.",
    )
