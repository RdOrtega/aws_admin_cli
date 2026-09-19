"""Translate botocore exceptions into ``aws_admin_cli`` domain exceptions.

Nothing from botocore should ever cross above this module: any code that
calls AWS wraps the call in :func:`aws_error_boundary` (or decorates the
function with :func:`map_aws_errors`), and callers only ever see
``aws_admin_cli.core.exceptions`` types.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Final, ParamSpec, TypeVar

from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
    ReadTimeoutError,
)

from aws_admin_cli.core.exceptions import (
    AccessDeniedError,
    AwsError,
    EndpointUnavailableError,
    MissingCredentialsError,
    ResourceAlreadyExistsError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    ThrottlingError,
    UnknownAwsError,
    ValidationError,
)

_ERROR_CODE_MAP: Final[dict[str, type[AwsError]]] = {
    "NoSuchEntity": ResourceNotFoundError,
    "NoSuchBucket": ResourceNotFoundError,
    "NoSuchKey": ResourceNotFoundError,
    # S3's HeadBucket/HeadObject return this bare, generic code (no XML body) on a
    # 404, rather than "NoSuchBucket"/"NoSuchKey" -- that's an S3-specific HEAD-request
    # quirk, not something any other service happens to also use "404" for.
    "404": ResourceNotFoundError,
    "InvalidInstanceID.NotFound": ResourceNotFoundError,
    "ResourceNotFoundException": ResourceNotFoundError,
    "InvalidGroup.NotFound": ResourceNotFoundError,
    "InvalidAMIID.NotFound": ResourceNotFoundError,
    "InvalidAMIID.Malformed": ResourceNotFoundError,
    "InvalidKeyPair.NotFound": ResourceNotFoundError,
    "EntityAlreadyExists": ResourceAlreadyExistsError,
    "BucketAlreadyExists": ResourceAlreadyExistsError,
    "BucketAlreadyOwnedByYou": ResourceAlreadyExistsError,
    "InvalidPermission.Duplicate": ResourceAlreadyExistsError,
    "InvalidKeyPair.Duplicate": ResourceAlreadyExistsError,
    "AccessDenied": AccessDeniedError,
    "AccessDeniedException": AccessDeniedError,
    "UnauthorizedOperation": AccessDeniedError,
    "InvalidClientTokenId": AccessDeniedError,
    "SignatureDoesNotMatch": AccessDeniedError,
    "ValidationError": ValidationError,
    "ValidationException": ValidationError,
    "InvalidParameterValue": ValidationError,
    "MalformedPolicyDocument": ValidationError,
    "BucketNotEmpty": ValidationError,
    "InvalidBucketName": ValidationError,
    # IAM refuses to delete an instance profile that still has a role attached.
    "DeleteConflict": ValidationError,
    # EC2's "mixed NetworkInterfaces with top-level SubnetId/SecurityGroupIds" and
    # similar shape errors -- a request-construction bug, not a missing resource.
    "InvalidParameterCombination": ValidationError,
    "NoSuchBucketPolicy": ResourceNotFoundError,
    "NoSuchTagSet": ResourceNotFoundError,
    "NoSuchPublicAccessBlockConfiguration": ResourceNotFoundError,
    "ServerSideEncryptionConfigurationNotFoundError": ResourceNotFoundError,
    "NoSuchLifecycleConfiguration": ResourceNotFoundError,
    "Throttling": ThrottlingError,
    "ThrottlingException": ThrottlingError,
    "RequestLimitExceeded": ThrottlingError,
    "TooManyRequestsException": ThrottlingError,
    "ServiceUnavailable": ServiceUnavailableError,
    "InternalError": ServiceUnavailableError,
    "InternalFailure": ServiceUnavailableError,
}

_LOCAL_HOSTS: Final[tuple[str, ...]] = ("localhost", "127.0.0.1", "::1", "0.0.0.0")

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _targeted_local_endpoint(
    exc: EndpointConnectionError | ConnectTimeoutError | ReadTimeoutError,
) -> bool:
    """Whether the connection failure's target endpoint looks like a local one.

    Reads the endpoint straight off the botocore exception (``exc.kwargs["endpoint_url"]``)
    rather than any CLI setting or profile name — this is a property of the specific
    failed connection, not a branch on ``settings.endpoint_url`` or ``profile``.
    """
    endpoint_url = exc.kwargs.get("endpoint_url", "")
    return any(host in endpoint_url for host in _LOCAL_HOSTS)


@contextmanager
def aws_error_boundary(service: str, operation: str) -> Iterator[None]:
    """Map botocore exceptions raised inside the block to domain exceptions.

    Args:
        service: AWS service being called (e.g. ``"iam"``), attached to any
            resulting domain error as metadata.
        operation: API operation being invoked (e.g. ``"CreateUser"``), attached
            to any resulting domain error as metadata.

    Yields:
        Nothing; the block runs for its side effects.

    Raises:
        AwsError: A subclass matching the AWS error code, or ``UnknownAwsError``
            when the code has no known mapping.
        ValidationError: botocore rejected a parameter locally, before ever
            sending the request (``ParamValidationError``).
        ServiceUnavailableError: The endpoint could not be reached at all, or didn't
            respond in time (connect or read timeout).
        MissingCredentialsError: No credentials were available for the call.
    """
    try:
        yield
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = error.get("Code", "Unknown")
        message = error.get("Message") or str(exc)
        error_cls = _ERROR_CODE_MAP.get(code, UnknownAwsError)
        raise error_cls(message, service=service, operation=operation, aws_code=code) from exc
    except ParamValidationError as exc:
        # Raised locally by botocore itself, before any request is sent -- never wrapped
        # in a ClientError, so it needs its own clause rather than a ``_ERROR_CODE_MAP``
        # entry (there is no AWS error code to look up).
        raise ValidationError(
            f"Parámetro inválido para {service} ({operation}): {exc}",
            service=service,
            operation=operation,
        ) from exc
    except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError) as exc:
        if _targeted_local_endpoint(exc):
            raise EndpointUnavailableError(
                f"No se pudo contactar a {service} ({operation}): {exc}",
                endpoint_url=exc.kwargs.get("endpoint_url", ""),
                hint="¿Está LocalStack corriendo? Ejecuta `make up`.",
                service=service,
                operation=operation,
            ) from exc
        raise ServiceUnavailableError(
            f"No se pudo contactar a {service} ({operation}): {exc}",
            hint="Verifica tu conectividad de red, la región configurada y cualquier proxy/VPN.",
            service=service,
            operation=operation,
        ) from exc
    except NoCredentialsError as exc:
        raise MissingCredentialsError(
            "No se encontraron credenciales utilizables para esta llamada a AWS.",
            hint=(
                "Revisa docs/aws-profiles.md o exporta "
                "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY."
            ),
        ) from exc


def map_aws_errors(
    *, service: str, operation: str
) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]:
    """Decorator form of :func:`aws_error_boundary`.

    Args:
        service: AWS service being called, attached to any resulting domain error.
        operation: API operation being invoked, attached to any resulting domain error.

    Returns:
        A decorator that wraps the target function's call in the same error boundary.
    """

    def decorator(func: Callable[_P, _T]) -> Callable[_P, _T]:
        @wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            with aws_error_boundary(service, operation):
                return func(*args, **kwargs)

        return wrapper

    return decorator
