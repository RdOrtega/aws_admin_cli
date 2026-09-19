"""Small, shared helpers for translating already-interpolated manifest properties.

Every step calls these instead of hand-rolling its own ``props["x"]`` /
``KeyError`` handling, so a missing/malformed property always fails the same
way (a domain ``ValidationError`` naming the resource and the property),
regardless of which step hit it.
"""

from pathlib import Path
from typing import Any

from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.iam import PolicyDocument


def require(props: dict[str, Any], key: str, resource_id: str) -> Any:
    """Return ``props[key]``, or raise if it's missing/``None``."""
    value = props.get(key)
    if value is None:
        raise ValidationError(
            f"El recurso '{resource_id}' no especifica la property obligatoria '{key}'.",
            hint=f"Añade `{key}: ...` a sus properties.",
        )
    return value


def require_str(props: dict[str, Any], key: str, resource_id: str) -> str:
    """Return ``props[key]`` as a ``str``, or raise if it's missing/``None``."""
    return str(require(props, key, resource_id))


def optional_str(props: dict[str, Any], key: str) -> str | None:
    """Return ``props[key]`` as a ``str``, or ``None`` if absent."""
    value = props.get(key)
    return str(value) if value is not None else None


def optional_int(props: dict[str, Any], key: str) -> int | None:
    """Return ``props[key]`` as an ``int``, or ``None`` if absent."""
    value = props.get(key)
    return int(value) if value is not None else None


def optional_bool(props: dict[str, Any], key: str, *, default: bool = False) -> bool:
    """Return ``props[key]`` as a ``bool``, or ``default`` if absent."""
    value = props.get(key)
    return bool(value) if value is not None else default


def resolve_policy_document(props: dict[str, Any], resource_id: str) -> PolicyDocument:
    """Resolve exactly one of the ``document``/``document_file`` properties into a document.

    Mirrors ``presentation/cli/iam_app.py``'s ``_resolve_policy_document`` --
    same two-source contract, same error shape -- but reading a manifest
    property (inline YAML, already a ``dict``) instead of a CLI flag.
    ``document`` (inline) is parsed with ``model_validate`` (a plain dict,
    PascalCase or snake_case keys both work); ``document_file`` is read as
    JSON text, same as AWS itself would hand back a policy document.
    """
    document = props.get("document")
    document_file = props.get("document_file")
    if document is not None and document_file is not None:
        raise ValidationError(
            f"El recurso '{resource_id}' especifica 'document' y 'document_file' a la vez.",
            hint="Usa solo una de las dos properties.",
        )
    if document is not None:
        return PolicyDocument.model_validate(document)
    if document_file is not None:
        return PolicyDocument.from_aws(Path(str(document_file)).read_text(encoding="utf-8"))
    raise ValidationError(
        f"El recurso '{resource_id}' necesita 'document' o 'document_file'.",
        hint="Añade un documento de política inline con `document:`, o una ruta a un "
        "JSON con `document_file:`.",
    )
