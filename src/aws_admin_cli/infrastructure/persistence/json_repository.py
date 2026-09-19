"""A generic, atomic, JSON-file-backed ``Repository[T]`` implementation."""

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Generic, Protocol, Self, TypeVar, cast

from pydantic import BaseModel

from aws_admin_cli.core.exceptions import PersistenceError

T = TypeVar("T", bound=BaseModel)

_STORE_VERSION = 1


class _KeyedModel(Protocol):
    """Structural type for a model that exposes its own repository key."""

    key: str


class JsonRepository(Generic[T]):
    """A ``Repository[T]`` backed by a single, atomically-written JSON file.

    On-disk shape: ``{"version": 1, "items": {"<key>": {...}, ...}}``. Reads
    are lazy and cached in memory for the life of the instance; ``save``/
    ``delete`` write through to disk immediately and atomically (temp file in
    the same directory, ``flush`` + ``fsync``, then ``os.replace``), so a
    crash mid-write can never corrupt the store -- a reader either sees the
    old file or the fully-written new one, never a half-written one.
    """

    def __init__(
        self: Self,
        path: Path,
        model: type[T],
        key_fn: Callable[[T], str] | None = None,
    ) -> None:
        """Initialize the repository. Does not touch the filesystem yet.

        Args:
            path: Path to the backing JSON file. Doesn't need to exist yet.
            model: The Pydantic model class stored in this repository.
            key_fn: How to compute an item's key. Defaults to reading the
                item's own ``.key`` (as ``ResourceRecord`` exposes) -- inject
                this for any ``T`` that doesn't have a ``.key`` property, to
                keep the repository generic (OCP).
        """
        self._path = path
        self._model = model
        self._key_fn = key_fn
        self._cache: dict[str, T] | None = None

    def _key_of(self: Self, item: T) -> str:
        if self._key_fn is not None:
            return self._key_fn(item)
        return cast("_KeyedModel", item).key

    def _load(self: Self) -> dict[str, T]:
        if self._cache is not None:
            return self._cache

        if not self._path.exists():
            self._cache = {}
            return self._cache

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PersistenceError(
                f"El almacén local en {self._path} no es JSON válido.",
                hint=f"Bórralo para reiniciarlo: rm {self._path}",
            ) from exc

        if not isinstance(raw, dict) or raw.get("version") != _STORE_VERSION:
            raise PersistenceError(
                f"El almacén local en {self._path} tiene un formato no soportado "
                f"(version esperada: {_STORE_VERSION}).",
                hint=f"Bórralo para reiniciarlo: rm {self._path}",
            )

        items_raw = raw.get("items", {})
        self._cache = {
            key: self._model.model_validate(value) for key, value in items_raw.items()
        }
        return self._cache

    def _persist(self: Self, store: dict[str, T]) -> None:
        payload = {
            "version": _STORE_VERSION,
            "items": {key: item.model_dump(mode="json") for key, item in store.items()},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                json.dump(payload, tmp_file, indent=2)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            tmp_path.replace(self._path)
        except OSError as exc:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink()
            raise PersistenceError(
                f"No se pudo escribir el almacén local en {self._path}.",
                hint="Verifica permisos de escritura y espacio en disco.",
            ) from exc

        self._cache = store

    def save(self: Self, item: T) -> None:
        """Insert or replace ``item``, persisting to disk immediately."""
        store = dict(self._load())
        store[self._key_of(item)] = item
        self._persist(store)

    def get(self: Self, key: str) -> T | None:
        """Return the item stored under ``key``, or ``None`` if there isn't one."""
        return self._load().get(key)

    def list_all(self: Self) -> list[T]:
        """Return every stored item."""
        return list(self._load().values())

    def delete(self: Self, key: str) -> bool:
        """Delete the item stored under ``key``. Returns ``False`` if it didn't exist."""
        store = dict(self._load())
        if key not in store:
            return False
        del store[key]
        self._persist(store)
        return True

    def exists(self: Self, key: str) -> bool:
        """Return whether an item is stored under ``key``."""
        return key in self._load()
