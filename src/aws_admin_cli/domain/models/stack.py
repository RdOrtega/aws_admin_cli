"""Stack domain models: a declarative manifest, its persisted apply state, and the statuses.

Every resource/stack moves through one of these statuses over its lifetime.

Pure domain: no boto3, no I/O, no YAML parsing (that's
``infrastructure/manifests/yaml_loader.py``'s job -- it hands this module an
already-parsed ``dict`` and this module is the only place that decides
whether it's a *valid* manifest). Every hand-authored/AWS-rejection-shaped
error here raises this project's own ``ValidationError``, exactly like every
other domain model in this codebase.
"""

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aws_admin_cli.core.exceptions import ValidationError

__all__ = [
    "ResourceKind",
    "ResourceSpec",
    "ResourceState",
    "ResourceStatus",
    "StackManifest",
    "StackState",
    "StackStatus",
    "compute_manifest_hash",
    "compute_resource_fingerprint",
]

_RESOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_STACK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")

# Any `kind` starting with one of these belongs to Networking/SecOps, never to a
# stack manifest -- see the module-level note on ResourceKind below for why.
_NETWORK_KIND_PREFIXES = ("vpc:", "subnet:", "security-group:")

_SEPARATION_OF_DUTIES_EXPLANATION = (
    "Los recursos de red (VPCs, subnets, security groups) los administra "
    "Networking/SecOps, no un manifiesto de stack -- igual que el módulo `vpc` de "
    "esta CLI es de solo lectura (ver docs/least-privilege.md, sección 'Separation "
    "of Duties'). Un stack los REFERENCIA por nombre (p. ej. `subnet: "
    "corp-private-1a`), vía NetworkResolver -- nunca los declara como un recurso "
    "que crear."
)


class ResourceKind(str, Enum):
    """Every resource type a stack manifest can declare.

    Deliberately has NO member for a VPC, subnet, or security group, and
    never will: the network is Networking/SecOps's resource, referenced by
    name via ``NetworkResolver``, never created by a stack. This absence is
    enforced structurally (see ``tests/unit/architecture/test_stack_no_network.py``),
    not just by convention -- adding a network member here is exactly the
    mistake that test exists to catch.
    """

    IAM_ROLE = "iam:role"
    IAM_POLICY = "iam:policy"
    IAM_POLICY_ATTACHMENT = "iam:policy-attachment"
    IAM_INSTANCE_PROFILE = "iam:instance-profile"
    S3_BUCKET = "s3:bucket"
    S3_BUCKET_POLICY = "s3:bucket-policy"
    EC2_KEY_PAIR = "ec2:key-pair"
    EC2_INSTANCE = "ec2:instance"


class ResourceStatus(str, Enum):
    """A single resource's lifecycle status within a stack apply/destroy."""

    PENDING = "pending"
    CREATING = "creating"
    CREATED = "created"
    SKIPPED = "skipped"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation-failed"
    DESTROYED = "destroyed"


class StackStatus(str, Enum):
    """A whole stack's status."""

    PLANNED = "planned"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    ROLLED_BACK = "rolled-back"
    ROLLBACK_INCOMPLETE = "rollback-incomplete"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"


class ResourceSpec(BaseModel):
    """One resource declaration within a manifest -- not yet resolved or applied."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: ResourceKind
    properties: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _validate_id(cls: type[Self], value: str) -> str:
        if not _RESOURCE_ID_RE.match(value):
            raise ValidationError(
                f"'{value}' no es un id de recurso válido.",
                hint="Usa minúsculas, dígitos y guiones, empezando por una letra "
                "(1-63 caracteres): ^[a-z][a-z0-9-]{1,62}$.",
            )
        return value

    @field_validator("kind", mode="before")
    @classmethod
    def _reject_network_kind(cls: type[Self], value: object) -> object:
        """Give a network `kind` a Separation-of-Duties error, not a generic enum rejection.

        Runs BEFORE pydantic tries to coerce ``value`` into the ``ResourceKind``
        enum -- a plain "not a valid ResourceKind" error would be technically
        true but wouldn't explain WHY vpc/subnet/security-group kinds don't
        (and never will) exist, which is the whole point of rejecting them.
        """
        if isinstance(value, str) and value.startswith(_NETWORK_KIND_PREFIXES):
            raise ValidationError(
                f"El recurso declara kind '{value}', que administra red -- no está "
                "permitido en un manifiesto de stack.",
                hint=_SEPARATION_OF_DUTIES_EXPLANATION,
            )
        return value


class StackManifest(BaseModel):
    """A full, validated stack manifest -- one YAML file's worth of declared resources."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    api_version: Literal["v1"] = Field(alias="apiVersion")
    name: str
    description: str | None = None
    resources: list[ResourceSpec] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls: type[Self], value: str) -> str:
        if not _STACK_NAME_RE.match(value):
            raise ValidationError(
                f"'{value}' no es un nombre de stack válido.",
                hint="Usa minúsculas, dígitos y guiones, empezando por una letra "
                "(1-41 caracteres): ^[a-z][a-z0-9-]{1,40}$.",
            )
        return value

    @model_validator(mode="after")
    def _validate_resources(self: Self) -> Self:
        if not self.resources:
            raise ValidationError(
                f"El stack '{self.name}' no declara ningún recurso.",
                hint="Añade al menos un recurso bajo `resources:`.",
            )

        ids = [resource.id for resource in self.resources]
        seen: set[str] = set()
        for resource_id in ids:
            if resource_id in seen:
                raise ValidationError(
                    f"El id de recurso '{resource_id}' está duplicado en el manifiesto.",
                    hint="Cada recurso necesita un id único dentro del stack.",
                )
            seen.add(resource_id)

        valid_ids = set(ids)
        for resource in self.resources:
            for dep in resource.depends_on:
                if dep not in valid_ids:
                    raise ValidationError(
                        f"El recurso '{resource.id}' depende de '{dep}', que no existe "
                        "en este manifiesto.",
                        hint="Ids disponibles: " + ", ".join(sorted(valid_ids)),
                    )
        return self

    def get_resource(self: Self, resource_id: str) -> ResourceSpec:
        """Look up a resource by logical id.

        Raises:
            KeyError: No resource with that id exists. Callers within a
                validated manifest never hit this -- every id used
                (``depends_on``, interpolation) is already checked against
                the manifest's own ids by the validators above or by
                ``domain/services/dependency_graph.py``.
        """
        for resource in self.resources:
            if resource.id == resource_id:
                return resource
        raise KeyError(resource_id)


class ResourceState(BaseModel):
    """One resource's persisted state, after (attempting) to apply it.

    ``created_by_stack`` is the single most important field here: it's what
    lets ``StackEngine`` tell "this apply created this, so rollback/destroy
    may touch it" apart from "this already existed and was merely resolved/
    reused, so rollback/destroy must NEVER touch it" -- see
    ``application/stacks/engine.py``'s module docstring for the rule this
    field exists to enforce.

    ``fingerprint`` is this resource's own declaration hash at the time it
    was applied (see ``compute_resource_fingerprint``) -- it's what lets
    ``StackEngine.plan`` detect a REPLACE for exactly the resources whose
    properties/kind/depends_on actually changed, not the whole stack every
    time any single resource does.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_id: str
    kind: ResourceKind
    status: ResourceStatus
    physical_id: str | None = None
    arn: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    created_by_stack: bool
    created_at: datetime | None = None
    error: str | None = None
    fingerprint: str | None = None


class StackState(BaseModel):
    """A whole stack's persisted state -- what ``StackEngine`` reads/writes via ``Repository``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: StackStatus
    profile: str
    region: str
    manifest_hash: str
    created_at: datetime
    updated_at: datetime
    resources: list[ResourceState] = Field(default_factory=list)

    @property
    def key(self: Self) -> str:
        """Unique repository key: ``"stack:<name>"`` (mirrors ``ResourceRecord.key``)."""
        return f"stack:{self.name}"

    def get_resource(self: Self, logical_id: str) -> ResourceState | None:
        """Return the persisted state for ``logical_id``, or ``None`` if never recorded."""
        return next((r for r in self.resources if r.logical_id == logical_id), None)


def _resource_dict(resource: ResourceSpec) -> dict[str, Any]:
    return {
        "id": resource.id,
        "kind": resource.kind.value,
        "properties": resource.properties,
        "depends_on": sorted(resource.depends_on),
    }


def compute_resource_fingerprint(resource: ResourceSpec) -> str:
    """Derive a stable SHA-256 fingerprint of one resource's own declaration.

    Recorded on its ``ResourceState.fingerprint`` after a successful apply,
    and recomputed from the manifest on every later ``plan`` -- a mismatch
    means THIS resource's kind/properties/depends_on changed since it was
    last applied (a REPLACE action), independent of whether any other
    resource in the manifest changed too.
    """
    canonical = json.dumps(_resource_dict(resource), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_manifest_hash(manifest: StackManifest) -> str:
    """Derive a stable SHA-256 fingerprint of ``manifest``'s resource declarations.

    Used to detect, at the whole-stack level, that the YAML changed since
    the last ``apply`` -- ``StackState.manifest_hash`` records it purely as
    a fast top-level "did anything at all change" signal; per-resource
    REPLACE detection in ``StackEngine.plan`` uses
    ``compute_resource_fingerprint`` instead, one resource at a time. Only
    ``resources`` (id, kind, properties, depends_on) are hashed --
    ``description`` is documentation, not a resource declaration, and
    shouldn't force a replan on its own.
    """
    canonical = json.dumps(
        [_resource_dict(resource) for resource in sorted(manifest.resources, key=lambda r: r.id)],
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
