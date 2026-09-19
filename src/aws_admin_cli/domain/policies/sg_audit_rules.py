"""Security group audit rule engine: pure functions from ``SecurityGroup`` to findings.

Zero I/O, zero boto3, zero logging -- every rule is a pure function of a
single ``SecurityGroup``, so this module is trivial to reach 100% coverage on
and trivial to reason about: given the same security group, it always
produces the same findings.

OCP by construction: adding rule SG009 means writing one more ``Rule``
function and appending it to ``_RULES``. Nothing else in this module -- not
:func:`audit_security_group`, not :func:`audit_security_groups` -- ever needs
to change to support a new rule.
"""

from collections.abc import Callable, Iterable
from typing import Final

from aws_admin_cli.domain.models.findings import SecurityFinding, Severity
from aws_admin_cli.domain.models.vpc import IpPermission, SecurityGroup

Rule = Callable[[SecurityGroup], list[SecurityFinding]]

_SSH_PORT = 22
_RDP_PORT = 3389
_WEB_PORTS: Final[frozenset[int]] = frozenset({80, 443})
_WIDE_RANGE_THRESHOLD = 100  # port_count strictly greater than this is "wide".

_DATABASE_PORTS: Final[dict[int, str]] = {
    3306: "MySQL/MariaDB",
    5432: "PostgreSQL",
    1433: "Microsoft SQL Server",
    1521: "Oracle",
    27017: "MongoDB",
    6379: "Redis",
    9200: "Elasticsearch",
    5984: "CouchDB",
    11211: "Memcached",
    9042: "Cassandra",
}

# Ports SG006 ("cualquier otro puerto") must NOT re-flag, because a more specific
# rule (SG001/SG002/SG004) already covers them -- keeps a single-purpose SG (e.g.
# the bastion, with only port 22 open) reporting exactly one finding, not two.
_SG006_EXCLUDED_PORTS: Final[frozenset[int]] = frozenset(
    {_SSH_PORT, _RDP_PORT, *_WEB_PORTS, *_DATABASE_PORTS}
)


def _permission_includes_port(permission: IpPermission, port: int) -> bool:
    """Whether ``permission`` (a TCP/UDP rule) covers ``port``.

    ``ip_protocol == "-1"`` (all protocols) is deliberately excluded here --
    that case is SG003's exclusive domain, so a rule matching it isn't also
    double-counted as covering every well-known port individually.
    """
    if permission.ip_protocol not in ("tcp", "udp"):
        return False
    if permission.from_port is None or permission.to_port is None:
        return False
    return permission.from_port <= port <= permission.to_port


def _permission_overlaps_any(permission: IpPermission, ports: Iterable[int]) -> bool:
    return any(_permission_includes_port(permission, port) for port in ports)


def _finding(
    rule_id: str,
    severity: Severity,
    sg: SecurityGroup,
    title: str,
    detail: str,
    recommendation: str,
) -> SecurityFinding:
    return SecurityFinding(
        rule_id=rule_id,
        severity=severity,
        resource_type="SecurityGroup",
        resource_id=sg.group_id,
        resource_name=sg.name,
        title=title,
        detail=detail,
        recommendation=recommendation,
    )


def rule_ssh_open(sg: SecurityGroup) -> list[SecurityFinding]:
    """SG001 CRITICAL: SSH (22) reachable from the whole internet."""
    findings: list[SecurityFinding] = []
    for permission in sg.ingress:
        if permission.open_to_world and _permission_includes_port(permission, _SSH_PORT):
            findings.append(
                _finding(
                    "SG001",
                    Severity.CRITICAL,
                    sg,
                    "SSH abierto al mundo",
                    f"El puerto {_SSH_PORT} (SSH) es accesible desde 0.0.0.0/0 o ::/0 en "
                    f"'{sg.display_name}' ({sg.group_id}).",
                    "Solicitar a Networking/SecOps restringir el origen del acceso SSH a "
                    "rangos internos o a un bastion host conocido, en vez de 0.0.0.0/0.",
                )
            )
    return findings


def rule_rdp_open(sg: SecurityGroup) -> list[SecurityFinding]:
    """SG002 CRITICAL: RDP (3389) reachable from the whole internet."""
    findings: list[SecurityFinding] = []
    for permission in sg.ingress:
        if permission.open_to_world and _permission_includes_port(permission, _RDP_PORT):
            findings.append(
                _finding(
                    "SG002",
                    Severity.CRITICAL,
                    sg,
                    "RDP abierto al mundo",
                    f"El puerto {_RDP_PORT} (RDP) es accesible desde 0.0.0.0/0 o ::/0 en "
                    f"'{sg.display_name}' ({sg.group_id}).",
                    "Solicitar a Networking/SecOps restringir el origen del acceso RDP a "
                    "rangos internos o a un bastion host conocido, en vez de 0.0.0.0/0.",
                )
            )
    return findings


def rule_all_protocols_open(sg: SecurityGroup) -> list[SecurityFinding]:
    """SG003 CRITICAL: every protocol/port reachable from the whole internet."""
    findings: list[SecurityFinding] = []
    for permission in sg.ingress:
        if permission.is_all_protocols and permission.open_to_world:
            findings.append(
                _finding(
                    "SG003",
                    Severity.CRITICAL,
                    sg,
                    "Todos los protocolos abiertos al mundo",
                    f"'{sg.display_name}' ({sg.group_id}) permite entrada de todos los "
                    "protocolos y puertos (-1) desde 0.0.0.0/0 o ::/0.",
                    "Solicitar a Networking/SecOps acotar esta regla a los protocolos y "
                    "puertos realmente necesarios, y restringir el origen.",
                )
            )
    return findings


def rule_database_port_open(sg: SecurityGroup) -> list[SecurityFinding]:
    """SG004 CRITICAL: a well-known database port reachable from the whole internet."""
    findings: list[SecurityFinding] = []
    for permission in sg.ingress:
        if not permission.open_to_world:
            continue
        for port, engine in _DATABASE_PORTS.items():
            if _permission_includes_port(permission, port):
                findings.append(
                    _finding(
                        "SG004",
                        Severity.CRITICAL,
                        sg,
                        f"Puerto de base de datos ({engine}) abierto al mundo",
                        f"El puerto {port} (probablemente {engine}) es accesible desde "
                        f"0.0.0.0/0 o ::/0 en '{sg.display_name}' ({sg.group_id}).",
                        f"Solicitar a Networking/SecOps restringir el origen del acceso a "
                        f"la base de datos ({engine}) a la VPC o a los security groups de "
                        "la aplicación, nunca a 0.0.0.0/0.",
                    )
                )
    return findings


def rule_wide_port_range_open(sg: SecurityGroup) -> list[SecurityFinding]:
    """SG005 HIGH: a port range wider than 100 ports open to the whole internet."""
    findings: list[SecurityFinding] = []
    for permission in sg.ingress:
        if permission.is_all_protocols or not permission.open_to_world:
            continue
        if permission.port_count > _WIDE_RANGE_THRESHOLD:
            findings.append(
                _finding(
                    "SG005",
                    Severity.HIGH,
                    sg,
                    "Rango amplio de puertos abierto al mundo",
                    f"El rango {permission.port_range_display} ({permission.port_count} "
                    f"puertos) es accesible desde 0.0.0.0/0 o ::/0 en "
                    f"'{sg.display_name}' ({sg.group_id}).",
                    "Solicitar a Networking/SecOps acotar este rango a los puertos "
                    "concretos que la aplicación necesita.",
                )
            )
    return findings


def rule_other_port_open(sg: SecurityGroup) -> list[SecurityFinding]:
    """SG006 MEDIUM: any other port (not 80/443, not already flagged above) open to the world."""
    findings: list[SecurityFinding] = []
    for permission in sg.ingress:
        if permission.is_all_protocols or not permission.open_to_world:
            continue
        if permission.port_count > _WIDE_RANGE_THRESHOLD:
            continue  # already SG005
        if _permission_overlaps_any(permission, _SG006_EXCLUDED_PORTS):
            continue  # already SG001/SG002/SG004, or an allowed web port
        findings.append(
            _finding(
                "SG006",
                Severity.MEDIUM,
                sg,
                "Puerto abierto al mundo",
                f"El puerto {permission.port_range_display} es accesible desde 0.0.0.0/0 "
                f"o ::/0 en '{sg.display_name}' ({sg.group_id}).",
                "Solicitar a Networking/SecOps confirmar si este puerto necesita estar "
                "abierto al mundo, o restringir el origen.",
            )
        )
    return findings


def rule_unrestricted_egress(sg: SecurityGroup) -> list[SecurityFinding]:
    """SG007 LOW: unrestricted egress to the world -- informational, extremely common."""
    findings: list[SecurityFinding] = []
    for permission in sg.egress:
        if permission.is_all_protocols and permission.open_to_world:
            findings.append(
                _finding(
                    "SG007",
                    Severity.LOW,
                    sg,
                    "Egress sin restricción",
                    f"'{sg.display_name}' ({sg.group_id}) permite salida sin restricción "
                    "hacia 0.0.0.0/0 o ::/0. Es el comportamiento por defecto de un SG "
                    "nuevo y extremadamente común -- no es, por sí solo, una alarma.",
                    "Si el principio de mínimo privilegio de egress importa para este "
                    "recurso, solicitar a Networking/SecOps acotar los destinos salientes.",
                )
            )
    return findings


def rule_no_ingress_rules(sg: SecurityGroup) -> list[SecurityFinding]:
    """SG008 INFO: no ingress rules at all -- possibly unused."""
    if sg.ingress:
        return []
    return [
        _finding(
            "SG008",
            Severity.INFO,
            sg,
            "Sin reglas de entrada",
            f"'{sg.display_name}' ({sg.group_id}) no tiene ninguna regla de ingress "
            "configurada -- puede estar sin uso.",
            "Confirmar con el equipo propietario si este security group sigue en uso; "
            "si no, solicitar a Networking/SecOps su retirada.",
        )
    ]


_RULES: Final[tuple[Rule, ...]] = (
    rule_ssh_open,
    rule_rdp_open,
    rule_all_protocols_open,
    rule_database_port_open,
    rule_wide_port_range_open,
    rule_other_port_open,
    rule_unrestricted_egress,
    rule_no_ingress_rules,
)


def audit_security_group(sg: SecurityGroup) -> list[SecurityFinding]:
    """Run every registered rule against a single security group.

    Args:
        sg: The security group to audit.

    Returns:
        Findings in rule-registration order (SG001, SG002, ...) -- unsorted by
        severity; sorting/filtering by severity is an application-layer
        concern (``audit_security_groups`` the use case), not this engine's.
    """
    findings: list[SecurityFinding] = []
    for rule in _RULES:
        findings.extend(rule(sg))
    return findings


def audit_security_groups(sgs: Iterable[SecurityGroup]) -> list[SecurityFinding]:
    """Run every registered rule against each security group in ``sgs``.

    Args:
        sgs: The security groups to audit.

    Returns:
        Every finding from every security group, concatenated in input order.
    """
    findings: list[SecurityFinding] = []
    for sg in sgs:
        findings.extend(audit_security_group(sg))
    return findings
