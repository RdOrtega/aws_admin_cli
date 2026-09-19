"""Table rendering: turn rows of data into a Rich ``Table``."""

from collections.abc import Mapping, Sequence
from typing import Any

from rich.table import Table


def to_table(rows: Sequence[Mapping[str, Any]], *, title: str | None = None) -> Table:
    """Render ``rows`` as a Rich ``Table``, deriving columns from the first row's keys.

    Args:
        rows: Records to render. Columns come from the keys of ``rows[0]``.
        title: Optional table title.

    Returns:
        A populated Rich ``Table``. An empty ``rows`` yields an empty table shell —
        callers that want a "no results" message should check for that themselves
        (see ``render()``).
    """
    table = Table(title=title)
    if not rows:
        return table

    columns = list(rows[0].keys())
    for column in columns:
        table.add_column(column)

    for row in rows:
        table.add_row(*(_format_cell(row.get(column)) for column in columns))

    return table


def _format_cell(value: Any) -> str:
    if value is None:
        return "[dim]-[/]"
    if isinstance(value, bool):
        return "[green]✓[/]" if value else "[red]✗[/]"
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value)
    return str(value)
