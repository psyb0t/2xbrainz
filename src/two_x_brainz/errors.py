"""Domain-specific exceptions."""

from __future__ import annotations


class TwoXBrainzError(Exception):
    """Base class for expected application failures."""


class ConfigurationError(TwoXBrainzError):
    """Raised when configuration is missing or invalid."""


class ProtocolError(TwoXBrainzError):
    """Raised when an upstream protocol payload violates its contract."""


class EmptyProviderContentError(ProtocolError):
    """Raised when a completion contains no operator-visible text."""


class RemoteServiceError(TwoXBrainzError):
    """Raised when a configured upstream service cannot complete a request."""


class CaptureError(TwoXBrainzError):
    """Raised when PipeWire capture cannot start or terminates unexpectedly."""


class AudioFixtureError(TwoXBrainzError):
    """Raised when a finite ASR benchmark fixture is unsafe or unsupported."""


class WebConsoleError(TwoXBrainzError):
    """Raised when the local web presentation cannot start or stop safely."""


class ReplayError(TwoXBrainzError):
    """Raised when a replay fixture is malformed."""
