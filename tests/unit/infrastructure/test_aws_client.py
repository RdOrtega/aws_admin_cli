"""Tests for ``aws_connection_is_healthy``: a real STS probe, mocked at the boto3 edge.

Every scenario monkeypatches ``ClientFactory.sts`` to return a stand-in client
whose ``get_caller_identity`` either succeeds or raises the exact botocore
exception a dead/misconfigured connection would raise -- never a real network
call, and never touching ``~/.aws`` (``profile="localstack"`` takes the
placeholder-credential path in ``Boto3SessionFactory.get_session``, so no
profile needs to exist on the machine running these tests).
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.infrastructure.aws.aws_client import aws_connection_is_healthy
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)

_ENDPOINT = "http://localhost:4566"


def _ctx() -> AppContext:
    return AppContext.build(Settings(profile="localstack", endpoint_url=_ENDPOINT))


def _stub_sts(
    monkeypatch: pytest.MonkeyPatch, *, raises: Exception | None, returns: Any = None
) -> None:
    """Make every ``ClientFactory.sts()`` (including the probe's own throwaway
    instance) return a fake STS client that either raises ``raises`` or
    returns ``returns`` from ``get_caller_identity()``.
    """
    client = MagicMock()
    if raises is not None:
        client.get_caller_identity.side_effect = raises
    else:
        client.get_caller_identity.return_value = returns
    monkeypatch.setattr(ClientFactory, "sts", lambda self: client)


def test_healthy_when_get_caller_identity_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_sts(
        monkeypatch,
        raises=None,
        returns={"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:root", "UserId": "X"},
    )
    assert aws_connection_is_healthy(_ctx()) is True


def test_unhealthy_on_endpoint_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_sts(monkeypatch, raises=EndpointConnectionError(endpoint_url=_ENDPOINT))
    assert aws_connection_is_healthy(_ctx()) is False


def test_unhealthy_on_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_sts(monkeypatch, raises=ConnectTimeoutError(endpoint_url=_ENDPOINT))
    assert aws_connection_is_healthy(_ctx()) is False


def test_unhealthy_on_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ReadTimeoutError`` is NOT a ``ConnectTimeoutError`` subclass in botocore --
    a distinct exception hierarchy that ``aws_error_boundary`` must also map, or
    this health check would let it escape unhandled instead of folding it to
    ``False`` like every other connectivity failure.
    """
    _stub_sts(monkeypatch, raises=ReadTimeoutError(endpoint_url=_ENDPOINT))
    assert aws_connection_is_healthy(_ctx()) is False


def test_unhealthy_on_no_credentials_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_sts(monkeypatch, raises=NoCredentialsError())
    assert aws_connection_is_healthy(_ctx()) is False


def test_unhealthy_on_an_ordinary_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not just connectivity failures -- ANY AWS error (AccessDenied, throttling,
    an unrecognized code, ...) means "can't complete an authenticated call right
    now", so it's ``False`` too. This is a health probe, not a normal call site.
    """
    error = ClientError(
        error_response={"Error": {"Code": "AccessDenied", "Message": "nope"}},
        operation_name="GetCallerIdentity",
    )
    _stub_sts(monkeypatch, raises=error)
    assert aws_connection_is_healthy(_ctx()) is False


def test_uses_a_fast_timeout_not_the_profiles_normal_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe's ``ClientFactory`` is built with short, retry-free timeouts --
    never the profile's own (much longer) ``connect_timeout``/``read_timeout``/
    ``max_attempts`` -- so a dead endpoint fails fast instead of hanging for the
    profile's full configured budget.
    """
    seen_settings: list[Settings] = []
    original_init = ClientFactory.__init__

    def _spy_init(self: ClientFactory, **kwargs: Any) -> None:
        seen_settings.append(kwargs["settings"])
        original_init(self, **kwargs)

    monkeypatch.setattr(ClientFactory, "__init__", _spy_init)
    _stub_sts(monkeypatch, raises=None, returns={"Account": "1", "Arn": "a", "UserId": "u"})

    aws_connection_is_healthy(_ctx(), timeout=2)

    probe_settings = seen_settings[-1]
    assert probe_settings.connect_timeout == 2
    assert probe_settings.read_timeout == 2
    assert probe_settings.max_attempts == 1
