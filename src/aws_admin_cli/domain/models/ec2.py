"""EC2 domain models.

Same hydration pattern as the rest of ``domain/models/``: PascalCase alias
generator matching AWS's own casing, ``populate_by_name=True``, and
``extra="ignore"``. ``tag_value`` is imported from ``domain/models/vpc.py``
rather than duplicated -- EC2 tags use the exact same
``[{"Key": ..., "Value": ...}]`` shape VPC resources do.

NO PRICING DATA. ``InstanceTypeSpec`` deliberately carries only vCPU/memory
specs, never a per-hour price: a price table hardcoded in this codebase would
go stale the moment AWS changes pricing, varies by region and by purchase
model (on-demand, reserved, spot, savings plans), and showing a number that
might already be wrong is worse than showing nothing. If cost visibility
matters, that's what AWS Cost Explorer / Pricing API are for -- not this CLI.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_pascal

from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.vpc import tag_value

_MODEL_CONFIG = ConfigDict(
    frozen=True,
    extra="ignore",
    populate_by_name=True,
    alias_generator=to_pascal,
)


class InstanceState(str, Enum):
    """An EC2 instance's lifecycle state, with its own valid-transition rules.

    Modeling these as methods/properties on the enum (rather than scattering
    ``if state == ...`` checks through use cases) means the state machine has
    exactly one place it can be wrong.
    """

    PENDING = "pending"
    RUNNING = "running"
    SHUTTING_DOWN = "shutting-down"
    TERMINATED = "terminated"
    STOPPING = "stopping"
    STOPPED = "stopped"

    @property
    def can_start(self: Self) -> bool:
        """Whether ``StartInstances`` is valid from this state: only from STOPPED."""
        return self is InstanceState.STOPPED

    @property
    def can_stop(self: Self) -> bool:
        """Whether ``StopInstances`` is valid from this state: only from RUNNING."""
        return self is InstanceState.RUNNING

    @property
    def can_reboot(self: Self) -> bool:
        """Whether ``RebootInstances`` is valid from this state: only from RUNNING."""
        return self is InstanceState.RUNNING

    @property
    def can_terminate(self: Self) -> bool:
        """Whether ``TerminateInstances`` is valid from this state.

        Allowed from anything except a state already terminating or terminated.
        """
        return self not in (InstanceState.TERMINATED, InstanceState.SHUTTING_DOWN)

    @property
    def is_transitional(self: Self) -> bool:
        """Whether this is a transient state the instance will move out of on its own."""
        return self in (
            InstanceState.PENDING,
            InstanceState.STOPPING,
            InstanceState.SHUTTING_DOWN,
        )

    @property
    def is_billable(self: Self) -> bool:
        """Whether compute is being billed in this state.

        Only ``RUNNING`` bills for compute -- but a ``STOPPED`` instance still
        bills for its attached EBS volumes, so "not billable" here means "not
        billed for compute", not "costs nothing".
        """
        return self is InstanceState.RUNNING

    def transition_error(self: Self, action: str) -> str:
        """Build a human-readable message for why ``action`` isn't valid from this state.

        Args:
            action: The attempted action, e.g. ``"iniciar"``, ``"detener"``.

        Returns:
            e.g. ``"No se puede iniciar una instancia en estado 'terminated'."``
        """
        return f"No se puede {action} una instancia en estado '{self.value}'."


class Ami(BaseModel):
    """An Amazon Machine Image, as returned by ``DescribeImages``."""

    model_config = _MODEL_CONFIG

    image_id: str
    name: str = ""
    description: str = ""
    architecture: str = ""
    platform_details: str = ""
    root_device_type: str = ""
    creation_date: datetime
    owner_id: str = ""
    state: str = ""
    public: bool = Field(default=False, alias="Public")
    tags: list[dict[str, str]] = Field(default_factory=list)
    # Only set when fetched through a region-pinned call (a single AMI lookup
    # routed to a specific region, or the every-region scan) -- ``None`` for
    # the plain profile-default ``describe_images`` call, same convention as
    # ``Instance.region``.
    region: str | None = None


class KeyPairInfo(BaseModel):
    """A key pair's metadata (never its private material), as returned by ``DescribeKeyPairs``."""

    model_config = _MODEL_CONFIG

    key_name: str
    key_pair_id: str
    key_fingerprint: str
    key_type: str = "rsa"
    tags: list[dict[str, str]] = Field(default_factory=list)


@dataclass(slots=True, repr=False)
class KeyMaterial:
    """A newly created key pair's PRIVATE key material.

    Deliberately not a Pydantic model: ``repr=False`` plus the manual
    ``__repr__``/``__str__`` overrides below are what keep ``private_key`` out
    of any traceback, log line, or ``f"{obj}"`` interpolation -- a Pydantic
    model's default repr would happily print every field, private key
    included, the first time this object crossed an exception message.
    """

    key_name: str
    private_key: str

    def __repr__(self: Self) -> str:
        """Redacted representation -- NEVER the private key material."""
        return f"<KeyMaterial name={self.key_name} [REDACTED]>"

    def __str__(self: Self) -> str:
        """Same redacted representation as ``__repr__``."""
        return self.__repr__()


class BlockDevice(BaseModel):
    """An EBS block device attached to an instance.

    ``volume_size_gb``/``volume_type``/``encrypted`` are ``None``/``False``
    when hydrated from a ``DescribeInstances`` response: that API's
    ``BlockDeviceMappings[].Ebs`` only carries ``VolumeId``/``Status``/
    ``AttachTime``/``DeleteOnTermination`` -- size, type, and encryption are
    properties of the EBS *volume* resource, only available via
    ``DescribeVolumes`` (which this phase's ``Ec2Gateway`` doesn't call). They
    ARE always known at launch time (``LaunchSpec`` carries them), just not
    recoverable from a plain instance describe afterwards.
    """

    model_config = _MODEL_CONFIG

    device_name: str
    volume_id: str | None = None
    volume_size_gb: int | None = None
    volume_type: str | None = None
    encrypted: bool = False
    delete_on_termination: bool = False


_MANAGED_BY_TAG_KEY = "ManagedBy"
_MANAGED_BY_TAG_VALUE = "aws-admin-cli"


class Instance(BaseModel):
    """An EC2 instance, from ``DescribeInstances`` (one flattened ``Instances[]`` entry)."""

    model_config = _MODEL_CONFIG

    instance_id: str
    instance_type: str
    state: InstanceState
    image_id: str
    private_ip_address: str | None = None
    public_ip_address: str | None = None
    subnet_id: str | None = None
    vpc_id: str | None = None
    availability_zone: str | None = None
    security_group_ids: list[str] = Field(default_factory=list)
    security_group_names: list[str] = Field(default_factory=list)
    key_name: str | None = None
    iam_instance_profile_arn: str | None = None
    launch_time: datetime
    architecture: str = ""
    block_devices: list[BlockDevice] = Field(default_factory=list)
    tags: list[dict[str, str]] = Field(default_factory=list)
    # Only set by ``describe_instances_all_regions`` (the region a cross-region scan
    # found this instance in) -- ``None`` for every single-region call, which already
    # knows its one target region from ``ctx.settings.region`` without needing this.
    region: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten_nested_response(cls: type[Self], data: object) -> object:
        """Flatten boto3's nested ``DescribeInstances`` shape before field validation.

        Handles (only when the raw AWS shape is present -- a value already in
        our own flat shape, e.g. from a test building an ``Instance`` with
        snake_case kwargs, passes through untouched):
        ``State`` (``{"Name": "running", ...}`` -> the plain string),
        ``SecurityGroups`` (-> ``SecurityGroupIds``/``SecurityGroupNames``),
        ``Placement.AvailabilityZone`` (-> top-level ``AvailabilityZone``),
        ``IamInstanceProfile.Arn`` (-> top-level ``IamInstanceProfileArn``),
        ``BlockDeviceMappings`` (-> top-level ``BlockDevices``).
        """
        if not isinstance(data, dict):
            return data
        flat: dict[str, Any] = dict(data)

        state = flat.get("State")
        if isinstance(state, dict):
            flat["State"] = state.get("Name")

        security_groups = flat.get("SecurityGroups")
        if isinstance(security_groups, list):
            flat.setdefault("SecurityGroupIds", [sg.get("GroupId") for sg in security_groups])
            flat.setdefault("SecurityGroupNames", [sg.get("GroupName") for sg in security_groups])

        placement = flat.get("Placement")
        if isinstance(placement, dict) and "AvailabilityZone" not in flat:
            flat["AvailabilityZone"] = placement.get("AvailabilityZone")

        iam_instance_profile = flat.get("IamInstanceProfile")
        if isinstance(iam_instance_profile, dict):
            flat["IamInstanceProfileArn"] = iam_instance_profile.get("Arn")

        block_device_mappings = flat.get("BlockDeviceMappings")
        if isinstance(block_device_mappings, list):
            flat["BlockDevices"] = [
                {
                    "DeviceName": bdm.get("DeviceName"),
                    "VolumeId": (bdm.get("Ebs") or {}).get("VolumeId"),
                    "DeleteOnTermination": (bdm.get("Ebs") or {}).get("DeleteOnTermination", False),
                }
                for bdm in block_device_mappings
            ]

        return flat

    @property
    def name(self: Self) -> str | None:
        """This instance's ``Name`` tag, if it has one."""
        return tag_value(self.tags, "Name")

    @property
    def display_name(self: Self) -> str:
        """The ``Name`` tag if set, otherwise the raw ``instance_id`` -- always non-empty."""
        return self.name or self.instance_id

    @property
    def managed_by_cli(self: Self) -> bool:
        """Whether this instance carries the ``ManagedBy=aws-admin-cli`` tag.

        The load-bearing check for every destructive EC2 operation: this CLI
        refuses to stop/terminate an instance it didn't tag as its own unless
        told ``--force`` (see ``domain/policies/launch_rules.check_managed_tag``).
        """
        return tag_value(self.tags, _MANAGED_BY_TAG_KEY) == _MANAGED_BY_TAG_VALUE

    @property
    def root_volume_id(self: Self) -> str | None:
        """This instance's first attached EBS volume ID, or ``None`` if it has none.

        ``DescribeInstances`` never labels which ``BlockDeviceMappings`` entry
        is the root device (that's only available via ``DescribeImages``'
        ``RootDeviceName``, a separate call) -- for every instance this CLI
        itself launches, the root volume is the only one attached, so "first
        block device" is root in practice. Good enough for audit tooling
        that just needs *a* volume to snapshot before cleanup; not a
        guarantee for an instance with multiple attached volumes.
        """
        return self.block_devices[0].volume_id if self.block_devices else None


class LaunchSpec(BaseModel):
    """A fully-resolved, validated set of launch parameters -- ready for ``run_instance``.

    Built entirely by the ``launch_instance`` use case (AMI, subnet, and
    security groups already resolved to IDs; user-data and guard rails
    already validated) -- the gateway's only job is translating this into
    boto3 ``run_instances`` kwargs, never resolving or validating anything
    itself.
    """

    model_config = _MODEL_CONFIG

    image_id: str
    instance_type: str
    subnet_id: str
    security_group_ids: list[str]
    key_name: str | None = None
    iam_instance_profile_arn: str | None = None
    user_data: str | None = None
    assign_public_ip: bool = False
    volume_size_gb: int = 8
    volume_type: str = "gp3"
    encrypted: bool = True
    tags: dict[str, str] = Field(default_factory=dict)
    min_count: int = 1
    max_count: int = 1


@dataclass(frozen=True, slots=True)
class InstanceTypeSpec:
    """A curated, static catalog entry: vCPU/memory only, no pricing (see module docstring)."""

    instance_type: str
    vcpus: int
    memory_gib: float
    family: str
    is_burstable: bool


_INSTANCE_CATALOG: Final[dict[str, InstanceTypeSpec]] = {
    spec.instance_type: spec
    for spec in (
        InstanceTypeSpec("t2.micro", 1, 1.0, "t2", True),
        InstanceTypeSpec("t2.small", 1, 2.0, "t2", True),
        InstanceTypeSpec("t2.medium", 2, 4.0, "t2", True),
        InstanceTypeSpec("t2.large", 2, 8.0, "t2", True),
        InstanceTypeSpec("t3.micro", 2, 1.0, "t3", True),
        InstanceTypeSpec("t3.small", 2, 2.0, "t3", True),
        InstanceTypeSpec("t3.medium", 2, 4.0, "t3", True),
        InstanceTypeSpec("t3.large", 2, 8.0, "t3", True),
        InstanceTypeSpec("t3a.micro", 2, 1.0, "t3a", True),
        InstanceTypeSpec("t3a.small", 2, 2.0, "t3a", True),
        InstanceTypeSpec("t3a.medium", 2, 4.0, "t3a", True),
        InstanceTypeSpec("t3a.large", 2, 8.0, "t3a", True),
        InstanceTypeSpec("m5.large", 2, 8.0, "m5", False),
        InstanceTypeSpec("m5.xlarge", 4, 16.0, "m5", False),
        InstanceTypeSpec("m5.2xlarge", 8, 32.0, "m5", False),
        InstanceTypeSpec("m6i.large", 2, 8.0, "m6i", False),
        InstanceTypeSpec("m6i.xlarge", 4, 16.0, "m6i", False),
        InstanceTypeSpec("m6i.2xlarge", 8, 32.0, "m6i", False),
        InstanceTypeSpec("c5.large", 2, 4.0, "c5", False),
        InstanceTypeSpec("c5.xlarge", 4, 8.0, "c5", False),
        InstanceTypeSpec("c6i.large", 2, 4.0, "c6i", False),
        InstanceTypeSpec("c6i.xlarge", 4, 8.0, "c6i", False),
        InstanceTypeSpec("r5.large", 2, 16.0, "r5", False),
        InstanceTypeSpec("r5.xlarge", 4, 32.0, "r5", False),
    )
}


def get_instance_spec(instance_type: str) -> InstanceTypeSpec | None:
    """Look up ``instance_type`` in the static catalog.

    Args:
        instance_type: e.g. ``"t3.micro"``.

    Returns:
        Its ``InstanceTypeSpec``, or ``None`` if it's not in the (deliberately
        small, curated) catalog -- an unknown type is not an error, just
        unavailable spec info.
    """
    return _INSTANCE_CATALOG.get(instance_type)


_MAX_USER_DATA_BYTES = 16384

_SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("una AWS Access Key ID", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("una referencia a aws_secret_access_key", re.compile(r"aws_secret_access_key", re.IGNORECASE)),
    ("una clave privada (PEM)", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("un literal password=", re.compile(r"password\s*=", re.IGNORECASE)),
    ("un literal token=", re.compile(r"token\s*=", re.IGNORECASE)),
)


def validate_user_data(raw: str) -> str:
    """Validate EC2 user-data: size limit, and a real security guard rail against secrets.

    User-data is readable, in plain text, by ANY process running on the
    instance via IMDS (``http://169.254.169.254/latest/user-data``) -- not
    just the process that consumes it at boot. Embedding a credential there
    is equivalent to handing it to every process and every user with shell
    access on that box, forever (or until the instance is replaced). This
    guard rail is real security posture, not a style nag.

    Args:
        raw: The user-data script/content, as read from the source file.

    Returns:
        ``raw`` unchanged, if it passes both checks.

    Raises:
        ValidationError: ``raw`` exceeds EC2's 16384-byte hard limit (encoded
            as UTF-8), or looks like it contains a credential.
    """
    encoded = raw.encode("utf-8")
    if len(encoded) > _MAX_USER_DATA_BYTES:
        raise ValidationError(
            f"El user-data ocupa {len(encoded)} bytes, supera el límite de EC2 de "
            f"{_MAX_USER_DATA_BYTES} bytes.",
            hint="Reduce el script, o sube el contenido a S3 y descárgalo desde el "
            "user-data en tiempo de arranque en vez de incluirlo inline.",
        )
    for description, pattern in _SECRET_PATTERNS:
        if pattern.search(raw):
            raise ValidationError(
                f"El user-data parece contener {description}.",
                hint="El user-data es legible por cualquier proceso de la instancia vía "
                "IMDS (http://169.254.169.254/latest/user-data) -- nunca debe contener "
                "credenciales. Usa un IAM role (--iam-role) o AWS Secrets Manager en su lugar.",
            )
    return raw
