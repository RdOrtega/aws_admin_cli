"""Application-wide exception hierarchy across domain, application, and infra.

Every exception raised for a reason the user can act on inherits from
``AwsAdminCliError`` and carries an ``exit_code`` (following, where sensible,
the BSD ``sysexits.h`` convention) plus an optional actionable ``hint``. The
CLI entry point (``aws_admin_cli.main.run``) is the single place that turns
these into process exit codes and user-facing messages.
"""

from typing import Any, Self


class AwsAdminCliError(Exception):
    """Base class for every error the CLI knows how to handle gracefully.

    Attributes:
        exit_code: Process exit code to use when this error reaches the top
            of the call stack unhandled.
        message: Human-readable description of what went wrong.
        hint: Optional, actionable suggestion for how to fix the problem.
    """

    exit_code: int = 1

    def __init__(self: Self, message: str, *, hint: str | None = None) -> None:
        """Initialize the error.

        Args:
            message: Human-readable description of what went wrong.
            hint: Optional, actionable suggestion for how to fix the problem.
        """
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self: Self) -> str:
        """Return the human-readable message."""
        return self.message


class ConfigurationError(AwsAdminCliError):
    """Raised when the CLI's own configuration is invalid or incomplete."""

    exit_code = 78  # EX_CONFIG


class ProfileNotFoundError(ConfigurationError):
    """Raised when the requested AWS profile does not exist locally."""


class MissingCredentialsError(ConfigurationError):
    """Raised when no usable AWS credentials could be resolved."""


class AwsError(AwsAdminCliError):
    """Base class for every error that originated from an AWS API call.

    Attributes:
        service: AWS service the failing call targeted (e.g. ``"iam"``).
        operation: API operation that failed (e.g. ``"CreateUser"``).
        aws_code: Error code AWS returned (e.g. ``"NoSuchEntity"``).
    """

    exit_code = 1

    def __init__(
        self: Self,
        message: str,
        *,
        hint: str | None = None,
        service: str | None = None,
        operation: str | None = None,
        aws_code: str | None = None,
    ) -> None:
        """Initialize the error.

        Args:
            message: Human-readable description of what went wrong.
            hint: Optional, actionable suggestion for how to fix the problem.
            service: AWS service the failing call targeted.
            operation: API operation that failed.
            aws_code: Error code AWS returned.
        """
        super().__init__(message, hint=hint)
        self.service = service
        self.operation = operation
        self.aws_code = aws_code

    def __str__(self: Self) -> str:
        """Return the message plus any available service/operation/code metadata."""
        meta = [
            f"{key}={value}"
            for key, value in (
                ("service", self.service),
                ("operation", self.operation),
                ("aws_code", self.aws_code),
            )
            if value is not None
        ]
        if not meta:
            return self.message
        return f"{self.message} ({', '.join(meta)})"


class ResourceNotFoundError(AwsError):
    """Raised when a referenced AWS resource does not exist."""

    exit_code = 4


class ResourceAlreadyExistsError(AwsError):
    """Raised when attempting to create an AWS resource that already exists."""

    exit_code = 5


class AccessDeniedError(AwsError):
    """Raised when the caller lacks permission to perform the operation."""

    exit_code = 77  # EX_NOPERM


class ValidationError(AwsError):
    """Raised when AWS rejected the request as malformed or invalid."""

    exit_code = 64  # EX_USAGE


class ThrottlingError(AwsError):
    """Raised when AWS is rate-limiting the caller."""

    exit_code = 75  # EX_TEMPFAIL


class ServiceUnavailableError(AwsError):
    """Raised when AWS (or a local endpoint standing in for it) is unreachable."""

    exit_code = 69  # EX_UNAVAILABLE


class EndpointUnavailableError(ServiceUnavailableError):
    """Raised when a local endpoint (e.g. LocalStack) refused the connection outright.

    A ``ServiceUnavailableError`` distinguished from AWS-side unavailability
    (throttled, ``InternalError``, a real-AWS network blip, ...): Docker/
    LocalStack simply isn't listening on ``endpoint_url``. The TUI
    (``presentation.tui.navigation.NavigationStack``) reacts to this specific
    subclass with its own dedicated recovery ceremony instead of the generic
    error banner every other ``AwsAdminCliError`` gets.

    Attributes:
        endpoint_url: The local endpoint that refused the connection.
    """

    def __init__(
        self: Self,
        message: str,
        *,
        endpoint_url: str,
        hint: str | None = None,
        service: str | None = None,
        operation: str | None = None,
    ) -> None:
        """Initialize the error.

        Args:
            message: Human-readable description of what went wrong.
            endpoint_url: The local endpoint that refused the connection.
            hint: Optional, actionable suggestion for how to fix the problem.
            service: AWS service the failing call targeted.
            operation: API operation that failed.
        """
        super().__init__(message, hint=hint, service=service, operation=operation)
        self.endpoint_url = endpoint_url


class UnknownAwsError(AwsError):
    """Raised when AWS returned an error code with no known mapping."""


class OperationTimeoutError(AwsError):
    """Raised when a ``wait_for_state`` poll loop times out before reaching the target state."""

    exit_code = 75  # EX_TEMPFAIL -- same family as ThrottlingError: retry later.


class PersistenceError(AwsAdminCliError):
    """Raised when reading or writing local application state fails.

    Reserved for local state/caching adapters introduced in Fase 2.
    """

    exit_code = 74  # EX_IOERR


class StackError(AwsAdminCliError):
    """Base class for stack-orchestration errors (Fase 6: ``stack`` manifests)."""

    exit_code = 1


class StackApplyError(StackError):
    """Raised when a stack ``apply`` fails and rollback leaves orphaned resources.

    Every ``apply`` failure that reaches the CLI as an error goes through
    this exception -- even a fully successful rollback (``ROLLED_BACK``, no
    orphans) surfaces this way, so the caller always gets the same shape of
    error to handle. ``orphaned`` is empty in that case; non-empty means at
    least one compensation itself failed (``ROLLBACK_INCOMPLETE``) or
    ``no_rollback=True`` was requested, and those resources are still live in
    AWS.
    """

    # A dedicated code, deliberately distinct from every other AwsError code
    # above: "rollback incomplete, orphaned resources exist" is not a plain
    # AWS failure (1), not a validation error (64), and not a timeout (75) --
    # it needs its own signal so scripts/CI can tell "the apply failed but
    # cleaned up after itself" apart from "the apply failed AND left a mess".
    exit_code = 70

    def __init__(
        self: Self,
        message: str,
        *,
        hint: str | None = None,
        orphaned: list[Any] | None = None,
    ) -> None:
        """Initialize the error.

        Args:
            message: Human-readable description of what went wrong.
            hint: Optional, actionable suggestion for how to fix the problem.
            orphaned: ``ResourceState`` entries left behind in AWS -- created
                by this apply (or a prior one) and NOT cleaned up by
                rollback. Empty when rollback fully succeeded. Typed ``Any``
                rather than ``domain.models.stack.ResourceState`` here: this
                module (``core/``) stays dependency-free, per the layering
                this codebase documents in ``docs/architecture.md`` -- it's
                domain that depends on core's exceptions, never the reverse.

        Note:
            The original failure that triggered the apply/rollback must be
            attached as ``__cause__`` (i.e. raise this ``from original_exc``)
            -- never let a cleanup failure hide *why* the apply failed in the
            first place.
        """
        super().__init__(message, hint=hint)
        self.orphaned = orphaned or []


class StackNotFoundError(StackError):
    """Raised when a referenced stack has no persisted state."""

    exit_code = 4
