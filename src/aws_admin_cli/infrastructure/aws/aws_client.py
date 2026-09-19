"""Real, cloud-agnostic AWS/LocalStack connectivity probe: ``sts:GetCallerIdentity``.

Deliberately separate from ``connectivity.py``'s ``local_endpoint_is_reachable``
(a raw TCP socket check, LocalStack-only, cheap enough to run on every header
redraw): this module makes an actual signed AWS request, so it costs real
latency and only ever runs where that cost is acceptable -- once per
navigation action, as the circuit-breaker gate before entering a module that
needs a live AWS connection (see ``presentation/tui/app.py``). It works
identically against LocalStack and real AWS with no branch on
``settings.is_local`` anywhere in this file: ``ClientFactory._client_kwargs``
remains the ONE place that decides which endpoint a client actually talks to.
"""

from typing import TYPE_CHECKING

from aws_admin_cli.core.exceptions import AwsAdminCliError
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.error_mapper import aws_error_boundary

if TYPE_CHECKING:
    from aws_admin_cli.core.context import AppContext

__all__ = ["aws_connection_is_healthy"]

_DEFAULT_HEALTH_CHECK_TIMEOUT_SECONDS = 2


def aws_connection_is_healthy(
    ctx: "AppContext", *, timeout: int = _DEFAULT_HEALTH_CHECK_TIMEOUT_SECONDS
) -> bool:
    """Whether the configured AWS/LocalStack target is reachable and authenticated right now.

    A real ``sts:GetCallerIdentity`` call -- the same "network + credentials +
    endpoint all actually work" check ``doctor_payload`` already makes -- but
    fast-failing at ``timeout`` seconds (both connect and read, retries
    disabled) instead of the profile's normal, much longer settings, and
    against a throwaway ``ClientFactory`` so this probe never disturbs
    ``ctx.client_factory``'s own cached clients.

    Never raises: ``aws_error_boundary`` turns every connectivity/credentials
    failure (unreachable endpoint, connect/read timeout, missing credentials)
    into an ``AwsAdminCliError`` subclass, caught here and folded into
    ``False``. Anything else STS could plausibly return (AccessDenied,
    throttling, an unrecognized error code, ...) still means "can't complete
    an authenticated call against this target right now", so it's ``False``
    too -- this is a health probe, not a normal AWS call site that should let
    its own errors propagate to the caller.
    """
    probe_settings = ctx.settings.with_overrides(
        connect_timeout=timeout, read_timeout=timeout, max_attempts=1
    )
    probe_factory = ClientFactory(
        session_factory=ctx.client_factory.session_factory, settings=probe_settings
    )
    try:
        with aws_error_boundary("sts", "GetCallerIdentity"):
            probe_factory.sts().get_caller_identity()
    except AwsAdminCliError:
        return False
    return True
