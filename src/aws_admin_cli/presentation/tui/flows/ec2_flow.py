"""The EC2 TUI screen: instances, their lifecycle, tags, AMIs, and resource audit.

Follows the same patterns ``s3_flow.py``/``iam_flow.py`` established (see
their module docstrings for the full rationale): dynamic lists,
filter-then-list (never an auto-pick), a guard-rail retry on
``ValidationError``, and the shared ``flows/_shared.py::confirm_destructive``
arrow-key destructive confirmation.

``instance launch`` shows the same "resolved summary, then confirm" UX the
CLI's ``instance_launch`` does before ever calling ``LaunchInstanceUseCase``.

AMI Management and Resource Audit are nested submenus of this screen (not
separate top-level flows), the same way IAM's "Audit Inactive Users" is a
submenu of ``IamFlow`` rather than its own root-menu entry -- AMIs are
inherently EC2-scoped resources sharing the same gateway, and Resource Audit
is a read-only view over the same instance data Search already lists.

Alarms/Auto-Stop/Last-Activity are a deliberate LOCAL SIMULATION (see
``application/services/ec2_audit_metadata.py``'s module docstring), the same
"CloudWatch-style" local ledger IAM's audit feature already uses -- not a
real CloudWatch integration.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from functools import partial
from pathlib import Path
from typing import ClassVar, Final, Self

from rich.table import Table

from aws_admin_cli.application.dto.ec2 import (
    CreateAmiRequest,
    CreateKeyPairRequest,
    LaunchInstanceRequest,
    ListInstancesRequest,
    SetInstanceSecurityGroupsRequest,
)
from aws_admin_cli.application.dto.vpc import ListSubnetsRequest
from aws_admin_cli.application.services.ec2_audit_metadata import (
    ALARM_CONFIGURED,
    AUTO_STOP_ENABLED,
    SEED_LAST_ACTIVITY,
    read_instance_metadata_bool,
    read_instance_metadata_datetime,
)
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.exceptions import AwsAdminCliError, ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.ec2 import Ami, Instance, InstanceState
from aws_admin_cli.domain.models.iam import IamUser
from aws_admin_cli.domain.models.vpc import SecurityGroup, tag_value, tags_to_dict
from aws_admin_cli.infrastructure.local.sg_seed import ensure_demo_security_groups
from aws_admin_cli.presentation.cli.formatters.render import render
from aws_admin_cli.presentation.tui.flows._shared import (
    announce_result,
    clear_and_banner,
    confirm_destructive,
    confirm_yes_no,
    prompt_text_or_cancel,
    relative_path_display,
    run_with_spinner,
)
from aws_admin_cli.presentation.tui.flows.error_handler import aws_error_handler
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT, Choice, Separator
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.tui.prompter import Prompter
from aws_admin_cli.presentation.wiring import (
    Ec2UseCases,
    build_ec2_use_cases,
    build_iam_use_cases,
    build_vpc_use_cases,
)

__all__ = ["SECURITY_USERDATA_TEMPLATE", "Ec2Flow"]

_CREATE_INSTANCE = "create_instance"
_DELETE_INSTANCE = "delete_instance"
_AMI_MANAGEMENT = "ami_management"
_AUDIT_RESOURCES = "audit_resources"
_SEARCH_THRESHOLD = 25

_EXPLORE_SEARCH = "search"

_DETAIL_EDIT_NAME = "EDIT_NAME"
_DETAIL_MANAGE_TAGS = "MANAGE_TAGS"
_DETAIL_EDIT_GROUPS = "EDIT_GROUPS"
_DETAIL_VIEW_VPC = "VIEW_VPC"
_DETAIL_START = "START"
_DETAIL_STOP = "STOP"
_DETAIL_REBOOT = "REBOOT"
_DETAIL_BACK = "BACK"

_RUN_YES = "RUN_YES"
_RUN_NO = "RUN_NO"

_TAG_ADD = "tag_add"
_TAG_DELETE = "tag_delete"

_CONFIRM_YES = "CONFIRM_YES"
_CONFIRM_NO = "CONFIRM_NO"
_CONFIRM_BACK = "CONFIRM_BACK"

_AMI_SEARCH = "SEARCH_AMIs"
_AMI_CREATE = "CREATE_AMI"
_AMI_MENU_BACK = "BACK"

_AMI_DETAIL_EDIT_NAME = "EDIT_NAME"
_AMI_DETAIL_MANAGE_TAGS = "MANAGE_TAGS"
_AMI_DETAIL_CREATE_EC2 = "CREATE_EC2"
_AMI_DETAIL_DELETE = "DELETE_AMI"
_AMI_DETAIL_BACK = "BACK"

_REBOOT_YES = "REBOOT_YES"
_REBOOT_NO = "REBOOT_NO"
_REBOOT_BACK = "REBOOT_BACK"

_AUDIT_UNDERUTILIZED = "audit_underutilized"
_AUDIT_ZOMBIE = "audit_zombie"
_UNDERUTILIZED_MIN_DAYS = 15
_ZOMBIE_MIN_DAYS = 30

_UNDERUTILIZED_LABEL = "🟡 Underutilized EC2 (Running > 15d - Candidatas a apagar)"
_ZOMBIE_LABEL = "🔴 Zombie EC2 (Stopped > 30d - Candidatas a eliminar)"

# Curated common/free-tier-friendly types, not the full EC2 catalog -- picked over a
# free-text prompt so a typo can never become a launch request for a nonexistent type.
_INSTANCE_TYPES: tuple[str, ...] = (
    "t2.micro",
    "t2.small",
    "t2.medium",
    "t3.micro",
    "t3.small",
    "t3.medium",
)

_STORAGE_DEFAULT = "storage_default"
_STORAGE_CUSTOM = "storage_custom"
_STORAGE_MANUAL = "storage_manual"

_DEFAULT_VOLUME_SIZE_GB = 8

# Curated volume-size presets offered instead of a raw text prompt -- picked over free
# entry so a typo can never become a launch request for a nonsensical size. "Manual
# entry" is the escape hatch for anything outside this curated set.
_STORAGE_PRESETS: tuple[tuple[int, str], ...] = (
    (500, "500 GiB (Standard High-Capacity)"),
    (1024, "1 TiB / 1024 GiB (Enterprise DB / Workloads)"),
    (1536, "1.5 TiB / 1536 GiB (Analytics & Big Data)"),
    (2048, "2 TiB / 2048 GiB (Large Data Store)"),
)

# Curated distro aliases (see application/services/ami_resolver.py's _ALIASES) offered
# as a menu instead of a free-text AMI ID -- "Ingresar AMI ID manualmente" is the escape
# hatch for anything outside this curated set.
_AMI_CHOICES: tuple[tuple[str, str], ...] = (
    ("Amazon Linux 2023", "amazon-linux-2023"),
    ("Ubuntu Server 24.04 LTS", "ubuntu-24.04"),
    ("Debian 12", "debian-12"),
)
_AMI_MANUAL = "ami_manual"

_TAGS_BACK = "BACK"

_OWNER_CUSTOM = "OWNER_CUSTOM"
_OWNER_ASSIGN_YES = "OWNER_ASSIGN_YES"
_OWNER_ASSIGN_NO = "OWNER_ASSIGN_NO"

_KEY_PAIR_CONTINUE = "KEY_PAIR_CONTINUE"
_KEY_PAIR_TAG = "KeyPair"
# What both the Launch Summary and the KeyPair tag show when the instance HAS an
# Owner: an owned instance is reachable through its owner, so no SSH key is created
# for it at all, and "-" says "deliberately none" where "(none)" reads like "missing".
_NO_KEY_PAIR = "-"
_KEY_NAME_PREFIX = "key-"
# AWS accepts up to 255 ASCII characters in a KeyName; this keeps the generated name
# to the subset that is also safe as a filename, since the private key is saved as
# "<KeyName>.pem".
_KEY_NAME_FORBIDDEN = re.compile(r"[^A-Za-z0-9._-]+")
_KEYS_DIR_NAME = "keys"
_KEYS_EC2_SUBDIR = "ec2"

_TAG_KEY_PRESETS: Final[tuple[tuple[str, str], ...]] = (
    ("Name (Nombre identificador)", "Name"),
    ("Environment (Entorno)", "Environment"),
    ("Owner (Responsable / Área)", "Owner"),
    ("Project (Proyecto)", "Project"),
    ("CostCenter (Centro de costo)", "CostCenter"),
)
_TAG_KEY_CUSTOM = "CUSTOM"

_TAG_VALUE_PRESETS: Final[dict[str, tuple[str, ...]]] = {
    "Environment": ("dev", "staging", "prod", "test"),
    "Owner": ("sysadmin", "devops", "cloud-team"),
}
_TAG_VALUE_CUSTOM = "CUSTOM_VALUE"
_TAG_NAME_DEFAULT_VALUE = "localstack-ec2-app"

_WIZARD_BACK_LABEL = "<- Atrás"

# A standard cloud-init/bash security baseline: patch the OS, then install and start the
# CloudWatch Agent. Offered as one of the Provision Wizard's User Data choices (the "from
# scratch" flow only -- see `_create_instance_step_user_data`), as an alternative to
# uploading a local script or launching with no user-data at all.
SECURITY_USERDATA_TEMPLATE = """#!/bin/bash
# Security baseline: patch the OS and install the CloudWatch Agent.
yum update -y
yum install -y amazon-cloudwatch-agent
systemctl enable amazon-cloudwatch-agent
systemctl start amazon-cloudwatch-agent
"""

_USERDATA_LOCAL = "userdata_local"
_USERDATA_TEMPLATE = "userdata_template"
_USERDATA_NONE = "userdata_none"
_USERDATA_SUMMARY_TEMPLATE = "Security Template Attached"
_USERDATA_SUMMARY_NONE = "none"
_DEFAULT_USERDATA_PATH = "./user_data.sh"


def _format_gib(gib: int) -> str:
    """Render a GiB size with a TiB hint once it's large enough for one to matter."""
    if gib >= 1024 and gib % 1024 == 0:
        return f"{gib} GiB ({gib // 1024} TiB)"
    if gib >= 1024:
        return f"{gib} GiB (~{gib / 1024:.2f} TiB)"
    return f"{gib} GiB"


class _WizardNav(Enum):
    """What a Provision Wizard step returns to its driving loop in ``_create_instance``.

    NEXT: advance ``step_index`` by one, to the next step.
    BACK: retreat ``step_index`` by one, to the previous step -- the state
        collected so far (``_LaunchWizardState``) is NEVER cleared, so the
        previous step re-shows with whatever was already chosen there as
        its default/pre-checked selection.
    CANCEL: abort the whole wizard immediately, discarding ``state``.
    """

    NEXT = "next"
    BACK = "back"
    CANCEL = "cancel"


@dataclass(slots=True)
class _LaunchWizardState:
    """What's been collected so far across the few reusable launch-prep steps.

    A single mutable instance threaded through the step functions still in
    use (Name, AMI, Storage) -- NOT a frozen/immutable snapshot -- so a
    step's own "<- Atrás" can re-show it with its previous answer intact.
    The minimal "from scratch" flow resolves VPC/Subnet/Security
    Groups/Owner/Instance Type as local values instead of threading them
    through this state, since it doesn't let a user step backward across
    those in sequence. AMI Quick Launch DOES support stepping back through
    all of its questions -- see ``_QuickLaunchState``/``_QuickLaunchStep``
    below, which only reuses this dataclass for its own Storage sub-step.
    """

    name: str | None = None
    ami_choice: str | None = None
    ami_ref: str | None = None
    storage_choice: str | None = None
    volume_size_gb: int | None = None


@dataclass(slots=True)
class _OwnerSubState:
    """What's been collected so far across the shared Owner sub-flow's 3 questions.

    Shared, reusable state for the ``_owner_step_confirm``/``_owner_step_filter``/
    ``_owner_step_select`` trio below -- both the minimal "from scratch" flow
    (``_CreateInstanceState.owner``) and AMI Quick Launch
    (``_QuickLaunchState.owner``) embed one of these rather than each
    duplicating their own copy of Owner/IAM-filter bookkeeping. ``iam_users``
    is fetched exactly once, in ``_owner_step_confirm`` the moment "Sí" is
    chosen, and cached here so ``_owner_step_filter``/``_owner_step_select``
    and both flows' transition functions (deciding whether to even show the
    filter step) all reuse the same list instead of re-querying IAM.
    """

    wants_owner: bool | None = None
    iam_users: list[IamUser] | None = None
    owner_filter_query: str = ""
    owner: str | None = None


@dataclass(slots=True)
class _KeyPairSubState:
    """The auto-generated key pair for an instance launched WITHOUT an Owner.

    Same "shared sub-state" idea as ``_OwnerSubState``: both the minimal
    "from scratch" flow and AMI Quick Launch embed one of these and drive the
    exact same ``_key_pair_step_auto``/``_ensure_key_pair`` pair, so the two
    flows share this behavior verbatim rather than each growing its own copy.

    ``key_name`` is decided in the STEP (so the summary and the "<- Back"
    mapping have something stable to show), but the key pair itself is only
    created in ``_ensure_key_pair``, at execute time. That split is the whole
    point: a user who steps back out of the wizard, or cancels at the final
    confirmation, must not leave an orphaned key pair behind in AWS -- exactly
    the same "nothing is created until the last confirm" contract every other
    step in these wizards already honors.
    """

    key_name: str | None = None
    saved_path: Path | None = None
    fingerprint: str | None = None
    created: bool = False


def _key_pair_base_name(instance_name: str) -> str:
    """``key-<instance name>``, reduced to characters legal in both a KeyName and a filename."""
    slug = _KEY_NAME_FORBIDDEN.sub("-", instance_name).strip("-._")
    return f"{_KEY_NAME_PREFIX}{slug or 'ec2'}"


def _key_pair_destination_dir() -> Path:
    """Where auto-generated private keys are saved -- ``keys/ec2/`` at the CLI project root.

    Both EC2 creation wizards (from scratch and from AMI) save here instead
    of ``~/.ssh`` so a key generated for an ownerless instance is easy to
    find and to keep out of version control (``keys/`` and ``*.pem`` are both
    in the project's ``.gitignore``) -- separate from ``ec2_app.py``'s own
    ``key-pair create`` command, which still defaults to ``~/.ssh``. The
    ``ec2/`` subdirectory keeps this CLI's other generated secrets (IAM's
    ``keys/iam/credentials-<name>.txt``) out of the same folder.
    """
    return Path.cwd() / _KEYS_DIR_NAME / _KEYS_EC2_SUBDIR


def _unique_key_pair_name(base: str, taken: set[str], destination_dir: Path) -> str:
    """``base``, or ``base-2``/``base-3``/... until it collides with nothing.

    Two collision sources, both fatal at creation time and both checked here
    so the name shown in the step is the name that will actually work: a key
    pair AWS already has under that name, and a ``<name>.pem`` already sitting
    in the destination directory (``CreateKeyPairUseCase`` refuses to
    overwrite one, since AWS hands out private material exactly once).
    """
    candidate = base
    suffix = 2
    while candidate in taken or (destination_dir / f"{candidate}.pem").exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


class _QuickLaunchStep(Enum):
    """Every question AMI Quick Launch can ask, in forward order.

    ``_quick_launch_from_ami``'s driving loop is the ONLY thing that
    sequences these -- no step method ever calls another step method or
    returns a different step's ``_WizardNav`` meaning. ``_quick_launch_next_step``/
    ``_quick_launch_prev_step`` are the single source of truth for what
    "forward"/"back" mean from each step, including the two conditional
    branches (skipping the Owner sub-steps entirely when the user declines
    an Owner; skipping OWNER_FILTER when the account has no IAM users at
    all) -- so the exact back-target mapping is defined in exactly one place
    each direction, never duplicated inside a step's own return logic.
    """

    NAME = "name"
    OWNER_CONFIRM = "owner_confirm"
    OWNER_FILTER = "owner_filter"
    OWNER_SELECT = "owner_select"
    KEY_PAIR = "key_pair"
    VPC = "vpc"
    SECURITY_GROUPS = "security_groups"
    STORAGE = "storage"
    CONFIRM = "confirm"


@dataclass(slots=True)
class _QuickLaunchState:
    """What's been collected so far across AMI Quick Launch's steppable questions.

    Same "single mutable instance, never reset on Back" contract as
    ``_LaunchWizardState`` -- stepping BACK and then forward again always
    re-shows a step with its previous answer already selected.
    """

    name: str | None = None
    owner: _OwnerSubState = field(default_factory=_OwnerSubState)
    key_pair: _KeyPairSubState = field(default_factory=_KeyPairSubState)
    vpc_id: str | None = None
    subnet_id: str | None = None
    security_groups: list[SecurityGroup] | None = None
    storage_state: _LaunchWizardState = field(default_factory=_LaunchWizardState)


def _quick_launch_next_step(
    current: _QuickLaunchStep, state: _QuickLaunchState
) -> _QuickLaunchStep:
    """What "forward" means from ``current`` -- the single source of truth for it.

    The three conditional branches: declining an Owner (``wants_owner`` is
    ``False``) skips both IAM sub-steps and routes through KEY_PAIR instead;
    ASSIGNING an Owner skips KEY_PAIR entirely (an owned instance is reached
    through its owner, so no SSH key is generated for it at all); and an
    empty IAM user list (``iam_users`` is falsy, only known once
    OWNER_CONFIRM has run) skips OWNER_FILTER, since OWNER_SELECT's own
    free-text fallback IS that case's whole step.

    KEY_PAIR sits immediately after the Owner decision rather than later in
    the wizard on purpose: it exists ONLY as the other arm of that decision,
    so keeping the fork adjacent to the question that creates it is what lets
    both this function and ``_quick_launch_prev_step`` express it as one
    branch each, in one place -- the same shape ``_create_instance_next_step``
    uses.
    """
    if current is _QuickLaunchStep.NAME:
        return _QuickLaunchStep.OWNER_CONFIRM
    if current is _QuickLaunchStep.OWNER_CONFIRM:
        if not state.owner.wants_owner:
            return _QuickLaunchStep.KEY_PAIR
        if state.owner.iam_users:
            return _QuickLaunchStep.OWNER_FILTER
        return _QuickLaunchStep.OWNER_SELECT
    if current is _QuickLaunchStep.OWNER_FILTER:
        return _QuickLaunchStep.OWNER_SELECT
    if current is _QuickLaunchStep.OWNER_SELECT:
        return _QuickLaunchStep.VPC
    if current is _QuickLaunchStep.KEY_PAIR:
        return _QuickLaunchStep.VPC
    if current is _QuickLaunchStep.VPC:
        return _QuickLaunchStep.SECURITY_GROUPS
    if current is _QuickLaunchStep.SECURITY_GROUPS:
        return _QuickLaunchStep.STORAGE
    if current is _QuickLaunchStep.STORAGE:
        return _QuickLaunchStep.CONFIRM
    raise AssertionError(f"CONFIRM has no 'next' step: {current}")


def _quick_launch_prev_step(
    current: _QuickLaunchStep, state: _QuickLaunchState
) -> _QuickLaunchStep | None:
    """What "back" means from ``current`` -- ``None`` means "exit the whole wizard".

    Mirrors ``_quick_launch_next_step``'s conditional branches exactly
    (VPC's predecessor depends on ``wants_owner`` -- OWNER_SELECT when one
    was assigned, KEY_PAIR when it wasn't; OWNER_SELECT's depends on whether
    OWNER_FILTER was ever shown) -- this is the exact mapping the "Create EC2
    from this AMI" spec calls for, question by question.
    """
    if current is _QuickLaunchStep.NAME:
        return None
    if current is _QuickLaunchStep.OWNER_CONFIRM:
        return _QuickLaunchStep.NAME
    if current is _QuickLaunchStep.OWNER_FILTER:
        return _QuickLaunchStep.OWNER_CONFIRM
    if current is _QuickLaunchStep.OWNER_SELECT:
        if state.owner.iam_users:
            return _QuickLaunchStep.OWNER_FILTER
        return _QuickLaunchStep.OWNER_CONFIRM
    if current is _QuickLaunchStep.KEY_PAIR:
        return _QuickLaunchStep.OWNER_CONFIRM
    if current is _QuickLaunchStep.VPC:
        if state.owner.wants_owner:
            return _QuickLaunchStep.OWNER_SELECT
        return _QuickLaunchStep.KEY_PAIR
    if current is _QuickLaunchStep.SECURITY_GROUPS:
        return _QuickLaunchStep.VPC
    if current is _QuickLaunchStep.STORAGE:
        return _QuickLaunchStep.SECURITY_GROUPS
    if current is _QuickLaunchStep.CONFIRM:
        return _QuickLaunchStep.STORAGE
    raise AssertionError(f"Unhandled step: {current}")


class _CreateInstanceStep(Enum):
    """Every question the minimal "from scratch" EC2 creation wizard can ask.

    Same state-machine discipline as ``_QuickLaunchStep`` -- see that
    class's own docstring -- with no VPC/Security Groups/Storage steps
    (those stay silent smart defaults here, per this flow's own spec) and
    the Owner sub-flow's 3 questions sitting between AMI and the final
    Confirm instead of between Name and Network.
    """

    NAME = "name"
    AMI = "ami"
    OWNER_CONFIRM = "owner_confirm"
    OWNER_FILTER = "owner_filter"
    OWNER_SELECT = "owner_select"
    KEY_PAIR = "key_pair"
    USER_DATA = "user_data"
    CONFIRM = "confirm"


@dataclass(slots=True)
class _CreateInstanceState:
    """What's been collected so far across the minimal "from scratch" wizard's questions.

    ``launch_state`` reuses ``_LaunchWizardState`` for Name/AMI (the same
    dataclass ``_wizard_step_name``/``_wizard_step_base_image`` already operate on);
    ``owner`` reuses the same ``_OwnerSubState``/``_owner_step_*`` trio AMI
    Quick Launch does, so the two flows share the ENTIRE Owner sub-flow
    verbatim, not just its shape. ``resolved_*`` fields are filled in by
    ``_create_instance_step_confirm`` (VPC/Subnet/Security Groups are silent
    smart defaults here -- there's no interactive step for them to belong
    to) and consumed by ``_create_instance_execute``. ``user_data``/
    ``user_data_summary`` are this flow's own step (see
    ``_create_instance_step_user_data``) -- AMI Quick Launch has no
    equivalent, so ``_QuickLaunchState`` carries neither.
    """

    launch_state: _LaunchWizardState = field(default_factory=_LaunchWizardState)
    owner: _OwnerSubState = field(default_factory=_OwnerSubState)
    key_pair: _KeyPairSubState = field(default_factory=_KeyPairSubState)
    user_data: str | None = None
    user_data_summary: str = _USERDATA_SUMMARY_NONE
    resolved_ami: Ami | None = None
    resolved_vpc_id: str | None = None
    resolved_subnet_id: str | None = None
    resolved_security_groups: list[SecurityGroup] | None = None


def _create_instance_next_step(
    current: _CreateInstanceStep, state: _CreateInstanceState
) -> _CreateInstanceStep:
    """What "forward" means from ``current`` -- mirrors ``_quick_launch_next_step``."""
    if current is _CreateInstanceStep.NAME:
        return _CreateInstanceStep.AMI
    if current is _CreateInstanceStep.AMI:
        return _CreateInstanceStep.OWNER_CONFIRM
    if current is _CreateInstanceStep.OWNER_CONFIRM:
        if not state.owner.wants_owner:
            return _CreateInstanceStep.KEY_PAIR
        if state.owner.iam_users:
            return _CreateInstanceStep.OWNER_FILTER
        return _CreateInstanceStep.OWNER_SELECT
    if current is _CreateInstanceStep.OWNER_FILTER:
        return _CreateInstanceStep.OWNER_SELECT
    if current is _CreateInstanceStep.OWNER_SELECT:
        return _CreateInstanceStep.USER_DATA
    if current is _CreateInstanceStep.KEY_PAIR:
        return _CreateInstanceStep.USER_DATA
    if current is _CreateInstanceStep.USER_DATA:
        return _CreateInstanceStep.CONFIRM
    raise AssertionError(f"CONFIRM has no 'next' step: {current}")


def _create_instance_prev_step(
    current: _CreateInstanceStep, state: _CreateInstanceState
) -> _CreateInstanceStep | None:
    """What "back" means from ``current`` -- ``None`` means "exit the whole wizard".

    Mirrors ``_quick_launch_prev_step`` exactly, with AMI (not Name) as the
    Owner sub-flow's entry predecessor and CONFIRM (not a Network step) as
    its exit successor -- the exact mapping this flow's own spec calls for.
    """
    if current is _CreateInstanceStep.NAME:
        return None
    if current is _CreateInstanceStep.AMI:
        return _CreateInstanceStep.NAME
    if current is _CreateInstanceStep.OWNER_CONFIRM:
        return _CreateInstanceStep.AMI
    if current is _CreateInstanceStep.OWNER_FILTER:
        return _CreateInstanceStep.OWNER_CONFIRM
    if current is _CreateInstanceStep.OWNER_SELECT:
        if state.owner.iam_users:
            return _CreateInstanceStep.OWNER_FILTER
        return _CreateInstanceStep.OWNER_CONFIRM
    if current is _CreateInstanceStep.KEY_PAIR:
        return _CreateInstanceStep.OWNER_CONFIRM
    if current is _CreateInstanceStep.USER_DATA:
        if state.owner.wants_owner:
            return _CreateInstanceStep.OWNER_SELECT
        return _CreateInstanceStep.KEY_PAIR
    if current is _CreateInstanceStep.CONFIRM:
        return _CreateInstanceStep.USER_DATA
    raise AssertionError(f"Unhandled step: {current}")


_INTERNAL_TAG_KEYS = frozenset({"CreatedAt"})


def _clean_tags(raw_tags: list[dict[str, str]]) -> dict[str, str]:
    """User-facing tags only -- excludes internal bookkeeping keys like ``CreatedAt``.

    ``CreatedAt`` is real AWS tag data (set once at launch by
    ``LaunchInstanceUseCase``), but it is this tool's own bookkeeping, not
    something the user configured -- an instance gets its own "Created At"
    row in the detail table instead, never mixed into the Tags section or
    the Add/Delete tag screen. Shared by instances and AMIs alike.
    """
    all_tags = tags_to_dict(raw_tags)
    return {key: value for key, value in all_tags.items() if key not in _INTERNAL_TAG_KEYS}


def _instance_tags(instance: Instance) -> dict[str, str]:
    return _clean_tags(instance.tags)


def _ami_tags(ami: Ami) -> dict[str, str]:
    return _clean_tags(ami.tags)


def _ami_display_name(ami: Ami) -> str:
    """The AMI's identity: its ``Name`` tag if set, else its native name, else its ID.

    AWS never lets an AMI's native ``Name`` attribute be changed after
    creation -- ``_edit_ami_name`` writes a ``Name`` TAG instead (the same
    "friendly name" convention every other AWS resource uses). Every screen
    that shows an AMI's name goes through this helper so "Edit Name" actually
    changes what's displayed as the AMI's identity, instead of the edit
    silently landing in the Tags block as a same-keyed secondary tag while
    the native name stays frozen on screen.
    """
    return tag_value(ami.tags, "Name") or ami.name or ami.image_id


def _launch_tags(owner: str, key_name: str | None) -> dict[str, str]:
    """The tags every launch writes, built identically by both creation wizards.

    ``Owner`` is only written when one was actually assigned (an unowned
    instance carries no Owner tag at all, same as before). ``KeyPair`` is
    ALWAYS written -- the auto-generated key's name, or ``"-"`` for an owned
    instance that deliberately has none -- so "no key" reads the same on the
    resource as it does in the Launch Summary, instead of being an absent tag
    the reader has to interpret.
    """
    tags: dict[str, str] = {}
    if owner:
        tags["Owner"] = owner
    tags[_KEY_PAIR_TAG] = key_name or _NO_KEY_PAIR
    return tags


def _tags_excluding_owner(tags: dict[str, str]) -> dict[str, str]:
    """The "Tags" block's own contents once "Owner"/"Name"/"KeyPair" have their own row.

    ``Owner``, ``Name`` and ``KeyPair`` stay real, editable user tags (unlike
    ``CreatedAt``) -- all three are still returned by
    ``_instance_tags``/``_ami_tags`` for the Add/Delete tag screen, just not
    repeated in this specific display block once each has its own row above it.
    """
    return {
        key: value
        for key, value in tags.items()
        if key not in ("Owner", "Name", _KEY_PAIR_TAG)
    }


_STATE_COLORS: dict[InstanceState, str] = {
    InstanceState.RUNNING: "green",
    InstanceState.STOPPED: "red",
    InstanceState.PENDING: "yellow",
}


def _colored_state(state: InstanceState) -> str:
    color = _STATE_COLORS.get(state)
    return f"[{color}]{state.value}[/]" if color else state.value


def _format_tags_block(tags: dict[str, str]) -> str:
    """Tags rendered as "Key: Value" lines -- never the raw Python dict repr."""
    if not tags:
        return "(no tags)"
    return "\n".join(f"{key}: {value}" for key, value in tags.items())


def _resolve_last_activity(ctx: AppContext, instance: Instance) -> datetime | None:
    """The "last activity" signal for the table/audit views: a seeded override, or launch time.

    AWS gives no API to ask "when was this instance last used" -- a seeded
    override (set by a test-data seeder, mirroring IAM's SEED_LAST_* fields)
    wins when present; otherwise ``launch_time`` is the best real signal this
    CLI has without a genuine CloudWatch integration.
    """
    seeded = read_instance_metadata_datetime(
        ctx.resource_repository, instance.instance_id, SEED_LAST_ACTIVITY
    )
    if seeded is not None:
        return seeded
    return instance.launch_time


def _days_idle(last_activity: datetime | None, *, now: datetime) -> int | None:
    """Days since ``last_activity``, or ``None`` if it's unknown."""
    if last_activity is None:
        return None
    return (now - last_activity).days


def _is_underutilized(instance: Instance, days_idle: int | None) -> bool:
    """RUNNING for more than ``_UNDERUTILIZED_MIN_DAYS`` days -- a candidate to stop.

    Reuses the same simulated "days idle" clock ``_resolve_last_activity``
    already provides (this audit is a documented local simulation, not real
    CloudWatch) as a stand-in for "days in the current state" -- building a
    genuine state-transition timeline is out of scope here.
    """
    return (
        instance.state is InstanceState.RUNNING
        and days_idle is not None
        and days_idle > _UNDERUTILIZED_MIN_DAYS
    )


def _is_zombie(instance: Instance, days_idle: int | None) -> bool:
    """STOPPED for more than ``_ZOMBIE_MIN_DAYS`` days -- a candidate to terminate."""
    return (
        instance.state is InstanceState.STOPPED
        and days_idle is not None
        and days_idle > _ZOMBIE_MIN_DAYS
    )


def _fmt_activity(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value is not None else "Never"


def _fmt_yes_no(value: bool) -> str:
    return "Yes" if value else "No"


_AMI_NAME_PREFIX = "AMI-"
_EC2_NAME_PREFIX = "ec2-"


def _ec2_name_suggestion_from_ami(ami: Ami) -> str:
    """The suggested EC2 name when cloning ``ami``: ``"ec2-<AMI's own name>"``.

    Strips an existing ``"AMI-"`` prefix first (``"AMI-Sales"`` -> ``"ec2-Sales"``,
    never ``"ec2-AMI-Sales"``) since that's the exact default ``_ami_create``
    already suggests when an AMI is made from an instance -- without this,
    round-tripping "create an AMI, then an EC2 from it" would double the
    prefix.
    """
    base = _ami_display_name(ami)
    if base.startswith(_AMI_NAME_PREFIX):
        base = base[len(_AMI_NAME_PREFIX) :]
    return f"{_EC2_NAME_PREFIX}{base}"


@dataclass(slots=True)
class Ec2Flow:
    """EC2's top-level TUI screen: list/filter instances, launch, terminate, AMIs, audit."""

    title: ClassVar[str] = "🖥️ EC2 Compute & Fleet Control"

    ctx: AppContext
    prompter: Prompter

    @aws_error_handler
    def menu(self: Self) -> NavAction:
        """Show EC2's menu once."""
        self._clear_and_banner()
        instances = build_ec2_use_cases(self.ctx).list_instances_global.execute(
            ListInstancesRequest()
        )
        self._render_instance_stats(instances)
        selected = self.prompter.select(
            "EC2 (Instances) -- What do you want to do?", self._choices()
        )
        if selected is None or selected == NAV_EXIT:
            return NavAction.EXIT
        if selected == NAV_BACK:
            return NavAction.BACK
        if selected == _EXPLORE_SEARCH:
            self._search_instances()
        elif selected == _AMI_MANAGEMENT:
            self._ami_menu()
        elif selected == _CREATE_INSTANCE:
            self._create_instance()
        elif selected == _DELETE_INSTANCE:
            self._delete_instance()
        return NavAction.STAY

    def _choices(self: Self) -> list[Choice | Separator]:
        return [
            Choice(title="🔍 Search / Filter Instances", value=_EXPLORE_SEARCH),
            Choice(title="💿 AMI Management", value=_AMI_MANAGEMENT),
            Choice(title="+ Launch Instance", value=_CREATE_INSTANCE),
            Choice(title="❌ Terminate Instance", value=_DELETE_INSTANCE),
            Separator(),
            Choice(title="↩️  Back", value=NAV_BACK),
        ]

    def _clear_and_banner(self: Self) -> None:
        """Terminal Cleanup: clear the screen and redraw the banner -- every step/screen change.

        A thin delegate to ``flows._shared.clear_and_banner`` (the actual,
        centralized implementation every flow shares) -- kept as a bound
        method purely so every existing call site in this module reads
        ``self._clear_and_banner()`` without a signature change.
        """
        clear_and_banner(self.ctx)

    # -- Search (shared by List, Terminate, and AMI Create-from-instance) --------------

    def _query_instances(
        self: Self, message: str = "Enter search query:", *, global_: bool = False
    ) -> list[Instance] | None:
        """Ask for a free-text query (blank = everyone); matches against the display name.

        Returns the matches (possibly empty, if the query hit nothing), or
        ``None`` if cancelled or there are no instances at all to search.
        ``global_`` scans every AWS region (the "Search / Filter Instances"
        root menu entry only) instead of just the active session region
        (Terminate and AMI Create-from-instance's pickers, which need an
        instance the active-region client can actually act on).
        """
        use_cases = build_ec2_use_cases(self.ctx)
        list_use_case = use_cases.list_instances_global if global_ else use_cases.list_instances
        instances = list_use_case.execute(ListInstancesRequest())
        if not instances:
            self.ctx.err_console.print("[yellow]No instances found.[/]")
            self.prompter.pause()
            return None

        self._clear_and_banner()
        query = prompt_text_or_cancel(self.prompter, self.ctx.err_console, message)
        if query is None:
            return None
        if not query:
            return instances
        needle = query.lower()
        return [i for i in instances if needle in i.display_name.lower()]

    def _filter_and_pick_instance(self: Self, message: str) -> Instance | None:
        """Search EVERY region, render the results table, then pick -- never an auto-pick.

        Global (like ``_search_instances``) so Terminate and AMI
        Create-from-instance can target an instance in ANY region, not just
        the active session region -- the active session region stays
        reserved for launching brand-new instances. Returns the picked
        ``Instance`` itself (region-stamped) rather than a bare ID, so the
        caller can route its action to that same region.
        """
        matches = self._query_instances(global_=True)
        if matches is None:
            return None
        if not matches:
            self.ctx.err_console.print("[yellow]No matches.[/]")
            self.prompter.pause()
            return None

        self._render_instance_table(matches, show_region=True)
        choices: list[Choice | Separator] = [
            Choice(
                title=f"{i.instance_id}  {i.display_name}  [{i.region}]", value=i.instance_id
            )
            for i in matches
        ]
        choices.append(Separator())
        choices.append(Choice(title="<- Cancel", value=NAV_BACK))
        selected = self.prompter.select(
            message, choices, use_search=len(matches) > _SEARCH_THRESHOLD
        )
        if selected is None or selected == NAV_BACK:
            return None
        return next(i for i in matches if i.instance_id == selected)

    # -- Stats line + search results table (State Filtering: terminated is hidden by the

    # ``ListInstancesUseCase`` default -- this screen never sees a terminated instance) --

    def _render_instance_stats(self: Self, instances: list[Instance]) -> None:
        """Print "  Instances: N Total  |  R Running  |  S Stopped" above the service menu."""
        total = len(instances)
        running = sum(1 for i in instances if i.state is InstanceState.RUNNING)
        self.ctx.err_console.print()  # spacing from the previous prompt's answer line
        self.ctx.err_console.print(
            f"  Instances: {total} Total  |  {running} Running  |  {total - running} Stopped"
        )

    def _render_instance_table(
        self: Self, instances: list[Instance], *, show_region: bool = False
    ) -> None:
        """Search & Manage results: Name, ID, [Region,] State, IP, SGs, Last Activity, etc.

        ``show_region`` is set for every one of this module's instance
        pickers -- "Search / Filter Instances", Terminate, and AMI
        Create-from-instance -- since all three now scan every region.
        """
        table = Table(title="Instances")
        table.add_column("Name", style="bold")
        table.add_column("ID")
        if show_region:
            table.add_column("Region")
        table.add_column("State")
        table.add_column("IP")
        table.add_column("SGs")
        table.add_column("Last Activity")
        table.add_column("Alarms")
        table.add_column("Auto-Stop")
        for instance in instances:
            ip = instance.public_ip_address or instance.private_ip_address or "(none)"
            sgs = ", ".join(instance.security_group_names) or "(none)"
            last_activity = _fmt_activity(_resolve_last_activity(self.ctx, instance))
            alarms = _fmt_yes_no(
                read_instance_metadata_bool(
                    self.ctx.resource_repository, instance.instance_id, ALARM_CONFIGURED
                )
            )
            auto_stop = str(
                read_instance_metadata_bool(
                    self.ctx.resource_repository, instance.instance_id, AUTO_STOP_ENABLED
                )
            )
            row = [instance.display_name, instance.instance_id]
            if show_region:
                row.append(instance.region or "-")
            row.extend([instance.state.value, ip, sgs, last_activity, alarms, auto_stop])
            table.add_row(*row)
        self.ctx.console.print(table)

    def _search_instances(self: Self) -> None:
        """"Search / Filter Instances": the one picker that scans every region.

        Unlike ``_filter_and_pick_instance`` (Terminate, AMI Create-from-instance),
        a match here can be outside the active session region -- the detail/action
        screen routes every call through THAT instance's own region (via
        ``instance.region``), so a foreign-region pick works exactly like a
        local one, without ever touching the active session region (which
        stays the launch/creation target -- see ``_choices``/``_create_instance``).
        """
        matches = self._query_instances(global_=True)
        if matches is None:
            return
        if not matches:
            self.ctx.err_console.print("[yellow]No matches.[/]")
            self.prompter.pause()
            return

        self._render_instance_table(matches, show_region=True)
        choices: list[Choice | Separator] = [
            Choice(
                title=f"{i.instance_id}  {i.display_name}  [{i.region}]", value=i.instance_id
            )
            for i in matches
        ]
        choices.append(Separator())
        choices.append(Choice(title="<- Cancel", value=NAV_BACK))
        selected = self.prompter.select(
            "Which instance do you want to view?",
            choices,
            use_search=len(matches) > _SEARCH_THRESHOLD,
        )
        if selected is None or selected == NAV_BACK:
            return

        instance = next(i for i in matches if i.instance_id == selected)
        self._instance_detail_loop(instance.instance_id, region=instance.region)

    @staticmethod
    def _detail_menu_choices(instance: Instance) -> list[Choice | Separator]:
        """The flat, state-adaptive action menu for one instance's detail screen.

        Mutually exclusive by construction: ``can_start`` is only ``True``
        when STOPPED, ``can_stop`` only when RUNNING -- never both, and
        neither in a transitional state (pending/stopping/shutting-down).
        """
        choices: list[Choice | Separator] = [
            Choice(title="Name (Modificar nombre)", value=_DETAIL_EDIT_NAME),
            Choice(title="Tags (Gestionar metadatos)", value=_DETAIL_MANAGE_TAGS),
            Choice(title="Groups (Modificar security groups)", value=_DETAIL_EDIT_GROUPS),
            Choice(title="VPC (Ver red y subred)", value=_DETAIL_VIEW_VPC),
        ]
        if instance.state.can_stop:
            choices.append(Choice(title="Stop (Apagar instancia)", value=_DETAIL_STOP))
        elif instance.state.can_start:
            choices.append(Choice(title="Start (Encender instancia)", value=_DETAIL_START))
        if instance.state.can_reboot:
            choices.append(Choice(title="Reboot (Reiniciar instancia)", value=_DETAIL_REBOOT))
        choices.append(Separator())
        choices.append(Choice(title="↩️  Back", value=_DETAIL_BACK))
        return choices

    def _instance_detail_loop(self: Self, instance_id: str, *, region: str | None = None) -> None:
        """Detail view + one flat, state-adaptive action menu -- no nested submenus.

        Clears the screen and redraws the banner on every entry into this
        loop, so no trace of the prior search screen lingers. Every action
        (Name/Tags/Groups/VPC/Start/Stop/Reboot) returns here, and
        ``_render_instance_detail`` re-fetches the instance fresh on each
        pass -- so a state change (e.g. 'stopped' -> 'running') is always
        visible on the very next redraw, with no separate refresh step.

        ``region`` is the instance's own region (``None`` for the active
        session region) -- carried in from ``_search_instances`` for a
        global-scan pick, so every action below routes to the SAME region's
        client the instance actually lives in, never the active session
        region (which stays reserved for launching brand-new instances).
        """
        while True:
            instance = self._render_instance_detail(instance_id, region=region)
            selected = self.prompter.select(
                f"Instance '{instance.display_name}' ({instance.state.value}) -- "
                "what do you want to do?",
                self._detail_menu_choices(instance),
            )
            if selected is None or selected == _DETAIL_BACK:
                return
            if selected == _DETAIL_EDIT_NAME:
                self._edit_instance_name(instance_id, region=region)
            elif selected == _DETAIL_MANAGE_TAGS:
                self._edit_tags(
                    instance_id,
                    fetch_tags=lambda: _instance_tags(
                        build_ec2_use_cases(self.ctx).get_instance.execute(
                            instance_id, region=region
                        )
                    ),
                    region=region,
                )
            elif selected == _DETAIL_EDIT_GROUPS:
                self._edit_instance_security_groups(instance_id, instance)
            elif selected == _DETAIL_VIEW_VPC:
                self._view_instance_network(instance)
            elif selected == _DETAIL_START:
                wiring = build_ec2_use_cases(self.ctx)
                self._run_instance_action(
                    confirm_message=None,
                    running_label="Starting instance",
                    done_label="Start requested.",
                    action=partial(
                        wiring.start_instance.execute, instance, wait=False, timeout_s=300
                    ),
                )
            elif selected == _DETAIL_STOP:
                wiring = build_ec2_use_cases(self.ctx)
                self._run_instance_action(
                    confirm_message="¿Confirmas apagar la instancia?",
                    running_label="Stopping instance",
                    done_label="Stop requested.",
                    action=partial(
                        wiring.stop_instance.execute,
                        instance,
                        force=False,
                        wait=False,
                        timeout_s=300,
                    ),
                )
            elif selected == _DETAIL_REBOOT:
                wiring = build_ec2_use_cases(self.ctx)
                self._run_instance_action(
                    confirm_message="¿Confirmas reiniciar la instancia?",
                    running_label="Rebooting instance",
                    done_label="Reboot requested.",
                    action=partial(wiring.reboot_instance.execute, instance),
                )

    def _run_instance_action(
        self: Self,
        *,
        confirm_message: str | None,
        running_label: str,
        done_label: str,
        action: Callable[[], object],
    ) -> None:
        """Optionally confirm (Sí/No/<- Back), then run ``action`` under a spinner.

        ``confirm_message`` is ``None`` for Start (never confirmed, same as
        before); Stop/Reboot pass one, and "No" and "<- Back" both simply
        skip the action -- there is no nested screen here for them to mean
        something different.
        """
        if confirm_message is not None:
            choice = self.prompter.select(
                confirm_message,
                [
                    Choice(title="Sí, ejecutar", value=_RUN_YES),
                    Choice(title="No", value=_RUN_NO),
                    Separator(),
                    Choice(title="↩️  Back", value=_DETAIL_BACK),
                ],
                default=_RUN_NO,
            )
            if choice is None or choice in (_RUN_NO, _DETAIL_BACK):
                return
        run_with_spinner(
            self.ctx.err_console, f"[bold green]{running_label}...[/bold green]", action
        )
        announce_result(self.ctx, self.prompter, f"[green]{done_label}[/]")

    def _edit_instance_name(self: Self, instance_id: str, *, region: str | None = None) -> None:
        """Rename via the ``Name`` tag. An empty ENTER means "Back", never a blank name."""
        new_name = prompt_text_or_cancel(self.prompter, self.ctx.err_console, "New instance name")
        if not new_name:
            return
        build_ec2_use_cases(self.ctx).tag_resource.execute(
            instance_id, {"Name": new_name}, region=region
        )
        announce_result(self.ctx, self.prompter, f"[green]Name updated to '{new_name}'.[/]")

    def _view_instance_network(self: Self, instance: Instance) -> None:
        """Read-only: the instance's VPC/Subnet, resolved via the VPC gateway's own reads."""
        self._clear_and_banner()
        vpc_use_cases = build_vpc_use_cases(self.ctx)
        region = instance.region
        table = Table(title=f"Network for {instance.instance_id}", show_header=False)
        table.add_column("Property", style="bold cyan")
        table.add_column("Value")

        if instance.vpc_id:
            vpcs = vpc_use_cases.gateway.describe_vpcs([instance.vpc_id], None, region=region)
            vpc_value = f"{vpcs[0].vpc_id} ({vpcs[0].cidr_block})" if vpcs else instance.vpc_id
        else:
            vpc_value = "(none)"
        table.add_row("VPC", vpc_value)

        if instance.subnet_id:
            subnets = vpc_use_cases.gateway.describe_subnets(
                [instance.subnet_id], None, None, region=region
            )
            if subnets:
                subnet = subnets[0]
                public = "sí" if subnet.is_public else ("no" if subnet.is_public is False else "?")
                subnet_value = (
                    f"{subnet.subnet_id} ({subnet.cidr_block}, AZ {subnet.availability_zone}, "
                    f"pública: {public})"
                )
            else:
                subnet_value = instance.subnet_id
        else:
            subnet_value = "(none)"
        table.add_row("Subnet", subnet_value)

        table.add_row(
            "Security Groups",
            ", ".join(
                f"{name} ({gid})"
                for name, gid in zip(
                    instance.security_group_names, instance.security_group_ids, strict=False
                )
            )
            or "(none)",
        )
        self.ctx.console.print(table)
        self.prompter.pause()

    def _edit_instance_security_groups(self: Self, instance_id: str, instance: Instance) -> None:
        """Groups: no manual typing -- a real ``describe_security_groups`` checkbox.

        Applies via ``modify_instance_attribute`` (``set_instance_security_groups``
        on the gateway) -- an instance-attribute change, never a network mutation;
        see that method's docstring and ``test_vpc_readonly.py`` for the boundary.
        """
        if not instance.vpc_id:
            self.ctx.err_console.print("[yellow]This instance has no VPC to pick groups from.[/]")
            self.prompter.pause()
            return
        region = instance.region
        available = build_vpc_use_cases(self.ctx).list_security_groups.execute(
            instance.vpc_id, region=region
        )
        if not available:
            self.ctx.err_console.print(f"[yellow]No security groups found in {instance.vpc_id}.[/]")
            self.prompter.pause()
            return
        choices = [
            Choice(
                title=f"{sg.group_name} ({sg.group_id})",
                value=sg.group_id,
                checked=sg.group_id in instance.security_group_ids,
            )
            for sg in available
        ]
        selected_ids = self.prompter.checkbox(
            f"Security Groups para la instancia en VPC {instance.vpc_id} "
            "(SPACE marca, ENTER confirma):",
            choices,
        )
        if selected_ids is None:
            return
        if not selected_ids:
            self.ctx.err_console.print("[red]Debes dejar al menos un Security Group asignado.[/]")
            self.prompter.pause()
            return
        if set(selected_ids) == set(instance.security_group_ids):
            return
        build_ec2_use_cases(self.ctx).set_instance_security_groups.execute(
            SetInstanceSecurityGroupsRequest(
                instance_id=instance_id, group_ids=tuple(selected_ids)
            ),
            region=instance.region,
        )
        self.ctx.console.print(
            f"[green]Security Groups actualizados: {len(selected_ids)} asignado(s).[/]"
        )
        self.prompter.pause()

    def _render_instance_detail(
        self: Self, instance_id: str, *, region: str | None = None
    ) -> Instance:
        """Clear the screen and print the instance's vertical detail table.

        Mandatory ``console.clear()`` equivalent before every paint of this
        screen -- called here, not by each caller, so the guarantee holds no
        matter which flow (detail loop, Change State, Terminate) reaches it.
        ``region`` (the instance's own region, ``None`` for the active
        session region) routes the refetch to the right regional client.
        """
        self._clear_and_banner()
        self.ctx.err_console.print()  # spacing between the header box and this subtitle
        instance = build_ec2_use_cases(self.ctx).get_instance.execute(instance_id, region=region)

        table = Table(title=f"Instance: {instance_id}", show_header=False)
        table.add_column("Property", style="bold cyan")
        table.add_column("Value")
        table.add_row("Name", instance.display_name)
        table.add_row("Instance ID", instance.instance_id)
        table.add_row("Region", instance.region or self.ctx.settings.region)
        table.add_row("State", _colored_state(instance.state))
        table.add_row("Type", instance.instance_type)
        table.add_row(
            "Network", f"{instance.vpc_id or '(none)'} / {instance.subnet_id or '(none)'}"
        )
        table.add_row(
            "IPs",
            f"Public: {instance.public_ip_address or '(none)'}  |  "
            f"Private: {instance.private_ip_address or '(none)'}",
        )
        table.add_row("Key Pair", instance.key_name or "(none)")
        table.add_row("Created At", tag_value(instance.tags, "CreatedAt") or "(unknown)")
        table.add_row("Owner", tag_value(instance.tags, "Owner") or "(sin asignar)")
        table.add_row("Tags", _format_tags_block(_tags_excluding_owner(_instance_tags(instance))))
        self.ctx.console.print(table)
        return instance

    # -- Edit Tags -----------------------------------------------------------

    def _edit_tags(
        self: Self,
        resource_id: str,
        *,
        fetch_tags: Callable[[], dict[str, str]],
        region: str | None = None,
    ) -> None:
        """Add/Delete tag screen, shared by instances and AMIs alike.

        ``fetch_tags`` re-reads the resource's current (cleaned) tags on
        every pass -- an instance passes a lambda around
        ``get_instance.execute``, an AMI one around ``get_ami.execute``.
        ``resource_id`` is just an EC2 resource ID: ``CreateTags``/``DeleteTags``
        (via ``set_instance_tags``/``delete_instance_tags``) work the same for
        an instance or an AMI ID -- the underlying AWS API is resource-agnostic.
        ``region`` is the instance's own region -- always ``None`` for an AMI
        (AMIs aren't scanned globally, so their tag edits stay untouched).
        """
        while True:
            self._clear_and_banner()
            tags = fetch_tags()
            render(tags or {"(no tags)": ""}, ctx=self.ctx, title=f"tags for {resource_id}")
            delete_disabled = None if tags else "no tags"
            selected = self.prompter.select(
                "Tags -- what do you want to do?",
                [
                    Choice(title="Add / update tag", value=_TAG_ADD),
                    Choice(title="Delete tag", value=_TAG_DELETE, disabled=delete_disabled),
                    Separator(),
                    Choice(title="↩️  Back", value=NAV_BACK),
                ],
            )
            if selected is None or selected == NAV_BACK:
                return
            if selected == _TAG_ADD:
                self._add_tag(resource_id, region=region)
            elif selected == _TAG_DELETE:
                self._delete_tag(resource_id, tags, region=region)

    def _confirm_yes_no_back(self: Self, message: str) -> bool:
        """Standard TUI confirm (Sí/No/<- Back) -- replaces a raw ``(y/N)`` text prompt.

        Only "Sí" returns ``True``; "No", "<- Back", and Ctrl+C/Esc all mean
        "don't proceed" -- there is no separate screen here for "Back" to
        return to that's different from just not confirming.
        """
        choice = self.prompter.select(
            message,
            [
                Choice(title="Sí", value=_CONFIRM_YES),
                Choice(title="No", value=_CONFIRM_NO),
                Separator(),
                Choice(title="↩️  Back", value=_CONFIRM_BACK),
            ],
            default=_CONFIRM_NO,
        )
        return choice == _CONFIRM_YES

    def _add_tag(self: Self, resource_id: str, *, region: str | None = None) -> None:
        """Add/update a tag -- same guided key/value presets (incl. Owner) as the launch wizard.

        Standardizes editing an existing resource's tags with creating them:
        both now go through ``_wizard_pick_tag_key``/``_wizard_pick_tag_value``
        (Name/Environment/Owner/Project/CostCenter/Custom), instead of a raw
        free-text prompt that let "Owner" drift into any spelling.
        """
        self._clear_and_banner()
        key = self._wizard_pick_tag_key()
        if key is None:
            return
        value = self._wizard_pick_tag_value(key)
        if not value:
            return
        if not self._confirm_yes_no_back(f"Apply tag '{key}={value}'?"):
            return
        build_ec2_use_cases(self.ctx).tag_resource.execute(
            resource_id, {key: value}, region=region
        )
        announce_result(self.ctx, self.prompter, f"[green]Tag '{key}={value}' applied.[/]")

    def _delete_tag(
        self: Self, resource_id: str, tags: dict[str, str], *, region: str | None = None
    ) -> None:
        choices: list[Choice | Separator] = [
            Choice(title=f"{k} = {v}", value=k) for k, v in tags.items()
        ]
        choices.append(Separator())
        choices.append(Choice(title="<- Cancel", value=NAV_BACK))
        key = self.prompter.select("Which tag do you want to delete?", choices)
        if key is None or key == NAV_BACK:
            return
        if not self._confirm_yes_no_back(f"Delete the tag '{key}'?"):
            return
        build_ec2_use_cases(self.ctx).untag_resource.execute(resource_id, (key,), region=region)
        announce_result(self.ctx, self.prompter, f"[green]Tag '{key}' deleted.[/]")

    # -- Provision Wizard: a step-index state machine, driven by _create_instance --------
    #
    # Every _wizard_step_* method has the same shape: show one step's prompt(s), write
    # the answer(s) onto `state`, and return a _WizardNav telling the driving loop what
    # to do next. NEXT/BACK move `step_index` forward/backward by exactly one; CANCEL
    # aborts the whole wizard. Because `state` is one mutable object threaded through
    # every call, going BACK and then forward again always re-shows a step with its
    # previous answer already selected -- nothing already entered is ever lost.

    def _wizard_step_name(self: Self, wiring: Ec2UseCases, state: _LaunchWizardState) -> _WizardNav:
        """Step 1: Instance Name -- the ONLY tag this wizard ever asks for.

        No "<- Atrás" here: there is no step before this one, so cancelling
        (Ctrl+C, Esc, or typing "cancel"/"back") aborts the wizard straight
        back to the EC2 menu, same as it always has.
        """
        del wiring
        name = prompt_text_or_cancel(
            self.prompter,
            self.ctx.err_console,
            "Nombre de la instancia (Instance Name)",
            default=state.name or "",
        )
        if not name:
            return _WizardNav.CANCEL
        state.name = name
        return _WizardNav.NEXT

    def _wizard_step_base_image(
        self: Self, wiring: Ec2UseCases, state: _LaunchWizardState
    ) -> _WizardNav:
        """Step 2: base OS image -- curated aliases, or a manual image ID as the escape hatch.

        Standard-flow-only (see ``_create_instance_step_ami``): the user is
        deploying a plain OS image, not browsing the AMI module, so nothing
        printed here -- not the select title, not the manual-entry choice,
        not the free-text prompt -- ever says "AMI". ``_AMI_CHOICES`` and
        ``_AMI_MANUAL`` stay their internal names (never rendered), same as
        ``state.ami_choice``/``state.ami_ref``.
        """
        del wiring
        curated_values = {alias for _, alias in _AMI_CHOICES}
        while True:
            choices: list[Choice | Separator] = [
                Choice(title=f"{label} ({alias})", value=alias) for label, alias in _AMI_CHOICES
            ]
            choices.append(Choice(title="Ingresar ID de imagen manualmente", value=_AMI_MANUAL))
            choices.append(Separator())
            choices.append(Choice(title=_WIZARD_BACK_LABEL, value=NAV_BACK))
            default = state.ami_choice if state.ami_choice in curated_values else None
            ami_choice = self.prompter.select(
                "Imagen base (Sistema Operativo):", choices, default=default
            )
            if ami_choice is None:
                return _WizardNav.CANCEL
            if ami_choice == NAV_BACK:
                return _WizardNav.BACK
            if ami_choice != _AMI_MANUAL:
                state.ami_choice = ami_choice
                state.ami_ref = ami_choice
                return _WizardNav.NEXT
            ami_ref = prompt_text_or_cancel(
                self.prompter, self.ctx.err_console, "ID de imagen", default=state.ami_ref or ""
            )
            if not ami_ref:
                continue  # back to the image select above, not the whole wizard
            state.ami_choice = _AMI_MANUAL
            state.ami_ref = ami_ref
            return _WizardNav.NEXT

    def _wizard_step_storage(
        self: Self, wiring: Ec2UseCases, state: _LaunchWizardState
    ) -> _WizardNav:
        """Step 7: EBS Storage -- default (8 GB, gp3) vs a custom size, both via select."""
        del wiring
        while True:
            choices: list[Choice | Separator] = [
                Choice(
                    title=f"Default ({_DEFAULT_VOLUME_SIZE_GB} GB, gp3)", value=_STORAGE_DEFAULT
                ),
                Choice(title="Custom size", value=_STORAGE_CUSTOM),
                Separator(),
                Choice(title=_WIZARD_BACK_LABEL, value=NAV_BACK),
            ]
            choice = self.prompter.select(
                "EBS storage:", choices, default=state.storage_choice or _STORAGE_DEFAULT
            )
            if choice is None:
                return _WizardNav.CANCEL
            if choice == NAV_BACK:
                return _WizardNav.BACK
            state.storage_choice = choice
            if choice == _STORAGE_DEFAULT:
                state.volume_size_gb = _DEFAULT_VOLUME_SIZE_GB
                self.ctx.err_console.print(
                    f"[green]Selected storage: {_format_gib(state.volume_size_gb)}[/]"
                )
                return _WizardNav.NEXT
            # Custom size: a technical preset menu, or manual entry as the escape hatch.
            # "<- Back" here falls back to the Default/Custom select above -- never the
            # whole wizard.
            gib = self._wizard_pick_custom_storage_gib()
            if gib is None:
                continue
            state.volume_size_gb = gib
            self.ctx.err_console.print(f"[green]Selected storage: {_format_gib(gib)}[/]")
            return _WizardNav.NEXT

    def _wizard_pick_custom_storage_gib(self: Self) -> int | None:
        """The "Custom size" preset menu: a curated GiB pick, manual entry, or back.

        Returns the chosen size in GiB, or ``None`` if the user backed all the
        way out ("<- Back" here, or a cancel/Ctrl+C at the preset select
        itself) -- the caller re-shows the Default/Custom select in that
        case. A cancel from the manual-entry prompt falls back to this
        preset menu instead, one level at a time.
        """
        preset_choices: list[Choice | Separator] = [
            Choice(title=label, value=str(gib)) for gib, label in _STORAGE_PRESETS
        ]
        preset_choices.append(
            Choice(title="Manual entry (Type custom GiB...)", value=_STORAGE_MANUAL)
        )
        preset_choices.append(Separator())
        preset_choices.append(Choice(title=_WIZARD_BACK_LABEL, value=NAV_BACK))
        while True:
            choice = self.prompter.select("Custom storage size:", preset_choices)
            if choice is None or choice == NAV_BACK:
                return None
            if choice != _STORAGE_MANUAL:
                return int(choice)
            # Manual entry: keep asking until a valid number, or a cancel falls back
            # to the preset menu above (not the whole Custom-size flow).
            while True:
                raw = prompt_text_or_cancel(
                    self.prompter, self.ctx.err_console, "Custom storage (GiB)"
                )
                if raw is None:
                    break
                if raw.isdigit() and int(raw) > 0:
                    return int(raw)
                self.ctx.err_console.print("[red]Enter a positive whole number of GiB.[/]")

    def _wizard_pick_tag_key(self: Self) -> str | None:
        """The guided Tag Key select: a curated category, a custom one, or back.

        Returns ``None`` if the user backed out ("<- Back" here, or a cancel
        at the Custom Key prompt falls back to re-showing this same select).
        """
        choices: list[Choice | Separator] = [
            Choice(title=label, value=value) for label, value in _TAG_KEY_PRESETS
        ]
        choices.append(
            Choice(title="Custom Key (Escribir categoría personalizada)...", value=_TAG_KEY_CUSTOM)
        )
        choices.append(Separator())
        choices.append(Choice(title=_WIZARD_BACK_LABEL, value=_TAGS_BACK))
        while True:
            choice = self.prompter.select(
                "Tags / Metadatos de organización (Categoría -> Valor):", choices
            )
            if choice is None or choice == _TAGS_BACK:
                return None
            if choice != _TAG_KEY_CUSTOM:
                return choice
            key = prompt_text_or_cancel(self.prompter, self.ctx.err_console, "Custom Key")
            if key is None:
                continue  # back to this same select
            if not key:
                self.ctx.err_console.print("[red]La clave del tag no puede estar vacía.[/]")
                continue
            return key

    def _wizard_pick_tag_value(self: Self, key: str) -> str | None:
        """The guided Tag Value select/prompt for ``key``, or ``None`` if backed out."""
        presets = _TAG_VALUE_PRESETS.get(key)
        if presets is not None:
            choices: list[Choice | Separator] = [Choice(title=p, value=p) for p in presets]
            choices.append(Choice(title="Custom value...", value=_TAG_VALUE_CUSTOM))
            choices.append(Separator())
            choices.append(Choice(title=_WIZARD_BACK_LABEL, value=_TAGS_BACK))
            choice = self.prompter.select(f"Tag Value for '{key}':", choices)
            if choice is None or choice == _TAGS_BACK:
                return None
            if choice != _TAG_VALUE_CUSTOM:
                return choice
        default = _TAG_NAME_DEFAULT_VALUE if key == "Name" else ""
        return prompt_text_or_cancel(
            self.prompter, self.ctx.err_console, f"Value for '{key}'", default=default
        )

    def _resolve_ami(self: Self, wiring: Ec2UseCases, ami_ref: str) -> Ami | None:
        """Resolve ``ami_ref`` to an ``Ami``, with a LocalStack-only fallback.

        LocalStack/moto expose a reduced, fictional AMI catalog (see
        ``AmiResolver``'s own docstring) -- a curated alias like 'debian-12'
        can legitimately have no match there even though it's a perfectly
        valid alias against real AWS. When that happens for a CURATED alias
        (never for a manually-typed AMI ID, where silently substituting a
        different image would be actively wrong) AND this session targets a
        local endpoint, this falls back to the newest AMI actually available
        in the account rather than dead-ending the wizard. Returns ``None``
        on an unrecoverable failure (real AWS, a bad manual ID, or no AMI at
        all in this account) -- the error is already printed by then.
        """
        try:
            return wiring.ami_resolver.resolve(ami_ref)
        except ResourceNotFoundError as exc:
            is_curated_alias = any(alias == ami_ref for _, alias in _AMI_CHOICES)
            if not (is_curated_alias and self.ctx.settings.is_local):
                self.ctx.err_console.print(f"[red]{exc}[/]")
                if exc.hint:
                    self.ctx.err_console.print(f"[yellow]{exc.hint}[/]")
                self.prompter.pause()
                return None
            fallback = wiring.list_amis.execute()
            if not fallback:
                self.ctx.err_console.print(
                    f"[red]El alias '{ami_ref}' no resolvió a ninguna imagen, y este entorno "
                    "no tiene ninguna imagen disponible como respaldo.[/]"
                )
                self.prompter.pause()
                return None
            self.ctx.err_console.print(
                f"[yellow]El alias '{ami_ref}' no está en el catálogo reducido de este "
                "entorno local -- usando la imagen más reciente disponible como respaldo.[/]"
            )
            return max(fallback, key=lambda ami: ami.creation_date)

    # -- The shared Owner sub-flow: OWNER_CONFIRM -> [OWNER_FILTER ->] OWNER_SELECT --------
    #
    # Reused VERBATIM by both EC2 creation flows (the minimal "from scratch" wizard and
    # AMI Quick Launch) -- each embeds one ``_OwnerSubState`` and calls these same three
    # methods from its own driving loop, so the Question -> Filter -> Selection -> Return
    # logic lives in exactly one place. Each returns a _WizardNav exactly like the
    # Provision Wizard's own _wizard_step_* methods: NEXT advances, BACK retreats to
    # whatever the CALLING flow's own next/prev-step function decides is this sub-flow's
    # predecessor (never hardcoded here -- that's precisely what makes it reusable across
    # two flows with different surrounding steps), CANCEL is never returned by these three
    # (there's no dead-end here an Owner decision can hit). None of these ever call each
    # other directly -- the owning flow's driving loop is the only thing that sequences
    # them.

    def _owner_step_confirm(
        self: Self, owner: _OwnerSubState, key_pair: _KeyPairSubState
    ) -> _WizardNav:
        """"¿Desea asignar un Propietario?" -- Sí/No/Back.

        "No" resolves ``owner.owner`` to ``""`` immediately (an explicit "no
        Owner", never written as a tag) and skips both IAM sub-steps
        entirely. "Sí" fetches the IAM user list once, here, and caches it on
        ``owner.iam_users`` -- both the owning flow's next-step function (to
        decide whether OWNER_FILTER is even shown) and ``_owner_step_select``
        reuse that same cached list, never re-fetching it. Also resets
        ``key_pair`` to a blank state: a user can reach "Sí" AFTER already
        having visited KEY_PAIR once (declined, saw the proposed name, then
        backed out and changed their mind) -- without this reset, that
        earlier proposed ``key_name`` would silently survive, both showing a
        stale Key Pair in the summary and making ``_ensure_key_pair`` create
        one nobody asked for, on an instance that now HAS an Owner.
        """
        choice = self.prompter.select(
            "¿Desea asignar un Propietario (IAM Owner) a esta instancia?",
            [
                Choice(title="Sí, asignar Propietario", value=_OWNER_ASSIGN_YES),
                Choice(title="No, continuar sin Owner", value=_OWNER_ASSIGN_NO),
                Separator(),
                Choice(title="↩️  Back", value=NAV_BACK),
            ],
            default=_OWNER_ASSIGN_NO if owner.wants_owner is False else _OWNER_ASSIGN_YES,
        )
        if choice is None or choice == NAV_BACK:
            return _WizardNav.BACK
        owner.wants_owner = choice == _OWNER_ASSIGN_YES
        if not owner.wants_owner:
            owner.owner = ""
            return _WizardNav.NEXT
        key_pair.key_name = None
        key_pair.saved_path = None
        key_pair.fingerprint = None
        key_pair.created = False
        owner.iam_users = build_iam_use_cases(self.ctx).list_users.execute(None)
        if not owner.iam_users:
            self.ctx.err_console.print(
                "[yellow]No hay usuarios IAM en esta cuenta -- ingresa el Owner manualmente.[/]"
            )
        return _WizardNav.NEXT

    def _owner_step_filter(self: Self, owner: _OwnerSubState) -> _WizardNav:
        """"Filtrar usuarios IAM" -- blank ENTER means "show everyone", not Back.

        Only ever reached when ``owner.iam_users`` is non-empty (see the
        owning flow's own next-step function) -- an empty account skips
        straight from OWNER_CONFIRM to OWNER_SELECT, which has its own
        free-text fallback. Unlike every other free-text prompt in this
        module, this one does NOT go through ``prompt_text_or_cancel``:
        Ctrl+C/Esc is the only way to back out of it.
        """
        query = self.prompter.text(
            "Filtrar usuarios IAM (Presiona Enter en blanco para ver todos):",
            default=owner.owner_filter_query,
        )
        if query is None:
            return _WizardNav.BACK
        owner.owner_filter_query = query
        return _WizardNav.NEXT

    def _owner_step_select(
        self: Self, owner: _OwnerSubState, *, default_owner: str | None
    ) -> _WizardNav:
        """"Seleccionar usuario IAM (Owner)" -- the filtered picker, or a free-text fallback.

        The free-text fallback (zero IAM users in this account) is itself
        the whole of this step in that case -- OWNER_FILTER was never shown,
        so there's no filter query to apply. "<- Back" on the results select
        loops back to re-show THIS SAME select (not a step back) when the
        chosen entry was "Otro (texto libre)" and its own free-text prompt
        was cancelled -- a sub-choice within this one step, same convention
        ``_wizard_step_base_image``'s manual-entry retry uses.
        """
        if not owner.iam_users:
            free_text = prompt_text_or_cancel(
                self.prompter,
                self.ctx.err_console,
                "Owner (texto libre)",
                default=owner.owner or default_owner or "",
            )
            if not free_text:
                return _WizardNav.BACK
            owner.owner = free_text
            return _WizardNav.NEXT

        needle = owner.owner_filter_query.strip().lower()
        matches = sorted(
            u.user_name for u in owner.iam_users if not needle or needle in u.user_name.lower()
        )
        if not matches:
            self.ctx.err_console.print(
                f"[yellow]Sin coincidencias para '{owner.owner_filter_query}'.[/]"
            )
            self.prompter.pause()
            return _WizardNav.BACK  # back to the filter prompt to try a different query

        choices: list[Choice | Separator] = [Choice(title=name, value=name) for name in matches]
        choices.append(Separator())
        choices.append(Choice(title="Otro (texto libre)", value=_OWNER_CUSTOM))
        choices.append(Choice(title="↩️  Back", value=NAV_BACK))
        default_selection = owner.owner if owner.owner in matches else default_owner
        while True:
            selected = self.prompter.select(
                "Owner (usuario IAM):",
                choices,
                default=default_selection if default_selection in matches else None,
                use_search=len(matches) > _SEARCH_THRESHOLD,
            )
            if selected is None or selected == NAV_BACK:
                return _WizardNav.BACK
            if selected != _OWNER_CUSTOM:
                owner.owner = selected
                return _WizardNav.NEXT
            custom = prompt_text_or_cancel(
                self.prompter,
                self.ctx.err_console,
                "Owner (texto libre)",
                default=owner.owner or "",
            )
            if not custom:
                continue  # re-show this same picker, not a step back
            owner.owner = custom
            return _WizardNav.NEXT

    # -- Key Pair sub-flow (shared by BOTH creation wizards) -----------------------
    #
    # Reached only when the user declined an Owner. The two halves are deliberately
    # split across the wizard's two phases: the STEP decides the name (so the summary
    # and "<- Back" have something stable to show and nothing has been created yet),
    # and ``_ensure_key_pair`` does the single AWS call, at execute time, after the
    # final confirmation.

    def _key_pair_step_auto(
        self: Self, key_pair: _KeyPairSubState, *, instance_name: str
    ) -> _WizardNav:
        """"Sin Owner -> se generará una Key Pair automática" -- Continuar/Back.

        No list of existing keys to scroll: an instance with no Owner gets a
        key named after itself, resolved here to something that collides with
        neither an existing AWS key pair nor an existing ``.pem`` on disk, and
        shown before anything is created so the user can still back out.

        Recomputed on every entry (rather than reused from ``key_pair.key_name``)
        because the instance name itself can change: stepping back to Name,
        renaming, and coming forward again must not keep proposing a key named
        after the old name.
        """
        destination_dir = _key_pair_destination_dir()
        try:
            existing = build_ec2_use_cases(self.ctx).list_key_pairs.execute()
            taken = {info.key_name for info in existing}
        except AwsAdminCliError as exc:
            # Listing is a convenience for picking a free name, not a precondition:
            # if it fails, fall back to the plain name and let the create call be
            # the thing that reports a real collision.
            self.ctx.err_console.print(f"[yellow]No se pudieron listar las key pairs: {exc}[/]")
            taken = set()
        key_pair.key_name = _unique_key_pair_name(
            _key_pair_base_name(instance_name), taken, destination_dir
        )

        self.ctx.console.print(
            "[yellow]Esta instancia quedará SIN Propietario (huérfana).[/] "
            "Para conservar acceso SSH se generará una Key Pair automáticamente."
        )
        self.ctx.console.print(f"  Key Pair: [bold]{key_pair.key_name}[/]")
        pem_path = destination_dir / f"{key_pair.key_name}.pem"
        self.ctx.console.print(f"  Se guardará en: [bold]{pem_path}[/]")

        choice = self.prompter.select(
            "¿Continuar con esta Key Pair automática?",
            [
                Choice(title="Continuar", value=_KEY_PAIR_CONTINUE),
                Separator(),
                Choice(title="↩️  Back", value=NAV_BACK),
            ],
            default=_KEY_PAIR_CONTINUE,
        )
        if choice is None or choice == NAV_BACK:
            return _WizardNav.BACK
        return _WizardNav.NEXT

    def _ensure_key_pair(self: Self, wiring: Ec2UseCases, key_pair: _KeyPairSubState) -> bool:
        """Create the pending key pair, exactly once. ``False`` means "abort the launch".

        Idempotent across the ``--confirm-large`` retry both execute paths
        offer: the second attempt must reuse the key created by the first,
        never try to create it again (AWS would reject the duplicate name, and
        ``CreateKeyPairUseCase`` would refuse to overwrite the ``.pem``).

        A no-op when there's nothing pending -- an instance WITH an Owner
        carries ``key_name is None`` and never reaches an AWS call here.
        """
        if key_pair.key_name is None or key_pair.created:
            return True
        try:
            saved_path, info = run_with_spinner(
                self.ctx.err_console,
                f"[bold green]Generando Key Pair '{key_pair.key_name}'...[/bold green]",
                lambda: wiring.create_key_pair.execute(
                    CreateKeyPairRequest(
                        name=key_pair.key_name or "",
                        destination_dir=_key_pair_destination_dir(),
                    )
                ),
            )
        except AwsAdminCliError as exc:
            self.ctx.err_console.print(f"[red]No se pudo generar la Key Pair: {exc}[/]")
            if exc.hint:
                self.ctx.err_console.print(f"[yellow]{exc.hint}[/]")
            self.prompter.pause()
            return False
        key_pair.saved_path = saved_path
        key_pair.fingerprint = info.key_fingerprint
        key_pair.created = True
        return True

    def _render_key_pair_result(self: Self, key_pair: _KeyPairSubState) -> None:
        """Show where the auto-generated private key landed -- nothing when there is none.

        Prints the PATH and fingerprint, never the private key body:
        ``CreateKeyPairUseCase`` states that contract outright ("NEVER the
        private material itself; callers must not log or render it") and
        ``KeyMaterial`` overrides ``__repr__``/``__str__`` to enforce it. The
        file it points at is already written ``0600``, which is both what SSH
        requires and a safer home for a secret than a terminal this TUI
        deliberately clears between screens. Shared verbatim by both creation
        wizards' success screens, so the ``keys/`` path and the example SSH
        command read identically no matter which one generated the key.
        """
        if key_pair.key_name is None or key_pair.saved_path is None:
            return
        table = Table(title="Key Pair generada automáticamente", show_header=False)
        table.add_column("Field", style="bold cyan")
        table.add_column("Value")
        pem_relpath = relative_path_display(key_pair.saved_path)
        table.add_row("Key Pair", key_pair.key_name)
        table.add_row("Llave privada", pem_relpath)
        table.add_row("Permisos", "0600 (solo tu usuario)")
        if key_pair.fingerprint:
            table.add_row("Fingerprint", key_pair.fingerprint)
        self.ctx.console.print(table)
        self.ctx.console.print(
            "[yellow]Generada automáticamente porque la instancia quedó sin Propietario. "
            "AWS entrega la llave privada UNA sola vez: guarda ese archivo, no se puede "
            "volver a descargar.[/]"
        )
        self.ctx.console.print(
            f"[cyan]Conéctate por SSH:[/] ssh -i {pem_relpath} ec2-user@<IP>"
        )
        self.ctx.console.print(f"[bold green]✔ Key Pair guardada en: {pem_relpath}[/]")

    def _render_launch_summary(
        self: Self,
        *,
        region: str,
        name: str,
        ami: Ami,
        instance_type: str,
        vpc_id: str,
        subnet_id: str,
        security_groups: list[SecurityGroup],
        key_name: str | None,
        volume_size_gb: int,
        owner: str,
        tags: dict[str, str],
        show_ami_name: bool = True,
        user_data_summary: str | None = None,
    ) -> None:
        """Render the shared Launch Summary table.

        ``show_ami_name`` is ``False`` for the "from scratch" wizard: that
        flow deploys a plain OS base image, not an AMI the user picked
        through the AMI module, so its row is labeled "Imagen Base" and shows
        only the technical image ID -- never the word "AMI", and never the
        image's own ``name`` (which, for a cloned/custom AMI resolved as a
        LocalStack fallback, would leak that AMI's own naming into what's
        conceptually a brand-new instance). AMI Quick Launch deploys
        explicitly from a chosen AMI, so it keeps the "AMI" row label and
        shows that AMI's name alongside its ID.

        ``user_data_summary`` stays ``None`` for AMI Quick Launch, which has
        no User Data step at all -- that omits the "UserData" row entirely
        instead of showing a stray "none" for a question that was never
        asked. Only the "from scratch" wizard (``_create_instance_step_user_data``)
        ever passes one.
        """
        table = Table(title="Launch Summary", show_header=False)
        table.add_column("Field", style="bold cyan")
        table.add_column("Value")
        table.add_row("Region", region)
        table.add_row("Name", name)
        if show_ami_name:
            table.add_row("AMI", f"{ami.image_id} ({ami.name})")
        else:
            table.add_row("Imagen Base", ami.image_id)
        table.add_row("Instance Type", instance_type)
        table.add_row("VPC", vpc_id)
        table.add_row("Subnet", subnet_id)
        table.add_row(
            "Security Groups",
            ", ".join(sg.group_id for sg in security_groups) or "(VPC default)",
        )
        table.add_row("Key Pair", key_name or _NO_KEY_PAIR)
        table.add_row("Storage", f"{_format_gib(volume_size_gb)}, gp3")
        if user_data_summary is not None:
            table.add_row("UserData", user_data_summary)
        table.add_row("Owner", owner or "(sin asignar)")
        other_tags = _tags_excluding_owner(tags)
        table.add_row("Tags", ", ".join(f"{k}={v}" for k, v in other_tags.items()) or "(none)")
        self.ctx.console.print(table)

    def _create_instance(self: Self) -> None:
        """Ultra-minimal EC2 creation: Name, AMI, Owner (optional+filtered) -- the rest defaults.

        Instance Type is a fixed default (never shown); VPC/Subnet/Security
        Group resolve to the account's default VPC, its first subnet, and
        that VPC's ``default`` security group without prompting; Storage
        stays at the AMI's native default (8 GB gp3). Owner now goes through
        the EXACT SAME steppable sub-flow (Sí/No/Back -> filter -> pick) AMI
        Quick Launch uses -- see ``_owner_step_confirm``/``_owner_step_filter``/
        ``_owner_step_select`` -- so the two flows share this logic
        verbatim, driven here by ``_create_instance_next_step``/
        ``_create_instance_prev_step`` the same way ``_quick_launch_from_ami``
        drives its own steps. Anyone needing per-launch control over storage
        size or Security Groups uses AMI Quick Launch instead. This flow also
        asks for optional User Data (``_create_instance_step_user_data``) --
        AMI Quick Launch skips that step entirely, since an operator picking
        an existing AMI already made their provisioning choice when that
        image was built.
        """
        state = _CreateInstanceState()
        wiring = build_ec2_use_cases(self.ctx)
        step = _CreateInstanceStep.NAME

        while True:
            self._clear_and_banner()
            if step is _CreateInstanceStep.NAME:
                nav = self._create_instance_step_name(state)
            elif step is _CreateInstanceStep.AMI:
                nav = self._create_instance_step_ami(wiring, state)
            elif step is _CreateInstanceStep.OWNER_CONFIRM:
                nav = self._owner_step_confirm(state.owner, state.key_pair)
            elif step is _CreateInstanceStep.OWNER_FILTER:
                nav = self._owner_step_filter(state.owner)
            elif step is _CreateInstanceStep.OWNER_SELECT:
                nav = self._owner_step_select(state.owner, default_owner=None)
            elif step is _CreateInstanceStep.KEY_PAIR:
                nav = self._key_pair_step_auto(
                    state.key_pair, instance_name=state.launch_state.name or "ec2"
                )
            elif step is _CreateInstanceStep.USER_DATA:
                nav = self._create_instance_step_user_data(state)
            else:
                nav = self._create_instance_step_confirm(state)
                if nav is _WizardNav.NEXT:
                    self._create_instance_execute(wiring, state)
                    return

            if nav is _WizardNav.CANCEL:
                return
            if nav is _WizardNav.BACK:
                prev_step = _create_instance_prev_step(step, state)
                if prev_step is None:
                    return
                step = prev_step
                continue
            step = _create_instance_next_step(step, state)

    def _create_instance_step_name(self: Self, state: _CreateInstanceState) -> _WizardNav:
        """Step 1: Instance Name. No predecessor -- backing out here exits the whole wizard."""
        name = prompt_text_or_cancel(
            self.prompter,
            self.ctx.err_console,
            "Nombre de la instancia (Instance Name)",
            default=state.launch_state.name or "",
        )
        if not name:
            return _WizardNav.CANCEL
        state.launch_state.name = name
        return _WizardNav.NEXT

    def _create_instance_step_ami(
        self: Self, wiring: Ec2UseCases, state: _CreateInstanceState
    ) -> _WizardNav:
        nav = self._wizard_step_base_image(wiring, state.launch_state)
        if nav is not _WizardNav.NEXT:
            return nav
        assert state.launch_state.ami_ref is not None
        resolved_ami = self._resolve_ami(wiring, state.launch_state.ami_ref)
        if resolved_ami is None:
            return _WizardNav.CANCEL
        state.resolved_ami = resolved_ami
        return _WizardNav.NEXT

    def _create_instance_step_user_data(self: Self, state: _CreateInstanceState) -> _WizardNav:
        """Optional cloud-init User Data: a local script, the security template, or none.

        Standard-flow-only -- AMI Quick Launch never reaches this step (see
        ``_create_instance``'s own docstring): an operator picking an
        existing AMI already made their provisioning choice when that image
        was built. An invalid/missing local path re-shows THIS SAME select
        (``continue``), never the whole wizard -- same sub-choice retry
        convention ``_wizard_step_base_image``'s manual AMI ID entry uses.
        """
        while True:
            choice = self.prompter.select(
                "Script de Aprovisionamiento (User Data):",
                [
                    Choice(
                        title="📄 Cargar script local (buscar ./user_data.sh o ingresar ruta)",
                        value=_USERDATA_LOCAL,
                    ),
                    Choice(
                        title="⚡ Usar plantilla de seguridad (Update OS + CloudWatch Agent)",
                        value=_USERDATA_TEMPLATE,
                    ),
                    Choice(title="🚫 Ninguno (Arrancar SO limpio)", value=_USERDATA_NONE),
                    Separator(),
                    Choice(title="↩️  Back", value=NAV_BACK),
                ],
                default=_USERDATA_NONE,
            )
            if choice is None or choice == NAV_BACK:
                return _WizardNav.BACK
            if choice == _USERDATA_TEMPLATE:
                state.user_data = SECURITY_USERDATA_TEMPLATE
                state.user_data_summary = _USERDATA_SUMMARY_TEMPLATE
                return _WizardNav.NEXT
            if choice == _USERDATA_NONE:
                state.user_data = None
                state.user_data_summary = _USERDATA_SUMMARY_NONE
                return _WizardNav.NEXT
            # _USERDATA_LOCAL
            raw_path = prompt_text_or_cancel(
                self.prompter,
                self.ctx.err_console,
                "Ruta del script local",
                default=_DEFAULT_USERDATA_PATH,
            )
            if not raw_path:
                continue  # back to the select above, not the whole wizard
            script_path = Path(raw_path)
            if not script_path.is_file():
                self.ctx.err_console.print(
                    f"[red]'{raw_path}' no existe o no es un archivo.[/]"
                )
                continue
            state.user_data = script_path.read_text(encoding="utf-8")
            state.user_data_summary = f"{script_path.name} (Attached)"
            return _WizardNav.NEXT

    def _create_instance_step_confirm(self: Self, state: _CreateInstanceState) -> _WizardNav:
        """Silently resolve VPC/Subnet/Security Groups, then confirm.

        No interactive step for VPC/Subnet/Security Groups here -- they're
        smart defaults, per this flow's own spec. Renders the summary and
        asks the final Sí/No/Back confirmation.
        """
        assert state.launch_state.name is not None
        assert state.resolved_ami is not None
        wiring = build_ec2_use_cases(self.ctx)
        try:
            resolved_vpc = wiring.network_resolver.resolve_vpc(None)
        except AwsAdminCliError as exc:
            self.ctx.err_console.print(f"[red]{exc}[/]")
            if exc.hint:
                self.ctx.err_console.print(f"[yellow]{exc.hint}[/]")
            self.prompter.pause()
            return _WizardNav.CANCEL

        vpc_use_cases = build_vpc_use_cases(self.ctx)
        subnets = vpc_use_cases.list_subnets.execute(
            ListSubnetsRequest(vpc_id=resolved_vpc.vpc_id)
        )
        if not subnets:
            self.ctx.err_console.print(f"[red]VPC '{resolved_vpc.vpc_id}' has no subnets.[/]")
            self.prompter.pause()
            return _WizardNav.CANCEL

        ensure_demo_security_groups(self.ctx, resolved_vpc.vpc_id)
        available_sgs = vpc_use_cases.list_security_groups.execute(resolved_vpc.vpc_id)
        default_sg = next((sg for sg in available_sgs if sg.group_name == "default"), None)
        resolved_sgs = [default_sg] if default_sg is not None else []

        state.resolved_vpc_id = resolved_vpc.vpc_id
        state.resolved_subnet_id = subnets[0].subnet_id
        state.resolved_security_groups = resolved_sgs

        instance_type = _INSTANCE_TYPES[0]
        owner = state.owner.owner or ""
        tags = _launch_tags(owner, state.key_pair.key_name)
        self._render_launch_summary(
            region=self.ctx.settings.region,
            name=state.launch_state.name,
            ami=state.resolved_ami,
            instance_type=instance_type,
            vpc_id=state.resolved_vpc_id,
            subnet_id=state.resolved_subnet_id,
            security_groups=resolved_sgs,
            key_name=state.key_pair.key_name,
            volume_size_gb=_DEFAULT_VOLUME_SIZE_GB,
            owner=owner,
            tags=tags,
            show_ami_name=False,
            user_data_summary=state.user_data_summary,
        )
        confirm_choice = self.prompter.select(
            "¿Confirmas el despliegue?",
            [
                Choice(title="Sí, desplegar EC2", value=_CONFIRM_YES),
                Choice(title="No, cancelar", value=_CONFIRM_NO),
                Separator(),
                Choice(title="↩️  Back", value=_CONFIRM_BACK),
            ],
            default=_CONFIRM_YES,
        )
        if confirm_choice == _CONFIRM_YES:
            return _WizardNav.NEXT
        if confirm_choice == _CONFIRM_BACK:
            return _WizardNav.BACK
        return _WizardNav.CANCEL

    def _create_instance_execute(
        self: Self, wiring: Ec2UseCases, state: _CreateInstanceState
    ) -> None:
        assert state.launch_state.name is not None
        assert state.resolved_ami is not None
        assert state.resolved_vpc_id is not None
        assert state.resolved_subnet_id is not None
        assert state.resolved_security_groups is not None
        name = state.launch_state.name
        instance_type = _INSTANCE_TYPES[0]
        owner = state.owner.owner or ""
        if not self._ensure_key_pair(wiring, state.key_pair):
            return
        tags = _launch_tags(owner, state.key_pair.key_name)
        request = LaunchInstanceRequest(
            name=name,
            ami_ref=state.resolved_ami.image_id,
            instance_type=instance_type,
            subnet_ref=state.resolved_subnet_id,
            security_group_refs=tuple(sg.group_id for sg in state.resolved_security_groups),
            vpc_ref=state.resolved_vpc_id,
            key_name=state.key_pair.key_name,
            volume_size_gb=_DEFAULT_VOLUME_SIZE_GB,
            tags=tags,
            user_data=state.user_data,
        )
        try:
            instance = run_with_spinner(
                self.ctx.err_console,
                f"[bold green]Launching instance '{name}'...[/bold green]",
                lambda: wiring.launch_instance.execute(request),
            )
        except ValidationError as exc:
            self.ctx.err_console.print(f"[red]{exc}[/]")
            if exc.hint:
                self.ctx.err_console.print(f"[yellow]{exc.hint}[/]")
            if not confirm_yes_no(
                self.prompter, "Retry confirming the instance type (--confirm-large)?"
            ):
                return
            instance = run_with_spinner(
                self.ctx.err_console,
                f"[bold green]Launching instance '{name}'...[/bold green]",
                lambda: wiring.launch_instance.execute(replace(request, confirm_large=True)),
            )
        self.ctx.console.print()
        self.ctx.console.print(
            f"[bold green]✅ Instancia '{instance.instance_id}' desplegada correctamente.[/]"
        )
        self._render_key_pair_result(state.key_pair)
        self.ctx.console.print()
        self.prompter.pause()

    # -- Terminate (Permanent) -----------------------------------------------------------

    def _delete_instance(self: Self) -> None:
        picked = self._filter_and_pick_instance("Which instance do you want to terminate?")
        if picked is None:
            return
        instance_id = picked.instance_id
        instance = self._render_instance_detail(instance_id, region=picked.region)

        if not confirm_destructive(self.prompter, kind="instance", name=instance_id):
            return

        wiring = build_ec2_use_cases(self.ctx)
        try:
            result = run_with_spinner(
                self.ctx.err_console,
                f"[bold green]Terminating instance '{instance_id}'...[/bold green]",
                lambda: wiring.terminate_instance.execute(
                    instance, force=False, dry_run=False, wait=False, timeout_s=300
                ),
            )
        except ValidationError as exc:
            self.ctx.err_console.print(f"[red]{exc}[/]")
            if exc.hint:
                self.ctx.err_console.print(f"[yellow]{exc.hint}[/]")
            if not confirm_yes_no(
                self.prompter,
                "This instance is not managed by this CLI. " "Terminate it anyway (--force)?",
            ):
                return
            result = run_with_spinner(
                self.ctx.err_console,
                f"[bold green]Terminating instance '{instance_id}'...[/bold green]",
                lambda: wiring.terminate_instance.execute(
                    instance, force=True, dry_run=False, wait=False, timeout_s=300
                ),
            )

        label = result.display_name if result is not None else instance.display_name
        announce_result(self.ctx, self.prompter, f"[green]Termination requested for '{label}'.[/]")

    # -- AMI Management (Search / Copy / Create) -----------------------------------------

    @staticmethod
    def _ami_menu_choices() -> list[Choice | Separator]:
        return [
            Choice(title="Search AMIs (Buscar y gestionar)", value=_AMI_SEARCH),
            Choice(title="Create AMI from Instance (Crear imagen)", value=_AMI_CREATE),
            Separator(),
            Choice(title="↩️  Back", value=_AMI_MENU_BACK),
        ]

    def _render_ami_stats(self: Self, amis: list[Ami]) -> None:
        """Print "AMIs: N Total | N Available | N Private" above the AMI menu.

        Mirrors ``S3Flow._render_bucket_stats``: paid on every render rather
        than cached, since the whole point is to reflect the account's
        current AMIs (an AMI can go from ``pending``/``available`` or get
        deregistered between redraws) rather than a stale first reading.
        """
        total = len(amis)
        available = sum(1 for ami in amis if ami.state == "available")
        private = sum(1 for ami in amis if not ami.public)
        self.ctx.err_console.print()  # spacing from the previous prompt's answer line
        self.ctx.err_console.print(
            f"  AMIs: {total} Total  |  {available} Available 🟢  |  {private} Private 🔒"
        )

    def _ami_menu(self: Self) -> None:
        """The AMI Management submenu: Search / Create / Back.

        Same clear-then-redraw discipline as the Resource Audit submenu
        below: every entry into this loop redraws a clean screen.
        """
        while True:
            self._clear_and_banner()
            amis = build_ec2_use_cases(self.ctx).list_amis_global.execute(owner="self")
            self._render_ami_stats(amis)
            selected = self.prompter.select(
                "AMI Management -- what do you want to do?", self._ami_menu_choices()
            )
            if selected is None or selected == _AMI_MENU_BACK:
                return
            if selected == _AMI_SEARCH:
                self._ami_search()
            elif selected == _AMI_CREATE:
                self._ami_create()

    def _query_amis(self: Self, message: str = "Enter search query:") -> list[Ami] | None:
        """Ask for a free-text query (blank = everyone); matches AMI name or ID.

        Exclude EC2s is inherent here: this only ever queries AMIs
        (``DescribeImages``), never touches instance data. Scans every AWS
        region, like ``_query_instances(global_=True)`` -- an AMI created in
        any region shows up here, each carrying its own ``.region``.

        ``owner="self"``: only AMIs this account created -- never Amazon's
        own public catalog, which would otherwise flood this screen.
        """
        amis = build_ec2_use_cases(self.ctx).list_amis_global.execute(owner="self")
        if not amis:
            self.ctx.err_console.print("[yellow]No AMIs found.[/]")
            self.prompter.pause()
            return None

        self._clear_and_banner()
        query = prompt_text_or_cancel(self.prompter, self.ctx.err_console, message)
        if query is None:
            return None
        if not query:
            return amis
        needle = query.lower()
        return [a for a in amis if needle in a.name.lower() or needle in a.image_id.lower()]

    def _render_ami_table(self: Self, amis: list[Ami]) -> None:
        """AMI search results: Name, AMI ID, Region, State, Creation Date, Root Device Type, Tags.

        ``amis`` comes from the every-region scan (see ``_query_amis``), so
        the Region column shows where each one actually lives.
        """
        table = Table(title="AMIs")
        table.add_column("Name", style="bold")
        table.add_column("AMI ID")
        table.add_column("Region")
        table.add_column("State")
        table.add_column("Creation Date")
        table.add_column("Root Device Type")
        table.add_column("Tags")
        for ami in amis:
            tags = tags_to_dict(ami.tags)
            tags_label = ", ".join(f"{k}={v}" for k, v in tags.items()) if tags else "(no tags)"
            table.add_row(
                _ami_display_name(ami),
                ami.image_id,
                ami.region or "-",
                ami.state or "unknown",
                _fmt_activity(ami.creation_date),
                ami.root_device_type or "unknown",
                tags_label,
            )
        self.ctx.console.print(table)

    def _pick_ami(self: Self, message: str) -> Ami | None:
        """Search AMIs, render the results table, then pick one -- never an auto-pick."""
        matches = self._query_amis()
        if matches is None:
            return None
        if not matches:
            self.ctx.err_console.print("[yellow]No matches.[/]")
            self.prompter.pause()
            return None
        self._render_ami_table(matches)
        by_id = {ami.image_id: ami for ami in matches}
        choices: list[Choice | Separator] = [
            Choice(
                title=f"{ami.image_id}  {_ami_display_name(ami)}  [{ami.region}]",
                value=ami.image_id,
            )
            for ami in matches
        ]
        choices.append(Separator())
        choices.append(Choice(title="<- Cancel", value=NAV_BACK))
        selected = self.prompter.select(
            message, choices, use_search=len(matches) > _SEARCH_THRESHOLD
        )
        if selected is None or selected == NAV_BACK:
            return None
        return by_id[selected]

    def _ami_search(self: Self) -> None:
        """Search -> pick -> detail/manage, the same single-shot shape as ``_search_instances``."""
        ami = self._pick_ami("Which AMI do you want to view?")
        if ami is None:
            return
        self._ami_detail_loop(ami.image_id, region=ami.region)

    @staticmethod
    def _ami_detail_menu_choices() -> list[Choice | Separator]:
        """The flat action menu for one AMI's detail screen -- same shape as an instance's."""
        return [
            Choice(title="Edit Name", value=_AMI_DETAIL_EDIT_NAME),
            Choice(title="Manage Tags (Owner)", value=_AMI_DETAIL_MANAGE_TAGS),
            Choice(title="Create EC2 from this AMI", value=_AMI_DETAIL_CREATE_EC2),
            Choice(title="Delete AMI (Deregister)", value=_AMI_DETAIL_DELETE),
            Separator(),
            Choice(title="↩️  Back", value=_AMI_DETAIL_BACK),
        ]

    def _ami_detail_loop(self: Self, image_id: str, *, region: str | None = None) -> None:
        """Detail view + flat action menu for one AMI -- mirrors ``_instance_detail_loop``.

        ``region`` is the AMI's own region (``None`` for the active session
        region) -- carried in from ``_ami_search``'s global pick, so every
        action below (Name/Tags/Deregister, and re-fetching the detail
        itself) routes to the SAME region's client the AMI actually lives
        in, never the active session region.
        """
        while True:
            ami = self._render_ami_detail(image_id, region=region)
            selected = self.prompter.select(
                f"AMI '{_ami_display_name(ami)}' -- what do you want to do?",
                self._ami_detail_menu_choices(),
            )
            if selected is None or selected == _AMI_DETAIL_BACK:
                return
            if selected == _AMI_DETAIL_EDIT_NAME:
                self._edit_ami_name(image_id, region=region)
            elif selected == _AMI_DETAIL_MANAGE_TAGS:
                self._edit_tags(
                    image_id,
                    fetch_tags=partial(self._fetch_ami_tags, image_id, region=region),
                    region=region,
                )
            elif selected == _AMI_DETAIL_CREATE_EC2:
                self._quick_launch_from_ami(ami)
            elif selected == _AMI_DETAIL_DELETE and self._deregister_ami_flow(
                image_id, region=region
            ):
                return  # the AMI is gone -- nothing left here to redraw

    def _fetch_ami_tags(self: Self, image_id: str, *, region: str | None = None) -> dict[str, str]:
        return _ami_tags(
            build_ec2_use_cases(self.ctx).get_ami.execute(image_id, region=region)
        )

    def _quick_launch_from_ami(self: Self, ami: Ami) -> None:
        """Quick Launch: a genuine steppable wizard (Name / Owner / Network / Storage / Confirm).

        Driven by ``_quick_launch_next_step``/``_quick_launch_prev_step`` --
        this loop itself never decides what "forward"/"back" means, it just
        asks those two functions and dispatches to whichever step method
        comes back. "<- Back" (or Ctrl+C/Esc) at ANY step re-shows its own
        immediate predecessor with every previously-collected answer still
        intact, exactly per the mapping in this flow's spec; only backing
        out of the very first step (Name) exits the whole wizard, back to
        the AMI's own detail screen. Every step is entered through
        ``self._clear_and_banner()`` first, so stepping back redraws a clean
        screen too -- prompts never stack up regardless of which direction
        the user is moving.
        """
        default_name = _ec2_name_suggestion_from_ami(ami)
        current_owner_tag = tag_value(ami.tags, "Owner")
        state = _QuickLaunchState()
        wiring = build_ec2_use_cases(self.ctx)
        step = _QuickLaunchStep.NAME

        while True:
            self._clear_and_banner()
            if step is _QuickLaunchStep.NAME:
                nav = self._quick_launch_step_name(state, default_name)
            elif step is _QuickLaunchStep.OWNER_CONFIRM:
                nav = self._owner_step_confirm(state.owner, state.key_pair)
            elif step is _QuickLaunchStep.OWNER_FILTER:
                nav = self._owner_step_filter(state.owner)
            elif step is _QuickLaunchStep.OWNER_SELECT:
                nav = self._owner_step_select(state.owner, default_owner=current_owner_tag)
            elif step is _QuickLaunchStep.KEY_PAIR:
                nav = self._key_pair_step_auto(state.key_pair, instance_name=state.name or "ec2")
            elif step is _QuickLaunchStep.VPC:
                nav = self._quick_launch_step_vpc(state)
            elif step is _QuickLaunchStep.SECURITY_GROUPS:
                nav = self._quick_launch_step_security_groups(state)
            elif step is _QuickLaunchStep.STORAGE:
                nav = self._quick_launch_step_storage(wiring, state)
            else:
                nav = self._quick_launch_step_confirm(state, ami)
                if nav is _WizardNav.NEXT:
                    self._quick_launch_execute(wiring, ami, state)
                    return

            if nav is _WizardNav.CANCEL:
                return
            if nav is _WizardNav.BACK:
                prev_step = _quick_launch_prev_step(step, state)
                if prev_step is None:
                    return
                step = prev_step
                continue
            step = _quick_launch_next_step(step, state)

    def _quick_launch_step_name(
        self: Self, state: _QuickLaunchState, default_name: str
    ) -> _WizardNav:
        """Step 1: Instance Name. No predecessor -- backing out here exits the whole wizard."""
        name = prompt_text_or_cancel(
            self.prompter, self.ctx.err_console, "Instance name", default=state.name or default_name
        )
        if not name:
            return _WizardNav.CANCEL
        state.name = name
        return _WizardNav.NEXT

    def _quick_launch_step_vpc(self: Self, state: _QuickLaunchState) -> _WizardNav:
        vpc_use_cases = build_vpc_use_cases(self.ctx)
        vpcs = vpc_use_cases.list_vpcs.execute()
        if not vpcs:
            self.ctx.err_console.print("[red]No VPCs available to launch into.[/]")
            self.prompter.pause()
            return _WizardNav.CANCEL
        choices: list[Choice | Separator] = [
            Choice(title=f"{vpc.display_name} ({vpc.vpc_id}, {vpc.cidr_block})", value=vpc.vpc_id)
            for vpc in vpcs
        ]
        choices.append(Separator())
        choices.append(Choice(title="↩️  Back", value=NAV_BACK))
        vpc_id = self.prompter.select("VPC:", choices, default=state.vpc_id)
        if vpc_id is None or vpc_id == NAV_BACK:
            return _WizardNav.BACK
        state.vpc_id = vpc_id
        return _WizardNav.NEXT

    def _quick_launch_step_security_groups(self: Self, state: _QuickLaunchState) -> _WizardNav:
        assert state.vpc_id is not None
        subnets = build_vpc_use_cases(self.ctx).list_subnets.execute(
            ListSubnetsRequest(vpc_id=state.vpc_id)
        )
        if not subnets:
            self.ctx.err_console.print(f"[red]VPC '{state.vpc_id}' has no subnets.[/]")
            self.prompter.pause()
            return _WizardNav.CANCEL
        state.subnet_id = subnets[0].subnet_id  # auto-picked -- only VPC/SG are interactive here

        resolved_sgs = self._quick_pick_security_groups(state.vpc_id)
        if resolved_sgs is None:
            return _WizardNav.BACK
        state.security_groups = resolved_sgs
        return _WizardNav.NEXT

    def _quick_launch_step_storage(
        self: Self, wiring: Ec2UseCases, state: _QuickLaunchState
    ) -> _WizardNav:
        """Delegates to the Provision Wizard's own Storage step, on ``state.storage_state``."""
        return self._wizard_step_storage(wiring, state.storage_state)

    def _quick_launch_step_confirm(self: Self, state: _QuickLaunchState, ami: Ami) -> _WizardNav:
        assert state.name is not None
        assert state.vpc_id is not None
        assert state.subnet_id is not None
        assert state.security_groups is not None
        assert state.storage_state.volume_size_gb is not None
        instance_type = _INSTANCE_TYPES[0]
        tags = _launch_tags(state.owner.owner or "", state.key_pair.key_name)
        self._render_launch_summary(
            region=self.ctx.settings.region,
            name=state.name,
            ami=ami,
            instance_type=instance_type,
            vpc_id=state.vpc_id,
            subnet_id=state.subnet_id,
            security_groups=state.security_groups,
            key_name=state.key_pair.key_name,
            volume_size_gb=state.storage_state.volume_size_gb,
            owner=state.owner.owner or "",
            tags=tags,
        )
        confirm_choice = self.prompter.select(
            "¿Confirmas el despliegue?",
            [
                Choice(title="Sí, desplegar EC2", value=_CONFIRM_YES),
                Choice(title="No, cancelar", value=_CONFIRM_NO),
                Separator(),
                Choice(title="↩️  Back", value=_CONFIRM_BACK),
            ],
            default=_CONFIRM_YES,
        )
        if confirm_choice == _CONFIRM_YES:
            return _WizardNav.NEXT
        if confirm_choice == _CONFIRM_BACK:
            return _WizardNav.BACK
        return _WizardNav.CANCEL

    def _quick_launch_execute(
        self: Self, wiring: Ec2UseCases, ami: Ami, state: _QuickLaunchState
    ) -> None:
        """Fully resolved state -> ``run_instances``.

        Same --confirm-large retry every other launch path in this module
        offers on a guard-rail ``ValidationError``.
        """
        assert state.name is not None
        assert state.vpc_id is not None
        assert state.subnet_id is not None
        assert state.security_groups is not None
        assert state.storage_state.volume_size_gb is not None
        instance_type = _INSTANCE_TYPES[0]
        if not self._ensure_key_pair(wiring, state.key_pair):
            return
        tags = _launch_tags(state.owner.owner or "", state.key_pair.key_name)
        request = LaunchInstanceRequest(
            name=state.name,
            ami_ref=ami.image_id,
            instance_type=instance_type,
            subnet_ref=state.subnet_id,
            security_group_refs=tuple(sg.group_id for sg in state.security_groups),
            vpc_ref=state.vpc_id,
            key_name=state.key_pair.key_name,
            volume_size_gb=state.storage_state.volume_size_gb,
            tags=tags,
        )
        try:
            instance = run_with_spinner(
                self.ctx.err_console,
                "[bold green]Desplegando instancia desde AMI en AWS...[/bold green]",
                lambda: wiring.launch_instance.execute(request),
            )
        except ValidationError as exc:
            self.ctx.err_console.print(f"[red]{exc}[/]")
            if exc.hint:
                self.ctx.err_console.print(f"[yellow]{exc.hint}[/]")
            if not confirm_yes_no(
                self.prompter, "Retry confirming the instance type (--confirm-large)?"
            ):
                return
            instance = run_with_spinner(
                self.ctx.err_console,
                "[bold green]Desplegando instancia desde AMI en AWS...[/bold green]",
                lambda: wiring.launch_instance.execute(replace(request, confirm_large=True)),
            )
        self.ctx.console.print()
        self.ctx.console.print(
            f"[bold green]✅ Instancia '{instance.instance_id}' desplegada correctamente "
            f"desde AMI {ami.image_id}.[/]"
        )
        self._render_key_pair_result(state.key_pair)
        self.ctx.console.print()
        self.prompter.pause()

    def _quick_pick_security_groups(self: Self, vpc_id: str) -> list[SecurityGroup] | None:
        """A checkbox over ``vpc_id``'s security groups -- no inner confirm sub-screen.

        Quick Launch has its own single final confirmation (the deploy
        summary); repeating "N selected, Continue?" here would just be a
        second confirmation for the same decision. Empty selection falls
        back to the VPC's own ``default`` group, same smart-default as the
        full wizard's Security Groups step.
        """
        ensure_demo_security_groups(self.ctx, vpc_id)
        available = build_vpc_use_cases(self.ctx).list_security_groups.execute(vpc_id)
        if not available:
            self.ctx.err_console.print(
                "[yellow]No security groups in this VPC -- using the VPC default.[/]"
            )
            return []
        sg_by_id = {sg.group_id: sg for sg in available}
        choices: list[Choice | Separator] = [
            Choice(title=f"{sg.group_name} ({sg.group_id})", value=sg.group_id)
            for sg in available
        ]
        choices.append(Separator())
        choices.append(Choice(title="↩️  Back", value=NAV_BACK))
        selected_ids = self.prompter.checkbox(
            f"Security Group para la VPC {vpc_id} (Vacío = SG default) -- "
            f"{len(available)} disponibles, SPACE marca, ENTER confirma:",
            choices,
        )
        if selected_ids is None or NAV_BACK in selected_ids:
            return None
        if not selected_ids:
            default_sg = next((sg for sg in available if sg.group_name == "default"), None)
            if default_sg is not None:
                selected_ids = [default_sg.group_id]
        return [sg_by_id[gid] for gid in selected_ids]

    def _render_ami_detail(self: Self, image_id: str, *, region: str | None = None) -> Ami:
        """Clear the screen and print one AMI's vertical detail table."""
        self._clear_and_banner()
        ami = build_ec2_use_cases(self.ctx).get_ami.execute(image_id, region=region)

        table = Table(title=f"AMI: {image_id}", show_header=False)
        table.add_column("Property", style="bold cyan")
        table.add_column("Value")
        table.add_row("AMI ID", ami.image_id)
        table.add_row("Name", _ami_display_name(ami))
        table.add_row("Region", ami.region or self.ctx.settings.region)
        table.add_row("State", ami.state or "unknown")
        table.add_row("Architecture", ami.architecture or "unknown")
        table.add_row("Platform/OS", ami.platform_details or "Linux/UNIX")
        table.add_row("Root Device", ami.root_device_type or "unknown")
        table.add_row("Creation Date", _fmt_activity(ami.creation_date))
        table.add_row("Description", ami.description or "(none)")
        table.add_row("Owner", tag_value(ami.tags, "Owner") or "(sin asignar)")
        table.add_row("Tags", _format_tags_block(_tags_excluding_owner(_ami_tags(ami))))
        self.ctx.console.print(table)
        return ami

    def _edit_ami_name(self: Self, image_id: str, *, region: str | None = None) -> None:
        """Rename via the ``Name`` tag -- AWS never lets you change an AMI's native Name.

        An empty ENTER means "Back", same rule as everywhere else in this
        module: never writes a blank name.
        """
        new_name = prompt_text_or_cancel(self.prompter, self.ctx.err_console, "New AMI name")
        if not new_name:
            return
        build_ec2_use_cases(self.ctx).tag_resource.execute(
            image_id, {"Name": new_name}, region=region
        )
        announce_result(self.ctx, self.prompter, f"[green]Name tag updated to '{new_name}'.[/]")

    def _deregister_ami_flow(self: Self, image_id: str, *, region: str | None = None) -> bool:
        """Confirm (Sí, eliminar AMI/No, cancelar/<- Back), then deregister.

        Returns ``True`` if deleted.
        """
        choice = self.prompter.select(
            f"¿Eliminar (deregister) la AMI '{image_id}'? Esta acción no se puede deshacer.",
            [
                Choice(title="Sí, eliminar AMI", value=_CONFIRM_YES),
                Choice(title="No, cancelar", value=_CONFIRM_NO),
                Separator(),
                Choice(title="↩️  Back", value=_CONFIRM_BACK),
            ],
            default=_CONFIRM_NO,
        )
        if choice != _CONFIRM_YES:
            return False
        build_ec2_use_cases(self.ctx).deregister_ami.execute(image_id, region=region)
        announce_result(self.ctx, self.prompter, f"[green]AMI '{image_id}' deregistered.[/]")
        return True

    def _ami_create(self: Self) -> None:
        """Create AMI from Instance: pick an instance, name (defaulted), Owner, Reboot choice.

        No Description prompt and no Storage configuration here -- kept
        deliberately lean, only the fields actually asked for.
        """
        self._clear_and_banner()
        picked = self._filter_and_pick_instance(
            "Which instance do you want to create an AMI from?"
        )
        if picked is None:
            return
        instance_id = picked.instance_id
        instance = build_ec2_use_cases(self.ctx).get_instance.execute(
            instance_id, region=picked.region
        )

        self._clear_and_banner()
        name = prompt_text_or_cancel(
            self.prompter,
            self.ctx.err_console,
            "AMI name",
            default=f"AMI-{instance.display_name}",
        )
        if not name:
            return

        self._clear_and_banner()
        owner = self._wizard_pick_tag_value("Owner")
        tags = {"Owner": owner} if owner else {}

        self._clear_and_banner()
        reboot_choice = self.prompter.select(
            "Para garantizar la integridad de los datos:",
            [
                Choice(
                    title="Sí, apagar temporalmente para copia segura", value=_REBOOT_YES
                ),
                Choice(title="No, crear en caliente (Sin apagar)", value=_REBOOT_NO),
                Separator(),
                Choice(title="↩️  Back", value=_REBOOT_BACK),
            ],
            default=_REBOOT_NO,
        )
        if reboot_choice is None or reboot_choice == _REBOOT_BACK:
            return
        no_reboot = reboot_choice == _REBOOT_NO  # "Sí" (reboot for safety) -> NoReboot=False

        wiring = build_ec2_use_cases(self.ctx)
        try:
            ami = run_with_spinner(
                self.ctx.err_console,
                f"[bold green]Creating AMI '{name}'...[/bold green]",
                lambda: wiring.create_ami.execute(
                    CreateAmiRequest(
                        instance_id=instance_id,
                        name=name,
                        no_reboot=no_reboot,
                        tags=tags,
                    ),
                    region=instance.region,
                ),
            )
        except AwsAdminCliError as exc:
            self.ctx.err_console.print(f"[red]{exc}[/]")
            if exc.hint:
                self.ctx.err_console.print(f"[yellow]{exc.hint}[/]")
            self.prompter.pause()
            return
        announce_result(
            self.ctx,
            self.prompter,
            f"[green]AMI '{ami.image_id}' created from '{instance_id}'.[/]",
        )

    # -- Resource Audit (Governance): checkbox + differentiated Stop/Terminate action ----
    #
    # EC2-exclusive: every row here comes from ``list_instances`` (``DescribeInstances``)
    # -- never AMIs, SGs, or any other resource type -- and both categories additionally
    # gate on the instance's actual ``state``, not just the simulated idle clock.

    def _collect_audit_rows(
        self: Self,
        use_cases: Ec2UseCases,
        predicate: Callable[[Instance, int | None], bool],
        *,
        now: datetime,
    ) -> list[tuple[Instance, datetime | None, int]]:
        rows: list[tuple[Instance, datetime | None, int]] = []
        for instance in use_cases.list_instances.execute(ListInstancesRequest()):
            last_activity = _resolve_last_activity(self.ctx, instance)
            days = _days_idle(last_activity, now=now)
            if predicate(instance, days):
                assert days is not None  # the predicates above already exclude None
                rows.append((instance, last_activity, days))
        return rows

    def _collect_underutilized_rows(
        self: Self, use_cases: Ec2UseCases, *, now: datetime
    ) -> list[tuple[Instance, datetime | None, int]]:
        return self._collect_audit_rows(use_cases, _is_underutilized, now=now)

    def _collect_zombie_rows(
        self: Self, use_cases: Ec2UseCases, *, now: datetime
    ) -> list[tuple[Instance, datetime | None, int]]:
        return self._collect_audit_rows(use_cases, _is_zombie, now=now)

    def _render_audit_table(
        self: Self, rows: list[tuple[Instance, datetime | None, int]], *, title: str
    ) -> None:
        table = Table(title=title)
        table.add_column("Instance", style="bold")
        table.add_column("ID")
        table.add_column("Last Activity")
        table.add_column("Days Idle", justify="right")
        for instance, last_activity, days in rows:
            table.add_row(
                instance.display_name, instance.instance_id, _fmt_activity(last_activity), str(days)
            )
        self.ctx.console.print(table)

    def _audit_act(
        self: Self,
        rows: list[tuple[Instance, datetime | None, int]],
        *,
        table_title: str,
        empty_message: str,
        checkbox_message: str,
        confirm_message: str,
        confirm_yes_label: str,
        confirm_no_label: str,
        action: Callable[[Ec2UseCases, Instance], object],
        success_verb: str,
    ) -> None:
        """One audit category, end to end: table -> checkbox -> confirm -> Stop/Terminate.

        Every abort path (no candidates, Esc/Ctrl+C on the checkbox, an empty
        confirm, ticking "<- Back", declining the confirm) returns here
        cleanly -- the caller's own loop re-shows the audit menu with fresh
        data on the very next pass, no separate "refresh" step needed.
        """
        self._clear_and_banner()
        if not rows:
            self.ctx.err_console.print(f"[green]{empty_message}[/]")
            self.prompter.pause()
            return
        self._render_audit_table(rows, title=table_title)

        by_id = {instance.instance_id: instance for instance, _, _ in rows}
        choices: list[Choice | Separator] = [
            Choice(
                title=f"{instance.display_name} ({instance.instance_id}, {days}d)",
                value=instance.instance_id,
            )
            for instance, _, days in rows
        ]
        choices.append(Separator())
        choices.append(Choice(title="↩️  Back", value=NAV_BACK))
        selected_ids = self.prompter.checkbox(checkbox_message, choices)
        if not selected_ids or NAV_BACK in selected_ids:
            return  # None (Esc/Ctrl+C), [] (blank ENTER), or "<- Back" ticked -- all abort

        confirm_choice = self.prompter.select(
            confirm_message.format(n=len(selected_ids)),
            [
                Choice(title=confirm_yes_label, value=_CONFIRM_YES),
                Choice(title=confirm_no_label, value=_CONFIRM_NO),
                Separator(),
                Choice(title="↩️  Back", value=_CONFIRM_BACK),
            ],
            default=_CONFIRM_NO,
        )
        if confirm_choice != _CONFIRM_YES:
            return

        use_cases = build_ec2_use_cases(self.ctx)
        succeeded: list[str] = []
        skipped: list[tuple[str, str]] = []
        for instance_id in selected_ids:
            instance = by_id[instance_id]
            try:
                run_with_spinner(
                    self.ctx.err_console,
                    f"[bold green]Processing {instance_id}...[/bold green]",
                    partial(action, use_cases, instance),
                )
            except ValidationError as exc:
                skipped.append((instance_id, str(exc)))
            else:
                succeeded.append(instance_id)

        if succeeded:
            self.ctx.console.print(
                f"[green]{len(succeeded)} instancia(s) {success_verb}: {', '.join(succeeded)}[/]"
            )
        if skipped:
            self.ctx.err_console.print(f"[yellow]{len(skipped)} instancia(s) omitida(s):[/]")
            for instance_id, reason in skipped:
                self.ctx.err_console.print(f"  [yellow]{instance_id}: {reason}[/]")
        self.prompter.pause()

    def _audit_menu(self: Self) -> None:
        """Resource Audit submenu: Underutilized (Stop) / Zombie (Terminate) / Back."""
        while True:
            self._clear_and_banner()
            use_cases = build_ec2_use_cases(self.ctx)
            self._render_instance_stats(use_cases.list_instances.execute(ListInstancesRequest()))
            selected = self.prompter.select(
                "Resource Audit -- choose a view:",
                [
                    Choice(title=_UNDERUTILIZED_LABEL, value=_AUDIT_UNDERUTILIZED),
                    Choice(title=_ZOMBIE_LABEL, value=_AUDIT_ZOMBIE),
                    Separator(),
                    Choice(title="↩️  Back", value=NAV_BACK),
                ],
            )
            if selected is None or selected == NAV_BACK:
                return
            now = datetime.now(UTC)
            if selected == _AUDIT_UNDERUTILIZED:
                self._audit_act(
                    self._collect_underutilized_rows(use_cases, now=now),
                    table_title=_UNDERUTILIZED_LABEL,
                    empty_message=(
                        f"No underutilized (running > {_UNDERUTILIZED_MIN_DAYS}d) instances."
                    ),
                    checkbox_message=(
                        "Selecciona las instancias a apagar (SPACE marca, ENTER confirma):"
                    ),
                    confirm_message="¿Apagar {n} instancia(s) seleccionada(s)?",
                    confirm_yes_label="Sí, apagar seleccionadas",
                    confirm_no_label="No, dejar activas",
                    action=lambda uc, instance: uc.stop_instance.execute(
                        instance, force=False, wait=False, timeout_s=300
                    ),
                    success_verb="apagada(s)",
                )
            elif selected == _AUDIT_ZOMBIE:
                self._audit_act(
                    self._collect_zombie_rows(use_cases, now=now),
                    table_title=_ZOMBIE_LABEL,
                    empty_message=f"No zombie (stopped > {_ZOMBIE_MIN_DAYS}d) instances.",
                    checkbox_message=(
                        "Selecciona las instancias a eliminar (SPACE marca, ENTER confirma):"
                    ),
                    confirm_message=(
                        "¿TERMINAR/ELIMINAR {n} instancia(s) seleccionada(s)? "
                        "Esta acción no se puede deshacer."
                    ),
                    confirm_yes_label="Sí, TERMINAR/ELIMINAR seleccionadas",
                    confirm_no_label="No, dejar intactas",
                    action=lambda uc, instance: uc.terminate_instance.execute(
                        instance, force=False, dry_run=False, wait=False, timeout_s=300
                    ),
                    success_verb="eliminada(s)",
                )
