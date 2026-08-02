"""2xbrainz continuous conversation copilot."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

_DISTRIBUTION_NAME = "2xbrainz"
_SOURCE_VERSION = "0.0.0+source"


def _resolve_version() -> str:
    try:
        return package_version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _SOURCE_VERSION


VERSION = _resolve_version()

__all__ = ["VERSION"]
