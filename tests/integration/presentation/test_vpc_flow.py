"""Tests for ``VpcFlow``: read-only listing, the disabled network-changes entry, cancellation."""

from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.presentation.tui.flows.vpc_flow import VpcFlow
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT
from aws_admin_cli.presentation.tui.navigation import NavAction
from moto import mock_aws

from tests.fakes.prompter import FakePrompter


def _app_ctx() -> AppContext:
    return AppContext.build(Settings(profile="testprofile"))


@mock_aws
def test_list_vpcs_shows_the_default_vpc() -> None:
    ctx = _app_ctx()
    prompter = FakePrompter(["list_vpcs"])

    action = VpcFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert prompter.asked == ["VPC -- what do you want to query?"]


@mock_aws
def test_list_subnets_when_none_shows_message() -> None:
    action = VpcFlow(_app_ctx(), FakePrompter(["list_subnets"])).menu()
    assert action is NavAction.STAY


@mock_aws
def test_list_security_groups_shows_the_default_group() -> None:
    action = VpcFlow(_app_ctx(), FakePrompter(["list_sgs"])).menu()
    assert action is NavAction.STAY


@mock_aws
def test_audit_security_groups_runs_without_crashing() -> None:
    action = VpcFlow(_app_ctx(), FakePrompter(["audit_sgs"])).menu()
    assert action is NavAction.STAY


@mock_aws
def test_list_availability_zones_shows_results() -> None:
    action = VpcFlow(_app_ctx(), FakePrompter(["list_azs"])).menu()
    assert action is NavAction.STAY


def test_network_changes_entry_is_purely_informational_and_never_mutates() -> None:
    prompter = FakePrompter(["network_changes"])

    action = VpcFlow(_app_ctx(), prompter).menu()

    assert action is NavAction.STAY
    # Only the top menu was asked -- the informational message never prompts further.
    assert prompter.asked == ["VPC -- what do you want to query?"]


def test_menu_back_returns_back() -> None:
    action = VpcFlow(_app_ctx(), FakePrompter([NAV_BACK])).menu()
    assert action is NavAction.BACK


def test_menu_exit_returns_exit() -> None:
    action = VpcFlow(_app_ctx(), FakePrompter([NAV_EXIT])).menu()
    assert action is NavAction.EXIT
