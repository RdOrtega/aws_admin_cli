"""The interactive TUI layer: arrow-navigable menus wrapping the same use cases the CLI calls.

Nothing under ``presentation/cli/`` imports from here, and nothing here
imports ``typer``/``click`` -- the two presentation layers are siblings, both
built on ``presentation/wiring.py``, never on each other.
"""
