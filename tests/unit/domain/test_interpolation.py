"""Tests for domain/services/interpolation.py: substitution, escaping, error messages."""

import pytest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.services.interpolation import resolve_properties

_OUTPUTS = {"app-bucket": {"arn": "arn:aws:s3:::demo", "name": "demo"}}


def test_simple_full_value_substitution_preserves_the_raw_output() -> None:
    result = resolve_properties({"ref": "${app-bucket.arn}"}, _OUTPUTS)
    assert result == {"ref": "arn:aws:s3:::demo"}


def test_substitution_embedded_in_text_concatenates_as_string() -> None:
    result = resolve_properties({"ref": "prefix-${app-bucket.name}-suffix"}, _OUTPUTS)
    assert result == {"ref": "prefix-demo-suffix"}


def test_substitution_inside_nested_lists_and_dicts() -> None:
    properties = {
        "statement": [
            {"resource": ["${app-bucket.arn}", "${app-bucket.arn}/*"]},
            {"other": "plain"},
        ]
    }
    result = resolve_properties(properties, _OUTPUTS)
    assert result == {
        "statement": [
            {"resource": ["arn:aws:s3:::demo", "arn:aws:s3:::demo/*"]},
            {"other": "plain"},
        ]
    }


def test_escaped_reference_produces_a_literal() -> None:
    result = resolve_properties({"script": "echo $${not.a_reference}"}, _OUTPUTS)
    assert result == {"script": "echo ${not.a_reference}"}


def test_escaped_reference_as_the_whole_value() -> None:
    result = resolve_properties({"ref": "$${app-bucket.arn}"}, _OUTPUTS)
    assert result == {"ref": "${app-bucket.arn}"}


def test_unknown_resource_id_raises_with_available_ids() -> None:
    with pytest.raises(ValidationError) as exc_info:
        resolve_properties({"ref": "${no-existe.arn}"}, _OUTPUTS)
    message = str(exc_info.value) + str(exc_info.value.hint)
    assert "no-existe" in message
    assert "app-bucket" in message


def test_unknown_output_key_raises_with_available_keys_for_that_resource() -> None:
    with pytest.raises(ValidationError) as exc_info:
        resolve_properties({"ref": "${app-bucket.no_output}"}, _OUTPUTS)
    message = str(exc_info.value) + str(exc_info.value.hint)
    assert "no_output" in message
    assert "arn" in message  # the keys app-bucket DOES expose
    assert "name" in message


def test_non_string_values_pass_through_unchanged() -> None:
    result = resolve_properties({"count": 3, "enabled": True, "nothing": None}, _OUTPUTS)
    assert result == {"count": 3, "enabled": True, "nothing": None}


def test_multiple_references_in_one_string() -> None:
    outputs = {"a": {"x": "1"}, "b": {"y": "2"}}
    result = resolve_properties({"ref": "${a.x}-${b.y}"}, outputs)
    assert result == {"ref": "1-2"}
