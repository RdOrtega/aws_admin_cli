"""Local-ledger metadata for EC2 audit signals AWS exposes no client-settable value for.

Mirrors ``iam_audit_metadata.py`` exactly, for the same reason: AWS gives no
API to ask "when did anyone last do anything with this instance", and this
CLI's Alarms/Auto-Stop columns are a deliberate local simulation (see the
EC2/AMI blueprint), not a real CloudWatch integration. Every caller -- the
TUI's Search table and Resource Audit views (``presentation/tui/flows/
ec2_flow.py``) -- goes through these functions instead of touching
``ResourceRecord``/the repository directly, so both encode/decode the same
fields identically. Uses the SAME ``ec2:instance:{id}`` ledger key
``LaunchInstanceUseCase``/``TerminateInstanceUseCase`` already read/write, so
writes here merge into (never clobber) the launch-time metadata.
"""

from datetime import UTC, datetime
from typing import Any

from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.models.ec2 import Instance
from aws_admin_cli.domain.ports.repository import Repository

__all__ = [
    "ALARM_CONFIGURED",
    "AUTO_STOP_ENABLED",
    "SEED_LAST_ACTIVITY",
    "read_instance_metadata_bool",
    "read_instance_metadata_datetime",
    "write_instance_metadata",
]

SEED_LAST_ACTIVITY = "seed_last_activity"
AUTO_STOP_ENABLED = "auto_stop_enabled"
ALARM_CONFIGURED = "alarm_configured"


def _instance_record_key(instance_id: str) -> str:
    """The local-ledger ``ResourceRecord`` key for ``instance_id``, shared with launch/terminate."""
    return f"ec2:instance:{instance_id}"


def read_instance_metadata_datetime(
    repository: Repository[ResourceRecord], instance_id: str, field_name: str
) -> datetime | None:
    """Read one datetime field back out of ``instance_id``'s local ``ResourceRecord`` metadata.

    Same defensive-parse contract as ``iam_audit_metadata.read_user_metadata_datetime``:
    values are always written as ISO-8601 strings, so this works identically
    whether the record was written earlier in this process or by a separate
    invocation.
    """
    record = repository.get(_instance_record_key(instance_id))
    if record is None:
        return None
    raw = record.metadata.get(field_name)
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(raw))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def read_instance_metadata_bool(
    repository: Repository[ResourceRecord],
    instance_id: str,
    field_name: str,
    *,
    default: bool = False,
) -> bool:
    """Read one boolean field back out of ``instance_id``'s local ``ResourceRecord`` metadata."""
    record = repository.get(_instance_record_key(instance_id))
    if record is None:
        return default
    raw = record.metadata.get(field_name)
    if raw is None:
        return default
    return bool(raw)


def write_instance_metadata(
    repository: Repository[ResourceRecord],
    *,
    instance: Instance,
    profile: str,
    region: str,
    updates: dict[str, datetime | bool | None],
) -> None:
    """Merge ``updates`` into ``instance``'s local ``ResourceRecord`` metadata.

    A ``None`` value clears that key. Self-healing: works even if ``instance``
    has no existing record yet (e.g. it predates this CLI's tracking, or was
    launched directly against AWS) by creating one on the spot.
    """
    key = _instance_record_key(instance.instance_id)
    existing = repository.get(key)
    metadata: dict[str, Any] = dict(existing.metadata) if existing is not None else {}
    for field_name, value in updates.items():
        if value is None:
            metadata.pop(field_name, None)
        elif isinstance(value, datetime):
            metadata[field_name] = value.isoformat()
        else:
            metadata[field_name] = value
    repository.save(
        ResourceRecord(
            resource_type="ec2:instance",
            identifier=instance.instance_id,
            arn=None,
            profile=profile,
            region=region,
            created_at=existing.created_at if existing is not None else datetime.now(UTC),
            metadata=metadata,
        )
    )
