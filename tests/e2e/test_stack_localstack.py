"""E2E test: the full stack orchestration flow against a real LocalStack (requires `make up`).

The rehearsal this whole engine was built for: apply the example manifest,
confirm every resource actually shows up under the ordinary (non-stack)
listing commands, destroy it, and confirm the account is clean again -- plus
a real rollback scenario using the intentionally-broken example manifest.
"""

import json
from pathlib import Path

import pytest
from aws_admin_cli.main import app
from typer.testing import CliRunner

from tests.e2e.conftest import LOCALSTACK_ENDPOINT

_BASE_ARGS = ["--profile", "testprofile", "--endpoint-url", LOCALSTACK_ENDPOINT]
_STACK_NAME = "demo-webapp"
_EXAMPLES_DIR = Path(__file__).parents[2] / "examples"


def _json(result_stdout: str) -> object:
    return json.loads(result_stdout)


def _has_name_tag(instances: list[dict[str, object]], name: str) -> bool:
    return any(
        any(tag.get("Value") == name for tag in (instance.get("Tags") or []))
        for instance in instances
    )


def _cleanup(cli_runner: CliRunner) -> None:
    cli_runner.invoke(app, [*_BASE_ARGS, "stack", "destroy", _STACK_NAME, "--yes", "--force"])


@pytest.mark.e2e
@pytest.mark.usefixtures("skip_if_localstack_down")
def test_stack_apply_status_and_destroy_full_flow(cli_runner: CliRunner) -> None:
    try:
        apply_result = cli_runner.invoke(
            app,
            [
                *_BASE_ARGS,
                "--output",
                "json",
                "stack",
                "apply",
                str(_EXAMPLES_DIR / "webapp-stack.yaml"),
                "--yes",
            ],
        )
        assert apply_result.exit_code == 0, apply_result.output

        status_result = cli_runner.invoke(
            app,
            [*_BASE_ARGS, "--output", "json", "stack", "status", _STACK_NAME, "--refresh"],
        )
        assert status_result.exit_code == 0, status_result.output
        status_payload = _json(status_result.stdout)
        assert isinstance(status_payload, dict)
        assert status_payload["status"] == "applied"
        # --refresh found no drift: every resource this apply created is still CREATED.
        assert all(
            r["status"] == "created" for r in status_payload["resources"] if r["created_by_stack"]
        )

        # Cross-check: the resources show up under the ordinary listing commands too --
        # this stack didn't do anything a human using those commands directly couldn't see.
        instances = _json(
            cli_runner.invoke(
                app,
                [*_BASE_ARGS, "--output", "json", "ec2", "instance", "list", "--managed-only"],
            ).stdout
        )
        assert isinstance(instances, list)
        assert _has_name_tag(instances, "demo-webapp-server")

        roles = _json(
            cli_runner.invoke(app, [*_BASE_ARGS, "--output", "json", "iam", "role", "list"]).stdout
        )
        assert isinstance(roles, list)
        assert any(r["RoleName"] == "demo-webapp-role" for r in roles)

        buckets = _json(
            cli_runner.invoke(
                app, [*_BASE_ARGS, "--output", "json", "s3", "bucket", "list"]
            ).stdout
        )
        assert isinstance(buckets, list)
        assert any(b["Name"] == "demo-webapp-assets" for b in buckets)

        destroy_result = cli_runner.invoke(
            app, [*_BASE_ARGS, "--output", "json", "stack", "destroy", _STACK_NAME, "--yes"]
        )
        assert destroy_result.exit_code == 0, destroy_result.output

        instances_after = _json(
            cli_runner.invoke(
                app,
                [*_BASE_ARGS, "--output", "json", "ec2", "instance", "list", "--managed-only"],
            ).stdout
        )
        assert isinstance(instances_after, list)
        still_running = [i for i in instances_after if i["State"] != "terminated"]
        assert not _has_name_tag(still_running, "demo-webapp-server")

        roles_after = _json(
            cli_runner.invoke(app, [*_BASE_ARGS, "--output", "json", "iam", "role", "list"]).stdout
        )
        assert isinstance(roles_after, list)
        assert not any(r["RoleName"] == "demo-webapp-role" for r in roles_after)

        buckets_after = _json(
            cli_runner.invoke(
                app, [*_BASE_ARGS, "--output", "json", "s3", "bucket", "list"]
            ).stdout
        )
        assert isinstance(buckets_after, list)
        assert not any(b["Name"] == "demo-webapp-assets" for b in buckets_after)
    finally:
        _cleanup(cli_runner)


@pytest.mark.e2e
@pytest.mark.usefixtures("skip_if_localstack_down")
def test_stack_apply_failure_rolls_back_and_leaves_the_account_clean(
    cli_runner: CliRunner,
) -> None:
    try:
        result = cli_runner.invoke(
            app,
            [
                *_BASE_ARGS,
                "stack",
                "apply",
                str(_EXAMPLES_DIR / "broken-stack.yaml"),
                "--yes",
            ],
        )
        assert result.exit_code != 0

        roles = _json(
            cli_runner.invoke(app, [*_BASE_ARGS, "--output", "json", "iam", "role", "list"]).stdout
        )
        assert isinstance(roles, list)
        assert not any(r["RoleName"] == "demo-webapp-role" for r in roles)

        buckets = _json(
            cli_runner.invoke(
                app, [*_BASE_ARGS, "--output", "json", "s3", "bucket", "list"]
            ).stdout
        )
        assert isinstance(buckets, list)
        assert not any(b["Name"] == "demo-webapp-assets" for b in buckets)
    finally:
        _cleanup(cli_runner)
