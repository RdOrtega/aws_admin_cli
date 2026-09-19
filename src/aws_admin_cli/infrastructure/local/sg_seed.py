"""Local-only Security Group seeder for the EC2 launch wizard's demo environment.

Deliberately NOT part of ``VpcGateway``/``Boto3VpcGateway``/``Ec2Gateway`` (Separation
of Duties -- see ``docs/least-privilege.md``'s "Separation of Duties" section and
``tests/unit/architecture/test_vpc_readonly.py``, which fails the build the instant a
``create_security_group``/``authorize_security_group*`` call appears in any of those
ports or in ``application/use_cases/ec2/``): this module calls boto3 directly, entirely
outside every port those architecture tests police, so this narrow, explicitly-scoped,
LOCAL-ONLY demo convenience never has to be smuggled into a Protocol that must stay
describe-only for real AWS. It is called from the presentation layer
(``presentation/tui/flows/ec2_flow.py``), not from a ``Flow`` importing boto3 itself.
"""

import logging

from aws_admin_cli.core.context import AppContext

_logger = logging.getLogger("aws_admin_cli")

# name -> (description, [(protocol, from_port, to_port, cidr), ...])
_DEMO_SECURITY_GROUPS: dict[str, tuple[str, list[tuple[str, int, int, str]]]] = {
    "web-sg": (
        "Demo SG (aws_admin_cli): HTTP/HTTPS",
        [("tcp", 80, 80, "0.0.0.0/0"), ("tcp", 443, 443, "0.0.0.0/0")],
    ),
    "db-sg": ("Demo SG (aws_admin_cli): PostgreSQL", [("tcp", 5432, 5432, "0.0.0.0/0")]),
    "ssh-only": ("Demo SG (aws_admin_cli): SSH", [("tcp", 22, 22, "0.0.0.0/0")]),
}


def ensure_demo_security_groups(ctx: AppContext, vpc_id: str) -> None:
    """Idempotently create ``web-sg``/``db-sg``/``ssh-only`` in ``vpc_id`` if missing.

    LOCAL-ONLY: a no-op the instant ``ctx.settings.is_local`` is ``False`` -- this
    must never run a mutating EC2 call against a real AWS account. Together with the
    VPC's own always-present ``default`` security group, this guarantees at least 4
    real, selectable security groups for the EC2 launch wizard's checkbox step.

    Safe to call before every launch: each group is looked up by name first, mirroring
    ``localstack/init/01-bootstrap.sh``'s own describe-then-create idempotent pattern
    for the rest of the simulated network, so a second call never duplicates or fails.
    """
    if not ctx.settings.is_local:
        return
    client = ctx.client_factory.ec2()
    response = client.describe_security_groups()
    existing = {
        group["GroupName"]
        for group in response.get("SecurityGroups", [])
        if group.get("VpcId") == vpc_id
    }
    for name, (description, rules) in _DEMO_SECURITY_GROUPS.items():
        if name in existing:
            continue
        group_id = client.create_security_group(
            GroupName=name, Description=description, VpcId=vpc_id
        )["GroupId"]
        for protocol, from_port, to_port, cidr in rules:
            client.authorize_security_group_ingress(
                GroupId=group_id,
                IpPermissions=[
                    {
                        "IpProtocol": protocol,
                        "FromPort": from_port,
                        "ToPort": to_port,
                        "IpRanges": [{"CidrIp": cidr}],
                    }
                ],
            )
        _logger.debug("Seeded demo security group '%s' (%s) in VPC '%s'.", name, group_id, vpc_id)
