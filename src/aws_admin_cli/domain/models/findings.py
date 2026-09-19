"""Security-audit findings: the output shape of the SG audit rule engine.

Pure data -- no I/O, no AWS SDK. ``domain/policies/sg_audit_rules.py`` is what
produces these; this module only defines the shape.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Self


class Severity(str, Enum):
    """A finding's severity, ordered worst-to-best via :attr:`rank`."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"

    @property
    def rank(self: Self) -> int:
        """Numeric rank for sorting/comparison: ``CRITICAL`` (4) down to ``INFO`` (0)."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}


@dataclass(frozen=True, slots=True)
class SecurityFinding:
    """One audit finding against a network resource.

    ``recommendation`` is deliberately phrased as a REQUEST to the team that
    owns the resource ("Solicitar a Networking restringir..."), never as an
    auto-remediation instruction -- consistent with this tool's Separation of
    Duties stance (see ``domain/ports/vpc_gateway.py``): it reports, it never
    fixes.
    """

    rule_id: str
    severity: Severity
    resource_type: str
    resource_id: str
    resource_name: str | None
    title: str
    detail: str
    recommendation: str
