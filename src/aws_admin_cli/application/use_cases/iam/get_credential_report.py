"""Use case: fetch and parse the account's IAM credential report.

The credential report is AWS's own authoritative snapshot of every user's
console/API-key hygiene -- the same data source the IAM console's Security
Status page is built from. Parsing lives here, not in the gateway: the port
only promises raw CSV bytes back (see ``IamGateway.get_credential_report``),
same "gateway returns primitives, the use case shapes them" split
``AuditBucketsUseCase`` already uses for S3's audit view.
"""

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from aws_admin_cli.domain.ports.iam_gateway import IamGateway

__all__ = ["CredentialReportEntry", "GetCredentialReportUseCase"]

# The report's own sentinel strings for "this column doesn't apply" -- never a real
# timestamp, so all three collapse to `None` rather than a failed parse.
_NOT_AVAILABLE_VALUES = frozenset({"N/A", "no_information", "not_supported", ""})

# Not a real IAM user -- can't be searched, edited, or disabled through this app --
# so every audit view filters it out before a caller ever sees it.
ROOT_ACCOUNT_USER = "<root_account>"


def _parse_datetime(raw: str | None) -> datetime | None:
    if raw is None or raw in _NOT_AVAILABLE_VALUES:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() == "true"


@dataclass(frozen=True, slots=True)
class CredentialReportEntry:
    """One row of the IAM credential report, decoded into typed fields.

    Only the columns this app's audit views actually read are kept -- the
    real report has more (``access_key_*_last_rotated``, ``cert_*_active``,
    etc.) that nothing here uses.
    """

    user_name: str
    arn: str
    user_creation_time: datetime | None
    password_enabled: bool
    password_last_used: datetime | None
    password_last_changed: datetime | None
    mfa_active: bool
    access_key_1_active: bool
    access_key_1_last_used_date: datetime | None
    access_key_2_active: bool
    access_key_2_last_used_date: datetime | None

    @property
    def last_activity(self: Self) -> datetime | None:
        """Most recent of console login / either access key's last use.

        ``None`` means never active by any of those three signals (a brand
        new account included) -- the audit views treat that as maximally
        inactive, not as "no data".
        """
        candidates = [
            ts
            for ts in (
                self.password_last_used,
                self.access_key_1_last_used_date,
                self.access_key_2_last_used_date,
            )
            if ts is not None
        ]
        return max(candidates) if candidates else None

    @classmethod
    def from_csv_row(cls: type[Self], row: dict[str, str]) -> Self:
        """Build one entry from a ``csv.DictReader`` row of the raw report."""
        return cls(
            user_name=row["user"],
            arn=row["arn"],
            user_creation_time=_parse_datetime(row.get("user_creation_time")),
            password_enabled=_parse_bool(row.get("password_enabled")),
            password_last_used=_parse_datetime(row.get("password_last_used")),
            password_last_changed=_parse_datetime(row.get("password_last_changed")),
            mfa_active=_parse_bool(row.get("mfa_active")),
            access_key_1_active=_parse_bool(row.get("access_key_1_active")),
            access_key_1_last_used_date=_parse_datetime(row.get("access_key_1_last_used_date")),
            access_key_2_active=_parse_bool(row.get("access_key_2_active")),
            access_key_2_last_used_date=_parse_datetime(row.get("access_key_2_last_used_date")),
        )


@dataclass(frozen=True, slots=True)
class GetCredentialReportUseCase:
    """Fetch the IAM credential report and parse it into one entry per real user.

    The root account's own synthetic row (``<root_account>``) is dropped
    here -- every caller wants "IAM users", and the root account is never
    one of those anywhere else in this app.
    """

    gateway: IamGateway

    def execute(self: Self) -> list[CredentialReportEntry]:
        """Return one ``CredentialReportEntry`` per IAM user in the account."""
        content = self.gateway.get_credential_report()
        reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
        return [
            CredentialReportEntry.from_csv_row(row)
            for row in reader
            if row.get("user") != ROOT_ACCOUNT_USER
        ]
