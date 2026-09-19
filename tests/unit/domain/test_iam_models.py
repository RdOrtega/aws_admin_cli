"""Tests for the IAM domain models: hydration, validation, and policy documents."""

import json
from datetime import UTC, datetime
from urllib.parse import quote

import pytest
from aws_admin_cli.core.exceptions import ValidationError as DomainValidationError
from aws_admin_cli.domain.models.iam import (
    IamUser,
    PolicyDocument,
    PolicyEffect,
    sanitize_path,
    sanitize_user_name,
    service_trust_policy,
)

# A real-shaped boto3 IAM `User` response (PascalCase, as AWS actually returns it).
_BOTO3_USER_RESPONSE = {
    "Path": "/",
    "UserName": "alice",
    "UserId": "AIDAEXAMPLE123456789",
    "Arn": "arn:aws:iam::123456789012:user/alice",
    "CreateDate": datetime(2024, 1, 1, tzinfo=UTC),
    "Tags": [{"Key": "Team", "Value": "platform"}],
}


def test_hydrates_from_pascal_case_boto3_response() -> None:
    user = IamUser.model_validate(_BOTO3_USER_RESPONSE)

    assert user.user_name == "alice"
    assert user.user_id == "AIDAEXAMPLE123456789"
    assert user.arn == "arn:aws:iam::123456789012:user/alice"
    assert user.tags == [{"Key": "Team", "Value": "platform"}]


@pytest.mark.parametrize("bad_name", ["", "has spaces", "a" * 65, "semi;colon"])
def test_invalid_user_name_raises_domain_validation_error(bad_name: str) -> None:
    response = dict(_BOTO3_USER_RESPONSE, UserName=bad_name)

    with pytest.raises(DomainValidationError):
        IamUser.model_validate(response)


def test_invalid_path_raises_domain_validation_error() -> None:
    response = dict(_BOTO3_USER_RESPONSE, Path="no-leading-slash/")

    with pytest.raises(DomainValidationError):
        IamUser.model_validate(response)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Harold Ortega", "Harold_Ortega"),
        ("no spaces here too", "no_spaces_here_too"),
        ("already-valid", "already-valid"),
        ("", ""),
    ],
)
def test_sanitize_user_name_replaces_spaces(raw: str, expected: str) -> None:
    assert sanitize_user_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Admin", "/Admin/"),
        ("/Admin", "/Admin/"),
        ("Admin/", "/Admin/"),
        ("/Admin/", "/Admin/"),
        ("/", "/"),
        ("", ""),
    ],
)
def test_sanitize_path_wraps_in_slashes(raw: str, expected: str) -> None:
    assert sanitize_path(raw) == expected


def test_to_aws_json_uses_pascal_case_keys() -> None:
    document = PolicyDocument.model_validate(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}],
        }
    )

    raw = document.to_aws_json()
    parsed = json.loads(raw)

    assert parsed["Version"] == "2012-10-17"
    assert parsed["Statement"][0]["Effect"] == "Allow"
    assert "  " not in raw  # no superfluous whitespace


def test_to_aws_json_roundtrips_through_from_aws() -> None:
    document = PolicyDocument.model_validate(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Deny", "Action": "s3:*", "Resource": "*"}],
        }
    )

    roundtripped = PolicyDocument.from_aws(document.to_aws_json())

    assert roundtripped == document


def test_from_aws_decodes_url_encoded_string() -> None:
    raw = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
        }
    )
    encoded = quote(raw)

    document = PolicyDocument.from_aws(encoded)

    assert document.statement[0].effect is PolicyEffect.ALLOW


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ({"Effect": "Allow", "Action": "*", "Resource": "*"}, True),
        ({"Effect": "Deny", "Action": "*", "Resource": "*"}, False),
        (
            {"Effect": "Allow", "Action": "s3:*", "Resource": "arn:aws:s3:::bucket/*"},
            False,
        ),
    ],
    ids=["allow-full-wildcard", "deny-full-wildcard", "allow-scoped"],
)
def test_has_full_wildcard(statement: dict[str, object], expected: bool) -> None:
    document = PolicyDocument.model_validate({"Version": "2012-10-17", "Statement": [statement]})

    assert document.has_full_wildcard() is expected


def test_service_trust_policy_grants_sts_assume_role() -> None:
    document = service_trust_policy("ec2.amazonaws.com")

    assert len(document.statement) == 1
    statement = document.statement[0]
    assert statement.effect is PolicyEffect.ALLOW
    assert "sts:AssumeRole" in statement.action
    assert statement.principal == {"Service": "ec2.amazonaws.com"}
