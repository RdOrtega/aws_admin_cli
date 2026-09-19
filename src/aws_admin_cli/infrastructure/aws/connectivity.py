"""Lightweight TCP reachability check for a local endpoint (e.g. LocalStack).

Deliberately NOT a boto3/botocore call: the TUI's main-menu banner re-probes
this on every redraw (no caching -- see ``_MainMenu._resolve_status``), so a
container stopped or started mid-session is reflected the next time the main
menu redraws, with no extra wiring. A real AWS API call -- even a cheap one --
would be both slower and, for a real (non-LocalStack) endpoint, a
billable/rate-limited round trip. A raw socket connect answers "is anything
listening on this host:port" without touching credentials or any AWS
protocol at all.
"""

import socket
from urllib.parse import urlsplit

__all__ = ["local_endpoint_is_reachable"]

# Short enough that a redraw never feels laggy while LocalStack is down (a refused
# connection on localhost returns near-instantly anyway; this timeout only bounds the
# worst case, e.g. a black-holed host that never sends a RST).
_DEFAULT_TIMEOUT_SECONDS = 0.2


def local_endpoint_is_reachable(
    endpoint_url: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> bool:
    """Whether a TCP connection to ``endpoint_url``'s host:port succeeds.

    Never raises: any socket error (connection refused, timeout, DNS
    failure, malformed URL, ...) is treated as "not reachable" -- the
    banner's own contract is to show OFFLINE, never to break the TUI, when
    LocalStack's container isn't up.
    """
    parsed = urlsplit(endpoint_url)
    host = parsed.hostname
    if host is None:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
