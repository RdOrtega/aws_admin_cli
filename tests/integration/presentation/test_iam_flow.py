"""Tests for ``IamFlow``: search, the user detail/edit screens, and create (New/Copy).

Delete User (top-level menu) is covered under "Delete" below. It's still
absent from the per-user detail screen's own action menu -- see
``test_user_detail_action_menu_has_no_delete_option``.
"""

import io
from dataclasses import replace
from pathlib import Path

import botocore.exceptions
import pytest
from aws_admin_cli.application.dto.iam import AttachPolicyRequest
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.presentation.tui.flows.iam_flow import (
    IamFlow,
    _AccessPrefsState,
    _CreateUserState,
    _ensure_demo_iam_resources,
    _generate_secure_password,
    _render_credentials_file_contents,
    _WizardNav,
)
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT, Choice
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.tui.prompter import QuestionaryPrompter
from aws_admin_cli.presentation.wiring import build_iam_use_cases
from moto import mock_aws
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from tests.fakes.prompter import FakePrompter


def _disable_user_for_test(ctx: AppContext, name: str) -> None:
    """Attach the shared deny-all policy directly (no ``disabled_at``) -- an "Unknown" case."""
    use_cases = build_iam_use_cases(ctx)
    policy_arn = use_cases.resolve_deny_all_policy.execute()
    use_cases.attach_policy.execute(
        AttachPolicyRequest(principal_name=name, policy_arn=policy_arn, principal_type="user")
    )


def _app_ctx() -> AppContext:
    return AppContext.build(Settings(profile="testprofile"))


def _local_app_ctx() -> AppContext:
    """A context with ``is_local`` True -- same convention ``test_sg_seed.py`` uses."""
    return AppContext.build(Settings(profile="testprofile", endpoint_url="http://localhost:4566"))


# -- Main menu shape and navigation ------------------------------------------------


def test_choices_match_the_unified_service_screen_template() -> None:
    """The service screen is one consolidated menu -- search/audit/create/delete/back.

    Guards the shared template: search, audit, create, and delete all sit at
    the root of the service screen, in this order, above one divider and
    Back -- same shape S3 and EC2 use, with IAM's own nouns.
    """
    ctx = _app_ctx()
    choices = IamFlow(ctx, FakePrompter())._choices()
    labels = [c.title if isinstance(c, Choice) else c.line for c in choices]
    assert labels == [
        "🔍 Search / Filter Users",
        "+ Create User",
        "❌ Delete User",
        "─" * 66,
        "↩️  Back",
    ]


@mock_aws
def test_menu_cancelled_at_top_level_exits() -> None:
    assert IamFlow(_app_ctx(), FakePrompter([None])).menu() is NavAction.EXIT


@mock_aws
def test_menu_back_returns_back() -> None:
    assert IamFlow(_app_ctx(), FakePrompter([NAV_BACK])).menu() is NavAction.BACK


@mock_aws
def test_menu_exit_returns_exit() -> None:
    assert IamFlow(_app_ctx(), FakePrompter([NAV_EXIT])).menu() is NavAction.EXIT


# -- Listar (Resource Explorer): empty state, cancellation, and search --------------
#
# No mode picker anymore: selecting "Search" asks ONE free-text query directly
# (blank = everyone), matched against the username. There is no more search by
# ID or by tag -- those capabilities were deliberately removed in favor of a
# single unified query box (see iam_flow.py's ``_query_users``).


@mock_aws
def test_list_users_when_none_exist_shows_message() -> None:
    # "search" reaches the empty-population message inside the search flow (it's
    # checked before ever asking for a query); the Explorer screen itself always
    # offers Search/Back regardless of Total.
    prompter = FakePrompter(["search"])

    action = IamFlow(_app_ctx(), prompter).menu()

    assert action is NavAction.STAY
    assert prompter.asked == [
        "IAM (Users) -- What do you want to do?"
    ]




@mock_aws
def test_search_cancelled_at_query_prompt_short_circuits() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(["search", None])
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert len(prompter.asked) == 2


@mock_aws
def test_search_with_no_match_shows_message() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(["search", "zzz-nope"])
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert len(prompter.asked) == 2


@mock_aws
def test_search_with_single_match_still_requires_explicit_pick() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(["search", "ali", "alice", NAV_BACK])
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    # menu, explorer, query, picker, detail-screen action, explorer again --
    # picker was mandatory, and the Explorer's own loop needs its own exit.
    assert len(prompter.asked) == 4


@mock_aws
def test_search_with_blank_query_lists_everyone() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")
    ctx.client_factory.iam().create_user(UserName="bob")

    prompter = FakePrompter(["search", "", "alice", NAV_BACK])
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY


# -- Editar: name/path, tags, groups, console, access keys --------------------------


@mock_aws
def test_edit_name_renames_independently_of_path() -> None:
    """Renaming never asks about Path at all -- the two are fully independent."""
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "edit",
            "name",
            "alice2",  # new name
            "edit",  # re-enter the edit submenu, now for "alice2"
            NAV_BACK,  # back from edit
            NAV_BACK,  # back from detail
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert [u["UserName"] for u in users] == ["alice2"]
    assert users[0]["Path"] == "/"  # untouched -- Path was never even asked about


@mock_aws
def test_edit_name_blank_acts_as_back_without_touching_the_user() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "edit",
            "name",
            "",  # blank -> Back, no rename
            NAV_BACK,  # back from edit
            NAV_BACK,  # back from detail
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert [u["UserName"] for u in users] == ["alice"]


@mock_aws
def test_edit_name_sanitizes_spaces() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, err_console=err_console)

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "edit",
            "name",
            "Harold Ortega",  # new name, contains a space
            NAV_BACK,  # back from edit
            NAV_BACK,  # back from detail
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert [u["UserName"] for u in users] == ["Harold_Ortega"]
    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "Name sanitized" in output


@mock_aws
def test_edit_path_changes_path_independently_of_name() -> None:
    """Changing Path never asks about Name at all -- the two are fully independent."""
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, err_console=err_console)

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "edit",
            "path",
            "Admin",  # new path, missing slashes
            NAV_BACK,  # back from edit
            NAV_BACK,  # back from detail
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert [u["UserName"] for u in users] == ["alice"]  # untouched
    assert users[0]["Path"] == "/Admin/"
    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "Path sanitized" in output


@mock_aws
def test_edit_path_blank_acts_as_back_without_forcing_any_structure() -> None:
    """BUG FIX: a blank Path answer must never force a default like "/team/"."""
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "edit",
            "path",
            "",  # blank -> Back, no change
            NAV_BACK,  # back from edit
            NAV_BACK,  # back from detail
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert users[0]["Path"] == "/"


@mock_aws
def test_add_tag_then_delete_it() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "edit",
            "tags",
            "tag_add",
            "Environment",
            "staging",
            NAV_BACK,  # back from tags screen
            NAV_BACK,  # back from edit
            NAV_BACK,  # back from detail
        ]
    )
    action = IamFlow(ctx, prompter).menu()
    assert action is NavAction.STAY

    iam = ctx.client_factory.iam()
    tags = {t["Key"]: t["Value"] for t in iam.get_user(UserName="alice")["User"]["Tags"]}
    assert tags["Environment"] == "staging"

    prompter2 = FakePrompter(
        [
            "search",
            "",
            "alice",
            "edit",
            "tags",
            "tag_delete",
            "Environment",
            "yes",
            NAV_BACK,
            NAV_BACK,
            NAV_BACK
        ]
    )
    action2 = IamFlow(ctx, prompter2).menu()
    assert action2 is NavAction.STAY
    # moto omits the "Tags" key entirely once a user has zero tags left.
    tags_after = {
        t["Key"]: t["Value"] for t in iam.get_user(UserName="alice")["User"].get("Tags", [])
    }
    assert "Environment" not in tags_after


@mock_aws
def test_add_user_to_new_group_then_remove() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "edit",
            "groups",
            "group_add",
            "__new_group__",
            "engineers",
            NAV_BACK,  # back from groups screen
            NAV_BACK,  # back from edit
            NAV_BACK,  # back from detail
        ]
    )
    action = IamFlow(ctx, prompter).menu()
    assert action is NavAction.STAY

    iam = ctx.client_factory.iam()
    groups = [g["GroupName"] for g in iam.list_groups_for_user(UserName="alice")["Groups"]]
    assert groups == ["engineers"]

    prompter2 = FakePrompter(
        [
            "search",
            "",
            "alice",
            "edit",
            "groups",
            "group_remove",
            "engineers",
            "yes",
            NAV_BACK,
            NAV_BACK,
            NAV_BACK
        ]
    )
    action2 = IamFlow(ctx, prompter2).menu()
    assert action2 is NavAction.STAY
    groups_after = iam.list_groups_for_user(UserName="alice")["Groups"]
    assert groups_after == []


@mock_aws
def test_grant_console_access_then_revoke() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "security",
            "console",
            "console_grant",  # auto-generates the password now, no manual entry
            "yes",  # confirm the reset
            NAV_BACK,  # back from console screen
            NAV_BACK,  # back from security menu
            NAV_BACK,  # back from detail
        ]
    )
    action = IamFlow(ctx, prompter).menu()
    assert action is NavAction.STAY

    iam = ctx.client_factory.iam()
    # moto's GetLoginProfile always reports PasswordResetRequired=False regardless of
    # what CreateLoginProfile was called with -- verified directly against moto below,
    # so this only asserts what moto can actually reflect back.
    assert iam.get_login_profile(UserName="alice")["LoginProfile"]["UserName"] == "alice"

    prompter2 = FakePrompter(
        [
            "search",
            "",
            "alice",
            "security",
            "console",
            "console_revoke",
            "yes",
            NAV_BACK,
            NAV_BACK,
            NAV_BACK,
        ]
    )
    action2 = IamFlow(ctx, prompter2).menu()
    assert action2 is NavAction.STAY
    with pytest.raises(botocore.exceptions.ClientError):
        iam.get_login_profile(UserName="alice")


@mock_aws
def test_console_access_create_login_password_via_security_menu() -> None:
    """Creating a login password is reachable through Security & Access -> Console
    Access -- no top-level "Reset Console Password" shortcut anymore, since that
    label was misleading for a user with no console access yet. Requires an
    explicit confirmation before it actually creates anything.
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "security",
            "console",
            "console_grant",
            "yes",  # confirm
            NAV_BACK,  # back from console screen
            NAV_BACK,  # back from security menu
            NAV_BACK,  # back from detail
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    iam = ctx.client_factory.iam()
    assert iam.get_login_profile(UserName="alice")["LoginProfile"]["UserName"] == "alice"


@mock_aws
def test_console_access_create_login_password_declined_leaves_login_profile_untouched() -> None:
    """Declining the confirmation prompt does nothing -- no login profile is created."""
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "security",
            "console",
            "console_grant",
            "no",  # decline
            NAV_BACK,
            NAV_BACK,
            NAV_BACK,
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    with pytest.raises(botocore.exceptions.ClientError):
        ctx.client_factory.iam().get_login_profile(UserName="alice")


@mock_aws
def test_console_access_create_login_password_back_leaves_login_profile_untouched() -> None:
    """"<- Back" at the confirmation is the same safe cancel as "No" -- not a full abort."""
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "security",
            "console",
            "console_grant",
            NAV_BACK,  # back at the confirmation
            NAV_BACK,  # back from console screen
            NAV_BACK,  # back from security menu
            NAV_BACK,  # back from detail
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    with pytest.raises(botocore.exceptions.ClientError):
        ctx.client_factory.iam().get_login_profile(UserName="alice")


def test_reset_console_password_confirmation_is_an_arrow_key_select_not_a_boolean_prompt() -> None:
    """The confirmation is a ``select`` (Yes/No/Back), not a ``confirm`` y/N prompt.

    Declining short-circuits before any IAM call, so no ``@mock_aws``/created user
    is needed here -- this only asserts which ``Prompter`` method gets used.
    """
    ctx = _app_ctx()

    class _NoConfirmPrompter(FakePrompter):
        def confirm(self, message: str, *, default: bool = False) -> bool:
            raise AssertionError(f"confirm() must not be used for: {message}")

    prompter = _NoConfirmPrompter(["no"])
    IamFlow(ctx, prompter)._set_console_password("alice")

    assert prompter.asked == ["Are you sure you want to reset the password for 'alice'?"]


@mock_aws
def test_user_detail_action_menu_has_no_delete_option() -> None:
    """The user detail screen's action panel is exactly [Edit Details, Security
    & Access, Enable/Disable, Back] -- selecting the old "delete_user" value
    (no longer a real ``Choice`` in this menu) matches nothing and simply
    redraws the same detail screen, never deleting the user.
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(["search", "", "alice", "delete_user", NAV_BACK])
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert any(u["UserName"] == "alice" for u in users)


@mock_aws
def test_create_and_deactivate_access_key(tmp_path: Path) -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "security",
            "keys",
            "key_create",
            NAV_BACK,  # back from keys screen
            NAV_BACK,  # back from security menu
            NAV_BACK,  # back from detail
        ]
    )
    action = IamFlow(ctx, prompter).menu()
    assert action is NavAction.STAY

    iam = ctx.client_factory.iam()
    keys = iam.list_access_keys(UserName="alice")["AccessKeyMetadata"]
    assert len(keys) == 1
    key_id = keys[0]["AccessKeyId"]
    assert keys[0]["Status"] == "Active"

    credentials_file = tmp_path / "keys" / "iam" / "credentials-alice.txt"
    assert credentials_file.exists()
    assert oct(credentials_file.stat().st_mode)[-3:] == "600"
    contents = credentials_file.read_text(encoding="utf-8")
    assert "# Username:    alice" in contents
    assert f"AWS_ACCESS_KEY_ID={key_id}" in contents
    assert "[alice]" in contents
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "./keys/iam/credentials-alice.txt" in output

    prompter2 = FakePrompter(
        [
            "search",
            "",
            "alice",
            "security",
            "keys",
            "key_toggle",
            key_id,
            NAV_BACK,
            NAV_BACK,
            NAV_BACK
        ]
    )
    action2 = IamFlow(ctx, prompter2).menu()
    assert action2 is NavAction.STAY
    assert iam.list_access_keys(UserName="alice")["AccessKeyMetadata"][0]["Status"] == "Inactive"


def _enable_virtual_mfa_device(ctx: AppContext, name: str, device_name: str) -> str:
    """Create and enable a virtual MFA device for ``name``; returns its serial (ARN)."""
    iam = ctx.client_factory.iam()
    serial = str(
        iam.create_virtual_mfa_device(VirtualMFADeviceName=device_name)["VirtualMFADevice"][
            "SerialNumber"
        ]
    )
    iam.enable_mfa_device(
        UserName=name,
        SerialNumber=serial,
        AuthenticationCode1="123456",
        AuthenticationCode2="123456",
    )
    return serial


@mock_aws
def test_mfa_device_deactivate_unlinks_but_keeps_the_virtual_device_object() -> None:
    """"Deactivate" only unlinks the device from the user -- the virtual device
    object itself still exists afterward (``list_virtual_mfa_devices``), unlike
    "Reset" which deletes it outright.
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")
    serial = _enable_virtual_mfa_device(ctx, "alice", "alice-mfa")

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "security",
            "mfa",
            "mfa_deactivate",
            serial,
            "yes",  # confirm
            NAV_BACK,  # back from MFA screen
            NAV_BACK,  # back from security menu
            NAV_BACK,  # back from detail
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    iam = ctx.client_factory.iam()
    assert iam.list_mfa_devices(UserName="alice")["MFADevices"] == []
    virtual_devices = iam.list_virtual_mfa_devices()["VirtualMFADevices"]
    virtual_serials = [d["SerialNumber"] for d in virtual_devices]
    assert serial in virtual_serials


@mock_aws
def test_mfa_device_reset_deactivates_and_deletes_the_virtual_device() -> None:
    """"Reset" deactivates AND deletes the virtual device object outright, so a
    fresh one can be registered from scratch -- unlike plain "Deactivate".
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")
    serial = _enable_virtual_mfa_device(ctx, "alice", "alice-mfa")

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "security",
            "mfa",
            "mfa_reset",
            serial,
            "yes",  # confirm
            NAV_BACK,
            NAV_BACK,
            NAV_BACK,
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    iam = ctx.client_factory.iam()
    assert iam.list_mfa_devices(UserName="alice")["MFADevices"] == []
    virtual_devices = iam.list_virtual_mfa_devices()["VirtualMFADevices"]
    virtual_serials = [d["SerialNumber"] for d in virtual_devices]
    assert serial not in virtual_serials


@mock_aws
def test_mfa_device_menu_actions_disabled_when_user_has_no_devices() -> None:
    """With no MFA device registered, both actions are declared ``disabled`` --
    selecting one anyway (a scripted test can still do this; a real
    ``questionary`` widget would refuse the keypress) must not attempt any
    IAM call.
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        [
            "search",
            "",
            "alice",
            "security",
            "mfa",
            NAV_BACK,  # back from MFA screen -- nothing to pick
            NAV_BACK,
            NAV_BACK,
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY


# -- Estado: Enable/Disable User via the shared deny-all policy ---------------------


@mock_aws
def test_disable_user_attaches_the_shared_deny_all_policy() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        ["search", "", "alice", "toggle_status", NAV_BACK]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    iam = ctx.client_factory.iam()
    attached = iam.list_attached_user_policies(UserName="alice")["AttachedPolicies"]
    assert any(p["PolicyName"] == "aws-admin-cli-deny-all" for p in attached)


@mock_aws
def test_enable_user_detaches_the_shared_deny_all_policy() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    # Disable, then re-enable in the same detail loop -- the toggle label flips itself.
    prompter = FakePrompter(
        ["search", "", "alice", "toggle_status", "toggle_status", NAV_BACK]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    iam = ctx.client_factory.iam()
    attached = iam.list_attached_user_policies(UserName="alice")["AttachedPolicies"]
    assert not any(p["PolicyName"] == "aws-admin-cli-deny-all" for p in attached)


@mock_aws
def test_deny_all_policy_is_created_once_and_reused_across_users() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")
    ctx.client_factory.iam().create_user(UserName="bob")

    prompter_alice = FakePrompter(["search", "", "alice", "toggle_status", NAV_BACK])
    IamFlow(ctx, prompter_alice).menu()
    prompter_bob = FakePrompter(["search", "", "bob", "toggle_status", NAV_BACK])
    IamFlow(ctx, prompter_bob).menu()

    iam = ctx.client_factory.iam()
    local_policies = iam.list_policies(Scope="Local")["Policies"]
    matches = [p for p in local_policies if p["PolicyName"] == "aws-admin-cli-deny-all"]
    assert len(matches) == 1


@mock_aws
def test_stats_active_count_drops_after_disabling_a_user() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")
    ctx.client_factory.iam().create_user(UserName="bob")

    # Disable alice first -- the stats line is computed fresh at the top of each
    # separate `.menu()` call, so this needs its own call before the one that
    # captures it, to see the post-disable count.
    disable_prompter = FakePrompter(["search", "", "alice", "toggle_status", NAV_BACK])
    IamFlow(ctx, disable_prompter).menu()

    err_console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, err_console=err_console)
    IamFlow(ctx, FakePrompter([NAV_BACK])).menu()

    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "Users: 2 Total  |  1 Active  |  1 Inactive" in output


@mock_aws
def test_user_detail_table_shows_the_eight_spec_d_columns_in_order() -> None:
    """The detail table's exact spec'd shape: Username / Status / Console /
    Active Keys / Groups / User ID / Last Seen / Tags, left to right, and
    nothing else -- MFA Enabled, the raw Access Keys list, CreateDate, and
    Attached Policies are gone; ARN/Path were never shown (never useful in
    the TUI, still available via the non-interactive CLI).
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    prompter = FakePrompter(["search", "", "alice", NAV_BACK])
    IamFlow(ctx, prompter).menu()

    output = console.file.getvalue()  # type: ignore[attr-defined]
    headers = [
        "Username",
        "Status",
        "Console",
        "Active Keys",
        "Groups",
        "User ID",
        "Last Seen",
        "Tags",
    ]
    positions = [output.index(header) for header in headers]
    assert positions == sorted(positions)  # left to right, in exactly this order

    assert "Active" in output
    assert "Never" in output  # moto never sets PasswordLastUsed

    assert "MFA Enabled" not in output
    assert "Attached Policies" not in output
    assert "CreateDate" not in output
    assert "UserId" not in output  # replaced by "User ID"
    assert "Last Activity" not in output  # replaced by "Last Seen"
    assert "Console Access" not in output  # replaced by "Console"
    assert "Active Access Keys" not in output  # replaced by "Active Keys"
    assert "Arn" not in output
    assert "Path" not in output


@mock_aws
def test_user_detail_table_has_one_blank_line_below_the_header_box() -> None:
    """The header box (``clear_and_banner``, on ``err_console``) and this table's own
    top border must not sit glued together -- one blank line breathes between them.
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    prompter = FakePrompter(["search", "", "alice", NAV_BACK])
    IamFlow(ctx, prompter).menu()

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert output.startswith("\n")
    assert not output.startswith("\n\n")


@mock_aws
def test_search_no_longer_renders_a_results_table() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    prompter = FakePrompter(["search", "", "alice", NAV_BACK])
    IamFlow(ctx, prompter).menu()

    # The picker leads straight into the detail view -- "user list" (the old
    # results-table title) never gets rendered before it.
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "user list" not in output


# -- Crear: New and Copy -------------------------------------------------------------


def test_generate_secure_password_meets_complexity_and_length() -> None:
    """No AWS/moto involved -- this is what our own code controls (moto doesn't
    faithfully echo back ``PasswordResetRequired``, so the integration tests below
    can't verify complexity or the flag through ``GetLoginProfile``)."""
    for _ in range(200):
        password = _generate_secure_password()
        assert len(password) == 16
        assert any(c.isupper() for c in password)
        assert any(c.islower() for c in password)
        assert any(c.isdigit() for c in password)
        assert any(c in "!@#$%^&*()-_=+" for c in password)

    assert len(_generate_secure_password(length=12)) == 12
    assert _generate_secure_password() != _generate_secure_password()


def test_console_confirm_always_forces_mandatory_password_reset() -> None:
    """Granting console access always sets ``reset_required = True`` -- there is
    no step anywhere (Create User or Copy User) that lets an admin choose
    otherwise. Only "Grant console access?" is ever asked here.
    """
    ctx = _app_ctx()
    access = _AccessPrefsState()
    prompter = FakePrompter(["yes"])

    nav = IamFlow(ctx, prompter)._access_prefs_step_console_confirm(access)

    assert nav is _WizardNav.NEXT
    assert access.wants_console is True
    assert access.reset_required is True
    assert prompter.asked == ["Grant console access?"]


def test_console_confirm_declined_still_defaults_reset_required_true() -> None:
    """Declining console access leaves ``reset_required`` at its default (unused,
    since no password is ever set for this user) -- never flips to ``False``.
    """
    ctx = _app_ctx()
    access = _AccessPrefsState()
    prompter = FakePrompter(["no"])

    nav = IamFlow(ctx, prompter)._access_prefs_step_console_confirm(access)

    assert nav is _WizardNav.NEXT
    assert access.wants_console is False
    assert access.reset_required is True


@mock_aws
def test_ensure_demo_iam_resources_creates_sample_groups_and_policies() -> None:
    ctx = _local_app_ctx()
    use_cases = build_iam_use_cases(ctx)

    _ensure_demo_iam_resources(ctx, use_cases)

    group_names = {g.group_name for g in use_cases.list_groups.execute(None)}
    assert {"Administrators", "Developers", "ReadOnlyUsers"} <= group_names
    policy_names = {
        p.policy_name for p in use_cases.list_policies.execute(scope="Local", only_attached=False)
    }
    assert {"SampleReadOnlyAccess", "SampleDeveloperAccess"} <= policy_names


@mock_aws
def test_ensure_demo_iam_resources_is_idempotent_on_a_second_call() -> None:
    ctx = _local_app_ctx()
    use_cases = build_iam_use_cases(ctx)

    _ensure_demo_iam_resources(ctx, use_cases)
    _ensure_demo_iam_resources(ctx, use_cases)

    admins = [g for g in use_cases.list_groups.execute(None) if g.group_name == "Administrators"]
    assert len(admins) == 1


def test_ensure_demo_iam_resources_never_mutates_against_a_non_local_target() -> None:
    """No ``endpoint_url`` -> ``is_local`` is False -> must never touch AWS.

    No ``@mock_aws`` here on purpose, same guard-under-test reasoning
    ``test_sg_seed.py``'s own non-local test uses: ``testprofile`` doesn't
    exist on this machine, so if the guard were broken this would fail
    loudly with a profile error instead of silently mutating something.
    """
    ctx = _app_ctx()  # no endpoint_url -> not local
    use_cases = build_iam_use_cases(ctx)
    _ensure_demo_iam_resources(ctx, use_cases)


@mock_aws
def test_create_new_user_minimal() -> None:
    ctx = _app_ctx()
    prompter = FakePrompter(
        [
            "create_user",
            "new",
            "alice",  # name
            "no",  # no console access
            "no",  # no programmatic access
            # no groups step -- account has none, auto-skipped; creation never asks about
            # policies at all (see _CreateUserStep -- attach them afterward via Edit User)
            "no",  # no tags
            "yes",  # confirm creation
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert any(u["UserName"] == "alice" for u in users)


@mock_aws
def test_create_new_user_never_asks_about_policies_even_when_the_account_has_some() -> None:
    """Policy attachment is no longer part of Create User (New) at all -- not
    merely skipped-when-empty, structurally absent from ``_CreateUserStep``.
    Seeding demo policies (which used to trigger a "Which policies do you
    want to attach?" checkbox step once the account had any) must not change
    the prompt sequence, and the new user must come out with zero attached
    policies -- they're attached afterward via Edit User -> Attached Policies.
    """
    ctx = _local_app_ctx()
    use_cases = build_iam_use_cases(ctx)
    _ensure_demo_iam_resources(ctx, use_cases)  # seeds demo groups AND policies
    prompter = FakePrompter(
        [
            "create_user",
            "new",
            "alice",
            "no",  # no console access
            "no",  # no programmatic access
            [],  # groups checkbox (demo groups exist, so it's offered) -- decline all
            # no policies step -- structurally gone, regardless of what's available
            "no",  # no tags
            "yes",  # confirm creation
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    iam = ctx.client_factory.iam()
    assert any(u["UserName"] == "alice" for u in iam.list_users()["Users"])
    assert iam.list_attached_user_policies(UserName="alice")["AttachedPolicies"] == []


@mock_aws
def test_create_new_user_with_console_and_key_and_tags() -> None:
    ctx = _app_ctx()
    prompter = FakePrompter(
        [
            "create_user",
            "new",
            "alice",
            "yes",  # wants console -- password is now auto-generated, reset always mandatory
            "yes",  # wants programmatic access
            "yes",  # wants tags
            "Owner",  # tag key
            "bob",  # tag value
            "no",  # no more tags
            "yes",  # confirm creation
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    iam = ctx.client_factory.iam()
    assert iam.get_login_profile(UserName="alice")["LoginProfile"]["UserName"] == "alice"
    assert len(iam.list_access_keys(UserName="alice")["AccessKeyMetadata"]) == 1
    tags = {t["Key"]: t["Value"] for t in iam.get_user(UserName="alice")["User"]["Tags"]}
    assert tags["Owner"] == "bob"


@mock_aws
def test_create_new_user_with_programmatic_access_saves_credentials_file(
    tmp_path: Path,
) -> None:
    """Enabling Programmatic Access writes ``keys/iam/credentials-<name>.txt`` at the
    project root (same isolated ``tmp_path`` cwd the global ``isolated_env``
    fixture already chdirs every test into), and the success screen shows the
    saved path.
    """
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    prompter = FakePrompter(
        [
            "create_user",
            "new",
            "alice",
            "no",  # no console access
            "yes",  # wants programmatic access
            "no",  # no tags
            "yes",  # confirm creation
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    credentials_file = tmp_path / "keys" / "iam" / "credentials-alice.txt"
    assert credentials_file.exists()
    assert oct(credentials_file.stat().st_mode)[-3:] == "600"

    contents = credentials_file.read_text(encoding="utf-8")
    iam = ctx.client_factory.iam()
    key = iam.list_access_keys(UserName="alice")["AccessKeyMetadata"][0]
    assert "# Username:    alice" in contents
    assert "# Environment: AWS" in contents
    assert "# Generated:" in contents
    assert f"AWS_ACCESS_KEY_ID={key['AccessKeyId']}" in contents
    assert "AWS_SECRET_ACCESS_KEY=" in contents
    assert "[alice]" in contents
    assert f"aws_access_key_id = {key['AccessKeyId']}" in contents
    assert "aws_secret_access_key = " in contents
    assert "region = us-east-1" in contents

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "./keys/iam/credentials-alice.txt" in output


def test_render_credentials_file_contents_labels_a_localstack_target() -> None:
    """``is_local=True`` -> the header's Environment line reads "LocalStack"."""
    contents = _render_credentials_file_contents(
        name="alice", access_key_id="AKIAEXAMPLE", secret_access_key="secret", is_local=True
    )
    assert "# Username:    alice" in contents
    assert "# Environment: LocalStack" in contents


def test_render_credentials_file_contents_labels_a_real_aws_target() -> None:
    """``is_local=False`` -> the header's Environment line reads "AWS"."""
    contents = _render_credentials_file_contents(
        name="alice", access_key_id="AKIAEXAMPLE", secret_access_key="secret", is_local=False
    )
    assert "# Environment: AWS" in contents


def test_render_credentials_file_contents_includes_the_ini_block() -> None:
    """The ``~/.aws/credentials``-shaped block is present, region hardcoded."""
    contents = _render_credentials_file_contents(
        name="alice", access_key_id="AKIAEXAMPLE", secret_access_key="s3cr3t", is_local=True
    )
    assert "AWS_ACCESS_KEY_ID=AKIAEXAMPLE" in contents
    assert "AWS_SECRET_ACCESS_KEY=s3cr3t" in contents
    assert "[alice]" in contents
    assert "aws_access_key_id = AKIAEXAMPLE" in contents
    assert "aws_secret_access_key = s3cr3t" in contents
    assert "region = us-east-1" in contents


@mock_aws
def test_create_new_user_sanitizes_spaces_and_uses_sanitized_name_downstream() -> None:
    """A space-containing name (e.g. "Harold Ortega", the real incident this
    guards against) must not just create a sanitized user -- every
    post-creation call (group membership, console password, access key) has
    to target the ACTUAL created name, not the raw typed one.
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_group(GroupName="engineers")
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, console=console, err_console=err_console)

    prompter = FakePrompter(
        [
            "create_user",
            "new",
            "Harold Ortega",  # name, contains a space
            "yes",  # wants console -- password is now auto-generated, reset always mandatory
            "yes",  # wants programmatic access
            ["engineers"],  # groups checkbox
            # creation never asks about policies -- attach them afterward via Edit User
            "no",  # no tags
            "yes",  # confirm creation
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    iam = ctx.client_factory.iam()
    users = [u["UserName"] for u in iam.list_users()["Users"]]
    assert "Harold_Ortega" in users
    assert "Harold Ortega" not in users
    assert iam.get_login_profile(UserName="Harold_Ortega")["LoginProfile"]["UserName"] == (
        "Harold_Ortega"
    )
    assert len(iam.list_access_keys(UserName="Harold_Ortega")["AccessKeyMetadata"]) == 1
    groups = [g["GroupName"] for g in iam.list_groups_for_user(UserName="Harold_Ortega")["Groups"]]
    assert groups == ["engineers"]
    assert "sanitized" in err_console.file.getvalue()  # type: ignore[attr-defined]


@mock_aws
def test_create_new_user_with_two_tags() -> None:
    ctx = _app_ctx()
    prompter = FakePrompter(
        [
            "create_user",
            "new",
            "alice",
            "no",  # no console access
            "no",  # no programmatic access
            "yes",  # wants tags
            "Owner",
            "bob",
            "yes",  # add another tag
            "Team",
            "platform",
            "no",  # no more tags
            "yes",  # confirm creation
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    iam = ctx.client_factory.iam()
    tags = {t["Key"]: t["Value"] for t in iam.get_user(UserName="alice")["User"]["Tags"]}
    assert tags["Owner"] == "bob"
    assert tags["Team"] == "platform"


@mock_aws
def test_create_new_user_cancelled_at_username_typing_the_word_aborts_cleanly() -> None:
    """Typing "cancel" at the Username prompt (not Ctrl+C) must print the
    visible notice and abort before anything is created -- the whole point
    of the new typed-abort-word contract in ``prompt_available_name``.
    """
    ctx = _app_ctx()
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, err_console=err_console)

    prompter = FakePrompter(["create_user", "new", "cancel"])
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert ctx.client_factory.iam().list_users()["Users"] == []
    assert "❌ Operation cancelled" in err_console.file.getvalue()  # type: ignore[attr-defined]


@mock_aws
def test_create_new_user_back_at_tag_key_returns_to_groups_not_the_tags_gate() -> None:
    """Typing "back" at a Tag key prompt goes straight to Groups -- the wizard's
    real previous step -- not a re-show of "Do you want to add tags?". A demo
    group is created first so Groups is actually offered. The wizard can still
    be completed afterward, proving no state was lost along the way.
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_group(GroupName="engineers")
    prompter = FakePrompter(
        [
            "create_user",
            "new",
            "alice",
            "no",  # no console access
            "no",  # no programmatic access
            ["engineers"],  # groups checkbox, forward
            "yes",  # wants tags
            "back",  # typed at the tag key prompt -> straight back to Groups
            ["engineers"],  # groups checkbox, re-shown, forward again
            "no",  # this time, decline tags
            "yes",  # confirm creation
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert any(u["UserName"] == "alice" for u in users)

    username_prompt = "Username (type 'cancel' to abort):"
    console_prompt = "Grant console access?"
    programmatic_prompt = "Enable programmatic access? (creates an Access Key)"
    groups_prompt = "Which groups do they belong to? (optional)"
    tags_gate_prompt = "Do you want to add tags to this user?"
    tag_key_prompt = "Tag key (blank or 'back' to return to Groups):"
    confirm_prompt = "Create this user?"

    assert prompter.asked == [
        "IAM (Users) -- What do you want to do?",
        "Create user -- how?",
        username_prompt,
        console_prompt,
        programmatic_prompt,
        groups_prompt,
        tags_gate_prompt,
        tag_key_prompt,  # "back" here -> straight to Groups, tags gate never re-shown for it
        groups_prompt,
        tags_gate_prompt,  # asked again on this fresh pass through Tags, as expected
        confirm_prompt,
    ]


def test_prompt_tag_field_treats_back_atras_cancel_and_blank_as_go_back() -> None:
    """Unit-level lock-in for ``_prompt_tag_field``'s exact trigger set: any of
    "back"/"atras"/"cancel" (case-insensitive, surrounding whitespace ignored),
    a blank line, or Ctrl+C/EOF (``None`` from the prompter) all mean the same
    thing -- ``None``, "go back to Groups" -- and nothing else does.
    """
    ctx = _app_ctx()
    flow = IamFlow(ctx, FakePrompter([]))

    for trigger in ("back", "BACK", "  back  ", "atras", "Atras", "cancel", "CANCEL", "", "   "):
        flow.prompter = FakePrompter([trigger])
        assert flow._prompt_tag_field("Tag key") is None

    flow.prompter = FakePrompter([None])
    assert flow._prompt_tag_field("Tag key") is None

    flow.prompter = FakePrompter(["Owner"])
    assert flow._prompt_tag_field("Tag key") == "Owner"


@mock_aws
def test_create_new_user_back_at_add_another_tag_also_returns_to_groups() -> None:
    """"<- Atrás" at "Add another tag?" -- after at least one tag was already typed
    this pass -- resolves the same way as Back at the Tag key/value prompts: straight
    to Groups, not a re-show of the tags gate. Consistent single Back target
    throughout this whole step, not two different behaviors depending on exactly
    which sub-prompt the admin backs out from.
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_group(GroupName="engineers")
    prompter = FakePrompter(
        [
            "create_user",
            "new",
            "alice",
            "no",  # no console access
            "no",  # no programmatic access
            ["engineers"],  # groups checkbox, forward
            "yes",  # wants tags
            "Owner",  # tag key
            "bob",  # tag value
            NAV_BACK,  # "Add another tag?" -> Atrás -> straight back to Groups
            ["engineers"],  # groups checkbox, re-shown, forward again
            "no",  # decline tags this time
            "yes",  # confirm creation
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert any(u["UserName"] == "alice" for u in users)
    tags = {
        t["Key"]: t["Value"]
        for t in ctx.client_factory.iam().get_user(UserName="alice")["User"]["Tags"]
    }
    assert "Owner" not in tags  # the "Owner": "bob" entry was mid-pass, never committed


@mock_aws
def test_create_new_user_step_navigation_backs_up_one_step_at_a_time_never_forward() -> None:
    """The wizard's state machine is single-source-of-truth ordered: Name / Console /
    Programmatic / Groups / Tags / Confirm (see ``_create_user_next_step`` /
    ``_create_user_prev_step``). "<- Atrás" at Programmatic must land
    deterministically on Console, and at Console on Username; "<- Atrás" at the
    Tags gate must land deterministically on Groups, re-showing that same
    checkbox -- never skipping a step and never bouncing forward past where
    "<- Atrás" was pressed. A demo group is created first so the Groups step is
    actually offered (an account with none auto-skips it, which would make this
    path untestable). Groups' OWN "<- Atrás" retreats to Programmatic Access
    like every other step, and is covered separately by
    ``test_create_new_user_groups_back_retreats_one_step_to_programmatic_access``.
    Asserting the exact ordered ``prompter.asked`` transcript -- not just the
    end state -- is what catches a transition function that quietly loops
    forward instead of stepping back.
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_group(GroupName="engineers")
    prompter = FakePrompter(
        [
            "create_user",
            "new",
            "alice",  # 1st Username visit
            "no",  # Console (forward)
            NAV_BACK,  # Programmatic -> Atrás -> must land back on Console
            NAV_BACK,  # Console -> Atrás -> must land back on Username
            "alice",  # 2nd Username visit -- proves state survived the round trip
            "no",  # Console (forward, 2nd time)
            "no",  # Programmatic (forward, 2nd time)
            ["engineers"],  # Groups checkbox (forward)
            NAV_BACK,  # Tags gate -> Atrás -> must land back on Groups, never forward again
            ["engineers"],  # Groups checkbox (re-shown, forward again)
            "no",  # Tags (decline, forward this time)
            "yes",  # confirm creation
        ]
    )

    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert any(u["UserName"] == "alice" for u in users)
    groups = [
        g["GroupName"]
        for g in ctx.client_factory.iam().list_groups_for_user(UserName="alice")["Groups"]
    ]
    assert groups == ["engineers"]

    username_prompt = "Username (type 'cancel' to abort):"
    console_prompt = "Grant console access?"
    programmatic_prompt = "Enable programmatic access? (creates an Access Key)"
    groups_prompt = "Which groups do they belong to? (optional)"
    tags_prompt = "Do you want to add tags to this user?"
    confirm_prompt = "Create this user?"

    assert prompter.asked == [
        "IAM (Users) -- What do you want to do?",
        "Create user -- how?",
        username_prompt,
        console_prompt,
        programmatic_prompt,
        console_prompt,  # Atrás from Programmatic -> Console, never forward
        username_prompt,  # Atrás from Console -> Username, the wizard's first step
        console_prompt,
        programmatic_prompt,
        groups_prompt,
        tags_prompt,  # forward to Tags
        groups_prompt,  # Atrás from Tags -> Groups, never straight back to Tags
        tags_prompt,
        confirm_prompt,
    ]


@mock_aws
def test_create_new_user_groups_back_retreats_one_step_to_programmatic_access() -> None:
    """"<- Atrás" at Groups steps back to Programmatic Access -- it does NOT abort.

    Previously this one screen was the wizard's odd one out: its Back aborted
    Create User (New) outright. Backing out of Groups now behaves like Back on
    every other step, which is what makes the chain in
    ``test_create_new_user_back_from_groups_walks_all_the_way_out_to_the_iam_menu``
    possible at all.
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_group(GroupName="engineers")
    prompter = FakePrompter(
        [
            "create_user",
            "new",
            "alice",
            "no",  # console
            "no",  # programmatic
            [NAV_BACK],  # Groups checkbox, "<- Atrás" ticked -> must land on Programmatic
            "no",  # Programmatic (re-shown), forward again
            ["engineers"],  # Groups checkbox (re-shown), forward
            "no",  # tags
            "yes",  # confirm creation
        ]
    )

    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    programmatic_prompt = "Enable programmatic access? (creates an Access Key)"
    groups_prompt = "Which groups do they belong to? (optional)"
    # The re-shown screen after Back is Programmatic Access, not Tags and not Groups.
    assert prompter.asked.index(programmatic_prompt) < prompter.asked.index(groups_prompt)
    assert prompter.asked.count(programmatic_prompt) == 2
    assert [u["UserName"] for u in ctx.client_factory.iam().list_users()["Users"]] == ["alice"]


@mock_aws
def test_create_new_user_back_from_groups_walks_all_the_way_out_to_the_iam_menu() -> None:
    """Back, pressed repeatedly from Groups, retreats one screen at a time and exits.

    Groups -> Programmatic -> Console -> Username -> the main IAM menu. This is
    the behavior the whole wizard promises and the one screen that used to break
    it (Groups) is now part of the chain. No user is created along the way.
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_group(GroupName="engineers")
    prompter = FakePrompter(
        [
            "create_user",
            "new",
            "alice",
            "no",  # console
            "no",  # programmatic
            [NAV_BACK],  # Groups checkbox, "<- Atrás" ticked -> Programmatic
            NAV_BACK,  # Programmatic -> Console
            NAV_BACK,  # Console  -> Username
            "cancel",  # Username -> out of the wizard, back to the IAM menu
        ]
    )

    action = IamFlow(ctx, prompter).menu()

    # STAY = the wizard handed control back to the IAM screen, which redraws itself.
    assert action is NavAction.STAY
    assert ctx.client_factory.iam().list_users()["Users"] == []
    username_prompt = "Username (type 'cancel' to abort):"
    console_prompt = "Grant console access?"
    programmatic_prompt = "Enable programmatic access? (creates an Access Key)"
    # Every screen on the way out is visited exactly once more, in reverse order --
    # no screen skipped, and nothing bounced forward.
    assert prompter.asked[-3:] == [programmatic_prompt, console_prompt, username_prompt]


@mock_aws
def test_create_new_user_groups_back_is_reachable_with_a_plain_enter_via_real_widget() -> None:
    """Drives the REAL ``QuestionaryPrompter`` with piped keystrokes, not ``FakePrompter``.

    "<- Atrás" is now a single tickable row appended to the SAME groups
    checkbox -- no separate "N selected, Continuar?" screen after it. Ticking
    it with <space> and pressing Enter must read back as Back regardless of
    what else was ticked alongside it, and a forward submission (Atrás left
    untouched) must commit the ticked groups and advance immediately.
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_group(GroupName="engineers")
    use_cases = build_iam_use_cases(ctx)

    def _drive(keys: str) -> tuple[_WizardNav, _CreateUserState]:
        state = _CreateUserState(available_groups=use_cases.list_groups.execute(None))
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            with create_app_session(input=pipe, output=DummyOutput()):
                return IamFlow(ctx, QuestionaryPrompter())._create_user_step_groups(state), state

    down = "\x1b[B"
    space = " "
    enter = "\r"
    ctrl_c = "\x03"

    # Tick "engineers", then move down onto "<- Atrás" and tick it too, then Enter.
    # "<- Atrás" being ticked wins: Back, and this pass's ticks are never committed.
    nav, state = _drive(space + down + space + enter)
    assert nav is _WizardNav.BACK
    assert state.group_names == []

    # Forward: tick "engineers" only, Enter -- no second screen in between.
    nav, state = _drive(space + enter)
    assert nav is _WizardNav.NEXT
    assert state.group_names == ["engineers"]

    # Forward with nothing ticked at all is also a valid, immediate submission.
    nav, state = _drive(enter)
    assert nav is _WizardNav.NEXT
    assert state.group_names == []

    # Cancelling (Ctrl+C) is Back too, per the Prompter cancellation contract.
    assert _drive(ctrl_c)[0] is _WizardNav.BACK


@mock_aws
def test_copy_user_clones_tags_groups_and_policies() -> None:
    ctx = _app_ctx()
    iam = ctx.client_factory.iam()
    iam.create_user(UserName="alice", Tags=[{"Key": "Team", "Value": "platform"}])
    iam.create_group(GroupName="engineers")
    iam.add_user_to_group(GroupName="engineers", UserName="alice")
    policy_arn = iam.create_policy(
        PolicyName="alice-policy",
        PolicyDocument=(
            '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
            '"Action": "s3:ListBucket", "Resource": "*"}]}'
        ),
    )["Policy"]["Arn"]
    iam.attach_user_policy(UserName="alice", PolicyArn=policy_arn)

    prompter = FakePrompter(
        [
            "create_user",
            "copy",
            "",  # search query: blank = everyone
            "alice",  # source picker
            "bob",  # new name
            "no",  # no console access for the new user
            "no",  # no programmatic access for the new user
            "yes",  # confirm creation
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    tags = {t["Key"]: t["Value"] for t in iam.get_user(UserName="bob")["User"]["Tags"]}
    assert tags["Team"] == "platform"
    groups = [g["GroupName"] for g in iam.list_groups_for_user(UserName="bob")["Groups"]]
    assert groups == ["engineers"]
    policies = [
        p["PolicyName"] for p in iam.list_attached_user_policies(UserName="bob")["AttachedPolicies"]
    ]
    assert policies == ["alice-policy"]


@mock_aws
def test_copy_user_sanitizes_spaces_in_new_name() -> None:
    ctx = _app_ctx()
    iam = ctx.client_factory.iam()
    iam.create_user(UserName="alice")
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, err_console=err_console)

    prompter = FakePrompter(
        [
            "create_user",
            "copy",
            "",  # search query: blank = everyone
            "alice",  # source picker
            "Harold Ortega",  # new name, contains a space
            "no",  # no console access for the new user
            "no",  # no programmatic access for the new user
            "yes",  # confirm creation
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = [u["UserName"] for u in iam.list_users()["Users"]]
    assert "Harold_Ortega" in users
    assert "Harold Ortega" not in users
    assert "sanitized" in err_console.file.getvalue()  # type: ignore[attr-defined]


@mock_aws
def test_copy_user_with_programmatic_access_saves_credentials_file(tmp_path: Path) -> None:
    """Copy User must generate an access key AND save it, exactly like Create New User:
    ``keys/iam/credentials-<new-name>.txt`` at 0600, path shown on the success screen.
    """
    ctx = _app_ctx()
    iam = ctx.client_factory.iam()
    iam.create_user(UserName="alice")
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    prompter = FakePrompter(
        [
            "create_user",
            "copy",
            "",  # search query: blank = everyone
            "alice",  # source picker
            "bob",  # new name
            "no",  # no console access for the new user
            "yes",  # wants programmatic access
            "yes",  # confirm creation
        ]
    )
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert len(iam.list_access_keys(UserName="bob")["AccessKeyMetadata"]) == 1

    credentials_file = tmp_path / "keys" / "iam" / "credentials-bob.txt"
    assert credentials_file.exists()
    assert oct(credentials_file.stat().st_mode)[-3:] == "600"

    contents = credentials_file.read_text(encoding="utf-8")
    key = iam.list_access_keys(UserName="bob")["AccessKeyMetadata"][0]
    assert "# Username:    bob" in contents
    assert f"AWS_ACCESS_KEY_ID={key['AccessKeyId']}" in contents
    assert "[bob]" in contents

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "./keys/iam/credentials-bob.txt" in output


# -- Delete User: filter-pick-confirm, same shape as S3/EC2's delete ---------------


@mock_aws
def test_delete_user_removes_the_user_after_confirmation() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(["delete_user", "", "alice", "yes"])
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert not any(u["UserName"] == "alice" for u in users)


@mock_aws
def test_declining_delete_confirmation_keeps_the_user() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(["delete_user", "", "alice", "no"])
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert any(u["UserName"] == "alice" for u in users)


@mock_aws
def test_delete_user_with_attachments_prompts_for_force_then_deletes() -> None:
    """``DeleteUserUseCase`` blocks on an attached policy unless ``force=True`` --
    the flow surfaces that as a follow-up confirm, same as EC2's terminate-instance
    force retry.
    """
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")
    _disable_user_for_test(ctx, "alice")  # attaches the shared deny-all policy

    prompter = FakePrompter(["delete_user", "", "alice", "yes", "yes"])
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert not any(u["UserName"] == "alice" for u in users)


@mock_aws
def test_delete_user_with_attachments_declining_force_keeps_the_user() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")
    _disable_user_for_test(ctx, "alice")

    prompter = FakePrompter(["delete_user", "", "alice", "yes", "no"])
    action = IamFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    users = ctx.client_factory.iam().list_users()["Users"]
    assert any(u["UserName"] == "alice" for u in users)
