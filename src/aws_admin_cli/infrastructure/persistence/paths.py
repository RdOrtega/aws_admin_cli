"""Filesystem path for the local resource ledger, isolated per profile."""

from pathlib import Path

from aws_admin_cli.core.config import Settings


def resource_store_path(settings: Settings) -> Path:
    """Return the per-profile path to the local resource ledger, ensuring it exists.

    Per-profile isolation is mandatory: ``localstack`` resources must never mix
    with a production profile's, so the profile name is part of the path. The
    containing directory is created with ``0o700`` permissions -- it can hold
    resource names/ARNs, so it must never be world-readable. ``mkdir(mode=...)``
    only applies that mode to a directory it actually creates, so the mode is
    re-asserted with ``chmod`` in case the directory already existed.

    Args:
        settings: Resolved configuration; ``data_dir`` and ``profile`` determine
            the path.

    Returns:
        Path to ``<data_dir>/<profile>/resources.json`` (the file itself may or
        may not exist yet -- only its parent directory is guaranteed to).
    """
    path = settings.data_dir / settings.profile / "resources.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    return path


def stack_store_path(settings: Settings) -> Path:
    """Return the per-profile path to the local stack-state ledger, ensuring it exists.

    Same isolation and permission rules as :func:`resource_store_path` -- a
    separate file (``stacks.json``, not ``resources.json``) because
    ``StackState`` is a different shape (one entry per *stack*, holding a
    list of its resources' own states) from the flat ``ResourceRecord``
    ledger every other module writes to.

    Args:
        settings: Resolved configuration; ``data_dir`` and ``profile``
            determine the path.

    Returns:
        Path to ``<data_dir>/<profile>/stacks.json`` (the file itself may or
        may not exist yet -- only its parent directory is guaranteed to).
    """
    path = settings.data_dir / settings.profile / "stacks.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    return path
