"""The one ``questionary.Style`` every prompt in this TUI shares.

Centralized here so every flow's ``select``/``checkbox``/``text``/``confirm``/
``path`` prompt looks consistent -- a flow never builds its own ``Style``.
"""

import questionary

__all__ = ["TUI_STYLE"]

TUI_STYLE: questionary.Style = questionary.Style(
    [
        ("qmark", "fg:#00afff bold"),
        ("question", "bold"),
        ("answer", "fg:#00af5f bold"),
        ("pointer", "fg:#00afff bold"),
        ("highlighted", "fg:#00afff bold"),
        ("selected", "fg:#00af5f"),
        ("separator", "fg:#6c6c6c"),
        ("instruction", "fg:#808080"),
        ("text", ""),
        ("disabled", "fg:#858585 italic"),
    ]
)
