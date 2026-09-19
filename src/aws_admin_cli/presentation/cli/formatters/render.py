"""Dispatch command output to the table or JSON formatter, per ``AppContext.output``."""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from aws_admin_cli.core.enums import OutputFormat
from aws_admin_cli.presentation.cli.formatters.json_out import to_json
from aws_admin_cli.presentation.cli.formatters.table import to_table

if TYPE_CHECKING:
    from aws_admin_cli.core.context import AppContext


def render(data: Any, *, ctx: "AppContext", title: str | None = None) -> None:
    """Render ``data`` through the table or JSON formatter, per ``ctx.output``.

    Args:
        data: A single mapping, a sequence of mappings, or any other
            JSON-serializable value.
        ctx: Application context; determines destination console and format.
        title: Optional table title (ignored in JSON mode).
    """
    rows = _as_rows(data)

    if ctx.output is OutputFormat.JSON:
        payload: Any = rows if _is_sequence(data) else data
        # Plain print, not Console markup: JSON on STDOUT must stay parseable and
        # must never be interleaved with Rich-formatted log output on STDERR.
        print(to_json(payload))
        return

    if not rows:
        ctx.console.print("[dim]Sin resultados[/]")
        return

    ctx.console.print(to_table(rows, title=title))


def _is_sequence(data: Any) -> bool:
    return isinstance(data, Sequence) and not isinstance(data, str | bytes)


def _as_rows(data: Any) -> Sequence[Mapping[str, Any]]:
    if isinstance(data, Mapping):
        return [data]
    if _is_sequence(data):
        return list(data)
    return []
