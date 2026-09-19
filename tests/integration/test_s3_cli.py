"""Integration tests for the `s3` CLI commands, via CliRunner + moto."""

import json
from pathlib import Path

from aws_admin_cli.main import app
from moto import mock_aws
from typer.testing import CliRunner

_BASE_ARGS = ["--profile", "testprofile"]


@mock_aws
def test_bucket_create_then_list_produces_parseable_json(cli_runner: CliRunner) -> None:
    create_result = cli_runner.invoke(
        app, [*_BASE_ARGS, "s3", "bucket", "create", "demo-cli-bucket"]
    )
    assert create_result.exit_code == 0, create_result.output

    list_result = cli_runner.invoke(
        app, [*_BASE_ARGS, "--output", "json", "s3", "bucket", "list"]
    )
    assert list_result.exit_code == 0, list_result.output
    payload = json.loads(list_result.stdout)
    assert any(bucket["Name"] == "demo-cli-bucket" for bucket in payload)


@mock_aws
def test_cp_local_to_s3_json_output_is_clean_of_progress_bar_noise(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    cli_runner.invoke(app, [*_BASE_ARGS, "s3", "bucket", "create", "demo-cp-bucket"])
    local_file = tmp_path / "upload.bin"
    local_file.write_bytes(b"x" * 1024)

    result = cli_runner.invoke(
        app,
        [
            *_BASE_ARGS,
            "--output",
            "json",
            "s3",
            "cp",
            str(local_file),
            "s3://demo-cp-bucket/upload.bin",
        ],
    )

    assert result.exit_code == 0, result.output
    # The whole point: even with a progress bar wired up, STDOUT must be clean,
    # parseable JSON -- the bar is disabled in JSON mode and routed to STDERR.
    payload = json.loads(result.stdout)
    assert payload["Key"] == "upload.bin"
    assert payload["Size"] == 1024


@mock_aws
def test_ls_json_output_is_a_flat_parseable_array(cli_runner: CliRunner, tmp_path: Path) -> None:
    cli_runner.invoke(app, [*_BASE_ARGS, "s3", "bucket", "create", "demo-ls-bucket"])
    local_file = tmp_path / "a.txt"
    local_file.write_text("hi")
    cli_runner.invoke(
        app, [*_BASE_ARGS, "s3", "cp", str(local_file), "s3://demo-ls-bucket/a.txt"]
    )

    result = cli_runner.invoke(
        app, [*_BASE_ARGS, "--output", "json", "s3", "ls", "s3://demo-ls-bucket"]
    )

    assert result.exit_code == 0, result.output
    # The regression this guards: `ls --output json` used to dump the whole
    # ObjectListing wrapper object ({"Objects": [...], "CommonPrefixes": [...],
    # ...}) instead of a flat, directly-iterable array like every other `list`
    # command -- `for row in json.loads(result.stdout)` would blow up on it.
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert any(row.get("Key") == "a.txt" for row in payload)


@mock_aws
def test_rm_recursive_without_yes_in_non_interactive_env_fails_cleanly(
    cli_runner: CliRunner,
) -> None:
    cli_runner.invoke(app, [*_BASE_ARGS, "s3", "bucket", "create", "demo-rm-bucket"])

    result = cli_runner.invoke(
        app, [*_BASE_ARGS, "s3", "rm", "s3://demo-rm-bucket/prefix/", "--recursive"]
    )

    assert result.exception is not None
    assert getattr(result.exception, "exit_code", None) == 64


def test_cp_both_local_raises_validation_error_with_hint(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    source = tmp_path / "a.txt"
    source.write_text("hello")
    dest = tmp_path / "b.txt"

    result = cli_runner.invoke(app, [*_BASE_ARGS, "s3", "cp", str(source), str(dest)])

    assert result.exception is not None
    assert getattr(result.exception, "exit_code", None) == 64
    assert "cp" in str(getattr(result.exception, "hint", ""))
