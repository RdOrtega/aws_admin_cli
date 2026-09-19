"""Boto3 session construction: the port and its concrete implementation.

Fase 1 carries no domain/application logic here; this factory's only job is
translating a resolved (profile, region) pair into a working boto3 ``Session``
and turning botocore's own credential/profile errors into ``aws_admin_cli``
domain errors.
"""

import os
from dataclasses import dataclass
from typing import Final, Protocol, Self
from urllib.parse import urlparse

import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ProfileNotFound

from aws_admin_cli.core.exceptions import MissingCredentialsError, ProfileNotFoundError

# Keyed by (profile, region, endpoint_url); module-level because Boto3SessionFactory is
# a frozen, slotted dataclass and so has no __dict__ to hang a cached_property off of.
_session_cache: dict[tuple[str, str, str], boto3.session.Session] = {}

# Hosts that can only ever be an emulator on this machine. Credentials against one of
# these are meaningless, so we supply placeholders rather than reading ~/.aws.
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset(
    {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
)
_PLACEHOLDER_CREDENTIAL: Final[str] = "test"


def _targets_local_emulator(endpoint_url: str | None) -> bool:
    """Whether ``endpoint_url`` points at an emulator running on this machine.

    Keyed on the ENDPOINT, never on the profile name: a profile called
    ``localstack`` that someone pointed at real AWS must still authenticate
    normally, and a profile called anything else pointed at
    ``http://localhost:4566`` must not.
    """
    if not endpoint_url:
        return False
    return urlparse(endpoint_url).hostname in _LOOPBACK_HOSTS


class SessionFactory(Protocol):
    """Port: something that can produce a boto3 ``Session``."""

    def get_session(self: Self) -> boto3.session.Session:
        """Return a boto3 ``Session`` for the caller's configured scope."""
        ...


@dataclass(frozen=True, slots=True)
class Boto3SessionFactory:
    """Builds (and caches) a boto3 ``Session`` for a given profile/region pair."""

    profile: str
    region: str
    endpoint_url: str | None = None

    def get_session(self: Self) -> boto3.session.Session:
        """Return a cached boto3 ``Session``, creating and validating it on first use.

        When ``endpoint_url`` points at a local emulator, the profile is
        bypassed entirely and placeholder credentials are used, so neither
        ``~/.aws/config`` nor ``~/.aws/credentials`` is ever read and a missing
        profile can't fail the run. ``AWS_ACCESS_KEY_ID`` /
        ``AWS_SECRET_ACCESS_KEY`` still win if they're exported, which keeps
        the e2e fixtures' own values in effect.

        Raises:
            ProfileNotFoundError: The configured profile doesn't exist locally.
            MissingCredentialsError: No usable credentials were found for it.
        """
        cache_key = (self.profile, self.region, self.endpoint_url or "")
        cached = _session_cache.get(cache_key)
        if cached is not None:
            return cached

        if _targets_local_emulator(self.endpoint_url):
            session = boto3.session.Session(
                aws_access_key_id=os.environ.get(
                    "AWS_ACCESS_KEY_ID", _PLACEHOLDER_CREDENTIAL
                ),
                aws_secret_access_key=os.environ.get(
                    "AWS_SECRET_ACCESS_KEY", _PLACEHOLDER_CREDENTIAL
                ),
                aws_session_token=os.environ.get("AWS_SESSION_TOKEN"),
                region_name=self.region,
            )
            _session_cache[cache_key] = session
            return session

        credentials_hint = (
            "Revisa docs/aws-profiles.md para configurar el perfil localstack, "
            "o exporta AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY."
        )
        try:
            session = boto3.session.Session(profile_name=self.profile, region_name=self.region)
            # Resolve credentials eagerly so a missing/partial setup fails here, at
            # session construction, rather than surfacing later as an opaque error
            # from the first AWS API call.
            resolved = session.get_credentials()
        except ProfileNotFound as exc:
            available = boto3.session.Session().available_profiles
            hint = (
                f"Perfiles disponibles: {', '.join(available)}."
                if available
                else "No hay perfiles configurados en ~/.aws/config ni ~/.aws/credentials."
            )
            raise ProfileNotFoundError(
                f"El perfil de AWS '{self.profile}' no existe.", hint=hint
            ) from exc
        except (NoCredentialsError, PartialCredentialsError) as exc:
            raise MissingCredentialsError(
                f"No se encontraron credenciales utilizables para el perfil '{self.profile}'.",
                hint=credentials_hint,
            ) from exc

        if resolved is None:
            raise MissingCredentialsError(
                f"No se encontraron credenciales utilizables para el perfil '{self.profile}'.",
                hint=credentials_hint,
            )

        _session_cache[cache_key] = session
        return session
