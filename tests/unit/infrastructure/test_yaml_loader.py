"""Tests for infrastructure/manifests/yaml_loader.py: parsing, errors, and the security guard."""

from pathlib import Path

import pytest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.infrastructure.manifests.yaml_loader import load_manifest

_VALID_MANIFEST = """\
apiVersion: v1
name: demo
resources:
  - id: app-role
    kind: iam:role
    properties:
      role_name: demo-role
      service: ec2.amazonaws.com
"""


def test_valid_manifest_loads(tmp_path: Path) -> None:
    path = tmp_path / "stack.yaml"
    path.write_text(_VALID_MANIFEST)

    manifest = load_manifest(path)

    assert manifest.name == "demo"
    assert len(manifest.resources) == 1
    assert manifest.resources[0].id == "app-role"


def test_missing_file_raises_with_the_path(tmp_path: Path) -> None:
    path = tmp_path / "no-existe.yaml"

    with pytest.raises(ValidationError) as exc_info:
        load_manifest(path)

    assert str(path) in str(exc_info.value)


def test_malformed_yaml_raises_with_line_and_column(tmp_path: Path) -> None:
    path = tmp_path / "malformed.yaml"
    path.write_text("apiVersion: v1\nname: demo\nresources:\n  - id: x\n  kind: iam:role\n")

    with pytest.raises(ValidationError) as exc_info:
        load_manifest(path)

    message = str(exc_info.value)
    assert "línea" in message
    assert "columna" in message


def test_non_mapping_root_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "list-root.yaml"
    path.write_text("- just\n- a\n- list\n")

    with pytest.raises(ValidationError):
        load_manifest(path)


def test_valid_yaml_that_fails_manifest_validation_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "no-resources.yaml"
    path.write_text("apiVersion: v1\nname: demo\nresources: []\n")

    with pytest.raises(ValidationError) as exc_info:
        load_manifest(path)
    assert exc_info.value.exit_code == 64


def test_missing_required_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "no-name.yaml"
    path.write_text("apiVersion: v1\nresources:\n  - id: x\n    kind: iam:role\n")

    with pytest.raises(ValidationError):
        load_manifest(path)


def test_file_over_size_limit_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "huge.yaml"
    path.write_text("x" * (1024 * 1024 + 1))

    with pytest.raises(ValidationError) as exc_info:
        load_manifest(path)
    assert "1 MB" in str(exc_info.value) or "1048576" in str(exc_info.value)


# -- SECURITY: yaml.safe_load, never yaml.load ---------------------------------------------


def test_python_object_apply_tag_is_rejected_and_never_executes(tmp_path: Path) -> None:
    path = tmp_path / "evil.yaml"
    marker = tmp_path / "pwned.txt"
    # If this ever ran (yaml.load instead of yaml.safe_load), it would create this file.
    path.write_text(
        f'!!python/object/apply:os.system ["touch {marker}"]\n'
    )

    with pytest.raises(ValidationError):
        load_manifest(path)

    assert not marker.exists(), "yaml.load ejecutó el payload -- se debe usar yaml.safe_load."


def test_python_object_apply_tag_nested_inside_a_manifest_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "evil-nested.yaml"
    path.write_text(
        "apiVersion: v1\n"
        "name: demo\n"
        "resources:\n"
        "  - id: app-role\n"
        "    kind: iam:role\n"
        "    properties:\n"
        '      role_name: !!python/object/apply:os.system ["echo pwned"]\n'
    )

    with pytest.raises(ValidationError):
        load_manifest(path)
