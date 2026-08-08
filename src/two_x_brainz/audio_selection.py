"""Interactive selection of the two PipeWire capture nodes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from two_x_brainz.capture import validate_pipewire_node_identifier
from two_x_brainz.errors import CaptureError

_MICROPHONE_MEDIA_CLASS = "Audio/Source"
_SYSTEM_MONITOR_MEDIA_CLASS = "Audio/Source"
_SYSTEM_SINK_MEDIA_CLASS = "Audio/Sink"
_MONITOR_NAME_SUFFIX = ".monitor"
_DEFAULT_SOURCE_ROLE = "source"
_DEFAULT_SINK_ROLE = "sink"
_DEVICE_DESCRIPTION_KEY = "description"
_DEVICE_DEFAULT_ROLE_KEY = "default_role"
_MAX_DEVICE_LABEL_CHARACTERS = 160


@dataclass(frozen=True, slots=True)
class AudioSelection:
    """Stable PipeWire node names for the local and remote conversation sides."""

    mic_node: str
    system_node: str
    mic_label: str = field(default="", compare=False)
    system_label: str = field(default="", compare=False)
    system_capture_sink: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        validate_pipewire_node_identifier(self.mic_node)
        validate_pipewire_node_identifier(self.system_node)


@dataclass(frozen=True, slots=True)
class AudioDevice:
    """A compatible, safe PipeWire device presented in the selection menu."""

    node_id: str
    name: str
    media_class: str
    label: str
    is_default: bool

    @property
    def capture_sink(self) -> bool:
        """Whether recording this device requires PipeWire sink monitor ports."""
        return self.media_class == _SYSTEM_SINK_MEDIA_CLASS

    @property
    def setup_label(self) -> str:
        """Return literal, bounded text for the local source option list."""
        default_marker = " [DEFAULT]" if self.is_default else ""
        return f"{self.label}{default_marker}\n  node: {self.name} [{self.node_id}]"


@dataclass(slots=True)
class AudioSelectionSetup:
    """Validated choices for the live audio settings screen."""

    microphones: tuple[AudioDevice, ...]
    system_monitors: tuple[AudioDevice, ...]
    selection: AudioSelection | None
    _revision: int = field(default=0, init=False)
    _changed: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    @property
    def revision(self) -> int:
        """Return the monotonic routing revision consumed by capture workers."""
        return self._revision

    @property
    def selection_available(self) -> bool:
        """Whether both currently selected nodes are visible in discovery."""
        return self.selection is not None and _selection_is_available(
            self.selection,
            self.microphones,
            self.system_monitors,
        )

    def select(
        self,
        microphone_index: int,
        system_monitor_index: int,
    ) -> AudioSelection:
        """Apply the two in-range UI choices and return their labelled pair."""
        try:
            microphone = self.microphones[microphone_index]
            system_monitor = self.system_monitors[system_monitor_index]
        except IndexError as error:
            raise CaptureError(
                "the selected PipeWire audio node is not available"
            ) from error
        selection = AudioSelection(
            mic_node=microphone.name,
            system_node=system_monitor.name,
            mic_label=microphone.label,
            system_label=system_monitor.label,
            system_capture_sink=system_monitor.capture_sink,
        )
        if selection != self.selection:
            self.selection = selection
            self._revision += 1
            self._changed.set()
        return selection

    def select_nodes(self, microphone_node: str, system_node: str) -> AudioSelection:
        """Apply stable browser-persisted node names after inventory validation."""
        microphone_index = _device_index(microphone_node, self.microphones)
        system_index = _device_index(system_node, self.system_monitors)
        if microphone_index is None or system_index is None:
            raise CaptureError("the selected PipeWire audio node is not available")
        return self.select(microphone_index, system_index)

    def notify_runtime_change(self) -> None:
        """Reconnect capture workers after a non-routing ASR setting changes."""
        self._revision += 1
        self._changed.set()

    def refresh(self, nodes: Sequence[Mapping[str, str]]) -> None:
        """Replace discovery candidates while retaining a reconnectable selection."""
        self.microphones = _candidate_devices(nodes, is_system_monitor=False)
        self.system_monitors = _candidate_devices(nodes, is_system_monitor=True)
        if self.selection is not None and self.selection_available:
            self.selection = _with_device_labels(
                self.selection,
                self.microphones,
                self.system_monitors,
            )

    async def wait_for_change(self, revision: int) -> int:
        """Wait until the operator selects a different routing pair."""
        while self._revision == revision:
            await self._changed.wait()
            self._changed.clear()
        return self._revision


def prepare_audio_selection_setup(
    *,
    nodes: Sequence[Mapping[str, str]],
) -> AudioSelectionSetup:
    """Prepare safe candidates for browser-owned selection and persistence."""
    microphones = _candidate_devices(nodes, is_system_monitor=False)
    system_monitors = _candidate_devices(nodes, is_system_monitor=True)
    return AudioSelectionSetup(
        microphones=microphones,
        system_monitors=system_monitors,
        selection=None,
    )


def _candidate_devices(
    nodes: Sequence[Mapping[str, str]],
    *,
    is_system_monitor: bool,
) -> tuple[AudioDevice, ...]:
    candidates: dict[str, AudioDevice] = {}
    for node in nodes:
        node_id = node.get("id")
        name = node.get("name")
        node_media_class = node.get("media_class")
        if (
            not isinstance(node_id, str)
            or not isinstance(name, str)
            or not isinstance(node_media_class, str)
        ):
            continue
        is_monitor = name.endswith(_MONITOR_NAME_SUFFIX)
        if not _is_candidate_device(
            media_class=node_media_class,
            is_monitor=is_monitor,
            is_system_audio=is_system_monitor,
        ):
            continue
        try:
            device = AudioDevice(
                node_id=node_id,
                name=name,
                media_class=node_media_class,
                label=_device_label(node.get(_DEVICE_DESCRIPTION_KEY), name),
                is_default=_is_default_device(
                    node.get(_DEVICE_DEFAULT_ROLE_KEY),
                    is_system_monitor,
                ),
            )
            validate_pipewire_node_identifier(device.node_id)
            validate_pipewire_node_identifier(device.name)
        except CaptureError:
            continue
        candidates[device.name] = device
    return tuple(
        sorted(
            candidates.values(),
            key=lambda device: (not device.is_default, device.label, device.name),
        )
    )


def _is_candidate_device(
    *,
    media_class: object,
    is_monitor: bool,
    is_system_audio: bool,
) -> bool:
    if not is_system_audio:
        return media_class == _MICROPHONE_MEDIA_CLASS and not is_monitor
    if media_class == _SYSTEM_MONITOR_MEDIA_CLASS and is_monitor:
        return True
    return media_class == _SYSTEM_SINK_MEDIA_CLASS


def _selection_is_available(
    selection: AudioSelection,
    microphones: Sequence[AudioDevice],
    system_outputs: Sequence[AudioDevice],
) -> bool:
    return _device_matches(selection.mic_node, microphones) and _device_matches(
        selection.system_node,
        system_outputs,
    )


def _device_matches(identifier: str, devices: Sequence[AudioDevice]) -> bool:
    return any(identifier in {device.node_id, device.name} for device in devices)


def _device_index(identifier: str, devices: Sequence[AudioDevice]) -> int | None:
    for index, device in enumerate(devices):
        if identifier in {device.node_id, device.name}:
            return index
    return None


def _with_device_labels(
    selection: AudioSelection,
    microphones: Sequence[AudioDevice],
    system_outputs: Sequence[AudioDevice],
) -> AudioSelection:
    microphone = _device_for_identifier(selection.mic_node, microphones)
    system_output = _device_for_identifier(selection.system_node, system_outputs)
    return AudioSelection(
        mic_node=selection.mic_node,
        system_node=selection.system_node,
        mic_label=microphone.label,
        system_label=system_output.label,
        system_capture_sink=system_output.capture_sink,
    )


def _device_for_identifier(
    identifier: str,
    devices: Sequence[AudioDevice],
) -> AudioDevice:
    for device in devices:
        if identifier in {device.node_id, device.name}:
            return device
    raise CaptureError("the requested PipeWire audio nodes are not available")


def _device_label(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    label = " ".join(value.split())
    visible = "".join(
        character if character.isprintable() else "�" for character in label
    )
    return visible[:_MAX_DEVICE_LABEL_CHARACTERS] or fallback


def _is_default_device(value: object, is_system_monitor: bool) -> bool:
    expected_role = _DEFAULT_SINK_ROLE if is_system_monitor else _DEFAULT_SOURCE_ROLE
    return value == expected_role
