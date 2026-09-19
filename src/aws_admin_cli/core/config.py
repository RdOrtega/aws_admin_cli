"""Application settings loaded from environment, ``.env``, and CLI overrides.

Precedence (highest wins): **CLI flag > environment variable > .env file > class
default**. Environment variables and the ``.env`` file are resolved automatically by
``pydantic-settings`` when a ``Settings`` instance is first constructed (typically once,
in :func:`aws_admin_cli.presentation.cli.callbacks.main_callback`). CLI flags are then
layered on top via :meth:`Settings.with_overrides`, which must be called with only the
flags the user actually passed — an unset flag is ``None`` and is filtered out before
merging, so it can never shadow a value already resolved from the environment.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aws_admin_cli.core.enums import LogLevel, OutputFormat
from aws_admin_cli.core.exceptions import ConfigurationError

_REGION_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")

# Perfiles que apuntan a un emulador local y el endpoint que se les asume cuando el
# usuario no fija uno explícitamente. Es un REGISTRO DE DATOS, no una bifurcación de
# lógica: la resolución ocurre aquí, en la capa de configuración, y aguas abajo sigue
# habiendo un único punto de decisión (``ClientFactory._client_kwargs``, que solo mira
# si ``endpoint_url`` está presente o no). Añadir un emulador nuevo es añadir una
# entrada, nunca un ``if`` en la fábrica de clientes.
_LOCAL_PROFILE_ENDPOINTS: Final[Mapping[str, str]] = {
    "localstack": "http://localhost:4566",
}


class Settings(BaseSettings):
    """Resolved, immutable runtime configuration for a single CLI invocation."""

    model_config = SettingsConfigDict(
        env_prefix="AWS_ADMIN_CLI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # Needed so that Settings.model_validate(merged) in with_overrides() actually
        # re-runs field validators over an already-instantiated Settings, instead of
        # pydantic's default of trusting a same-class instance as pre-validated.
        revalidate_instances="always",
    )

    profile: str = Field(default="localstack", description="AWS CLI profile to use.")
    region: str = Field(default="us-east-1", description="AWS region to target.")
    endpoint_url: str | None = Field(
        default=None,
        description="Custom service endpoint (e.g. LocalStack). None targets real AWS.",
    )
    output: OutputFormat = Field(
        default=OutputFormat.TABLE, description="Output rendering format."
    )
    log_level: LogLevel = Field(default=LogLevel.INFO, description="Logging verbosity.")
    data_dir: Path = Field(
        default_factory=lambda: Path.home() / ".aws_admin_cli",
        description="Directory for local state and cache files.",
    )
    max_attempts: int = Field(
        default=3, ge=1, le=10, description="Maximum boto3 retry attempts."
    )
    retry_mode: Literal["standard", "adaptive", "legacy"] = Field(
        default="standard", description="Boto3 retry mode."
    )
    connect_timeout: int = Field(
        default=10, ge=1, description="Boto3 connect timeout, in seconds."
    )
    read_timeout: int = Field(default=30, ge=1, description="Boto3 read timeout, in seconds.")

    @field_validator("endpoint_url", mode="before")
    @classmethod
    def _blank_endpoint_is_none(cls: type[Self], value: object) -> object:
        """Normalize an empty-string endpoint (e.g. from an unset env var) to None."""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("endpoint_url")
    @classmethod
    def _validate_endpoint_scheme(cls: type[Self], value: str | None) -> str | None:
        """Require a scheme so downstream code never has to guess the protocol."""
        if value is not None and not value.startswith(("http://", "https://")):
            raise ConfigurationError(
                f"endpoint_url debe comenzar con http:// o https:// (recibido: {value!r}).",
                hint="Usa algo como http://localhost:4566 para apuntar a LocalStack.",
            )
        return value

    @field_validator("region")
    @classmethod
    def _validate_region(cls: type[Self], value: str) -> str:
        """Reject values that don't look like an AWS region at all."""
        if not _REGION_RE.match(value):
            raise ConfigurationError(
                f"'{value}' no parece una región de AWS válida.",
                hint="Usa un formato como us-east-1, eu-west-2 o us-gov-west-1.",
            )
        return value

    @model_validator(mode="after")
    def _apply_local_profile_endpoint(self: Self) -> Self:
        """Give profiles listed in ``_LOCAL_PROFILE_ENDPOINTS`` their default endpoint.

        Only fills a gap: an ``endpoint_url`` that came from a CLI flag, an
        environment variable or ``.env`` always wins, because this runs after
        those have already been resolved and it never overwrites a non-None
        value. Idempotent, so re-validation via :meth:`with_overrides` is a
        no-op once the endpoint is set.

        Mutates ``self`` in place (via ``object.__setattr__``, bypassing
        ``frozen=True``) rather than returning ``self.model_copy(...)``:
        ``pydantic-settings`` silently discards a top-level "after" validator's
        replacement return value when construction goes through
        ``BaseSettings.__init__`` (i.e. every plain ``Settings(...)`` call, not
        just ``model_validate``) -- it only honors in-place mutation of the
        same instance. See https://docs.pydantic.dev/latest/concepts/validators/#model-validators.
        """
        if self.endpoint_url is not None:
            return self
        default_endpoint = _LOCAL_PROFILE_ENDPOINTS.get(self.profile)
        if default_endpoint is None:
            return self
        object.__setattr__(self, "endpoint_url", default_endpoint)
        return self

    @property
    def is_local(self: Self) -> bool:
        """Whether this configuration targets a local endpoint (e.g. LocalStack).

        Informational only — for user-facing messages and the ``doctor`` command.
        Must never be used to branch client-construction logic: the single decision
        point for that lives in ``ClientFactory._client_kwargs``.
        """
        return self.endpoint_url is not None

    def with_overrides(self: Self, **overrides: object) -> Self:
        """Return a new ``Settings`` with only the non-None overrides applied.

        Args:
            **overrides: Candidate field values, typically parsed CLI flags. A
                value of ``None`` means "flag not passed" and is dropped before
                merging, so it can never shadow a value already resolved from
                the environment or ``.env`` file.

        Returns:
            A new, independently re-validated ``Settings`` instance. ``self`` is
            never mutated.
        """
        applied = {key: value for key, value in overrides.items() if value is not None}
        if not applied:
            return self
        # self.endpoint_url may already carry the OLD profile's auto-injected default
        # (_apply_local_profile_endpoint doesn't touch a non-None endpoint_url, so it
        # can't tell "the user set this" apart from "I set this for a profile that's
        # about to change"). If it matches exactly what the old profile would have
        # produced, clear it so the validator re-derives it for the NEW profile
        # instead of carrying a stale value forward -- e.g. Settings()
        # (profile="localstack", auto-injected endpoint)
        # .with_overrides(profile="testprofile") must end up with endpoint_url=None,
        # not LocalStack's URL leaking into a real-AWS run.
        if (
            "profile" in applied
            and "endpoint_url" not in applied
            and self.endpoint_url == _LOCAL_PROFILE_ENDPOINTS.get(self.profile)
        ):
            applied["endpoint_url"] = None
        merged = self.model_copy(update=applied, deep=False)
        return type(self).model_validate(merged)
