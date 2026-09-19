"""Tests for the table/json/render formatters."""

import json
from dataclasses import dataclass

import pytest
from aws_admin_cli.core.enums import OutputFormat
from aws_admin_cli.presentation.cli.formatters.json_out import to_json
from aws_admin_cli.presentation.cli.formatters.render import render
from aws_admin_cli.presentation.cli.formatters.table import to_table
from rich.console import Console


def test_to_json_produces_parseable_json() -> None:
    payload = {"a": 1, "b": [1, 2, 3]}

    assert json.loads(to_json(payload)) == payload


@dataclass
class _FakeRenderContext:
    """Minimal stand-in for AppContext: render() only reads .output and .console."""

    output: OutputFormat
    console: Console


def test_render_empty_list_in_json_mode_prints_exactly_brackets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = _FakeRenderContext(output=OutputFormat.JSON, console=Console())

    render([], ctx=ctx)  # type: ignore[arg-type]

    captured = capsys.readouterr()
    assert captured.out.strip() == "[]"


def test_to_table_derives_columns_from_keys() -> None:
    table = to_table([{"name": "alice", "age": 30}, {"name": "bob", "age": 40}])

    columns = [column.header for column in table.columns]
    assert columns == ["name", "age"]
