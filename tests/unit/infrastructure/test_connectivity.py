"""Tests for ``local_endpoint_is_reachable``: a raw TCP probe, never an AWS call."""

import socket
from contextlib import closing

from aws_admin_cli.infrastructure.aws.connectivity import local_endpoint_is_reachable


def _listen(port: int = 0) -> tuple[socket.socket, int]:
    """Bind (and start listening on) ``port`` (an OS-assigned free one by default).

    ``SO_REUSEADDR`` so re-binding the exact same port right after a previous
    socket on it closed (as ``test_reflects_the_endpoint_going_down_and_back_up...``
    does, to simulate a restarted container on the same address) never flakes
    on a lingering TIME_WAIT.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    return server, server.getsockname()[1]


def test_reachable_when_something_is_listening() -> None:
    server, port = _listen()
    with closing(server):
        assert local_endpoint_is_reachable(f"http://127.0.0.1:{port}") is True


def test_unreachable_when_connection_is_refused() -> None:
    # Bind-then-close: guarantees the OS considers this port closed (refused), not
    # just "probably free" -- unlike picking an arbitrary high port number.
    server, port = _listen()
    server.close()

    assert local_endpoint_is_reachable(f"http://127.0.0.1:{port}", timeout=0.2) is False


def test_unreachable_for_malformed_url_never_raises() -> None:
    assert local_endpoint_is_reachable("not-a-url", timeout=0.2) is False


def test_unreachable_for_empty_url_never_raises() -> None:
    assert local_endpoint_is_reachable("", timeout=0.2) is False


def test_reflects_the_endpoint_going_down_and_back_up_with_no_caching() -> None:
    """No memoization anywhere: each call is a fresh probe of the endpoint's live state.

    This is what lets the TUI banner go ONLINE -> OFFLINE -> ONLINE across
    redraws as the user stops/starts the LocalStack container mid-session --
    there is nothing to invalidate, because nothing is ever cached.
    """
    server, port = _listen()
    url = f"http://127.0.0.1:{port}"

    assert local_endpoint_is_reachable(url, timeout=0.2) is True  # container running

    server.close()
    assert local_endpoint_is_reachable(url, timeout=0.2) is False  # container stopped

    restarted, _ = _listen(port)
    with closing(restarted):
        assert local_endpoint_is_reachable(url, timeout=0.2) is True  # container restarted
