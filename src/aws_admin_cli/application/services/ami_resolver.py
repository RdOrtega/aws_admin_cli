"""``AmiResolver``: translate a human-friendly AMI reference into an ``image_id``.

Same spirit as ``NetworkResolver``: an ID passes straight through, anything
else goes through a lookup -- here, either a known distro alias (resolved to
the newest matching AMI) or an exact name filter. Pure application layer:
depends only on the ``Ec2Gateway`` Protocol.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Self

from aws_admin_cli.core.exceptions import ResourceNotFoundError
from aws_admin_cli.domain.models.ec2 import Ami
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway

_AMI_ID_RE = re.compile(r"^ami-[0-9a-f]{8,17}$")

_logger = logging.getLogger("aws_admin_cli")

# alias -> (owner, Name filter pattern). Patterns use EC2's own filter wildcard
# syntax ("*"), matched server-side by DescribeImages.
_ALIASES: dict[str, tuple[str, str]] = {
    "amazon-linux-2023": ("amazon", "al2023-ami-2023.*-x86_64"),
    "amazon-linux-2": ("amazon", "amzn2-ami-hvm-*-x86_64-gp2"),
    "ubuntu-22.04": ("099720109477", "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"),
    "ubuntu-24.04": ("099720109477", "ubuntu/images/hvm-ssd/ubuntu-noble-24.04-amd64-server-*"),
    "debian-12": ("136693071363", "debian-12-amd64-*"),
}


@dataclass(slots=True)
class AmiResolver:
    """Resolves an AMI reference (``ami-...``, a known alias, or an exact name) to an ``Ami``.

    Caches every resolution for the lifetime of this instance -- resolving
    the same reference twice costs one ``describe_images`` call, not two.
    """

    gateway: Ec2Gateway
    _cache: dict[str, Ami] = field(default_factory=dict, init=False, repr=False)

    def resolve(self: Self, ref: str) -> Ami:
        """Resolve ``ref`` to an ``Ami``.

        Args:
            ref: ``"ami-xxxxxxxx"`` for a direct ID lookup; one of the known
                aliases (see ``_ALIASES``) to get the newest matching AMI from
                its owner; or any other string, matched as an exact ``Name``.

        Raises:
            ResourceNotFoundError: Nothing matched -- the hint lists the
                known aliases. An alias can never be "ambiguous": among
                several candidates, the newest by ``creation_date`` always wins.
        """
        cached = self._cache.get(ref)
        if cached is not None:
            return cached

        if _AMI_ID_RE.match(ref):
            resolved = self._resolve_by_id(ref)
        elif ref in _ALIASES:
            resolved = self._resolve_by_alias(ref)
        else:
            resolved = self._resolve_by_exact_name(ref)

        self._cache[ref] = resolved
        return resolved

    def _resolve_by_id(self: Self, ref: str) -> Ami:
        matches = self.gateway.describe_images([ref], None, None)
        if not matches:
            raise ResourceNotFoundError(
                f"No se encontró ninguna AMI con id '{ref}'.",
                hint="Alias disponibles: " + ", ".join(sorted(_ALIASES)),
            )
        return matches[0]

    def _resolve_by_alias(self: Self, ref: str) -> Ami:
        owner, name_pattern = _ALIASES[ref]
        candidates = self.gateway.describe_images(None, [owner], {"name": [name_pattern]})
        if not candidates:
            raise ResourceNotFoundError(
                f"El alias '{ref}' no resolvió a ninguna AMI en este entorno.",
                hint="LocalStack/moto exponen un catálogo de AMIs reducido y ficticio -- "
                "puede que este alias no tenga equivalente aquí. Usa un ami-id directo, "
                "o consulta `ec2 ami list` para ver qué hay disponible.",
            )
        _logger.debug("Alias '%s' resolvió a %d candidata(s).", ref, len(candidates))
        return max(candidates, key=lambda ami: ami.creation_date)

    def _resolve_by_exact_name(self: Self, ref: str) -> Ami:
        candidates = self.gateway.describe_images(None, None, {"name": [ref]})
        if not candidates:
            raise ResourceNotFoundError(
                f"No se encontró ninguna AMI con nombre exacto '{ref}'.",
                hint="Usa un ami-id, o uno de estos alias: " + ", ".join(sorted(_ALIASES)),
            )
        return max(candidates, key=lambda ami: ami.creation_date)
