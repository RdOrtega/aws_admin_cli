"""Local-ledger metadata for IAM audit signals AWS exposes no client-settable timestamp for.

AWS never lets a caller set ``PasswordLastUsed``/``AccessKeyLastUsed``, and
``ListAttachedUserPolicies`` carries no attach-date -- there is no way to ask
AWS "when was this user disabled" or to manufacture idle-days test data
through its own API. This module is the single place that reads/writes the
per-profile local ``ResourceRecord`` ledger (``JsonRepository``, already used
by ``CreateUserUseCase``/``DeleteUserUseCase``) standing in for both: every
caller -- the TUI's audit views (``presentation/tui/flows/iam_flow.py``) and
the ``iam user seed-audit-users`` CLI command -- goes through these functions
instead of touching ``ResourceRecord``/the repository directly, so both
encode/decode the same fields identically.
"""

from datetime import UTC, datetime
from typing import Any

from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.models.iam import IamUser
from aws_admin_cli.domain.ports.repository import Repository

__all__ = [
    "DISABLED_AT",
    "SEED_LAST_API_ACTIVITY",
    "SEED_LAST_CONSOLE_LOGIN",
    "read_user_metadata_datetime",
    "write_user_metadata",
]

DISABLED_AT = "disabled_at"
SEED_LAST_CONSOLE_LOGIN = "seed_last_console_login"
SEED_LAST_API_ACTIVITY = "seed_last_api_activity"


def _user_record_key(name: str) -> str:
    """The local-ledger ``ResourceRecord`` key for user ``name`` (see ``CreateUserUseCase``)."""
    return f"iam:user:{name}"


def read_user_metadata_datetime(
    repository: Repository[ResourceRecord], name: str, field_name: str
) -> datetime | None:
    """Read one datetime field back out of ``name``'s local ``ResourceRecord`` metadata.

    Values are always written as ISO-8601 strings (see ``write_user_metadata``)
    -- a fresh ``JsonRepository`` (a new CLI process) reloads metadata from
    disk as plain JSON, never as the original Python ``datetime``, so reading
    defensively here means this works identically whether the record was
    written earlier in this same process or by a completely separate
    invocation (e.g. the seeder CLI, read back later by a ``tui`` session).
    """
    record = repository.get(_user_record_key(name))
    if record is None:
        return None
    raw = record.metadata.get(field_name)
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(raw))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def write_user_metadata(
    repository: Repository[ResourceRecord],
    *,
    user: IamUser,
    profile: str,
    region: str,
    updates: dict[str, datetime | None],
) -> None:
    """Merge ``updates`` into ``user``'s local ``ResourceRecord`` metadata.

    A ``None`` value clears that key rather than storing it. Self-healing:
    works even if ``user`` has no existing record yet (e.g. it predates this
    CLI, or was created directly against AWS) by creating one on the spot --
    the record's own ``created_at`` is best-effort in that case, not this
    user's real IAM creation date.
    """
    key = _user_record_key(user.user_name)
    existing = repository.get(key)
    metadata: dict[str, Any] = (
        dict(existing.metadata) if existing is not None else {"path": user.path}
    )
    for field_name, value in updates.items():
        if value is None:
            metadata.pop(field_name, None)
        else:
            metadata[field_name] = value.isoformat()
    repository.save(
        ResourceRecord(
            resource_type="iam:user",
            identifier=user.user_name,
            arn=user.arn,
            profile=profile,
            region=region,
            created_at=existing.created_at if existing is not None else datetime.now(UTC),
            metadata=metadata,
        )
    )
