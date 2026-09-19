"""Table-driven tests for aws_error_boundary / _ERROR_CODE_MAP."""

import pytest
from aws_admin_cli.core.exceptions import (
    EndpointUnavailableError,
    MissingCredentialsError,
    ServiceUnavailableError,
    UnknownAwsError,
)
from aws_admin_cli.infrastructure.aws.error_mapper import _ERROR_CODE_MAP, aws_error_boundary
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)


def _client_error(code: str) -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": code, "Message": f"{code} happened"}},
        operation_name="TestOperation",
    )


@pytest.mark.parametrize("code", sorted(_ERROR_CODE_MAP))
def test_known_codes_map_to_expected_domain_error(code: str) -> None:
    expected_cls = _ERROR_CODE_MAP[code]

    with pytest.raises(expected_cls) as exc_info, aws_error_boundary("iam", "TestOperation"):
        raise _client_error(code)

    error = exc_info.value
    assert error.aws_code == code
    assert isinstance(error.__cause__, ClientError)


def test_unknown_code_maps_to_unknown_aws_error() -> None:
    with pytest.raises(UnknownAwsError) as exc_info, aws_error_boundary("iam", "TestOperation"):
        raise _client_error("TotallyMadeUpCode")

    assert exc_info.value.aws_code == "TotallyMadeUpCode"


def test_endpoint_connection_error_with_local_endpoint_hints_localstack() -> None:
    with (
        pytest.raises(ServiceUnavailableError) as exc_info,
        aws_error_boundary("sts", "GetCallerIdentity"),
    ):
        raise EndpointConnectionError(endpoint_url="http://localhost:4566/")

    assert exc_info.value.hint is not None
    assert "LocalStack" in exc_info.value.hint


def test_endpoint_connection_error_with_local_endpoint_raises_endpoint_unavailable() -> None:
    """The local-endpoint case raises the dedicated subclass ``NavigationStack`` special-cases."""
    with (
        pytest.raises(EndpointUnavailableError) as exc_info,
        aws_error_boundary("s3", "ListBuckets"),
    ):
        raise EndpointConnectionError(endpoint_url="http://localhost:4566/")

    assert exc_info.value.endpoint_url == "http://localhost:4566/"


def test_endpoint_connection_error_with_real_endpoint_is_not_endpoint_unavailable() -> None:
    """Non-local connection failures keep the base class -- no dedicated TUI ceremony."""
    with (
        pytest.raises(ServiceUnavailableError) as exc_info,
        aws_error_boundary("sts", "GetCallerIdentity"),
    ):
        raise EndpointConnectionError(endpoint_url="https://sts.us-east-1.amazonaws.com/")

    assert not isinstance(exc_info.value, EndpointUnavailableError)


def test_endpoint_connection_error_with_real_endpoint_has_generic_hint() -> None:
    with (
        pytest.raises(ServiceUnavailableError) as exc_info,
        aws_error_boundary("sts", "GetCallerIdentity"),
    ):
        raise EndpointConnectionError(endpoint_url="https://sts.us-east-1.amazonaws.com/")

    assert exc_info.value.hint is not None
    assert "LocalStack" not in exc_info.value.hint


def test_connect_timeout_error_with_local_endpoint_raises_endpoint_unavailable() -> None:
    with (
        pytest.raises(EndpointUnavailableError) as exc_info,
        aws_error_boundary("sts", "GetCallerIdentity"),
    ):
        raise ConnectTimeoutError(endpoint_url="http://localhost:4566/")

    assert exc_info.value.endpoint_url == "http://localhost:4566/"


def test_read_timeout_error_with_local_endpoint_raises_endpoint_unavailable() -> None:
    """``ReadTimeoutError`` is a distinct botocore hierarchy from ``ConnectTimeoutError``
    (not a subclass of it), so it needs its own coverage here -- this is the exact
    gap that let a hung/slow-responding endpoint escape ``aws_error_boundary``
    unmapped before ``ReadTimeoutError`` was added to its caught exception tuple.
    """
    with (
        pytest.raises(EndpointUnavailableError) as exc_info,
        aws_error_boundary("sts", "GetCallerIdentity"),
    ):
        raise ReadTimeoutError(endpoint_url="http://localhost:4566/")

    assert exc_info.value.endpoint_url == "http://localhost:4566/"


def test_read_timeout_error_with_real_endpoint_is_not_endpoint_unavailable() -> None:
    with (
        pytest.raises(ServiceUnavailableError) as exc_info,
        aws_error_boundary("sts", "GetCallerIdentity"),
    ):
        raise ReadTimeoutError(endpoint_url="https://sts.us-east-1.amazonaws.com/")

    assert not isinstance(exc_info.value, EndpointUnavailableError)


def test_no_credentials_error_maps_to_missing_credentials_error() -> None:
    with (
        pytest.raises(MissingCredentialsError),
        aws_error_boundary("sts", "GetCallerIdentity"),
    ):
        raise NoCredentialsError()
