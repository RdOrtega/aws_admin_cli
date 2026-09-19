"""The Repository port: a generic contract for local persistence adapters.

Implementations (e.g. ``JsonRepository``) MUST NOT raise AWS-related
exceptions of any kind. The only exception type a ``Repository``
implementation may raise is ``aws_admin_cli.core.exceptions.PersistenceError``
(or a subclass of it). Callers in ``application/`` rely on that contract to
tell "the local ledger is broken" apart from "AWS said no".
"""

from typing import Protocol, Self, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class Repository(Protocol[T]):
    """Port: generic local storage for a Pydantic model type."""

    def save(self: Self, item: T) -> None:
        """Insert or replace ``item``, keyed by however the implementation derives a key."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get(self: Self, key: str) -> T | None:
        """Return the item stored under ``key``, or ``None`` if there isn't one."""
        ...  # pragma: no cover -- Protocol body, never executed

    def list_all(self: Self) -> list[T]:
        """Return every stored item."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete(self: Self, key: str) -> bool:
        """Delete the item stored under ``key``. Returns ``False`` if it didn't exist."""
        ...  # pragma: no cover -- Protocol body, never executed

    def exists(self: Self, key: str) -> bool:
        """Return whether an item is stored under ``key``."""
        ...  # pragma: no cover -- Protocol body, never executed
