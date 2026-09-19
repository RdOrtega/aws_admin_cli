"""boto3 client construction.

This module is the single decision point in the whole project for whether a
client targets LocalStack or real AWS. ``_client_kwargs`` passes
``endpoint_url`` through when ``settings.endpoint_url`` is set, and leaves
boto3's normal AWS endpoint resolution untouched otherwise. No other module
may branch on that distinction (e.g. no ``if profile == "localstack"``
anywhere in this codebase).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

from botocore.config import Config

from aws_admin_cli import __version__

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Self

    from mypy_boto3_ec2.client import EC2Client
    from mypy_boto3_iam.client import IAMClient
    from mypy_boto3_s3.client import S3Client

    from aws_admin_cli.core.config import Settings
    from aws_admin_cli.infrastructure.aws.session_factory import SessionFactory


def _s3_config_override(settings: Settings) -> dict[str, Any]:
    """Force path-style addressing for S3 when a custom endpoint is set.

    Virtual-hosted-style addressing (the boto3 default) resolves
    ``<bucket>.<endpoint-host>`` via DNS -- against LocalStack that means
    ``mi-bucket.localhost``, which doesn't resolve and the request fails.
    Path-style (``<endpoint>/<bucket>/...``) sidesteps that entirely. Real AWS
    endpoints don't need this, so it's scoped to ``endpoint_url`` being set.
    """
    if not settings.endpoint_url:
        return {}
    return {"s3": {"addressing_style": "path"}}


# Per-service botocore.config.Config overrides, keyed by service name. Adding a
# new service's quirk later means adding an entry here -- `_botocore_config`
# and `create()` never need to change (OCP): no `if service_name == "..."`
# branch is added per service.
_SERVICE_CONFIG_OVERRIDES: Final[dict[str, Callable[[Settings], dict[str, Any]]]] = {
    "s3": _s3_config_override,
}


@dataclass(frozen=True, slots=True)
class ClientFactory:
    """Builds boto3 service clients uniformly configured from ``Settings``."""

    session_factory: SessionFactory
    settings: Settings
    _client_cache: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def _botocore_config(self: Self, service_name: str, *, region: str | None = None) -> Config:
        """Build the botocore ``Config`` for ``service_name``.

        Starts from the settings shared by every client this factory creates,
        then merges in that service's entry from ``_SERVICE_CONFIG_OVERRIDES``
        (if any) -- the only per-service branching point in this class, and
        it's a dict lookup, not a chain of ``if service_name == "..."``.

        ``region`` overrides ``settings.region`` for this one client -- used
        by callers that need a client pinned to a specific region (e.g. a
        launch wizard's region override, or ``CopyImage``'s destination
        region), without changing the profile-wide default every other
        client in this factory uses.
        """
        kwargs: dict[str, Any] = {
            "region_name": region or self.settings.region,
            "retries": {
                "max_attempts": self.settings.max_attempts,
                "mode": self.settings.retry_mode,
            },
            "connect_timeout": self.settings.connect_timeout,
            "read_timeout": self.settings.read_timeout,
            # Professional signature of a purpose-built SDK, visible to AWS/LocalStack
            # in request logs and CloudTrail.
            "user_agent_extra": f"aws-admin-cli/{__version__}",
        }
        override = _SERVICE_CONFIG_OVERRIDES.get(service_name)
        if override is not None:
            kwargs.update(override(self.settings))
        return Config(**kwargs)

    def _client_kwargs(
        self: Self, service_name: str, *, region: str | None = None
    ) -> dict[str, Any]:
        """Build the kwargs passed to ``Session.client()``, once per call.

        The single ``if`` below is the ONLY place in the project that decides
        between LocalStack and real AWS: it forwards a custom endpoint when one
        is configured, and otherwise leaves boto3 to resolve the real AWS
        endpoint on its own. No other module inspects ``settings.endpoint_url``
        or ``settings.profile`` to make that call.
        """
        kwargs: dict[str, Any] = {"config": self._botocore_config(service_name, region=region)}
        if self.settings.endpoint_url:
            kwargs["endpoint_url"] = self.settings.endpoint_url
        return kwargs

    def create(self: Self, service_name: str, *, region: str | None = None) -> Any:
        """Return a cached boto3 client for ``service_name``, creating it on first use.

        ``region`` (when given) gets its own cache slot, separate from the
        profile-default client for the same service -- so overriding the
        region for one call never evicts or mutates the shared default client.
        """
        cache_key = f"{service_name}@{region}" if region else service_name
        cached = self._client_cache.get(cache_key)
        if cached is not None:
            return cached
        session = self.session_factory.get_session()
        # boto3-stubs overloads Session.client() per literal service name; a dynamic
        # `service_name: str` plus an unpacked kwargs dict can never statically match
        # one of those overloads, regardless of typing effort here.
        client = session.client(  # type: ignore[call-overload]
            service_name, **self._client_kwargs(service_name, region=region)
        )
        self._client_cache[cache_key] = client
        return client

    def clear_cache(self: Self) -> None:
        """Drop every cached client.

        Called whenever ``settings.region`` changes after this factory was
        built (see ``AppContext.set_region``): ``create()``'s cache key for
        the profile-default client is the bare service name, with no region
        in it, so a client built under the old region would otherwise be
        handed back forever even after the switch.
        """
        self._client_cache.clear()

    def iam(self: Self) -> IAMClient:
        """Return a cached IAM client."""
        return cast("IAMClient", self.create("iam"))

    def s3(self: Self) -> S3Client:
        """Return a cached S3 client."""
        return cast("S3Client", self.create("s3"))

    def ec2(self: Self, *, region: str | None = None) -> EC2Client:
        """Return a cached EC2 client, optionally pinned to a specific ``region``."""
        return cast("EC2Client", self.create("ec2", region=region))

    def sts(self: Self) -> Any:
        """Return a cached STS client.

        Untyped: ``boto3-stubs`` was installed in Fase 0 with only the
        ``iam``/``s3``/``ec2`` extras, and this phase's pyproject.toml change is
        restricted to the entry-point script line. Adding the ``sts`` extra for
        this single Fase-1 ``GetCallerIdentity`` call isn't in scope here.
        """
        return self.create("sts")

    def cloudwatch(self: Self) -> Any:
        """Return a cached CloudWatch client.

        Untyped for the same reason ``sts()`` is: no ``mypy-boto3-cloudwatch``
        extra installed for this one gateway.
        """
        return self.create("cloudwatch")

    def lambda_client(self: Self, *, region: str | None = None) -> Any:
        """Return a cached Lambda client, optionally pinned to a specific ``region``.

        Named ``lambda_client`` (not ``lambda``, a reserved word) -- untyped
        for the same reason ``sts()``/``cloudwatch()`` are: no
        ``mypy-boto3-lambda`` extra installed for this service.
        """
        return self.create("lambda", region=region)
