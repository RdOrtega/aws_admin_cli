"""E2E test: full S3 flow against a real LocalStack (requires `make up`).

create bucket -> upload a file generated in tmp_path -> list -> download and
compare hash -> generate a presigned URL -> empty and delete the bucket.
"""

import hashlib
import json
from pathlib import Path

import pytest
from aws_admin_cli.main import app
from typer.testing import CliRunner

from tests.e2e.conftest import LOCALSTACK_ENDPOINT

_BASE_ARGS = ["--profile", "testprofile", "--endpoint-url", LOCALSTACK_ENDPOINT]
_BUCKET_NAME = "e2e-s3-flow-bucket"


@pytest.mark.e2e
@pytest.mark.usefixtures("skip_if_localstack_down")
def test_s3_bucket_object_flow_against_localstack(cli_runner: CliRunner, tmp_path: Path) -> None:
    try:
        create_result = cli_runner.invoke(
            app, [*_BASE_ARGS, "--output", "json", "s3", "bucket", "create", _BUCKET_NAME]
        )
        assert create_result.exit_code == 0, create_result.output

        source = tmp_path / "e2e-file.bin"
        source.write_bytes(b"e2e-s3-flow" * 1024)

        upload_result = cli_runner.invoke(
            app,
            [
                *_BASE_ARGS,
                "--output",
                "json",
                "s3",
                "cp",
                str(source),
                f"s3://{_BUCKET_NAME}/e2e-file.bin",
            ],
        )
        assert upload_result.exit_code == 0, upload_result.output

        list_result = cli_runner.invoke(
            app, [*_BASE_ARGS, "--output", "json", "s3", "ls", f"s3://{_BUCKET_NAME}"]
        )
        assert list_result.exit_code == 0, list_result.output
        listed_keys = [item["Key"] for item in json.loads(list_result.stdout)]
        assert "e2e-file.bin" in listed_keys

        destination = tmp_path / "e2e-file-downloaded.bin"
        download_result = cli_runner.invoke(
            app,
            [*_BASE_ARGS, "s3", "cp", f"s3://{_BUCKET_NAME}/e2e-file.bin", str(destination)],
        )
        assert download_result.exit_code == 0, download_result.output
        assert (
            hashlib.sha256(source.read_bytes()).hexdigest()
            == hashlib.sha256(destination.read_bytes()).hexdigest()
        )

        presign_result = cli_runner.invoke(
            app,
            [
                *_BASE_ARGS,
                "--output",
                "json",
                "s3",
                "presign",
                f"s3://{_BUCKET_NAME}/e2e-file.bin",
                "--expires-in",
                "900",
            ],
        )
        assert presign_result.exit_code == 0, presign_result.output
        assert _BUCKET_NAME in json.loads(presign_result.stdout)["Url"]
    finally:
        cli_runner.invoke(
            app, [*_BASE_ARGS, "s3", "bucket", "delete", _BUCKET_NAME, "--force", "--yes"]
        )
