"""Tests for ``Ec2Flow``: search, the instance detail/manage/tags screens, launch,
and the arrow-key destructive confirmation on terminate.
"""

import base64
import io
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from aws_admin_cli.application.dto.ec2 import SetInstanceTagsRequest
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.domain.models.ec2 import Ami, Instance, InstanceState
from aws_admin_cli.infrastructure.aws.gateways.boto3_ec2_gateway import Boto3Ec2Gateway
from aws_admin_cli.presentation.tui.flows.ec2_flow import (
    _AMI_DETAIL_BACK,
    _AMI_DETAIL_CREATE_EC2,
    _AMI_DETAIL_DELETE,
    _AMI_DETAIL_EDIT_NAME,
    _AMI_DETAIL_MANAGE_TAGS,
    _AMI_MANUAL,
    _AMI_MENU_BACK,
    _AUDIT_RESOURCES,
    _AUDIT_UNDERUTILIZED,
    _AUDIT_ZOMBIE,
    _CONFIRM_BACK,
    _CONFIRM_NO,
    _CONFIRM_YES,
    _DETAIL_BACK,
    _DETAIL_EDIT_GROUPS,
    _DETAIL_EDIT_NAME,
    _DETAIL_MANAGE_TAGS,
    _DETAIL_REBOOT,
    _DETAIL_START,
    _DETAIL_STOP,
    _DETAIL_VIEW_VPC,
    _KEY_PAIR_CONTINUE,
    _NO_KEY_PAIR,
    _OWNER_ASSIGN_NO,
    _OWNER_ASSIGN_YES,
    _OWNER_CUSTOM,
    _REBOOT_BACK,
    _REBOOT_YES,
    _RUN_YES,
    _STORAGE_DEFAULT,
    _USERDATA_LOCAL,
    _USERDATA_NONE,
    _USERDATA_SUMMARY_TEMPLATE,
    _USERDATA_TEMPLATE,
    SECURITY_USERDATA_TEMPLATE,
    Ec2Flow,
    _ami_display_name,
    _ami_tags,
    _ec2_name_suggestion_from_ami,
    _instance_tags,
    _key_pair_base_name,
    _key_pair_destination_dir,
    _KeyPairSubState,
    _LaunchWizardState,
    _unique_key_pair_name,
    _WizardNav,
)
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT, Choice
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.tui.prompter import QuestionaryPrompter
from aws_admin_cli.presentation.wiring import build_ec2_use_cases
from moto import mock_aws
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from tests.fakes.prompter import FakePrompter


def _app_ctx() -> AppContext:
    return AppContext.build(Settings(profile="testprofile"))


@pytest.fixture(autouse=True)
def _single_region_global_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scope every test's global (all-region) EC2 scan down to just its own
    active-profile region.

    Every test in this file only ever creates instances in one moto region
    (whatever ``ctx.settings.region`` resolves to), so a real ~20-region
    ``DescribeRegions``/``DescribeInstances`` fan-out on every ``.menu()``
    call would be both pointless and (multiplied across this whole file)
    extremely slow. Dedicated tests for the actual multi-region behavior
    (aggregation, per-region failure skipping) re-patch ``_list_regions``
    themselves, which overrides this for that one test.
    """

    def _fake_list_regions(self: Boto3Ec2Gateway) -> list[str]:
        return [self.client_factory.settings.region]

    monkeypatch.setattr(Boto3Ec2Gateway, "_list_regions", _fake_list_regions)


@pytest.fixture(autouse=True)
def _fake_ssh_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect the CWD to an ephemeral tmp dir for every test in this file.

    Several EC2 creation flows auto-generate a Key Pair for an instance
    launched without an Owner and save its private material under
    ``keys/`` at the current working directory (see
    ``_key_pair_destination_dir``) -- without this, running this test file
    would write real ``.pem`` files into this repo's own ``keys/`` dir.
    """
    monkeypatch.chdir(tmp_path)


def _default_subnet_and_sg() -> tuple[str, str]:
    client = boto3.client("ec2", region_name="us-east-1")
    vpcs = client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    vpc_id = vpcs[0]["VpcId"]
    subnets = client.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    subnet_id = subnets[0]["SubnetId"]
    sg = client.create_security_group(
        GroupName="ec2-flow-test-sg", Description="test", VpcId=vpc_id
    )
    return subnet_id, str(sg["GroupId"])


def _default_subnet_and_three_sgs() -> tuple[str, str, str, str]:
    """Like ``_default_subnet_and_sg``, but with three real SGs to tick in Step 5.

    Mirrors what ``sg_seed.ensure_demo_security_groups`` guarantees against
    LocalStack (``web-sg``/``db-sg``/``ssh-only`` alongside the VPC default),
    so the multi-selection tests below exercise the same shape a user sees.
    """
    client = boto3.client("ec2", region_name="us-east-1")
    vpcs = client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    vpc_id = vpcs[0]["VpcId"]
    subnets = client.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    subnet_id = subnets[0]["SubnetId"]
    group_ids = [
        str(
            client.create_security_group(
                GroupName=name, Description=f"test {name}", VpcId=vpc_id
            )["GroupId"]
        )
        for name in ("multi-web-sg", "multi-db-sg", "multi-ssh-sg")
    ]
    return subnet_id, group_ids[0], group_ids[1], group_ids[2]


def _applied_security_groups(instance: dict[str, Any]) -> set[str]:
    """Every SG actually attached to ``instance``, from either place AWS reports them.

    The gateway sends security groups inside ``NetworkInterfaces[0].Groups``
    (it must -- mixing that form with top-level ``SecurityGroupIds`` is
    rejected by EC2 with ``InvalidParameterCombination``; see
    ``boto3_ec2_gateway._build_network_interface``). Real AWS then echoes
    them back in BOTH ``Instances[].SecurityGroups`` and the interface's own
    ``Groups``; moto only fills the latter. Reading the union keeps this
    assertion true against moto, LocalStack and real AWS alike, instead of
    silently passing on an empty top-level list.
    """
    from_interfaces = {
        group["GroupId"]
        for interface in instance.get("NetworkInterfaces", [])
        for group in interface.get("Groups", [])
    }
    from_instance = {group["GroupId"] for group in instance.get("SecurityGroups", [])}
    return from_interfaces | from_instance


def _seed_last_activity(ctx: AppContext, instance_id: str, *, days_ago: int) -> None:
    """Backdate an instance's simulated "last activity" clock, for audit-threshold tests."""
    from aws_admin_cli.application.services.ec2_audit_metadata import (
        SEED_LAST_ACTIVITY,
        write_instance_metadata,
    )

    instance = build_ec2_use_cases(ctx).get_instance.execute(instance_id)
    write_instance_metadata(
        ctx.resource_repository,
        instance=instance,
        profile=ctx.settings.profile,
        region=ctx.settings.region,
        updates={SEED_LAST_ACTIVITY: datetime.now(UTC) - timedelta(days=days_ago)},
    )


def _any_ami_id() -> str:
    client = boto3.client("ec2", region_name="us-east-1")
    images = client.describe_images(Owners=["amazon"])["Images"]
    return str(images[0]["ImageId"])


def _launch_raw_instance_in_region(region: str) -> str:
    """A plain, unmanaged instance launched directly via boto3 in ``region`` --
    for tests exercising the global (every-region) EC2 scan, which don't need
    the full launch wizard's ``ManagedBy`` tagging.
    """
    client = boto3.client("ec2", region_name=region)
    ami_id = client.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    response = client.run_instances(ImageId=ami_id, MinCount=1, MaxCount=1, InstanceType="t2.micro")
    return str(response["Instances"][0]["InstanceId"])


def _create_raw_ami_in_region(region: str) -> str:
    """Launch a raw instance in ``region`` and create a self-owned AMI from it directly
    via boto3 -- for tests exercising the global (every-region) AMI scan.
    """
    client = boto3.client("ec2", region_name=region)
    instance_id = _launch_raw_instance_in_region(region)
    response = client.create_image(
        InstanceId=instance_id, Name=f"test-ami-{instance_id}", NoReboot=True
    )
    return str(response["ImageId"])


def _create_test_ami(ctx: AppContext, *, tags: dict[str, str] | None = None) -> str:
    """Create a real, deregisterable AMI from a managed instance -- for AMI detail tests."""
    instance_id = _launch_managed_instance(ctx)
    ec2 = ctx.client_factory.ec2()
    tag_specs = (
        [{"ResourceType": "image", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}]
        if tags
        else []
    )
    response = ec2.create_image(
        InstanceId=instance_id,
        Name=f"test-ami-{instance_id}",
        NoReboot=True,
        TagSpecifications=tag_specs,
    )
    return str(response["ImageId"])


def _running_instance() -> Instance:
    """A minimal, in-memory RUNNING ``Instance`` -- for menu-shape tests with no AWS calls."""
    return Instance(
        instance_id="i-0123456789abcdef0",
        instance_type="t3.micro",
        state=InstanceState.RUNNING,
        image_id="ami-1111111111111111",
        subnet_id="subnet-0a1b2c03",
        launch_time=datetime(2024, 1, 1, tzinfo=UTC),
    )


def _launch_managed_instance(ctx: AppContext) -> str:
    """Launch through the flow's own machinery so the instance carries ManagedBy.

    Prompt sequence mirrors the current minimal wizard: Name, AMI (select ->
    manual entry -> AMI ID text, since moto's AMI isn't a curated alias),
    Owner (Sí/No/Back -> no IAM users exist in this test's account, so
    ``_owner_step_select`` falls straight to a free-text prompt), User Data
    (-> "Ninguno"), then the deploy confirmation. Instance Type, VPC/Subnet,
    Security Groups and Storage all resolve to their defaults without ever
    prompting. Region is not asked here -- it comes from the session's own
    context (``ctx.settings.region``).
    """
    ami_id = _any_ami_id()
    prompter = FakePrompter(
        [
            "create_instance",
            "helper-instance",  # Name
            "ami_manual",  # AMI menu -> enter manually
            ami_id,
            _OWNER_ASSIGN_YES,  # ¿Desea asignar un Propietario? -> Sí
            "devops",  # no IAM users seeded -> straight to the free-text fallback
            _USERDATA_NONE,  # User Data -> "Ninguno"
            _CONFIRM_YES,  # confirm launch
        ]
    )
    Ec2Flow(ctx, prompter).menu()
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    return str(reservations[0]["Instances"][0]["InstanceId"])


# -- Main menu shape and navigation ------------------------------------------------


def test_choices_match_the_unified_service_screen_template() -> None:
    """The service screen is one consolidated menu -- no List/Create/Delete picker.

    Guards the shared template: search, create and delete sit at the root of
    every service screen, in this order, above one divider and Back. S3, IAM
    and EC2 each assert the same shape with their own nouns.
    """
    ctx = _app_ctx()
    choices = Ec2Flow(ctx, FakePrompter())._choices()
    labels = [c.title if isinstance(c, Choice) else c.line for c in choices]
    assert labels == [
        "🔍 Search / Filter Instances",
        "💿 AMI Management",
        "+ Launch Instance",
        "❌ Terminate Instance",
        "\u2500" * 66,
        "↩️  Back",
    ]


@mock_aws
def test_menu_back_returns_back() -> None:
    assert Ec2Flow(_app_ctx(), FakePrompter([NAV_BACK])).menu() is NavAction.BACK


@mock_aws
def test_menu_exit_returns_exit() -> None:
    assert Ec2Flow(_app_ctx(), FakePrompter([NAV_EXIT])).menu() is NavAction.EXIT


# -- Listar (Resource Explorer): empty state, cancellation, and search --------------
#
# No mode picker anymore: selecting "Search" asks ONE free-text query directly
# (blank = everyone), matched against the instance's display name. There is no
# more search by ID, type, state, or tag -- those capabilities were deliberately
# removed in favor of a single unified query box (see ec2_flow.py's ``_query_instances``).


@mock_aws
def test_list_instances_when_none_exist_shows_message() -> None:
    # "search" reaches the empty-population message inside the search flow; the
    # Explorer screen itself always offers Search/Back regardless of Total.
    prompter = FakePrompter(["search"])

    action = Ec2Flow(_app_ctx(), prompter).menu()

    assert action is NavAction.STAY
    assert prompter.asked == [
        "EC2 (Instances) -- What do you want to do?"
    ]




@mock_aws
def test_search_cancelled_at_query_prompt_short_circuits() -> None:
    ctx = _app_ctx()
    _launch_managed_instance(ctx)

    prompter = FakePrompter(["search", None])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert len(prompter.asked) == 2


@mock_aws
def test_search_with_no_match_shows_message() -> None:
    ctx = _app_ctx()
    _launch_managed_instance(ctx)

    prompter = FakePrompter(["search", "i-doesnotexist"])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert len(prompter.asked) == 2


@mock_aws
def test_search_with_single_match_still_requires_explicit_pick() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)

    prompter = FakePrompter(
        ["search", "helper", instance_id, _DETAIL_BACK]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    # menu, explorer, query, picker, detail-screen action, explorer again --
    # picker was mandatory, and the Explorer's own loop needs its own exit.
    assert len(prompter.asked) == 4


@mock_aws
def test_search_with_blank_query_lists_everyone() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)

    prompter = FakePrompter(["search", "", instance_id, _DETAIL_BACK])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY


# -- Global (every-region) fetch: List/Filter Instances + main menu stats -----------
#
# ``_single_region_global_scan`` (this file's autouse fixture) scopes every other
# test's global scan down to one region for speed; these tests re-patch
# ``_list_regions`` themselves to actually exercise more than one.


@mock_aws
def test_menu_stats_aggregate_instances_across_every_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Boto3Ec2Gateway, "_list_regions", lambda self: ["us-east-1", "us-west-2"])
    ctx = _app_ctx()
    _launch_raw_instance_in_region("us-east-1")
    _launch_raw_instance_in_region("us-west-2")
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, err_console=err_console)

    action = Ec2Flow(ctx, FakePrompter([NAV_EXIT])).menu()

    assert action is NavAction.EXIT
    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "Instances: 2 Total  |  2 Running  |  0 Stopped" in output


@mock_aws
def test_search_filter_instances_table_shows_the_region_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Boto3Ec2Gateway, "_list_regions", lambda self: ["us-east-1", "us-west-2"])
    ctx = _app_ctx()
    _launch_raw_instance_in_region("us-east-1")
    _launch_raw_instance_in_region("us-west-2")
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    prompter = FakePrompter(["search", "", NAV_BACK])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "Region" in output
    assert "us-east-1" in output
    assert "us-west-2" in output


@mock_aws
def test_picking_a_foreign_region_instance_shows_its_own_region_in_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A global-search pick outside the active session region opens its detail
    screen normally (routed through ITS OWN region's client) instead of being
    blocked -- and the active session (header/creation-target) region must
    never change as a result.
    """
    monkeypatch.setattr(Boto3Ec2Gateway, "_list_regions", lambda self: ["us-east-1", "us-west-2"])
    ctx = _app_ctx()  # active session region: us-east-1
    foreign_instance_id = _launch_raw_instance_in_region("us-west-2")
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    prompter = FakePrompter(["search", "", foreign_instance_id, _DETAIL_BACK])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert ctx.settings.region == "us-east-1"
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "us-west-2" in output


@mock_aws
def test_stopping_a_foreign_region_instance_routes_to_its_own_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An action (Stop) taken from a foreign-region instance's detail screen must
    land against THAT instance's own region -- never the active session region,
    which stays reserved as the creation target and must never change either.
    """
    monkeypatch.setattr(Boto3Ec2Gateway, "_list_regions", lambda self: ["us-east-1", "us-west-2"])
    ctx = _app_ctx()  # active session region: us-east-1
    foreign_instance_id = _launch_raw_instance_in_region("us-west-2")
    # moto instances go straight to `running`.

    prompter = FakePrompter(
        ["search", "", foreign_instance_id, _DETAIL_STOP, _RUN_YES, _DETAIL_BACK]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert ctx.settings.region == "us-east-1"

    foreign_client = ctx.client_factory.ec2(region="us-west-2")
    state = foreign_client.describe_instances(InstanceIds=[foreign_instance_id])["Reservations"][
        0
    ]["Instances"][0]["State"]["Name"]
    assert state in ("stopping", "stopped")


@mock_aws
def test_instance_detail_entry_redraws_the_banner() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, err_console=err_console)

    prompter = FakePrompter(["search", "", instance_id, _DETAIL_BACK])
    Ec2Flow(ctx, prompter).menu()

    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "AWS CLOUD ADMIN CLI v1.0" in output


# -- Detail screen: flat action menu -----------------------------------------------


def test_detail_menu_offers_only_stop_and_reboot_while_running() -> None:
    """The flat menu is state-adaptive: RUNNING shows Stop (+Reboot), never Start."""
    instance = _running_instance()
    labels = [
        c.value if isinstance(c, Choice) else c.line for c in Ec2Flow._detail_menu_choices(instance)
    ]
    assert _DETAIL_STOP in labels
    assert _DETAIL_REBOOT in labels
    assert _DETAIL_START not in labels
    assert _DETAIL_BACK in labels


def test_detail_menu_offers_only_start_while_stopped() -> None:
    """STOPPED shows Start, never Stop/Reboot (can_reboot is RUNNING-only too)."""
    instance = _running_instance().model_copy(update={"state": InstanceState.STOPPED})
    values = [c.value for c in Ec2Flow._detail_menu_choices(instance) if isinstance(c, Choice)]
    assert _DETAIL_START in values
    assert _DETAIL_STOP not in values
    assert _DETAIL_REBOOT not in values


@mock_aws
def test_detail_screen_stop_then_start_roundtrip() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)
    # moto instances go straight to `running`.

    prompter = FakePrompter(
        [
            "search",
            "",
            instance_id,
            _DETAIL_STOP,
            _RUN_YES,  # confirm stop
            _DETAIL_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]
    assert state in ("stopping", "stopped")


@mock_aws
def test_detail_screen_stop_confirm_back_does_not_call_stop_instances() -> None:
    """"<- Back" on the Stop confirmation skips the action, same as "No"."""
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)

    prompter = FakePrompter(
        ["search", "", instance_id, _DETAIL_STOP, _DETAIL_BACK, _DETAIL_BACK]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]
    assert state == "running"


@mock_aws
def test_detail_screen_edit_name_blank_enter_acts_as_back() -> None:
    """An empty ENTER on the rename prompt is a no-op Back, never a blank Name tag."""
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)

    prompter = FakePrompter(["search", "", instance_id, _DETAIL_EDIT_NAME, "", _DETAIL_BACK])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    instance = build_ec2_use_cases(ctx).get_instance.execute(instance_id)
    assert instance.display_name == "helper-instance"


@mock_aws
def test_detail_screen_edit_name_applies_the_new_name_tag() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)

    prompter = FakePrompter(
        ["search", "", instance_id, _DETAIL_EDIT_NAME, "renamed-instance", _DETAIL_BACK]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    instance = build_ec2_use_cases(ctx).get_instance.execute(instance_id)
    assert instance.display_name == "renamed-instance"


@mock_aws
def test_detail_screen_edit_groups_applies_the_checked_ids_via_modify_instance_attribute() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)
    instance = build_ec2_use_cases(ctx).get_instance.execute(instance_id)
    assert instance.vpc_id is not None
    ec2 = ctx.client_factory.ec2()
    extra_sg = ec2.create_security_group(
        GroupName="extra-detail-sg", Description="test", VpcId=instance.vpc_id
    )["GroupId"]

    prompter = FakePrompter(
        [
            "search",
            "",
            instance_id,
            _DETAIL_EDIT_GROUPS,
            [*instance.security_group_ids, extra_sg],  # checkbox: keep existing + add the new one
            _DETAIL_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    refreshed = build_ec2_use_cases(ctx).get_instance.execute(instance_id)
    assert extra_sg in refreshed.security_group_ids


@mock_aws
def test_detail_screen_edit_groups_rejects_an_empty_selection() -> None:
    """An empty checkbox confirm is refused, not applied as 'no security groups'."""
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)
    before = build_ec2_use_cases(ctx).get_instance.execute(instance_id)

    prompter = FakePrompter(
        ["search", "", instance_id, _DETAIL_EDIT_GROUPS, [], _DETAIL_BACK]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    after = build_ec2_use_cases(ctx).get_instance.execute(instance_id)
    assert set(after.security_group_ids) == set(before.security_group_ids)


@mock_aws
def test_detail_screen_view_vpc_is_read_only_and_returns_to_the_menu() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)

    prompter = FakePrompter(["search", "", instance_id, _DETAIL_VIEW_VPC, _DETAIL_BACK])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY


@mock_aws
def test_detail_screen_manage_tags_add_then_delete_it() -> None:
    """The flat menu's "Tags" entry still reaches the existing Add/Delete tag screen."""
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)

    prompter = FakePrompter(
        [
            "search",
            "",
            instance_id,
            _DETAIL_MANAGE_TAGS,
            "tag_add",
            "Environment",
            "staging",
            _CONFIRM_YES,  # confirm apply
            NAV_BACK,  # back from the tags screen
            _DETAIL_BACK,  # back from the instance detail screen
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()
    assert action is NavAction.STAY

    ec2 = ctx.client_factory.ec2()
    tags = {
        t["Key"]: t["Value"]
        for t in ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0][
            "Instances"
        ][0]["Tags"]
    }
    assert tags["Environment"] == "staging"

    prompter2 = FakePrompter(
        [
            "search",
            "",
            instance_id,
            _DETAIL_MANAGE_TAGS,
            "tag_delete",
            "Environment",  # picker
            _CONFIRM_YES,  # confirm delete
            NAV_BACK,  # back from the tags screen
            _DETAIL_BACK,  # back from the instance detail screen
        ]
    )
    action2 = Ec2Flow(ctx, prompter2).menu()
    assert action2 is NavAction.STAY

    tags_after = {
        t["Key"]: t["Value"]
        for t in ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0][
            "Instances"
        ][0]["Tags"]
    }
    assert "Environment" not in tags_after


@mock_aws
@pytest.mark.parametrize("declining_choice", [_CONFIRM_NO, _CONFIRM_BACK])
def test_detail_screen_delete_tag_confirm_no_or_back_keeps_the_tag(declining_choice: str) -> None:
    """The delete confirm is now a Sí/No/<- Back select -- declining it never deletes."""
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)
    build_ec2_use_cases(ctx).set_instance_tags.execute(
        SetInstanceTagsRequest(instance_id=instance_id, tags={"Environment": "staging"})
    )

    prompter = FakePrompter(
        [
            "search",
            "",
            instance_id,
            _DETAIL_MANAGE_TAGS,
            "tag_delete",
            "Environment",  # picker
            declining_choice,  # decline the delete
            NAV_BACK,  # back from the tags screen
            _DETAIL_BACK,  # back from the instance detail screen
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    tags = {
        t["Key"]: t["Value"]
        for t in ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0][
            "Instances"
        ][0]["Tags"]
    }
    assert tags["Environment"] == "staging"


@mock_aws
def test_detail_screen_add_tag_confirm_no_does_not_apply_it() -> None:
    """The Add/Update confirm select also gates the write -- "No" applies nothing."""
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)

    prompter = FakePrompter(
        [
            "search",
            "",
            instance_id,
            _DETAIL_MANAGE_TAGS,
            "tag_add",
            "Environment",
            "staging",
            _CONFIRM_NO,  # decline applying
            NAV_BACK,
            _DETAIL_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    tags = {
        t["Key"]: t["Value"]
        for t in ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0][
            "Instances"
        ][0]["Tags"]
    }
    assert "Environment" not in tags


def test_instance_tags_excludes_the_internal_created_at_bookkeeping_tag() -> None:
    """``CreatedAt`` is real tag data but never a "user" tag -- it gets its own detail row."""
    instance = _running_instance().model_copy(
        update={
            "tags": [
                {"Key": "Name", "Value": "my-app"},
                {"Key": "Environment", "Value": "dev"},
                {"Key": "CreatedAt", "Value": "2026-09-02T12:00:00+00:00"},
            ]
        }
    )
    tags = _instance_tags(instance)
    assert "CreatedAt" not in tags
    assert tags == {"Name": "my-app", "Environment": "dev"}


@mock_aws
def test_render_instance_detail_shows_created_at_as_its_own_row_not_in_tags() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    Ec2Flow(ctx, FakePrompter())._render_instance_detail(instance_id)

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "Created At" in output
    # The raw tag row must never show "CreatedAt" as one of the listed Tags.
    tags_section = output.split("Created At")[1]
    assert "CreatedAt" not in tags_section


@mock_aws
def test_render_instance_detail_shows_owner_row_and_excludes_it_from_the_tags_block() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)
    ctx.client_factory.ec2().create_tags(
        Resources=[instance_id], Tags=[{"Key": "Owner", "Value": "cloud-team"}]
    )
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    Ec2Flow(ctx, FakePrompter())._render_instance_detail(instance_id)

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "Owner" in output
    assert "cloud-team" in output
    # The Tags block must not repeat "Owner=..." now that it has its own row.
    tags_section = output.split("Owner")[-1]
    assert "Owner=" not in tags_section


@mock_aws
def test_render_instance_detail_shows_the_active_sessions_region_below_instance_id() -> None:
    ctx = _app_ctx()  # profile="testprofile" -> the config default region, us-east-1
    instance_id = _launch_managed_instance(ctx)
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    Ec2Flow(ctx, FakePrompter())._render_instance_detail(instance_id)

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "Region" in output
    assert "us-east-1" in output
    # "Region" must be the row right after "Instance ID", not just present anywhere.
    # splitlines()[0] is the rest of the "Instance ID" row itself; [1] is the next row.
    after_instance_id = output.split("Instance ID")[1]
    next_row = after_instance_id.lstrip().splitlines()[1]
    assert next_row.strip().lstrip("│").strip().startswith("Region")


@mock_aws
def test_render_instance_detail_prints_exactly_one_blank_line_before_the_subtitle() -> None:
    """Exactly one blank line must separate the header dashboard box from the
    "Instance: <id>" subtitle -- not zero, not two.

    The banner prints to ``err_console`` and the detail table (whose title is
    the subtitle) prints to ``console``; both are pointed at the same buffer
    here so their real terminal interleaving -- banner, blank line, table --
    is observable as one ordered stream.
    """
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)
    shared_file = io.StringIO()
    console = Console(file=shared_file, force_terminal=False, width=200)
    err_console = Console(file=shared_file, force_terminal=False, width=200)
    ctx = replace(ctx, console=console, err_console=err_console)

    Ec2Flow(ctx, FakePrompter())._render_instance_detail(instance_id)

    output = shared_file.getvalue()
    banner_end = output.rindex("╰")  # the header panel's bottom border
    after_banner = output[banner_end:].splitlines()[1:]  # drop the border line itself
    assert after_banner[0].strip() == ""
    assert after_banner[1].strip() != ""
    assert f"Instance: {instance_id}" in after_banner[1]


# -- AMI Management: Search -> detail -> Name/Tags/Deregister ------------------------


@mock_aws
def test_ami_search_blank_query_reaches_the_detail_screen() -> None:
    """Enter on a blank query lists every AMI (own + LocalStack's), then a pick opens detail."""
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx, tags={"Environment": "staging"})
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    prompter = FakePrompter(
        ["ami_management", "SEARCH_AMIs", "", ami_id, _AMI_DETAIL_BACK, _AMI_MENU_BACK]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert ami_id in output
    assert "Architecture" in output
    assert "Root Device" in output
    assert "Environment: staging" in output


@mock_aws
def test_ami_detail_shows_owner_row_and_excludes_it_from_the_tags_block() -> None:
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx, tags={"Owner": "sysadmin", "Environment": "staging"})
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    prompter = FakePrompter(
        ["ami_management", "SEARCH_AMIs", "", ami_id, _AMI_DETAIL_BACK, _AMI_MENU_BACK]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "sysadmin" in output
    tags_section = output.split("Owner")[-1]
    assert "Owner=" not in tags_section
    assert "Environment: staging" in output


@mock_aws
def test_ami_detail_edit_name_blank_enter_acts_as_back() -> None:
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_EDIT_NAME,
            "",
            _AMI_DETAIL_BACK,
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ami = build_ec2_use_cases(ctx).get_ami.execute(ami_id)
    assert "Name" not in _ami_tags(ami)


@mock_aws
def test_ami_detail_edit_name_sets_the_name_tag_not_the_native_name() -> None:
    """AWS never lets you rename an AMI's native ``Name`` -- this writes a ``Name`` tag instead."""
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)
    original_native_name = build_ec2_use_cases(ctx).get_ami.execute(ami_id).name

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_EDIT_NAME,
            "friendly-ami-name",
            _AMI_DETAIL_BACK,
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ami = build_ec2_use_cases(ctx).get_ami.execute(ami_id)
    assert ami.name == original_native_name  # the native field never changes
    assert _ami_tags(ami)["Name"] == "friendly-ami-name"


@mock_aws
def test_ami_detail_edit_name_updates_the_name_row_without_duplicating_it_in_tags() -> None:
    """BUG FIX: editing an AMI's Name must update its own detail row, not just a secondary tag.

    Before the fix, the "Name" row always showed AWS's frozen native Image
    Name, and the new ``Name`` tag only ever surfaced as an extra row inside
    the generic Tags block -- so a rename looked like it landed on the wrong
    field. Now the Name row itself reflects the tag, and it is never repeated
    below in Tags.
    """
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_EDIT_NAME,
            "friendly-ami-name",
            _AMI_DETAIL_BACK,
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    name_row = next(line for line in output.splitlines() if "Name" in line and "friendly" in line)
    assert "friendly-ami-name" in name_row
    tags_section = output.split("friendly-ami-name")[-1]
    assert "Name: friendly-ami-name" not in tags_section
    assert "Name=friendly-ami-name" not in tags_section


def test_ami_display_name_prefers_the_name_tag_over_the_native_image_name() -> None:
    ami = Ami(
        image_id="ami-1111111111111111",
        name="native-registered-name",
        tags=[{"Key": "Name", "Value": "friendly-tag-name"}],
        creation_date=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert _ami_display_name(ami) == "friendly-tag-name"


def test_ami_display_name_falls_back_to_native_name_then_image_id() -> None:
    with_native = Ami(
        image_id="ami-2222222222222222",
        name="native-only",
        creation_date=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert _ami_display_name(with_native) == "native-only"

    unnamed = Ami(image_id="ami-3333333333333333", creation_date=datetime(2024, 1, 1, tzinfo=UTC))
    assert _ami_display_name(unnamed) == "ami-3333333333333333"


def test_ec2_name_suggestion_strips_an_existing_ami_prefix_instead_of_doubling_it() -> None:
    """BUG FIX: "AMI-Sales" must suggest "ec2-Sales", never "ec2-AMI-Sales"."""
    ami = Ami(
        image_id="ami-4444444444444444",
        name="AMI-Sales",
        creation_date=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert _ec2_name_suggestion_from_ami(ami) == "ec2-Sales"


def test_ec2_name_suggestion_without_an_ami_prefix_just_adds_ec2_prefix() -> None:
    ami = Ami(
        image_id="ami-5555555555555555",
        name="Sales",
        creation_date=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert _ec2_name_suggestion_from_ami(ami) == "ec2-Sales"


@mock_aws
def test_ami_detail_manage_tags_add_then_delete_it() -> None:
    """The AMI detail's "Tags" entry reuses the same Add/Delete screen instances use."""
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_MANAGE_TAGS,
            "tag_add",
            "Environment",
            "staging",
            _CONFIRM_YES,
            NAV_BACK,  # back from the tags screen
            _AMI_DETAIL_BACK,  # back from the AMI detail screen
            _AMI_MENU_BACK,  # back from the AMI Management menu
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ami = build_ec2_use_cases(ctx).get_ami.execute(ami_id)
    assert _ami_tags(ami)["Environment"] == "staging"

    prompter2 = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_MANAGE_TAGS,
            "tag_delete",
            "Environment",
            _CONFIRM_YES,
            NAV_BACK,
            _AMI_DETAIL_BACK,
            _AMI_MENU_BACK,
        ]
    )
    action2 = Ec2Flow(ctx, prompter2).menu()

    assert action2 is NavAction.STAY
    ami_after = build_ec2_use_cases(ctx).get_ami.execute(ami_id)
    assert "Environment" not in _ami_tags(ami_after)


@mock_aws
def test_ami_detail_deregister_confirm_yes_calls_boto3_deregister_image() -> None:
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_DELETE,
            _CONFIRM_YES,
            _AMI_MENU_BACK,  # back from the AMI Management menu
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    remaining = ec2.describe_images(ImageIds=[ami_id])["Images"]
    assert remaining == []


@mock_aws
@pytest.mark.parametrize("declining_choice", [_CONFIRM_NO, _CONFIRM_BACK])
def test_ami_detail_deregister_confirm_no_or_back_keeps_the_ami(declining_choice: str) -> None:
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_DELETE,
            declining_choice,
            _AMI_DETAIL_BACK,
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    remaining = ec2.describe_images(ImageIds=[ami_id])["Images"]
    assert len(remaining) == 1


@mock_aws
def test_ami_search_filters_to_self_owned_amis_only() -> None:
    """``Owners=['self']`` -- Amazon's own public catalog must never show up here."""
    ctx = _app_ctx()
    self_ami_id = _create_test_ami(ctx)
    public_ami_id = _any_ami_id()  # one of moto's "amazon"-owned public AMIs

    amis = Ec2Flow(ctx, FakePrompter([""]))._query_amis()

    assert amis is not None
    found_ids = {ami.image_id for ami in amis}
    assert self_ami_id in found_ids
    assert public_ami_id not in found_ids


@mock_aws
def test_ami_search_scans_every_region_and_stamps_each_amis_own_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AMI listing/search is global (like the instance "Search / Filter" screen): an
    AMI living in a region other than the active session region must still show up,
    tagged with the region it actually lives in.
    """
    monkeypatch.setattr(Boto3Ec2Gateway, "_list_regions", lambda self: ["us-east-1", "us-west-2"])
    ctx = _app_ctx()  # active session region: us-east-1
    local_ami_id = _create_test_ami(ctx)
    foreign_ami_id = _create_raw_ami_in_region("us-west-2")

    amis = Ec2Flow(ctx, FakePrompter([""]))._query_amis()

    assert amis is not None
    regions_by_id = {ami.image_id: ami.region for ami in amis}
    assert regions_by_id.get(local_ami_id) == "us-east-1"
    assert regions_by_id.get(foreign_ami_id) == "us-west-2"
    assert ctx.settings.region == "us-east-1"


@mock_aws
def test_quick_launch_confirm_no_creates_nothing() -> None:
    """"No, cancelar" on the final confirm is the one genuine full-abort in this wizard."""
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)
    ec2 = ctx.client_factory.ec2()
    vpc_id = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0][
        "VpcId"
    ]
    sg_id = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": ["default"]},
        ]
    )["SecurityGroups"][0]["GroupId"]
    instances_before = len(
        [i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]
    )

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_CREATE_EC2,
            "declined-instance",
            _OWNER_ASSIGN_YES,  # ¿Desea asignar un Propietario? -> Sí
            "devops",  # no IAM users seeded -> straight to the free-text fallback
            vpc_id,
            [sg_id],
            _STORAGE_DEFAULT,
            _CONFIRM_NO,
            _AMI_DETAIL_BACK,
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    instances_after = len(
        [i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]
    )
    assert instances_after == instances_before


@mock_aws
def test_quick_launch_confirm_back_returns_to_storage_step_not_a_full_abort() -> None:
    """BUG FIX: "<- Back" at Storage/Confirmación must retreat one step, never fall through
    to launching and never abort the whole wizard -- it lands back on the Storage select,
    and the user can still complete the launch afterward.
    """
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)
    ec2 = ctx.client_factory.ec2()
    vpc_id = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0][
        "VpcId"
    ]
    sg_id = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": ["default"]},
        ]
    )["SecurityGroups"][0]["GroupId"]

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_CREATE_EC2,
            "confirm-back-instance",
            _OWNER_ASSIGN_NO,
            _KEY_PAIR_CONTINUE,
            vpc_id,
            [sg_id],
            _STORAGE_DEFAULT,  # STORAGE (forward)
            _CONFIRM_BACK,  # CONFIRM -> Back -> must land on STORAGE, not abort
            _STORAGE_DEFAULT,  # STORAGE (re-shown) -> forward again
            _CONFIRM_YES,  # CONFIRM (re-shown) -> launch
            _AMI_DETAIL_BACK,
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ec2.describe_instances()["Reservations"]
    assert any(
        {t["Key"]: t["Value"] for t in i.get("Tags", [])}.get("Name") == "confirm-back-instance"
        for r in reservations
        for i in r["Instances"]
    )


# -- Quick Launch: optional Owner (Sí/No/Back), then a filter-first IAM picker -------


@mock_aws
def test_quick_launch_owner_declined_leaves_no_owner_tag() -> None:
    """"No, continuar sin Owner" skips the IAM step entirely and writes no Owner tag."""
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)
    ec2 = ctx.client_factory.ec2()
    vpc_id = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0][
        "VpcId"
    ]
    sg_id = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": ["default"]},
        ]
    )["SecurityGroups"][0]["GroupId"]

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_CREATE_EC2,
            "no-owner-instance",
            _OWNER_ASSIGN_NO,  # ¿Desea asignar un Propietario? -> No
            _KEY_PAIR_CONTINUE,  # KEY_PAIR: accept the auto-generated key
            vpc_id,
            [sg_id],
            _STORAGE_DEFAULT,
            _CONFIRM_YES,
            _AMI_DETAIL_BACK,
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ec2.describe_instances()["Reservations"]
    new_instance = next(
        i
        for r in reservations
        for i in r["Instances"]
        if {t["Key"]: t["Value"] for t in i.get("Tags", [])}.get("Name") == "no-owner-instance"
    )
    tags = {t["Key"]: t["Value"] for t in new_instance["Tags"]}
    assert "Owner" not in tags
    # An ownerless ("huérfana") instance must get an auto-generated Key Pair instead.
    assert tags["KeyPair"].startswith("key-no-owner-instance")
    assert new_instance.get("KeyName") == tags["KeyPair"]
    pem_path = Path.cwd() / "keys" / "ec2" / f"{tags['KeyPair']}.pem"
    assert pem_path.exists()
    assert oct(pem_path.stat().st_mode)[-3:] == "600"


@mock_aws
def test_quick_launch_owner_confirm_back_returns_to_name_step_not_a_full_abort() -> None:
    """BUG FIX: "<- Back" at "¿Desea asignar Owner?" must retreat to Instance Name,
    never fall through/abort straight to the AMI's detail screen.
    """
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)
    ec2 = ctx.client_factory.ec2()
    instances_before = len(
        [i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]
    )

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_CREATE_EC2,
            "first-name-attempt",  # NAME (forward)
            NAV_BACK,  # OWNER_CONFIRM -> Back -> must land on NAME, not abort
            "",  # NAME (re-shown): blank Enter -> now genuinely exit the whole wizard
            _AMI_DETAIL_BACK,
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    instances_after = len(
        [i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]
    )
    assert instances_after == instances_before


@mock_aws
def test_quick_launch_back_navigation_follows_the_exact_step_mapping() -> None:
    """Walks every question forward, then backs out one at a time in strict reverse order,
    proving each "<- Back" lands on its exact required predecessor -- never a fall-through
    to the next step, never a jump past the immediate parent -- all the way out to exiting
    the wizard entirely. If any single transition in the mapping were wrong, the wrong
    prompt would receive the next scripted answer and either this assertion or the
    strict-queue ``FakePrompter`` itself would fail.
    """
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)
    ctx.client_factory.iam().create_user(UserName="alice")
    ec2 = ctx.client_factory.ec2()
    vpc_id = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0][
        "VpcId"
    ]
    sg_id = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": ["default"]},
        ]
    )["SecurityGroups"][0]["GroupId"]
    instances_before = len(
        [i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]
    )

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_CREATE_EC2,
            "step-mapping-instance",  # NAME (forward)
            _OWNER_ASSIGN_YES,  # OWNER_CONFIRM (forward)
            "ali",  # OWNER_FILTER (forward)
            "alice",  # OWNER_SELECT (forward)
            vpc_id,  # VPC (forward)
            [sg_id],  # SECURITY_GROUPS (forward)
            _STORAGE_DEFAULT,  # STORAGE (forward)
            _CONFIRM_BACK,  # CONFIRM -> Back -> must land on STORAGE
            NAV_BACK,  # STORAGE -> Back -> must land on SECURITY_GROUPS
            [NAV_BACK],  # SECURITY_GROUPS checkbox -> Back -> must land on VPC
            NAV_BACK,  # VPC -> Back -> must land on OWNER_SELECT (wants_owner was True)
            NAV_BACK,  # OWNER_SELECT -> Back -> must land on OWNER_FILTER
            None,  # OWNER_FILTER (Ctrl+C) -> Back -> must land on OWNER_CONFIRM
            NAV_BACK,  # OWNER_CONFIRM -> Back -> must land on NAME
            "",  # NAME (blank) -> exits the whole wizard, back to the AMI detail menu
            _AMI_DETAIL_BACK,
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    instances_after = len(
        [i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]
    )
    assert instances_after == instances_before  # backed all the way out -- nothing launched


@mock_aws
def test_quick_launch_back_then_forward_preserves_earlier_answers_and_allows_changes() -> None:
    """Backing up two steps (VPC -> OWNER_CONFIRM) and changing the Owner decision must
    neither lose the Name entered earlier nor prevent completing the launch afterward.
    """
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)
    ec2 = ctx.client_factory.ec2()
    vpc_id = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0][
        "VpcId"
    ]
    sg_id = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": ["default"]},
        ]
    )["SecurityGroups"][0]["GroupId"]

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_CREATE_EC2,
            "preserved-name-instance",  # NAME
            _OWNER_ASSIGN_NO,  # OWNER_CONFIRM: No (first attempt) -> routes to KEY_PAIR
            NAV_BACK,  # KEY_PAIR -> Back -> must land back on OWNER_CONFIRM
            _OWNER_ASSIGN_YES,  # OWNER_CONFIRM (re-shown): change of mind -> Sí
            # No IAM users seeded -> OWNER_FILTER is skipped entirely; OWNER_SELECT's own
            # free-text fallback IS this account's whole Owner sub-flow.
            "devops",  # OWNER_SELECT: no users -> free-text fallback
            vpc_id,  # VPC (forward again)
            [sg_id],  # SECURITY_GROUPS
            _STORAGE_DEFAULT,  # STORAGE
            _CONFIRM_YES,  # CONFIRM
            _AMI_DETAIL_BACK,
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ec2.describe_instances()["Reservations"]
    new_instance = next(
        i
        for r in reservations
        for i in r["Instances"]
        if {t["Key"]: t["Value"] for t in i.get("Tags", [])}.get("Name")
        == "preserved-name-instance"
    )
    tags = {t["Key"]: t["Value"] for t in new_instance["Tags"]}
    assert tags["Owner"] == "devops"  # the changed-mind Owner decision took effect
    # No Key Pair should linger from the earlier "No" pass through KEY_PAIR.
    assert tags["KeyPair"] == _NO_KEY_PAIR
    assert not new_instance.get("KeyName")


def test_ami_menu_has_exactly_search_create_and_back() -> None:
    labels_and_values = [
        (c.title, c.value) if isinstance(c, Choice) else (c.line, None)
        for c in Ec2Flow(_app_ctx(), FakePrompter())._ami_menu_choices()
    ]
    assert labels_and_values == [
        ("Search AMIs (Buscar y gestionar)", "SEARCH_AMIs"),
        ("Create AMI from Instance (Crear imagen)", "CREATE_AMI"),
        ("─" * 66, None),
        ("↩️  Back", "BACK"),
    ]


# -- AMI stats: Total / Available / Private counts -----------------------------------


@mock_aws
def test_ami_stats_shows_total_available_and_private_counts() -> None:
    """A freshly created AMI is both ``available`` and self-owned (private)."""
    ctx = _app_ctx()
    _create_test_ami(ctx)
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, err_console=err_console)

    action = Ec2Flow(ctx, FakePrompter(["ami_management", _AMI_MENU_BACK])).menu()

    assert action is NavAction.STAY
    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "AMIs: 1 Total  |  1 Available 🟢  |  1 Private 🔒" in output


@mock_aws
def test_ami_stats_shows_the_empty_state_when_the_account_owns_no_amis() -> None:
    ctx = _app_ctx()
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, err_console=err_console)

    action = Ec2Flow(ctx, FakePrompter(["ami_management", _AMI_MENU_BACK])).menu()

    assert action is NavAction.STAY
    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "AMIs: 0 Total  |  0 Available 🟢  |  0 Private 🔒" in output


@mock_aws
def test_ami_detail_create_ec2_launches_a_real_instance_not_another_ami() -> None:
    """BUG FIX: "Create EC2 from this AMI" must call run_instances, never create_image."""
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)  # also launches+seeds a subnet/SG in the default VPC
    ec2 = ctx.client_factory.ec2()
    amis_before = {i["ImageId"] for i in ec2.describe_images(Owners=["self"])["Images"]}
    vpc_id = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0][
        "VpcId"
    ]
    sg_id = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": ["default"]},
        ]
    )["SecurityGroups"][0]["GroupId"]

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_CREATE_EC2,
            "from-ami-instance",  # Quick Launch (a): name
            _OWNER_ASSIGN_YES,  # Quick Launch (a): ¿Desea asignar un Propietario? -> Sí
            "devops",  # no IAM users seeded -> straight to the free-text fallback
            vpc_id,  # Quick Launch (b): VPC
            [sg_id],  # Quick Launch (b): Security Groups checkbox
            _STORAGE_DEFAULT,  # Quick Launch (c): storage
            _CONFIRM_YES,  # final confirm: "Sí, desplegar EC2"
            _AMI_DETAIL_BACK,  # back from the (unchanged) AMI detail screen
            _AMI_MENU_BACK,  # back from the AMI Management menu
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ec2.describe_instances()["Reservations"]
    instances = [i for r in reservations for i in r["Instances"]]
    # _create_test_ami's own setup already launched one instance ("helper-instance");
    # this action must have launched a SECOND, real one from the picked AMI.
    assert len(instances) == 2

    def _name(instance: dict[str, Any]) -> str | None:
        return {t["Key"]: t["Value"] for t in instance["Tags"]}.get("Name")

    new_instance = next(i for i in instances if _name(i) == "from-ami-instance")
    assert new_instance["ImageId"] == ami_id
    new_tags = {t["Key"]: t["Value"] for t in new_instance["Tags"]}
    assert new_tags["Owner"] == "devops"

    # No new AMI was registered as a side effect of this action.
    amis_after = {i["ImageId"] for i in ec2.describe_images(Owners=["self"])["Images"]}
    assert amis_after == amis_before


@mock_aws
def test_ami_create_from_instance_applies_default_name_owner_and_reboot_mapping() -> None:
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    # Launch a source instance via the ordinary minimal flow, named "web-server".
    prompter0 = FakePrompter(
        [
            "create_instance",
            "web-server",
            "ami_manual",
            ami_id,
            _OWNER_ASSIGN_YES,  # ¿Desea asignar un Propietario? -> Sí
            "devops",  # no IAM users seeded -> straight to the free-text fallback
            _USERDATA_NONE,
            _CONFIRM_YES,
        ]
    )
    Ec2Flow(ctx, prompter0).menu()
    source_instance_id = str(
        ctx.client_factory.ec2().describe_instances()["Reservations"][0]["Instances"][0][
            "InstanceId"
        ]
    )

    prompter = FakePrompter(
        [
            "ami_management",
            "CREATE_AMI",
            "",  # _filter_and_pick_instance: blank query -> everyone
            source_instance_id,
            "AMI-web-server",  # accepting the suggested "AMI-[NombreDeLaEC2]" default
            "devops",  # Owner preset
            _REBOOT_YES,  # "Sí, apagar temporalmente para copia segura" -> NoReboot=False
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    images = ec2.describe_images(Owners=["self"])["Images"]
    created = next(i for i in images if i["Name"] == "AMI-web-server")
    tags = {t["Key"]: t["Value"] for t in created.get("Tags", [])}
    assert tags["Owner"] == "devops"


@mock_aws
def test_ami_create_from_instance_reboot_back_aborts_without_creating_an_ami() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)
    ec2 = ctx.client_factory.ec2()
    before = {i["ImageId"] for i in ec2.describe_images(Owners=["self"])["Images"]}

    prompter = FakePrompter(
        [
            "ami_management",
            "CREATE_AMI",
            "",
            instance_id,
            "AMI-helper-instance",
            "devops",
            _REBOOT_BACK,
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    after = {i["ImageId"] for i in ec2.describe_images(Owners=["self"])["Images"]}
    assert after == before


@mock_aws
def test_ami_create_from_instance_routes_to_the_source_instances_own_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Picking a foreign-region instance for "Create AMI from Instance" must create the
    image in THAT instance's own region, never the active session region.
    """
    monkeypatch.setattr(Boto3Ec2Gateway, "_list_regions", lambda self: ["us-east-1", "us-west-2"])
    ctx = _app_ctx()  # active session region: us-east-1
    foreign_instance_id = _launch_raw_instance_in_region("us-west-2")

    prompter = FakePrompter(
        [
            "ami_management",
            "CREATE_AMI",
            "",  # global instance picker: blank query -> everyone
            foreign_instance_id,
            "AMI-from-west",
            "devops",
            _REBOOT_YES,
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert ctx.settings.region == "us-east-1"

    foreign_images = ctx.client_factory.ec2(region="us-west-2").describe_images(Owners=["self"])[
        "Images"
    ]
    created = next(i for i in foreign_images if i["Name"] == "AMI-from-west")
    tags = {t["Key"]: t["Value"] for t in created.get("Tags", [])}
    assert tags["Owner"] == "devops"

    home_image_names = {
        i["Name"] for i in ctx.client_factory.ec2().describe_images(Owners=["self"])["Images"]
    }
    assert "AMI-from-west" not in home_image_names


# -- Resource Audit: Underutilized (Stop) / Zombie (Terminate) -----------------------


@mock_aws
def test_audit_underutilized_with_no_candidates_shows_message_and_returns() -> None:
    ctx = _app_ctx()

    prompter = FakePrompter([_AUDIT_RESOURCES, _AUDIT_UNDERUTILIZED, NAV_BACK])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY


@mock_aws
def test_audit_underutilized_confirm_yes_stops_the_selected_instances() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)  # RUNNING
    _seed_last_activity(ctx, instance_id, days_ago=20)  # > 15d

    prompter = FakePrompter(
        [
            _AUDIT_UNDERUTILIZED,
            [instance_id],
            _CONFIRM_YES,
            NAV_BACK,
        ]
    )
    Ec2Flow(ctx, prompter)._audit_menu()

    ec2 = ctx.client_factory.ec2()
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]
    assert state in ("stopping", "stopped")


@mock_aws
def test_audit_underutilized_checkbox_back_leaves_instances_untouched() -> None:
    """Ticking "<- Back" in the checkbox aborts before ever asking to confirm."""
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)
    _seed_last_activity(ctx, instance_id, days_ago=20)

    prompter = FakePrompter(
        [_AUDIT_RESOURCES, _AUDIT_UNDERUTILIZED, [NAV_BACK], NAV_BACK]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]
    assert state == "running"


@mock_aws
@pytest.mark.parametrize("declining_choice", [_CONFIRM_NO, _CONFIRM_BACK])
def test_audit_underutilized_confirm_no_or_back_leaves_instances_untouched(
    declining_choice: str,
) -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)
    _seed_last_activity(ctx, instance_id, days_ago=20)

    prompter = FakePrompter(
        [
            _AUDIT_RESOURCES,
            _AUDIT_UNDERUTILIZED,
            [instance_id],
            declining_choice,
            NAV_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]
    assert state == "running"


@mock_aws
def test_audit_zombie_confirm_yes_terminates_the_selected_instances() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)
    ctx.client_factory.ec2().stop_instances(InstanceIds=[instance_id])
    _seed_last_activity(ctx, instance_id, days_ago=45)  # > 30d

    prompter = FakePrompter(
        [
            _AUDIT_ZOMBIE,
            [instance_id],
            _CONFIRM_YES,
            NAV_BACK,
        ]
    )
    Ec2Flow(ctx, prompter)._audit_menu()

    ec2 = ctx.client_factory.ec2()
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]
    assert state in ("shutting-down", "terminated")


@mock_aws
def test_audit_zombie_running_instance_never_qualifies() -> None:
    """A RUNNING instance idle > 30d must never show up under Zombie (Stopped-only)."""
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)  # RUNNING, never stopped
    _seed_last_activity(ctx, instance_id, days_ago=45)

    prompter = FakePrompter([_AUDIT_RESOURCES, _AUDIT_ZOMBIE, NAV_BACK])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY  # the empty-state path, not a checkbox with our instance



# -- Crear (Launch): the minimal wizard (Name, AMI, Owner sub-flow) -----------------
#
# Instance Type, VPC/Subnet, Security Groups and Storage all resolve to smart
# defaults without ever prompting -- this deliberately replaced the old 9-step
# Provision Wizard. Owner goes through the same steppable Sí/No/Back -> filter ->
# pick sub-flow AMI Quick Launch uses (see ``_owner_step_confirm`` et al. in
# ec2_flow.py). Anyone needing per-launch control over storage size or Security
# Groups uses AMI Quick Launch (see the "Quick Launch from AMI" section below)
# instead.


@mock_aws
def test_create_instance_minimal_flow_uses_defaults_for_everything_but_name_ami_owner() -> None:
    """Only Name/AMI/Owner are ever asked -- Instance Type, VPC/Subnet/SG and Storage default."""
    ctx = _app_ctx()
    ami_id = _any_ami_id()

    prompter = FakePrompter(
        [
            "create_instance",
            "minimal-instance",  # Name
            "ami_manual",  # AMI menu -> enter manually
            ami_id,
            _OWNER_ASSIGN_YES,  # ¿Desea asignar un Propietario? -> Sí
            "devops",  # no IAM users seeded -> straight to the free-text fallback
            _USERDATA_NONE,
            _CONFIRM_YES,  # confirm launch
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    instance = ec2.describe_instances()["Reservations"][0]["Instances"][0]
    assert instance["InstanceType"] == "t2.micro"
    assert not instance.get("KeyName")

    tags = {t["Key"]: t["Value"] for t in instance["Tags"]}
    assert tags["Name"] == "minimal-instance"
    assert tags["Owner"] == "devops"
    # An instance WITH an Owner gets no Key Pair at all -- "-" says so cleanly.
    assert tags["KeyPair"] == _NO_KEY_PAIR
    # Only the framework's own bookkeeping tags may ride along with Name/Owner/KeyPair.
    assert set(tags) <= {"Name", "Owner", "KeyPair", "ManagedBy", "CreatedAt"}

    default_vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]
    default_sg = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [default_vpc["VpcId"]]},
            {"Name": "group-name", "Values": ["default"]},
        ]
    )["SecurityGroups"][0]["GroupId"]
    assert _applied_security_groups(instance) == {default_sg}

    volume_id = instance["BlockDeviceMappings"][0]["Ebs"]["VolumeId"]
    volume = ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
    assert volume["Size"] == 8


@mock_aws
def test_create_instance_with_curated_ami_alias_resolves_without_manual_id() -> None:
    """Picking a curated AMI alias (not 'Ingresar AMI ID manualmente') skips the text prompt."""
    ctx = _app_ctx()

    prompter = FakePrompter(
        [
            "create_instance",
            "al2023-instance",
            "amazon-linux-2023",
            _OWNER_ASSIGN_YES,
            "devops",
            _USERDATA_NONE,
            _CONFIRM_YES,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert not any("AMI ID" in asked for asked in prompter.asked)
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    assert [i for r in reservations for i in r["Instances"]]


@mock_aws
def test_create_instance_with_unresolvable_alias_falls_back_in_local_environment() -> None:
    """'debian-12' has no match in moto's reduced catalog -- the local-only fallback still launches.

    ``local_ctx`` only flips ``settings.endpoint_url`` (so ``is_local`` is
    True, enabling ``_resolve_ami``'s fallback) -- ``client_factory`` is left
    as the original, already-working one, so every AWS call in this test
    still reaches the same moto backend the rest of this file's setup uses.
    """
    ctx = _app_ctx()
    local_ctx = replace(
        ctx, settings=ctx.settings.model_copy(update={"endpoint_url": "http://localhost:4566"})
    )

    prompter = FakePrompter(
        [
            "create_instance",
            "debian-fallback-instance",
            "debian-12",  # curated alias with no match in moto's AMI catalog
            _OWNER_ASSIGN_YES,
            "devops",
            _USERDATA_NONE,
            _CONFIRM_YES,
        ]
    )
    action = Ec2Flow(local_ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = local_ctx.client_factory.ec2().describe_instances()["Reservations"]
    assert [i for r in reservations for i in r["Instances"]]


@mock_aws
def test_create_instance_owner_selects_directly_from_existing_iam_users() -> None:
    """Owner is a direct pick over real IAM users -- never free text when any exist."""
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        [
            "create_instance",
            "iam-owner-instance",
            "ami_manual",
            ami_id,
            _OWNER_ASSIGN_YES,
            "",  # Filtrar usuarios IAM: blank -> show everyone
            "alice",
            _USERDATA_NONE,
            _CONFIRM_YES,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    tags = {t["Key"]: t["Value"] for t in reservations[0]["Instances"][0]["Tags"]}
    assert tags["Owner"] == "alice"


@mock_aws
def test_create_instance_owner_custom_free_text_escape_hatch_when_iam_users_exist() -> None:
    """"Otro (texto libre)" still reaches an Owner outside the IAM users list."""
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        [
            "create_instance",
            "custom-owner-instance",
            "ami_manual",
            ami_id,
            _OWNER_ASSIGN_YES,
            "",  # Filtrar usuarios IAM: blank -> show everyone
            _OWNER_CUSTOM,
            "external-vendor",
            _USERDATA_NONE,
            _CONFIRM_YES,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    tags = {t["Key"]: t["Value"] for t in reservations[0]["Instances"][0]["Tags"]}
    assert tags["Owner"] == "external-vendor"


@mock_aws
def test_create_instance_owner_declined_leaves_no_owner_tag() -> None:
    """"No, continuar sin Owner" skips the IAM step entirely and writes no Owner tag."""
    ctx = _app_ctx()
    ami_id = _any_ami_id()

    prompter = FakePrompter(
        [
            "create_instance",
            "no-owner-instance",
            "ami_manual",
            ami_id,
            _OWNER_ASSIGN_NO,  # ¿Desea asignar un Propietario? -> No
            _KEY_PAIR_CONTINUE,  # KEY_PAIR: accept the auto-generated key
            _USERDATA_NONE,
            _CONFIRM_YES,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    instance = reservations[0]["Instances"][0]
    tags = {t["Key"]: t["Value"] for t in instance["Tags"]}
    assert "Owner" not in tags
    # An ownerless ("huérfana") instance must get an auto-generated Key Pair instead.
    assert tags["KeyPair"].startswith("key-no-owner-instance")
    assert instance.get("KeyName") == tags["KeyPair"]
    pem_path = Path.cwd() / "keys" / "ec2" / f"{tags['KeyPair']}.pem"
    assert pem_path.exists()


# -- User Data step: security template / local script / none ------------------------


def _instance_user_data(ctx: AppContext, instance_id: str) -> str | None:
    """The instance's UserData, base64-decoded -- ``None`` if it was never set.

    EC2's own API always returns UserData base64-encoded (moto included);
    boto3/botocore is what performs that encoding, once, when a plain-text
    string is passed to ``run_instances`` -- see ``boto3_ec2_gateway.py``'s
    ``_create_bucket_kwargs``-style comment on ``spec.user_data``. Decoding it
    back here is how these tests confirm the wizard never double-encodes.
    """
    attr = ctx.client_factory.ec2().describe_instance_attribute(
        InstanceId=instance_id, Attribute="userData"
    )
    value = attr["UserData"].get("Value")
    return base64.b64decode(value).decode("utf-8") if value else None


@mock_aws
def test_create_instance_user_data_security_template_is_base64_encoded_correctly() -> None:
    """Picking the security template must launch with EXACTLY ``SECURITY_USERDATA_TEMPLATE``
    as UserData -- base64-encoded once (by boto3), never double-encoded by this flow.
    """
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    prompter = FakePrompter(
        [
            "create_instance",
            "template-userdata-instance",
            "ami_manual",
            ami_id,
            _OWNER_ASSIGN_NO,
            _KEY_PAIR_CONTINUE,
            _USERDATA_TEMPLATE,  # "Usar plantilla de seguridad"
            _CONFIRM_YES,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    instance_id = str(reservations[0]["Instances"][0]["InstanceId"])
    assert _instance_user_data(ctx, instance_id) == SECURITY_USERDATA_TEMPLATE


@mock_aws
def test_create_instance_user_data_local_script_is_base64_encoded_correctly(
    tmp_path: Path,
) -> None:
    """Picking a local script must launch with EXACTLY that file's own content as
    UserData -- base64-encoded once (by boto3), never double-encoded by this flow.
    """
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    script_path = tmp_path / "custom_bootstrap.sh"
    script_content = "#!/bin/bash\necho 'custom bootstrap'\n"
    script_path.write_text(script_content)

    prompter = FakePrompter(
        [
            "create_instance",
            "local-userdata-instance",
            "ami_manual",
            ami_id,
            _OWNER_ASSIGN_NO,
            _KEY_PAIR_CONTINUE,
            _USERDATA_LOCAL,  # "Cargar script local"
            str(script_path),
            _CONFIRM_YES,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    instance_id = str(reservations[0]["Instances"][0]["InstanceId"])
    assert _instance_user_data(ctx, instance_id) == script_content


@mock_aws
def test_create_instance_user_data_local_script_missing_path_reprompts_the_select() -> None:
    """A path that doesn't exist must re-show the User Data select (not abort the
    wizard), so a typo doesn't cost the operator every earlier answer.
    """
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    prompter = FakePrompter(
        [
            "create_instance",
            "retry-userdata-instance",
            "ami_manual",
            ami_id,
            _OWNER_ASSIGN_NO,
            _KEY_PAIR_CONTINUE,
            _USERDATA_LOCAL,
            "/no/such/file.sh",  # missing -> re-shows the User Data select
            _USERDATA_NONE,
            _CONFIRM_YES,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    assert any(
        {t["Key"]: t["Value"] for t in i.get("Tags", [])}.get("Name")
        == "retry-userdata-instance"
        for r in reservations
        for i in r["Instances"]
    )


@mock_aws
def test_create_instance_user_data_none_launches_with_no_user_data() -> None:
    """"Ninguno (Arrancar SO limpio)" must launch with no UserData set at all."""
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    prompter = FakePrompter(
        [
            "create_instance",
            "clean-boot-instance",
            "ami_manual",
            ami_id,
            _OWNER_ASSIGN_NO,
            _KEY_PAIR_CONTINUE,
            _USERDATA_NONE,
            _CONFIRM_YES,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    instance_id = str(reservations[0]["Instances"][0]["InstanceId"])
    assert _instance_user_data(ctx, instance_id) is None


@mock_aws
def test_render_launch_summary_shows_security_template_attached_row() -> None:
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    ami = build_ec2_use_cases(ctx).get_ami.execute(ami_id)
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    Ec2Flow(ctx, FakePrompter())._render_launch_summary(
        region=ctx.settings.region,
        name="i",
        ami=ami,
        instance_type="t2.micro",
        vpc_id="vpc-x",
        subnet_id="subnet-x",
        security_groups=[],
        key_name=None,
        volume_size_gb=8,
        owner="",
        tags={},
        user_data_summary=_USERDATA_SUMMARY_TEMPLATE,
    )

    output = console.file.getvalue()  # type: ignore[attr-defined]
    row = next(line for line in output.splitlines() if "UserData" in line)
    assert _USERDATA_SUMMARY_TEMPLATE in row


@mock_aws
def test_render_launch_summary_shows_local_script_attached_row() -> None:
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    ami = build_ec2_use_cases(ctx).get_ami.execute(ami_id)
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    Ec2Flow(ctx, FakePrompter())._render_launch_summary(
        region=ctx.settings.region,
        name="i",
        ami=ami,
        instance_type="t2.micro",
        vpc_id="vpc-x",
        subnet_id="subnet-x",
        security_groups=[],
        key_name=None,
        volume_size_gb=8,
        owner="",
        tags={},
        user_data_summary="user_data.sh (Attached)",
    )

    output = console.file.getvalue()  # type: ignore[attr-defined]
    row = next(line for line in output.splitlines() if "UserData" in line)
    assert "user_data.sh (Attached)" in row


@mock_aws
def test_render_launch_summary_omits_user_data_row_when_none_is_passed() -> None:
    """AMI Quick Launch never passes ``user_data_summary`` at all -- the row must not
    appear, rather than showing a stray "none" for a question that was never asked.
    """
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    ami = build_ec2_use_cases(ctx).get_ami.execute(ami_id)
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    Ec2Flow(ctx, FakePrompter())._render_launch_summary(
        region=ctx.settings.region,
        name="i",
        ami=ami,
        instance_type="t2.micro",
        vpc_id="vpc-x",
        subnet_id="subnet-x",
        security_groups=[],
        key_name=None,
        volume_size_gb=8,
        owner="",
        tags={},
    )

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "UserData" not in output


@mock_aws
def test_create_instance_blank_name_acts_as_back() -> None:
    ctx = _app_ctx()
    prompter = FakePrompter(["create_instance", ""])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    assert not [i for r in reservations for i in r["Instances"]]


@mock_aws
def test_create_instance_cancelled_via_ctrl_c_at_ami_step_creates_nothing() -> None:
    """A genuine cancel (Ctrl+C/Esc -> ``None``) at the AMI select aborts the whole flow."""
    ctx = _app_ctx()
    prompter = FakePrompter(["create_instance", "demo-instance", None])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    assert not [i for r in reservations for i in r["Instances"]]


@mock_aws
def test_create_instance_owner_confirm_back_returns_to_ami_step_not_a_full_abort() -> None:
    """BUG FIX: "<- Back" at "¿Desea asignar Owner?" must retreat to AMI selection,
    never fall through/abort straight past it.
    """
    ctx = _app_ctx()
    ami_id = _any_ami_id()

    prompter = FakePrompter(
        [
            "create_instance",
            "back-to-ami-instance",
            "ami_manual",  # AMI (forward, manual entry)
            ami_id,
            NAV_BACK,  # OWNER_CONFIRM -> Back -> must land on AMI ("AMI:" select re-shown)
            "amazon-linux-2023",  # AMI (re-shown): pick a curated alias this time
            _OWNER_ASSIGN_NO,
            _KEY_PAIR_CONTINUE,
            _USERDATA_NONE,
            _CONFIRM_YES,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    assert [i for r in reservations for i in r["Instances"]]


@mock_aws
def test_create_instance_confirm_no_creates_nothing() -> None:
    """"No, cancelar" on the final confirm is the one genuine full-abort in this wizard."""
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    prompter = FakePrompter(
        [
            "create_instance",
            "declined-instance",
            "ami_manual",
            ami_id,
            _OWNER_ASSIGN_YES,
            "devops",
            _USERDATA_NONE,
            _CONFIRM_NO,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    assert not [i for r in reservations for i in r["Instances"]]


@mock_aws
def test_create_instance_confirm_back_returns_to_user_data_not_a_full_abort() -> None:
    """BUG FIX: "<- Back" at the final confirm must retreat one step (to USER_DATA, its
    immediate predecessor now that step exists), never fall through to launching and
    never abort the whole wizard.
    """
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    prompter = FakePrompter(
        [
            "create_instance",
            "confirm-back-instance",
            "ami_manual",
            ami_id,
            _OWNER_ASSIGN_YES,
            "devops",  # OWNER_SELECT (forward, no IAM users seeded -> free text)
            _USERDATA_NONE,  # USER_DATA (forward)
            _CONFIRM_BACK,  # CONFIRM -> Back -> must land on USER_DATA, not abort
            _USERDATA_NONE,  # USER_DATA (re-shown) -> forward again
            _CONFIRM_YES,  # CONFIRM (re-shown) -> launch
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    assert any(
        {t["Key"]: t["Value"] for t in i.get("Tags", [])}.get("Name") == "confirm-back-instance"
        for r in reservations
        for i in r["Instances"]
    )


@mock_aws
def test_create_instance_back_navigation_follows_the_exact_step_mapping() -> None:
    """Walks every question forward, then backs out one at a time in strict reverse order,
    proving each "<- Back" lands on its exact required predecessor -- never a fall-through,
    never a jump past the immediate parent -- all the way out to exiting the wizard.
    """
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    ctx.client_factory.iam().create_user(UserName="alice")
    ec2 = ctx.client_factory.ec2()
    instances_before = len(
        [i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]
    )

    prompter = FakePrompter(
        [
            "create_instance",
            "step-mapping-instance",  # NAME (forward)
            "ami_manual",  # AMI (forward)
            ami_id,
            _OWNER_ASSIGN_YES,  # OWNER_CONFIRM (forward)
            "ali",  # OWNER_FILTER (forward)
            "alice",  # OWNER_SELECT (forward)
            _USERDATA_NONE,  # USER_DATA (forward)
            _CONFIRM_BACK,  # CONFIRM -> Back -> must land on USER_DATA
            NAV_BACK,  # USER_DATA -> Back -> must land on OWNER_SELECT
            NAV_BACK,  # OWNER_SELECT -> Back -> must land on OWNER_FILTER
            None,  # OWNER_FILTER (Ctrl+C) -> Back -> must land on OWNER_CONFIRM
            NAV_BACK,  # OWNER_CONFIRM -> Back -> must land on AMI
            None,  # AMI (Ctrl+C) -> CANCEL -> exits the whole wizard (AMI has no "<- Back"
            # target of its own for a genuine Ctrl+C -- same contract
            # ``test_create_instance_cancelled_via_ctrl_c_at_ami_step_creates_nothing`` checks)
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    instances_after = len(
        [i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]
    )
    assert instances_after == instances_before  # backed all the way out -- nothing launched


@mock_aws
def test_create_instance_back_then_forward_preserves_earlier_answers_and_allows_changes() -> None:
    """Backing up from OWNER_SELECT to AMI and changing the AMI must neither lose the Name
    entered earlier nor prevent completing the launch afterward.
    """
    ctx = _app_ctx()
    ami_id = _any_ami_id()

    prompter = FakePrompter(
        [
            "create_instance",
            "preserved-name-instance",  # NAME
            "ami_manual",  # AMI (first attempt: manual entry)
            ami_id,
            _OWNER_ASSIGN_YES,
            "",  # OWNER_SELECT: no IAM users -> free text; blank Enter -> Back -> OWNER_CONFIRM
            NAV_BACK,  # OWNER_CONFIRM -> Back -> AMI
            "amazon-linux-2023",  # AMI (re-shown): change of mind -> curated alias
            _OWNER_ASSIGN_NO,  # OWNER_CONFIRM (re-shown): decline this time
            _KEY_PAIR_CONTINUE,  # KEY_PAIR: accept the auto-generated key
            _USERDATA_NONE,
            _CONFIRM_YES,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    new_instance = next(
        i
        for r in reservations
        for i in r["Instances"]
        if {t["Key"]: t["Value"] for t in i.get("Tags", [])}.get("Name")
        == "preserved-name-instance"
    )
    tags = {t["Key"]: t["Value"] for t in new_instance["Tags"]}
    assert "Owner" not in tags  # the changed-mind "No" decision took effect
    # Declining after an earlier free-text Owner attempt must still get a real Key Pair.
    assert tags["KeyPair"].startswith("key-preserved-name-instance")
    assert new_instance.get("KeyName") == tags["KeyPair"]


# -- Key Pair sub-flow: conditional on the Owner decision, shared by both wizards ----


def test_key_pair_base_name_sanitizes_and_prefixes() -> None:
    assert _key_pair_base_name("web server") == "key-web-server"
    assert _key_pair_base_name("api_v2.prod") == "key-api_v2.prod"
    assert _key_pair_base_name("!!!") == "key-ec2"  # nothing legal survives -> fallback


def test_unique_key_pair_name_avoids_collisions_with_aws_and_local_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "key-web.pem").touch()  # a local file the AWS-side list doesn't know about
    taken = {"key-web-2"}  # an AWS-side key pair the local directory doesn't know about
    assert _unique_key_pair_name("key-web", taken, tmp_path) == "key-web-3"


def test_unique_key_pair_name_returns_the_base_when_nothing_collides(tmp_path: Path) -> None:
    assert _unique_key_pair_name("key-fresh", set(), tmp_path) == "key-fresh"


def test_key_pair_destination_dir_is_keys_ec2_under_the_cwd(tmp_path: Path) -> None:
    """``./keys/ec2/`` -- not the bare ``./keys/`` IAM's credentials also live under."""
    assert _key_pair_destination_dir() == tmp_path / "keys" / "ec2"


def test_render_key_pair_result_shows_the_relative_keys_ec2_path(tmp_path: Path) -> None:
    """The success screen's table row, SSH example, and confirmation line all show
    ``./keys/ec2/<name>.pem`` -- never the absolute path.
    """
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)
    saved_path = tmp_path / "keys" / "ec2" / "key-web.pem"
    saved_path.parent.mkdir(parents=True)
    saved_path.touch()

    Ec2Flow(ctx, FakePrompter())._render_key_pair_result(
        _KeyPairSubState(
            key_name="key-web", saved_path=saved_path, fingerprint="ab:cd", created=True
        )
    )

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "./keys/ec2/key-web.pem" in output
    assert str(saved_path) not in output


@mock_aws
def test_create_instance_key_pair_step_back_returns_to_owner_confirm_not_a_full_abort() -> None:
    """BUG FIX: "<- Back" on the auto Key Pair step must retreat to "¿Desea asignar
    Owner?", never abort the whole wizard.
    """
    ctx = _app_ctx()
    ami_id = _any_ami_id()

    prompter = FakePrompter(
        [
            "create_instance",
            "keypair-back-instance",
            "ami_manual",
            ami_id,
            _OWNER_ASSIGN_NO,  # OWNER_CONFIRM -> No -> routes to KEY_PAIR
            NAV_BACK,  # KEY_PAIR -> Back -> must land on OWNER_CONFIRM, not abort
            _OWNER_ASSIGN_NO,  # OWNER_CONFIRM (re-shown): decline again
            _KEY_PAIR_CONTINUE,  # KEY_PAIR (re-shown): accept this time
            _USERDATA_NONE,
            _CONFIRM_YES,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    reservations = ctx.client_factory.ec2().describe_instances()["Reservations"]
    assert any(
        {t["Key"]: t["Value"] for t in i.get("Tags", [])}.get("Name") == "keypair-back-instance"
        for r in reservations
        for i in r["Instances"]
    )


@mock_aws
def test_quick_launch_key_pair_step_back_returns_to_owner_confirm_not_a_full_abort() -> None:
    """Same Back-mapping guarantee as the "from scratch" flow, for AMI Quick Launch."""
    ctx = _app_ctx()
    ami_id = _create_test_ami(ctx)
    ec2 = ctx.client_factory.ec2()
    vpc_id = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0][
        "VpcId"
    ]
    sg_id = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": ["default"]},
        ]
    )["SecurityGroups"][0]["GroupId"]

    prompter = FakePrompter(
        [
            "ami_management",
            "SEARCH_AMIs",
            "",
            ami_id,
            _AMI_DETAIL_CREATE_EC2,
            "quick-keypair-back-instance",
            _OWNER_ASSIGN_NO,  # OWNER_CONFIRM -> No -> routes to KEY_PAIR
            NAV_BACK,  # KEY_PAIR -> Back -> must land on OWNER_CONFIRM, not abort
            _OWNER_ASSIGN_NO,  # OWNER_CONFIRM (re-shown): decline again
            _KEY_PAIR_CONTINUE,  # KEY_PAIR (re-shown): accept this time
            vpc_id,
            [sg_id],
            _STORAGE_DEFAULT,
            _CONFIRM_YES,
            _AMI_DETAIL_BACK,
            _AMI_MENU_BACK,
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    reservations = ec2.describe_instances()["Reservations"]
    assert any(
        {t["Key"]: t["Value"] for t in i.get("Tags", [])}.get("Name")
        == "quick-keypair-back-instance"
        for r in reservations
        for i in r["Instances"]
    )


@mock_aws
def test_render_launch_summary_shows_the_sessions_global_region_with_no_prompt() -> None:
    """Region is never asked in the launch wizard -- it's inherited from
    ``ctx.settings.region`` (the Region Selector's own global switch), so the
    summary must reflect whatever that session-wide setting currently is.
    """
    ctx = AppContext.build(Settings(profile="testprofile", region="eu-west-1"))
    ami_id = _any_ami_id()
    ami = build_ec2_use_cases(ctx).get_ami.execute(ami_id)
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    Ec2Flow(ctx, FakePrompter())._render_launch_summary(
        region=ctx.settings.region,
        name="inherited-region-instance",
        ami=ami,
        instance_type="t2.micro",
        vpc_id="vpc-x",
        subnet_id="subnet-x",
        security_groups=[],
        key_name=None,
        volume_size_gb=8,
        owner="devops",
        tags={},
    )

    output = console.file.getvalue()  # type: ignore[attr-defined]
    region_row = next(line for line in output.splitlines() if "Region" in line)
    assert "eu-west-1" in region_row


@mock_aws
def test_render_launch_summary_shows_dash_for_key_pair_when_owner_is_assigned() -> None:
    """BUG FIX: an instance WITH an Owner must show "-" for Key Pair, never a generated name."""
    ctx = _app_ctx()
    ami_id = _any_ami_id()
    ami = build_ec2_use_cases(ctx).get_ami.execute(ami_id)
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    Ec2Flow(ctx, FakePrompter())._render_launch_summary(
        region=ctx.settings.region,
        name="owned-instance",
        ami=ami,
        instance_type="t2.micro",
        vpc_id="vpc-x",
        subnet_id="subnet-x",
        security_groups=[],
        key_name=None,
        volume_size_gb=8,
        owner="devops",
        tags={"Owner": "devops", "KeyPair": _NO_KEY_PAIR},
    )

    output = console.file.getvalue()  # type: ignore[attr-defined]
    key_pair_row = next(line for line in output.splitlines() if "Key Pair" in line)
    assert _NO_KEY_PAIR in key_pair_row
    assert "key-" not in key_pair_row


def test_wizard_ami_step_blank_manual_entry_returns_to_ami_select_not_full_cancel() -> None:
    """A blank image ID at the manual-entry prompt re-shows the image select, not a full cancel."""
    ctx = _app_ctx()
    state = _LaunchWizardState()
    prompter = FakePrompter(
        [
            _AMI_MANUAL,  # image select -> manual entry
            "",  # blank image ID -> back to the image select
            "amazon-linux-2023",  # image select (re-shown) -> curated alias
        ]
    )
    nav = Ec2Flow(ctx, prompter)._wizard_step_base_image(build_ec2_use_cases(ctx), state)

    assert nav is _WizardNav.NEXT
    assert state.ami_ref == "amazon-linux-2023"


def test_wizard_storage_step_back_from_preset_menu_returns_to_the_storage_select() -> None:
    """"<- Back" on the new preset menu re-shows "EBS storage:", not the whole wizard."""
    ctx = _app_ctx()
    state = _LaunchWizardState()
    prompter = FakePrompter(
        [
            "storage_custom",  # EBS storage: -> custom size
            NAV_BACK,  # preset menu: back
            "storage_default",  # EBS storage: (re-shown) -> default
        ]
    )
    nav = Ec2Flow(ctx, prompter)._wizard_step_storage(build_ec2_use_cases(ctx), state)

    assert nav is _WizardNav.NEXT
    assert state.volume_size_gb == 8
    assert prompter.asked.count("EBS storage:") == 2


# -- Borrar: arrow-key destructive confirmation ------------------------------------------


@mock_aws
def test_terminate_managed_instance_full_confirmation() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)

    prompter = FakePrompter(["delete_instance", "", instance_id, "yes"])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]
    assert state in ("shutting-down", "terminated")


@mock_aws
def test_terminate_selecting_no_leaves_instance_untouched() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)

    prompter = FakePrompter(["delete_instance", "", instance_id, "no"])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]
    assert state == "running"


@mock_aws
def test_terminate_selecting_cancel_leaves_instance_untouched() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)

    prompter = FakePrompter(["delete_instance", "", instance_id, NAV_BACK])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]
    assert state == "running"


@mock_aws
def test_terminate_unmanaged_instance_offers_force_and_retries() -> None:
    ctx = _app_ctx()
    subnet_id, sg_id = _default_subnet_and_sg()
    ami_id = _any_ami_id()
    ec2 = ctx.client_factory.ec2()
    # Launched OUTSIDE this CLI -- no ManagedBy tag -- so TerminateInstanceUseCase's
    # own guard rail must reject the plain (force=False) attempt.
    instance_id = ec2.run_instances(
        ImageId=ami_id,
        InstanceType="t2.micro",
        MinCount=1,
        MaxCount=1,
        SubnetId=subnet_id,
        SecurityGroupIds=[sg_id],
    )["Instances"][0]["InstanceId"]

    prompter = FakePrompter(
        [
            "delete_instance",
            "",
            instance_id,
            "yes",  # arrow-key delete confirmation
            "yes",  # arrow-key confirm --force retry
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]
    assert state in ("shutting-down", "terminated")


@mock_aws
def test_terminate_cancelled_at_picker_never_reaches_confirm() -> None:
    ctx = _app_ctx()
    instance_id = _launch_managed_instance(ctx)

    prompter = FakePrompter(["delete_instance", "", NAV_BACK])
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    ec2 = ctx.client_factory.ec2()
    state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]
    assert state == "running"


@mock_aws
def test_terminate_a_foreign_region_instance_routes_to_its_own_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminate's picker is global (like the instance "Search / Filter" screen): picking
    an instance outside the active session region must terminate it in ITS OWN region,
    never the active session region.
    """
    monkeypatch.setattr(Boto3Ec2Gateway, "_list_regions", lambda self: ["us-east-1", "us-west-2"])
    ctx = _app_ctx()  # active session region: us-east-1
    foreign_instance_id = _launch_raw_instance_in_region("us-west-2")
    # Launched OUTSIDE this CLI -- no ManagedBy tag -- so the plain (force=False)
    # attempt is rejected first, same as the unmanaged-instance test above.

    prompter = FakePrompter(
        [
            "delete_instance",
            "",  # global instance picker: blank query -> everyone
            foreign_instance_id,
            "yes",  # arrow-key delete confirmation
            "yes",  # arrow-key confirm --force retry
        ]
    )
    action = Ec2Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert ctx.settings.region == "us-east-1"

    foreign_client = ctx.client_factory.ec2(region="us-west-2")
    state = foreign_client.describe_instances(InstanceIds=[foreign_instance_id])["Reservations"][
        0
    ]["Instances"][0]["State"]["Name"]
    assert state in ("shutting-down", "terminated")


# -- Multi-selection through the REAL widget -----------------------------------------
#
# Everything above reaches ``_quick_pick_security_groups`` through ``FakePrompter``,
# which returns whatever list the test scripted. That proves what the FLOW does with
# several security groups, but it can never prove that a human is able to tick more
# than one, because the widget itself is never run. The tests below close exactly
# that gap: they drive the real ``questionary`` checkbox with a piped keystroke stream.

_SPACE = " "
_DOWN = "\x1b[B"
_ENTER = "\r"


@mock_aws
def test_quick_pick_security_groups_accepts_several_ticks_through_the_real_widget() -> None:
    """Ticking three entries with the real widget resolves all three security groups.

    Keystrokes: space (tick #1), down, space (tick #2), down, space (tick
    #3), Enter (confirm the checkbox). Unlike the old 9-step wizard's Security
    Groups step, Quick Launch has no follow-up "Continuar" select -- its own
    final deploy confirmation is the only confirmation. If the checkbox ever
    regressed to single-selection, this is the only test in the suite that
    would notice.
    """
    ctx = _app_ctx()
    _subnet_id, web_sg, db_sg, ssh_sg = _default_subnet_and_three_sgs()
    client = boto3.client("ec2", region_name="us-east-1")
    vpc_id = client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0][
        "VpcId"
    ]

    flow = Ec2Flow(ctx, QuestionaryPrompter())
    keys = _SPACE + _DOWN + _SPACE + _DOWN + _SPACE + _ENTER
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            resolved = flow._quick_pick_security_groups(vpc_id)

    assert resolved is not None
    assert len(resolved) == 3, "the checkbox returned fewer than the three ticks"
    # Every tick must be one of the VPC's real groups, not a duplicate of one entry.
    assert {sg.group_id for sg in resolved} <= {web_sg, db_sg, ssh_sg} | {
        group["GroupId"]
        for group in client.describe_security_groups(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )["SecurityGroups"]
    }


@mock_aws
def test_quick_pick_security_groups_message_states_how_many_are_selectable() -> None:
    """The prompt states the count, so "only one row" is never mistaken for "only one pick"."""
    ctx = _app_ctx()
    _subnet_id, _web, _db, _ssh = _default_subnet_and_three_sgs()
    client = boto3.client("ec2", region_name="us-east-1")
    vpc_id = client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0][
        "VpcId"
    ]

    prompter = FakePrompter([[]])
    Ec2Flow(ctx, prompter)._quick_pick_security_groups(vpc_id)

    # 3 seeded + the VPC's own default group.
    assert f"VPC {vpc_id}" in prompter.asked[0]
    assert "4 disponibles" in prompter.asked[0]
    assert "SPACE marca" in prompter.asked[0]


@mock_aws
def test_quick_pick_security_groups_with_only_the_default_group_falls_back_to_it() -> None:
    """A VPC with just its own ``default`` group still produces a valid Quick Launch pick.

    This is the state a user hits when the demo seeder never ran (a session
    that isn't pointed at a local endpoint): the only pickable entry is the
    VPC's own ``default`` group, and an empty (blank Enter) checkbox answer
    must still resolve to it, not dead-end the flow.
    """
    ctx = _app_ctx()
    client = boto3.client("ec2", region_name="us-east-1")
    vpc_id = client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0][
        "VpcId"
    ]

    prompter = FakePrompter([[]])
    resolved = Ec2Flow(ctx, prompter)._quick_pick_security_groups(vpc_id)

    assert "1 disponibles" in prompter.asked[0]
    assert resolved is not None
    assert len(resolved) == 1
    assert resolved[0].group_name == "default"
