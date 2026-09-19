"""Verifies Boto3IamGateway's list_* methods actually call get_paginator(...).

moto's IAM mock doesn't truncate results regardless of volume (confirmed
empirically: even a single, non-paginated ``list_users()`` call returns every
item), so an integration test that just counts results can't tell a paginated
implementation apart from one that forgot to paginate and got lucky. This
test catches that class of regression directly, at the boto3-client-call
level, independent of how many items a fake/mocked backend hands back.
"""

from unittest.mock import MagicMock

from aws_admin_cli.core.config import Settings
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.gateways.boto3_iam_gateway import Boto3IamGateway

from tests.conftest import FakeSessionFactory


def _gateway_with_mock_client() -> tuple[Boto3IamGateway, MagicMock]:
    session = MagicMock()
    client = session.client.return_value
    paginator = client.get_paginator.return_value
    paginator.paginate.return_value = [
        {"Users": [], "Policies": [], "Roles": [], "AttachedPolicies": []}
    ]
    settings = Settings(profile="localstack", region="us-east-1")
    factory = ClientFactory(session_factory=FakeSessionFactory(session=session), settings=settings)
    return Boto3IamGateway(client_factory=factory), client


def test_list_users_uses_paginator() -> None:
    gateway, client = _gateway_with_mock_client()

    gateway.list_users(None)

    client.get_paginator.assert_called_once_with("list_users")


def test_list_users_skips_malformed_record_instead_of_raising() -> None:
    """One user with a bad ``Path`` (e.g. missing slashes) must not block the rest.

    Regression test for the IAM TUI screen's infinite-loop bug: it fetches
    every user before showing any menu, so a single malformed record used
    to crash the whole screen on every redraw with no way to reach Delete
    User and fix it.
    """
    gateway, client = _gateway_with_mock_client()
    good_user = {
        "UserName": "demo-toggle-user",
        "UserId": "AID000000000000000EX",
        "Arn": "arn:aws:iam::000000000000:user/demo-toggle-user",
        "Path": "/",
        "CreateDate": "2026-08-28T12:00:00Z",
    }
    bad_user = {
        "UserName": "Administrador",
        "UserId": "AID000000000000000EY",
        "Arn": "arn:aws:iam::000000000000:user/Administrador",
        "Path": "Admin",  # missing leading/trailing "/" -- fails validate_path
        "CreateDate": "2026-08-28T12:00:00Z",
    }
    paginator = client.get_paginator.return_value
    paginator.paginate.return_value = [{"Users": [good_user, bad_user]}]

    users = gateway.list_users(None)

    assert [u.user_name for u in users] == ["demo-toggle-user"]


def test_list_policies_uses_paginator() -> None:
    gateway, client = _gateway_with_mock_client()

    gateway.list_policies("All", False)

    client.get_paginator.assert_called_once_with("list_policies")


def test_list_roles_uses_paginator() -> None:
    gateway, client = _gateway_with_mock_client()

    gateway.list_roles(None)

    client.get_paginator.assert_called_once_with("list_roles")


def test_list_attached_user_policies_uses_paginator() -> None:
    gateway, client = _gateway_with_mock_client()

    gateway.list_attached_user_policies("alice")

    client.get_paginator.assert_called_once_with("list_attached_user_policies")


def test_list_attached_role_policies_uses_paginator() -> None:
    gateway, client = _gateway_with_mock_client()

    gateway.list_attached_role_policies("demo-role")

    client.get_paginator.assert_called_once_with("list_attached_role_policies")
