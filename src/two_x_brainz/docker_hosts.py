"""Create safe Docker host mappings for host-resolved provider names."""

from __future__ import annotations

import ipaddress
import re
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from two_x_brainz.constants import ENV_AIGATE_URL, ENV_TALKIES_WS_URL

_DOCKER_ADD_HOST_FLAG = "--add-host="
_FULLY_QUALIFIED_HOSTNAME_DELIMITER = "."
_FQDN_PATTERN = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
_URL_ENVIRONMENT_KEYS = frozenset(
    {
        ENV_AIGATE_URL,
        ENV_TALKIES_WS_URL,
    }
)
_URL_SCHEMES = {
    ENV_AIGATE_URL: frozenset({"http", "https"}),
    ENV_TALKIES_WS_URL: frozenset({"ws", "wss"}),
}

ResolveIPv4 = Callable[[str], str | None]


def docker_host_arguments(
    environment_file: Path,
    resolve_ipv4: ResolveIPv4,
    endpoint_overrides: tuple[tuple[str, str], ...] = (),
) -> tuple[str, ...]:
    """Map resolvable fully-qualified provider hosts without exposing env values."""
    endpoints = (*_endpoint_values(environment_file), *endpoint_overrides)
    mappings: dict[str, str] = {}
    for name, endpoint in endpoints:
        hostname = _fully_qualified_hostname(name, endpoint)
        if hostname is None or hostname in mappings:
            continue
        address = _validated_ipv4(resolve_ipv4(hostname))
        if address is None:
            continue
        mappings[hostname] = address
    return tuple(
        f"{_DOCKER_ADD_HOST_FLAG}{hostname}:{address}"
        for hostname, address in mappings.items()
    )


def resolve_ipv4(hostname: str) -> str | None:
    """Return one validated IPv4 address using the host resolver."""
    try:
        addresses = socket.getaddrinfo(
            hostname,
            None,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return None
    for _, _, _, _, socket_address in addresses:
        address = socket_address[0]
        if not isinstance(address, str):
            continue
        validated_address = _validated_ipv4(address)
        if validated_address is not None:
            return validated_address
    return None


def main(arguments: list[str]) -> int:
    """Print shell-safe Docker host arguments for one dotenv-format file."""
    if len(arguments) not in {2, 4}:
        print(
            "usage: docker_hosts.py <environment-file> [aigate-url talkies-url]",
            file=sys.stderr,
        )
        return 2
    environment_file = Path(arguments[1])
    endpoint_overrides = ()
    if len(arguments) == 4:
        endpoint_overrides = (
            (ENV_AIGATE_URL, arguments[2]),
            (ENV_TALKIES_WS_URL, arguments[3]),
        )
    try:
        docker_arguments = docker_host_arguments(
            environment_file,
            resolve_ipv4,
            endpoint_overrides,
        )
    except OSError:
        print("error: read environment file for Docker host mappings", file=sys.stderr)
        return 1
    print(" ".join(docker_arguments))
    return 0


def _endpoint_values(environment_file: Path) -> tuple[tuple[str, str], ...]:
    endpoints: list[tuple[str, str]] = []
    for line in environment_file.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator != "=" or name not in _URL_ENVIRONMENT_KEYS:
            continue
        endpoints.append((name, _unquote(value.strip())))
    return tuple(endpoints)


def _fully_qualified_hostname(name: str, endpoint: str) -> str | None:
    parsed = urlparse(endpoint)
    hostname = parsed.hostname
    if (
        parsed.scheme not in _URL_SCHEMES[name]
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    normalized_hostname = hostname.rstrip(".")
    if _FULLY_QUALIFIED_HOSTNAME_DELIMITER not in normalized_hostname:
        return None
    if _FQDN_PATTERN.fullmatch(normalized_hostname) is None:
        return None
    return normalized_hostname


def _validated_ipv4(address: str | None) -> str | None:
    if address is None:
        return None
    try:
        parsed_address = ipaddress.ip_address(address)
    except ValueError:
        return None
    if not isinstance(parsed_address, ipaddress.IPv4Address):
        return None
    return address


def _unquote(value: str) -> str:
    if len(value) < 2 or value[0] != value[-1] or value[0] not in {"'", '"'}:
        return value
    return value[1:-1]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
