"""Tests for JsonRepository: roundtrip, atomicity, tolerance, and per-profile isolation."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aws_admin_cli.core.exceptions import PersistenceError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.infrastructure.persistence.json_repository import JsonRepository
from aws_admin_cli.infrastructure.persistence.paths import resource_store_path
from pytest_mock import MockerFixture


def _record(identifier: str = "alice", **overrides: object) -> ResourceRecord:
    defaults: dict[str, object] = {
        "resource_type": "iam:user",
        "identifier": identifier,
        "arn": f"arn:aws:iam::123456789012:user/{identifier}",
        "profile": "localstack",
        "region": "us-east-1",
        "created_at": datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        "metadata": {"path": "/"},
    }
    defaults.update(overrides)
    return ResourceRecord.model_validate(defaults)


@pytest.fixture
def repo(tmp_path: Path) -> JsonRepository[ResourceRecord]:
    return JsonRepository(tmp_path / "resources.json", ResourceRecord)


def test_save_get_roundtrip_preserves_all_fields_including_timezone(
    repo: JsonRepository[ResourceRecord],
) -> None:
    record = _record()

    repo.save(record)
    fetched = repo.get(record.key)

    assert fetched == record
    assert fetched is not None
    assert fetched.created_at.tzinfo is not None
    assert fetched.created_at.utcoffset() == record.created_at.utcoffset()


def test_get_missing_key_returns_none(repo: JsonRepository[ResourceRecord]) -> None:
    assert repo.get("iam:user:nobody") is None


def test_delete_returns_true_then_false(repo: JsonRepository[ResourceRecord]) -> None:
    record = _record()
    repo.save(record)

    assert repo.delete(record.key) is True
    assert repo.delete(record.key) is False


def test_list_all_returns_every_item(repo: JsonRepository[ResourceRecord]) -> None:
    alice = _record("alice")
    bob = _record("bob")
    repo.save(alice)
    repo.save(bob)

    items = repo.list_all()

    assert {item.identifier for item in items} == {"alice", "bob"}


def test_missing_file_returns_empty_list_without_creating_it(tmp_path: Path) -> None:
    path = tmp_path / "resources.json"
    repo = JsonRepository(path, ResourceRecord)

    assert repo.list_all() == []
    assert not path.exists()


def test_corrupt_json_raises_persistence_error(tmp_path: Path) -> None:
    path = tmp_path / "resources.json"
    path.write_text("{not valid json", encoding="utf-8")
    repo = JsonRepository(path, ResourceRecord)

    with pytest.raises(PersistenceError):
        repo.list_all()


def test_unsupported_version_raises_persistence_error(tmp_path: Path) -> None:
    path = tmp_path / "resources.json"
    path.write_text(json.dumps({"version": 99, "items": {}}), encoding="utf-8")
    repo = JsonRepository(path, ResourceRecord)

    with pytest.raises(PersistenceError):
        repo.list_all()


def test_atomic_write_failure_leaves_original_file_intact_and_no_orphan_temp_files(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    path = tmp_path / "resources.json"
    repo = JsonRepository(path, ResourceRecord)
    original = _record("alice")
    repo.save(original)  # succeeds: os.replace is not yet patched
    original_bytes = path.read_bytes()

    mocker.patch(
        "aws_admin_cli.infrastructure.persistence.json_repository.Path.replace",
        side_effect=OSError("disk full"),
    )

    with pytest.raises(PersistenceError):
        repo.save(_record("bob"))

    assert path.read_bytes() == original_bytes
    leftover = [p for p in tmp_path.iterdir() if p != path]
    assert leftover == []


def test_profile_isolation_produces_distinct_paths(tmp_path: Path) -> None:
    from aws_admin_cli.core.config import Settings

    localstack = Settings(data_dir=tmp_path, profile="localstack")
    production = Settings(data_dir=tmp_path, profile="production")

    localstack_path = resource_store_path(localstack)
    production_path = resource_store_path(production)

    assert localstack_path != production_path

    JsonRepository(localstack_path, ResourceRecord).save(_record("alice"))

    assert JsonRepository(production_path, ResourceRecord).list_all() == []
    assert localstack_path.parent.stat().st_mode & 0o777 == 0o700
