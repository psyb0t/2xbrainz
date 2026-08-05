"""Interactive, persistent selection of the two PipeWire capture nodes."""

from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from two_x_brainz.capture import validate_pipewire_node_identifier
from two_x_brainz.constants import (
    AUDIO_SELECTION_CONFIG_SCHEMA_VERSION,
    MAX_AUDIO_SELECTION_CONFIG_BYTES,
)
from two_x_brainz.errors import CaptureError, ConfigurationError

_MICROPHONE_MEDIA_CLASS = "Audio/Source"
_SYSTEM_MONITOR_MEDIA_CLASS = "Audio/Source"
_SYSTEM_SINK_MEDIA_CLASS = "Audio/Sink"
_MONITOR_NAME_SUFFIX = ".monitor"
_DEFAULT_SOURCE_ROLE = "source"
_DEFAULT_SINK_ROLE = "sink"
_DEVICE_DESCRIPTION_KEY = "description"
_DEVICE_DEFAULT_ROLE_KEY = "default_role"
_MAX_DEVICE_LABEL_CHARACTERS = 160
_SCHEMA_VERSION_KEY = "schema_version"
_MIC_NODE_KEY = "mic_node"
_SYSTEM_NODE_KEY = "system_node"
_CONFIG_KEYS = frozenset({_SCHEMA_VERSION_KEY, _MIC_NODE_KEY, _SYSTEM_NODE_KEY})
_CONFIG_FILE_MODE = 0o600
_CONFIG_DIRECTORY_MODE = 0o700
_NO_FOLLOW_OPEN_FLAG = getattr(os, "O_NOFOLLOW", 0)


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
        """Return literal, bounded text for the local Textual option list."""
        default_marker = " [DEFAULT]" if self.is_default else ""
        return f"{self.label}{default_marker}\n  node: {self.name} [{self.node_id}]"


@dataclass(slots=True)
class AudioSelectionSetup:
    """Validated choices and persistence for one Textual audio-setup session."""

    store: AudioSelectionStore
    microphones: tuple[AudioDevice, ...]
    system_monitors: tuple[AudioDevice, ...]
    selection: AudioSelection | None

    def select(
        self,
        microphone_index: int,
        system_monitor_index: int,
    ) -> AudioSelection:
        """Persist the two in-range UI choices and return their labelled pair."""
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
        self.store.save(selection)
        self.selection = selection
        return selection


class AudioSelectionStore:
    """Read and atomically persist a small, local-only audio-target configuration."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> AudioSelection | None:
        """Return a validated selection, or None when selection is required."""
        try:
            descriptor = os.open(self._path, os.O_RDONLY | _NO_FOLLOW_OPEN_FLAG)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                return None
            raise ConfigurationError("read audio selection configuration") from error
        try:
            with os.fdopen(descriptor, "rb") as config_file:
                file_status = os.fstat(config_file.fileno())
                if not stat.S_ISREG(file_status.st_mode):
                    return None
                if file_status.st_size > MAX_AUDIO_SELECTION_CONFIG_BYTES:
                    return None
                raw = config_file.read(MAX_AUDIO_SELECTION_CONFIG_BYTES + 1)
        except OSError as error:
            raise ConfigurationError("read audio selection configuration") from error
        if len(raw) > MAX_AUDIO_SELECTION_CONFIG_BYTES:
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return _selection_from_payload(payload)

    def save(self, selection: AudioSelection) -> None:
        """Atomically replace the local config with no audio or credential data."""
        try:
            self._path.parent.mkdir(
                mode=_CONFIG_DIRECTORY_MODE,
                parents=True,
                exist_ok=True,
            )
        except OSError as error:
            raise ConfigurationError("create audio selection directory") from error

        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, _CONFIG_FILE_MODE)
            with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
                json.dump(
                    {
                        _SCHEMA_VERSION_KEY: AUDIO_SELECTION_CONFIG_SCHEMA_VERSION,
                        _MIC_NODE_KEY: selection.mic_node,
                        _SYSTEM_NODE_KEY: selection.system_node,
                    },
                    config_file,
                    separators=(",", ":"),
                )
                config_file.flush()
                os.fsync(config_file.fileno())
            os.replace(temporary_path, self._path)
            os.chmod(self._path, _CONFIG_FILE_MODE)
        except OSError as error:
            raise ConfigurationError("write audio selection configuration") from error
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def prepare_audio_selection_setup(
    *,
    nodes: Sequence[Mapping[str, str]],
    store: AudioSelectionStore,
    mic_node: str | None,
    system_node: str | None,
) -> AudioSelectionSetup:
    """Prepare safe candidates and any reusable selection for the Textual app."""
    microphones = _candidate_devices(nodes, is_system_monitor=False)
    system_monitors = _candidate_devices(nodes, is_system_monitor=True)
    if not microphones:
        raise CaptureError("no compatible PipeWire microphone source is visible")
    if not system_monitors:
        raise CaptureError("no compatible PipeWire system-audio source is visible")

    explicit_selection = _explicit_selection(mic_node, system_node)
    if explicit_selection is not None:
        _require_available_selection(
            explicit_selection,
            microphones,
            system_monitors,
        )
        selection = _with_device_labels(
            explicit_selection,
            microphones,
            system_monitors,
        )
        store.save(selection)
        return AudioSelectionSetup(
            store=store,
            microphones=microphones,
            system_monitors=system_monitors,
            selection=selection,
        )

    selection: AudioSelection | None = None
    saved_selection = store.load()
    if saved_selection is not None and _selection_is_available(
        saved_selection,
        microphones,
        system_monitors,
    ):
        selection = _with_device_labels(
            saved_selection,
            microphones,
            system_monitors,
        )

    return AudioSelectionSetup(
        store=store,
        microphones=microphones,
        system_monitors=system_monitors,
        selection=selection,
    )


def _selection_from_payload(payload: object) -> AudioSelection | None:
    if not isinstance(payload, dict):
        return None
    config = cast(dict[str, object], payload)
    if frozenset(config) != _CONFIG_KEYS:
        return None
    if config.get(_SCHEMA_VERSION_KEY) != AUDIO_SELECTION_CONFIG_SCHEMA_VERSION:
        return None
    mic_node = config.get(_MIC_NODE_KEY)
    system_node = config.get(_SYSTEM_NODE_KEY)
    if not isinstance(mic_node, str) or not isinstance(system_node, str):
        return None
    try:
        return AudioSelection(mic_node=mic_node, system_node=system_node)
    except CaptureError:
        return None


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


def _explicit_selection(
    mic_node: str | None,
    system_node: str | None,
) -> AudioSelection | None:
    if mic_node is None and system_node is None:
        return None
    if mic_node is None or system_node is None:
        raise ConfigurationError(
            "--mic-node and --system-node must be supplied together"
        )
    return AudioSelection(mic_node=mic_node, system_node=system_node)


def _require_available_selection(
    selection: AudioSelection,
    microphones: Sequence[AudioDevice],
    system_outputs: Sequence[AudioDevice],
) -> None:
    if not _selection_is_available(selection, microphones, system_outputs):
        raise CaptureError("the requested PipeWire audio nodes are not available")


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
