"""Load a stack manifest YAML file into a validated ``StackManifest``.

Infrastructure layer (file I/O), not domain: ``domain/models/stack.py``
receives an already-parsed ``dict`` and knows nothing about YAML, files, or
paths. Always uses ``yaml.safe_load`` -- NEVER ``yaml.load``. The plain
``Loader`` in PyYAML supports tags like ``!!python/object/apply:...`` that
instantiate and CALL arbitrary Python objects while parsing -- a manifest is
untrusted input the moment it could come from anywhere but this operator's
own keyboard (a shared repo, a CI artifact, ...), so `yaml.load` here would
be a remote-code-execution vulnerability, not a convenience.
"""

from pathlib import Path

import yaml
from pydantic import ValidationError as PydanticValidationError

from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.stack import StackManifest

_MAX_MANIFEST_BYTES = 1024 * 1024  # 1 MB


def load_manifest(path: Path) -> StackManifest:
    """Read, parse, and validate the stack manifest at ``path``.

    Args:
        path: Path to a YAML file.

    Returns:
        The fully validated ``StackManifest``.

    Raises:
        ValidationError: ``path`` doesn't exist or isn't readable, exceeds
            the 1 MB size limit, contains malformed YAML (the message
            includes PyYAML's own line/column when it reports one), isn't
            shaped like a manifest (not a YAML mapping at the root), or
            fails ``StackManifest``'s own validation (bad id/name pattern,
            duplicate ids, an unknown ``depends_on`` target, a network
            ``kind``, ...).
    """
    if not path.exists():
        raise ValidationError(
            f"No existe el archivo de manifiesto '{path}'.", hint="Comprueba la ruta."
        )

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValidationError(
            f"No se pudo leer '{path}': {exc}.", hint="Comprueba los permisos de lectura."
        ) from exc

    if size > _MAX_MANIFEST_BYTES:
        raise ValidationError(
            f"El manifiesto '{path}' ocupa {size} bytes, supera el límite de "
            f"{_MAX_MANIFEST_BYTES} bytes (1 MB).",
            hint="Un manifiesto de stack no debería necesitar más de 1 MB. Si hay contenido "
            "voluminoso embebido (p. ej. un documento de política grande), muévelo a su "
            "propio archivo y referéncialo con la property `*_file` correspondiente.",
        )

    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(
            f"No se pudo leer '{path}': {exc}.", hint="Comprueba los permisos y la codificación."
        ) from exc

    try:
        # NEVER yaml.load: see this module's docstring. safe_load refuses any tag
        # that would construct/call an arbitrary Python object.
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        location = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            location = f" (línea {mark.line + 1}, columna {mark.column + 1})"
        raise ValidationError(
            f"'{path}' no es YAML válido{location}: {exc}.",
            hint="Revisa la sintaxis en ese punto (indentación, dos puntos, comillas).",
        ) from exc

    if not isinstance(data, dict):
        raise ValidationError(
            f"'{path}' no describe un manifiesto de stack: se esperaba un mapa YAML en "
            "la raíz del archivo.",
            hint="Un manifiesto empieza con `apiVersion: v1`, `name: ...`, `resources: [...]`.",
        )

    try:
        return StackManifest.model_validate(data)
    except ValidationError:
        # Already this project's own domain error (a ResourceSpec/StackManifest field
        # validator rejecting a bad id, a network kind, a duplicate, ...) -- propagate
        # as-is, it's already the right shape and message.
        raise
    except PydanticValidationError as exc:
        raise ValidationError(
            f"'{path}' no es un manifiesto de stack válido: {exc}.",
            hint="Revisa que tenga `apiVersion: v1`, `name`, y `resources` con "
            "id/kind/properties válidos para cada recurso.",
        ) from exc
