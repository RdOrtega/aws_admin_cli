"""The IAM TUI screen: users, their lifecycle, groups, console access, and access keys.

Follows the same patterns ``s3_flow.py``/``ec2_flow.py`` established (see
``s3_flow.py``'s module docstring for the full rationale): dynamic lists,
filter-then-list (never an auto-pick). Create User (New) and Copy User are
explicit step-pointer state machines -- same ``_WizardNav``/next-step/
prev-step architecture as ``ec2_flow.py``'s creation wizards -- so "<- Back"
always lands on the exact predecessor step, never a hardcoded nested loop a
user can get trapped in.

Delete User follows the same filter-then-pick-then-confirm shape as S3's
delete bucket / EC2's terminate instance: search/filter to a list (never an
auto-pick), an explicit arrow-key ``confirm_destructive`` gate (default
cursor on NO), then the delete itself -- retried with ``force=True`` only if
the plain attempt is rejected for still-attached policies/groups/console
access/keys, exactly ``DeleteUserUseCase``'s own guard rail. Roles/policies/
instance-profiles management stays off this menu (the OLD IAM screen's
scope, intentionally not carried over here).
"""

import os
import secrets
import string
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import ClassVar, Self

from rich.table import Table

from aws_admin_cli.application.dto.iam import (
    AddUserToGroupRequest,
    AttachPolicyRequest,
    CopyUserRequest,
    CreateAccessKeyRequest,
    CreateGroupRequest,
    CreatePolicyRequest,
    CreateUserRequest,
    DeactivateMfaDeviceRequest,
    DeleteAccessKeyRequest,
    DeleteLoginProfileRequest,
    DeleteUserRequest,
    DeleteUserTagsRequest,
    DetachPolicyRequest,
    RemoveUserFromGroupRequest,
    SetLoginProfileRequest,
    SetUserTagsRequest,
    UpdateAccessKeyRequest,
    UpdateUserRequest,
)
from aws_admin_cli.application.services.iam_audit_metadata import DISABLED_AT, write_user_metadata
from aws_admin_cli.application.use_cases.iam.get_user_detail import UserDetail
from aws_admin_cli.application.use_cases.iam.resolve_deny_all_policy import (
    DENY_ALL_POLICY_NAME,
)
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.exceptions import ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.iam import (
    AccessKeyMetadata,
    AttachedPolicy,
    IamGroup,
    IamUser,
    LoginProfile,
    PolicyDocument,
    PolicyEffect,
    PolicyStatement,
    sanitize_user_name,
    validate_resource_name,
)
from aws_admin_cli.presentation.cli.formatters.render import render
from aws_admin_cli.presentation.tui.flows._shared import (
    announce_result,
    clear_and_banner,
    confirm_destructive,
    confirm_yes_no,
    prompt_available_name,
    prompt_text_or_cancel,
    relative_path_display,
    run_with_spinner,
)
from aws_admin_cli.presentation.tui.flows.error_handler import aws_error_handler
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT, Choice, Separator
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.tui.prompter import Prompter
from aws_admin_cli.presentation.wiring import IamUseCases, build_iam_use_cases

__all__ = ["IamFlow"]

_WIZARD_BACK_LABEL = "<- Atrás"


class _WizardNav(Enum):
    """What a Create/Copy User wizard step returns to its driving loop.

    Same three-value contract ``ec2_flow.py``'s wizards use: NEXT advances
    to the next step, BACK retreats to the immediate predecessor (state
    collected so far is never cleared, so re-showing a step keeps its prior
    answer as the default), CANCEL aborts the whole wizard immediately.
    """

    NEXT = "next"
    BACK = "back"
    CANCEL = "cancel"


@dataclass(slots=True)
class _AccessPrefsState:
    """Console/Programmatic access preferences, shared by Create User (New) and Copy User.

    Same "shared reusable sub-state" idea as ``ec2_flow.py``'s ``_OwnerSubState``:
    both wizards embed one of these and drive the same two step methods
    (``_access_prefs_step_console_confirm``/``_programmatic``) from their own
    driving loop, so this exact Console -> Programmatic sequence lives in
    exactly one place. ``reset_required`` is NOT a question anyone is ever
    asked -- it stays hardcoded ``True`` (a compliance rule, not an admin
    choice) for every user granted console access here; it exists on this
    state only so ``_create_user_execute``/``_copy_user_execute`` have a
    single field to read when building the ``SetLoginProfileRequest``.
    """

    wants_console: bool = False
    reset_required: bool = True
    wants_key: bool = False


class _CreateUserStep(Enum):
    """Every question Create User (New) can ask, in forward order.

    ``_create_new_user``'s driving loop is the ONLY thing that sequences
    these -- no step method ever calls another step method. Mirrors
    ``ec2_flow.py``'s ``_QuickLaunchStep`` exactly: ``_create_user_next_step``/
    ``_create_user_prev_step`` are the single source of truth for
    "forward"/"back", including the conditional skip (GROUPS only when the
    account actually has any to offer). There is deliberately no
    "require password reset?" step -- see ``_AccessPrefsState``. There is also
    deliberately no policy-attachment step: a brand-new user is created bare
    and policies are attached afterward via the Edit User -> Attached
    Policies screen, not during creation.
    """

    NAME = "name"
    CONSOLE_CONFIRM = "console_confirm"
    PROGRAMMATIC_CONFIRM = "programmatic_confirm"
    GROUPS = "groups"
    TAGS = "tags"
    CONFIRM = "confirm"


@dataclass(slots=True)
class _CreateUserState:
    """What's been collected so far across Create User (New)'s steppable questions.

    ``available_groups`` is fetched once, before the driving loop starts (it
    doesn't depend on any earlier answer), and cached here so the GROUPS step
    and both transition functions (deciding whether to even show that step)
    all reuse the same list instead of re-querying IAM on every visit.
    """

    name: str | None = None
    access: _AccessPrefsState = field(default_factory=_AccessPrefsState)
    available_groups: list[IamGroup] = field(default_factory=list)
    group_names: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)


def _create_user_next_step(
    current: _CreateUserStep, state: _CreateUserState
) -> _CreateUserStep:
    """What "forward" means from ``current`` -- the single source of truth for it."""
    if current is _CreateUserStep.NAME:
        return _CreateUserStep.CONSOLE_CONFIRM
    if current is _CreateUserStep.CONSOLE_CONFIRM:
        return _CreateUserStep.PROGRAMMATIC_CONFIRM
    if current is _CreateUserStep.PROGRAMMATIC_CONFIRM:
        return _CreateUserStep.GROUPS if state.available_groups else _CreateUserStep.TAGS
    if current is _CreateUserStep.GROUPS:
        return _CreateUserStep.TAGS
    if current is _CreateUserStep.TAGS:
        return _CreateUserStep.CONFIRM
    raise AssertionError(f"CONFIRM has no 'next' step: {current}")


def _create_user_prev_step(
    current: _CreateUserStep, state: _CreateUserState
) -> _CreateUserStep | None:
    """What "back" means from ``current`` -- ``None`` means "exit the whole wizard"."""
    if current is _CreateUserStep.NAME:
        return None
    if current is _CreateUserStep.CONSOLE_CONFIRM:
        return _CreateUserStep.NAME
    if current is _CreateUserStep.PROGRAMMATIC_CONFIRM:
        return _CreateUserStep.CONSOLE_CONFIRM
    if current is _CreateUserStep.GROUPS:
        return _CreateUserStep.PROGRAMMATIC_CONFIRM
    if current is _CreateUserStep.TAGS:
        if state.available_groups:
            return _CreateUserStep.GROUPS
        return _CreateUserStep.PROGRAMMATIC_CONFIRM
    if current is _CreateUserStep.CONFIRM:
        return _CreateUserStep.TAGS
    raise AssertionError(f"Unhandled step: {current}")


class _CopyUserStep(Enum):
    """Every question Copy User can ask, in forward order.

    Same state-machine discipline as ``_CreateUserStep`` -- see that class's
    own docstring, including the absence of a "require password reset?"
    step. Copy User asks fewer questions than Create New (Groups, Policies,
    and Tags are cloned from the source automatically instead of asked
    about -- see ``CopyUserUseCase``), so this is its own, shorter step
    sequence rather than a variant of ``_CreateUserStep``.
    """

    SOURCE = "source"
    NEW_NAME = "new_name"
    CONSOLE_CONFIRM = "console_confirm"
    PROGRAMMATIC_CONFIRM = "programmatic_confirm"
    CONFIRM = "confirm"


@dataclass(slots=True)
class _CopyUserState:
    """What's been collected so far across Copy User's steppable questions."""

    source_name: str | None = None
    new_name: str | None = None
    access: _AccessPrefsState = field(default_factory=_AccessPrefsState)


def _copy_user_next_step(current: _CopyUserStep, state: _CopyUserState) -> _CopyUserStep:
    """What "forward" means from ``current`` -- mirrors ``_create_user_next_step``."""
    del state
    if current is _CopyUserStep.SOURCE:
        return _CopyUserStep.NEW_NAME
    if current is _CopyUserStep.NEW_NAME:
        return _CopyUserStep.CONSOLE_CONFIRM
    if current is _CopyUserStep.CONSOLE_CONFIRM:
        return _CopyUserStep.PROGRAMMATIC_CONFIRM
    if current is _CopyUserStep.PROGRAMMATIC_CONFIRM:
        return _CopyUserStep.CONFIRM
    raise AssertionError(f"CONFIRM has no 'next' step: {current}")


def _copy_user_prev_step(
    current: _CopyUserStep, state: _CopyUserState
) -> _CopyUserStep | None:
    """What "back" means from ``current`` -- ``None`` means "exit the whole wizard"."""
    del state
    if current is _CopyUserStep.SOURCE:
        return None
    if current is _CopyUserStep.NEW_NAME:
        return _CopyUserStep.SOURCE
    if current is _CopyUserStep.CONSOLE_CONFIRM:
        return _CopyUserStep.NEW_NAME
    if current is _CopyUserStep.PROGRAMMATIC_CONFIRM:
        return _CopyUserStep.CONSOLE_CONFIRM
    if current is _CopyUserStep.CONFIRM:
        return _CopyUserStep.PROGRAMMATIC_CONFIRM
    raise AssertionError(f"Unhandled step: {current}")


_CREATE_USER = "create_user"
_DELETE_USER = "delete_user"
_SEARCH_THRESHOLD = 25

_EXPLORE_SEARCH = "search"

_EDIT = "edit"
_SECURITY = "security"
_TOGGLE_STATUS = "toggle_status"

_STATUS_ACTIVE = "Active"
_STATUS_DISABLED = "Disabled"

_EDIT_NAME = "name"
_EDIT_PATH = "path"
_EDIT_TAGS = "tags"
_EDIT_GROUPS = "groups"
_EDIT_POLICIES = "policies"

_TAG_ADD = "tag_add"
_TAG_DELETE = "tag_delete"

_GROUP_ADD = "group_add"
_GROUP_REMOVE = "group_remove"
_NEW_GROUP = "__new_group__"

_POLICY_ATTACH = "policy_attach"
_POLICY_DETACH = "policy_detach"

_SECURITY_CONSOLE = "console"
_SECURITY_KEYS = "keys"
_SECURITY_MFA = "mfa"

_CONSOLE_GRANT = "console_grant"
_CONSOLE_UPDATE_PASSWORD = "console_update_password"
_CONSOLE_TOGGLE_RESET = "console_toggle_reset"
_CONSOLE_REVOKE = "console_revoke"

_KEY_CREATE = "key_create"
_KEY_TOGGLE = "key_toggle"
_KEY_DELETE = "key_delete"

_MFA_RESET = "mfa_reset"
_MFA_DEACTIVATE = "mfa_deactivate"

_CREATE_NEW = "new"
_CREATE_COPY = "copy"

_NO = "no"
_YES = "yes"

# See ``_prompt_tag_field``'s docstring for why this is local to tag entry rather
# than an addition to the shared ``_ABORT_WORDS`` in ``_shared.py``.
_TAG_ENTRY_BACK_WORDS = frozenset({"back", "atras", "cancel"})

_PASSWORD_LENGTH = 16
_PASSWORD_SYMBOLS = "!@#$%^&*()-_=+"


def _generate_secure_password(length: int = _PASSWORD_LENGTH) -> str:
    """Generate a CSPRNG password: >=1 uppercase, lowercase, digit, and symbol.

    Every IAM console-password flow in this CLI uses this instead of asking
    the admin to type one -- a hand-typed "Initial password" was the one
    place in the whole TUI that could end up weak, reused, or written down.
    Built entirely on ``secrets`` (never ``random``): ``secrets.choice`` picks
    each character, and the final Fisher-Yates shuffle uses
    ``secrets.randbelow`` for its swap index, so the guaranteed one-of-each
    character doesn't cluster predictably at the front of the string.
    """
    classes = [string.ascii_uppercase, string.ascii_lowercase, string.digits, _PASSWORD_SYMBOLS]
    pool = "".join(classes)
    chars = [secrets.choice(cls) for cls in classes]
    chars.extend(secrets.choice(pool) for _ in range(length - len(chars)))
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


_KEYS_DIR_NAME = "keys"
_KEYS_IAM_SUBDIR = "iam"
_CREDENTIALS_FILE_MODE = 0o600


def _credentials_file_path(name: str) -> Path:
    """``keys/iam/credentials-<name>.txt`` at the CLI's project root.

    Same ``keys/`` directory and convention ``ec2_flow.py``'s
    ``_key_pair_destination_dir`` uses for auto-generated ``.pem`` files --
    one place an admin knows to look for anything this CLI generated and
    can only show once. The ``iam/`` subdirectory keeps these credential
    files out of ``keys/ec2/``, where EC2's ``.pem`` files live.
    """
    return Path.cwd() / _KEYS_DIR_NAME / _KEYS_IAM_SUBDIR / f"credentials-{name}.txt"


_CREDENTIALS_FILE_REGION = "us-east-1"


def _render_credentials_file_contents(
    *, name: str, access_key_id: str, secret_access_key: str, is_local: bool
) -> str:
    """Build the full ``credentials-<name>.txt`` body: header, raw values, ini block.

    Three sections, in this order, so the file reads top-to-bottom as
    "what/when/where this was for" -> "the raw secret, if a script needs to
    grep it" -> "paste this block into ``~/.aws/credentials`` verbatim":

    1. A header -- Username, Environment (LocalStack vs AWS, from
       ``ctx.settings.is_local``, the same signal the banner's Status field
       and every other local-vs-real distinction in this CLI already uses),
       and the UTC generation timestamp.
    2. The raw ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` pair, for
       anything that reads env-var-style values directly.
    3. A ready-to-paste ``[<username>]`` block in the exact shape
       ``~/.aws/credentials`` expects, region hardcoded to
       ``us-east-1`` (this CLI has no per-user region concept to draw from).
    """
    environment = "LocalStack" if is_local else "AWS"
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        "# IAM Programmatic Access Credentials\n"
        f"# Username:    {name}\n"
        f"# Environment: {environment}\n"
        f"# Generated:   {generated_at}\n"
        "#\n"
        "# AWS shows the Secret Access Key exactly once, at creation --\n"
        "# this file is the only saved copy. Keep it secret; keep it safe.\n"
        "\n"
        f"AWS_ACCESS_KEY_ID={access_key_id}\n"
        f"AWS_SECRET_ACCESS_KEY={secret_access_key}\n"
        "\n"
        "# ~/.aws/credentials\n"
        f"[{name}]\n"
        f"aws_access_key_id = {access_key_id}\n"
        f"aws_secret_access_key = {secret_access_key}\n"
        f"region = {_CREDENTIALS_FILE_REGION}\n"
    )


def _save_credentials_file(
    name: str, access_key_id: str, secret_access_key: str, *, is_local: bool
) -> Path:
    """Write a freshly created Access Key ID/Secret to ``keys/credentials-<name>.txt``.

    ``0600`` (owner read/write only), same permissions
    ``CreateKeyPairUseCase`` writes EC2 ``.pem`` files with -- AWS shows a
    Secret Access Key exactly once, at creation, so losing it to terminal
    scrollback is unrecoverable without a saved copy. Overwrites any stale
    file for the same username outright (unlike the ``.pem`` case, this is a
    local convenience copy, not the sole record of a secret that can never
    be re-fetched: the caller always also shows it on screen).
    """
    destination = _credentials_file_path(name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    contents = _render_credentials_file_contents(
        name=name, access_key_id=access_key_id, secret_access_key=secret_access_key,
        is_local=is_local,
    )
    fd = os.open(str(destination), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, _CREDENTIALS_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(contents)
    return destination


_DEMO_GROUPS: tuple[str, ...] = ("Administrators", "Developers", "ReadOnlyUsers")

# name -> the actions its sample policy grants (Resource is always "*"). Scoped by
# service prefix rather than a bare "*" action, so none of these ever trips
# CreatePolicyUseCase's full-wildcard guard.
_DEMO_POLICIES: dict[str, list[str]] = {
    "SampleReadOnlyAccess": ["s3:Get*", "s3:List*", "ec2:Describe*"],
    "SampleDeveloperAccess": ["s3:*", "ec2:Describe*", "ec2:RunInstances"],
}


def _ensure_demo_iam_resources(ctx: AppContext, use_cases: IamUseCases) -> None:
    """Idempotently create a handful of sample Groups/Policies to select from.

    Without this, a brand-new account has zero customer-managed policies and
    zero groups, so the Create User wizard's Groups step and the Edit User ->
    Attached Policies screen would always auto-skip -- nothing to select,
    nothing to demonstrate attach with. Same idea as ``ec2_flow.py``'s
    ``ensure_demo_security_groups``, but seeded through the ordinary
    ``IamUseCases`` -- IAM's gateway already exposes full CRUD with no
    Separation-of-Duties boundary to route around, so there's no reason to
    bypass the validated, ledger-tracked path an admin creating one by hand
    goes through.

    LOCAL-ONLY: a no-op the instant ``ctx.settings.is_local`` is ``False`` --
    this must never run a mutating IAM call against a real AWS account. Each
    name is looked up first, so a second call never duplicates or fails.
    """
    if not ctx.settings.is_local:
        return

    existing_groups = {g.group_name for g in use_cases.list_groups.execute(None)}
    for name in _DEMO_GROUPS:
        if name in existing_groups:
            continue
        use_cases.create_group.execute(CreateGroupRequest(name=name))

    existing_policies = {
        p.policy_name for p in use_cases.list_policies.execute(scope="Local", only_attached=False)
    }
    for name, actions in _DEMO_POLICIES.items():
        if name in existing_policies:
            continue
        document = PolicyDocument(
            statement=[PolicyStatement(effect=PolicyEffect.ALLOW, action=actions, resource=["*"])]
        )
        use_cases.create_policy.execute(CreatePolicyRequest(name=name, document=document))


def _user_tags(user: IamUser) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in user.tags}


def _validate_username(raw: str) -> str:
    """Sanitize (spaces -> underscores) then validate a candidate username.

    Shared by ``_create_new_user`` and ``_copy_user`` -- the same rule both
    apply via ``CreateUserUseCase``/``CopyUserUseCase`` internally, just run
    here first so ``prompt_available_name`` can reject/retry before the rest
    of the create form is asked.
    """
    return validate_resource_name(sanitize_user_name(raw), max_length=64, field_label="username")


def _is_user_disabled(attached_policies: list[AttachedPolicy]) -> bool:
    """Whether the shared deny-all policy is attached -- the actual "Disabled" signal."""
    return any(p.policy_name == DENY_ALL_POLICY_NAME for p in attached_policies)


@dataclass(slots=True)
class IamFlow:
    """IAM's top-level TUI screen: list/filter users, create (New/Copy), delete, edit."""

    title: ClassVar[str] = "🔐 IAM Access & Identity Management"

    ctx: AppContext
    prompter: Prompter

    @aws_error_handler
    def menu(self: Self) -> NavAction:
        """Show IAM's menu once."""
        clear_and_banner(self.ctx)
        users = build_iam_use_cases(self.ctx).list_users.execute(None)
        self._render_user_stats(users)
        selected = self.prompter.select("IAM (Users) -- What do you want to do?", self._choices())
        if selected is None or selected == NAV_EXIT:
            return NavAction.EXIT
        if selected == NAV_BACK:
            return NavAction.BACK
        if selected == _EXPLORE_SEARCH:
            self._search_users()
        elif selected == _CREATE_USER:
            self._create_user()
        elif selected == _DELETE_USER:
            self._delete_user()
        return NavAction.STAY

    def _choices(self: Self) -> list[Choice | Separator]:
        return [
            Choice(title="🔍 Search / Filter Users", value=_EXPLORE_SEARCH),
            Choice(title="+ Create User", value=_CREATE_USER),
            Choice(title="❌ Delete User", value=_DELETE_USER),
            Separator(),
            Choice(title="↩️  Back", value=NAV_BACK),
        ]

    # -- Search (shared by List, Copy, and Delete) --------------------------------

    def _query_users(self: Self, message: str = "Enter search query:") -> list[IamUser] | None:
        """Ask for a free-text query (blank = everyone); matches against the username.

        Returns the matches (possibly empty, if the query hit nothing), or
        ``None`` if cancelled or there are no users at all to search.
        """
        users = build_iam_use_cases(self.ctx).list_users.execute(None)
        if not users:
            self.ctx.err_console.print("[yellow]No users found.[/]")
            self.prompter.pause()
            return None

        clear_and_banner(self.ctx)
        query = prompt_text_or_cancel(self.prompter, self.ctx.err_console, message)
        if query is None:
            return None
        if not query:
            return users
        needle = query.lower()
        return [u for u in users if needle in u.user_name.lower()]

    def _user_exists(self: Self, name: str) -> bool:
        try:
            build_iam_use_cases(self.ctx).get_user.execute(name)
        except ResourceNotFoundError:
            return False
        return True

    def _pick_user(self: Self, matches: list[IamUser], message: str) -> str | None:
        choices: list[Choice | Separator] = [
            Choice(title=u.user_name, value=u.user_name) for u in matches
        ]
        choices.append(Separator())
        choices.append(Choice(title="<- Cancel", value=NAV_BACK))
        selected = self.prompter.select(
            message, choices, use_search=len(matches) > _SEARCH_THRESHOLD
        )
        if selected is None or selected == NAV_BACK:
            return None
        return selected

    # -- Stats line + search (the service screen itself) ---------------------

    def _render_user_stats(self: Self, users: list[IamUser]) -> None:
        """Print "  Users: N Total  |  A Active  |  I Inactive" above the service menu.

        Active/Disabled here is the exact same signal as the user detail
        screen's Status column and the Enable/Disable User toggle: whether
        the shared deny-all policy is attached. Costs one
        ``ListAttachedUserPolicies`` call per user in ``users``, every time
        this screen renders.
        """
        use_cases = build_iam_use_cases(self.ctx)
        active = 0
        for user in users:
            attached = use_cases.list_attached_policies.execute(
                user.user_name, principal_type="user"
            )
            if not _is_user_disabled(attached):
                active += 1
        total = len(users)
        self.ctx.err_console.print()  # spacing from the previous prompt's answer line
        self.ctx.err_console.print(
            f"  Users: {total} Total  |  {active} Active  |  {total - active} Inactive"
        )

    def _search_users(self: Self) -> None:
        matches = self._query_users()
        if matches is None:
            return
        if not matches:
            self.ctx.err_console.print("[yellow]No matches.[/]")
            self.prompter.pause()
            return

        name = self._pick_user(matches, "Which user do you want to view?")
        if name is None:
            return
        self._user_detail_loop(name)

    def _delete_user(self: Self) -> None:
        matches = self._query_users()
        if matches is None:
            return
        if not matches:
            self.ctx.err_console.print("[yellow]No matches.[/]")
            self.prompter.pause()
            return

        name = self._pick_user(matches, "Which user do you want to delete?")
        if name is None:
            return

        if not confirm_destructive(self.prompter, kind="user", name=name):
            return

        use_cases = build_iam_use_cases(self.ctx)
        try:
            run_with_spinner(
                self.ctx.err_console,
                f"[bold green]Deleting user '{name}'...[/bold green]",
                lambda: use_cases.delete_user.execute(DeleteUserRequest(name=name, force=False)),
            )
        except ValidationError as exc:
            self.ctx.err_console.print(f"[red]{exc}[/]")
            if exc.hint:
                self.ctx.err_console.print(f"[yellow]{exc.hint}[/]")
            if not confirm_yes_no(
                self.prompter,
                f"'{name}' still has attachments. Detach everything and delete anyway (--force)?",
                yes_label="YES -- Detach all policies/keys and force delete user",
                no_label="NO -- Abort deletion",
            ):
                return
            run_with_spinner(
                self.ctx.err_console,
                f"[bold green]Deleting user '{name}'...[/bold green]",
                lambda: use_cases.delete_user.execute(DeleteUserRequest(name=name, force=True)),
            )
        announce_result(self.ctx, self.prompter, f"[green]User '{name}' deleted.[/]")

    # -- Enable/Disable tracking helpers (shared by the detail toggle) ----------------

    def _disable_user_with_tracking(self: Self, use_cases: IamUseCases, name: str) -> None:
        """Attach the shared deny-all policy and stamp ``disabled_at`` in the local ledger."""
        policy_arn = use_cases.resolve_deny_all_policy.execute()
        use_cases.attach_policy.execute(
            AttachPolicyRequest(principal_name=name, policy_arn=policy_arn, principal_type="user")
        )
        user = use_cases.get_user.execute(name)
        write_user_metadata(
            self.ctx.resource_repository,
            user=user,
            profile=self.ctx.settings.profile,
            region=self.ctx.settings.region,
            updates={DISABLED_AT: datetime.now(UTC)},
        )

    def _detach_deny_all_policy(self: Self, use_cases: IamUseCases, name: str) -> None:
        """Detach the shared deny-all policy from ``name``, if attached.

        This is our own disablement marker, not a user-set attachment -- so
        removing it never warrants the generic "attached policies" --force
        prompt that a genuinely blocking policy would.
        """
        policy_arn = use_cases.resolve_deny_all_policy.execute()
        use_cases.detach_policy.execute(
            DetachPolicyRequest(principal_name=name, policy_arn=policy_arn, principal_type="user")
        )

    def _reenable_user_with_tracking(self: Self, use_cases: IamUseCases, name: str) -> None:
        """Detach the shared deny-all policy and clear ``disabled_at`` from the local ledger."""
        self._detach_deny_all_policy(use_cases, name)
        user = use_cases.get_user.execute(name)
        write_user_metadata(
            self.ctx.resource_repository,
            user=user,
            profile=self.ctx.settings.profile,
            region=self.ctx.settings.region,
            updates={DISABLED_AT: None},
        )

    def _user_detail_loop(self: Self, name: str) -> None:
        """Detail view + [Edit Details, Security & Access, Enable/Disable User, Back].

        Security & Access is the one place for a user's whole credential
        lifecycle -- Console Access, Access Keys, and MFA Device -- so there's
        no separate top-level "Reset Console Password" shortcut (it always
        showed the same "Reset" wording whether the user had console access
        or not) and no Console Profile/Access Keys entries buried inside Edit
        Details either; see ``_security_menu``. There is deliberately no
        Delete User action anywhere in this module -- user deletion is
        disabled in the TUI entirely (still available, if ever needed,
        through the non-interactive `iam user delete` CLI command).

        Clears the screen and redraws the banner on every entry into this
        loop -- the first time (right after the search picker) and every
        time control comes back here after Edit Details, Security & Access,
        or the status toggle -- so no trace of the prior screen lingers, and
        the table always reflects the just-applied change.
        """
        while True:
            clear_and_banner(self.ctx)
            self.ctx.console.print()  # breathing room below the header box, above the table
            detail = self._render_user_detail(name)
            is_disabled = _is_user_disabled(detail.attached_policies)
            toggle_label = "Enable User" if is_disabled else "Disable User"
            selected = self.prompter.select(
                f"User '{name}' -- what do you want to do?",
                [
                    Choice(title="Edit Details", value=_EDIT),
                    Choice(title="Security & Access", value=_SECURITY),
                    Choice(title=toggle_label, value=_TOGGLE_STATUS),
                    Separator(),
                    Choice(title="↩️  Back", value=NAV_BACK),
                ],
            )
            if selected is None or selected == NAV_BACK:
                return
            if selected == _EDIT:
                name = self._edit_user(name)
            elif selected == _SECURITY:
                self._security_menu(name)
            elif selected == _TOGGLE_STATUS:
                self._toggle_user_status(name, currently_disabled=is_disabled)

    def _render_user_detail(self: Self, name: str) -> UserDetail:
        detail = build_iam_use_cases(self.ctx).get_user_detail.execute(name)
        console = "No"
        if detail.login_profile is not None:
            flag = "yes" if detail.login_profile.password_reset_required else "no"
            console = f"Yes (must-change: {flag})"
        status = _STATUS_DISABLED if _is_user_disabled(detail.attached_policies) else _STATUS_ACTIVE
        last_activity = (
            detail.user.password_last_used.strftime("%Y-%m-%d %H:%M UTC")
            if detail.user.password_last_used is not None
            else "Never"
        )
        active_key_count = sum(1 for k in detail.access_keys if k.status == "Active")
        render(
            {
                "Username": detail.user.user_name,
                "Status": status,
                "Console": console,
                "Active Keys": str(active_key_count),
                "Groups": ", ".join(g.group_name for g in detail.groups) or "(none)",
                "User ID": detail.user.user_id,
                "Last Seen": last_activity,
                "Tags": _user_tags(detail.user) or "(no tags)",
            },
            ctx=self.ctx,
            title=f"user: {detail.user.user_name}",
        )
        return detail

    # -- Enable / Disable --------------------------------------------------------

    def _toggle_user_status(self: Self, name: str, *, currently_disabled: bool) -> None:
        use_cases = build_iam_use_cases(self.ctx)
        if currently_disabled:
            self._reenable_user_with_tracking(use_cases, name)
            message = f"[green]User '{name}' enabled.[/]"
        else:
            self._disable_user_with_tracking(use_cases, name)
            message = f"[red]User '{name}' disabled.[/]"
        announce_result(self.ctx, self.prompter, message)

    # -- Edit ------------------------------------------------------------------

    def _edit_user(self: Self, name: str) -> str:
        """Show the Edit Details submenu until the user backs out. Returns the current user name.

        Name/Path/Tags/IAM Groups/Attached Policies only -- Console Access,
        Access Keys, and MFA Device live under Security & Access instead
        (``_security_menu``), not here.

        ``name`` is reassigned in place whenever ``_edit_name`` renames the
        user, so every subsequent action in this same editing session (and the
        caller, once this returns) keeps operating on the right identity.
        """
        while True:
            clear_and_banner(self.ctx)
            selected = self.prompter.select(
                f"Edit '{name}' -- what do you want to modify?",
                [
                    Choice(title="Name", value=_EDIT_NAME),
                    Choice(title="Path", value=_EDIT_PATH),
                    Choice(title="Tags", value=_EDIT_TAGS),
                    Choice(title="IAM Groups", value=_EDIT_GROUPS),
                    Choice(title="Attached Policies", value=_EDIT_POLICIES),
                    Separator(),
                    Choice(title="↩️  Back", value=NAV_BACK),
                ],
            )
            if selected is None or selected == NAV_BACK:
                return name
            if selected == _EDIT_NAME:
                renamed = self._edit_name(name)
                if renamed is not None:
                    name = renamed
            elif selected == _EDIT_PATH:
                self._edit_path(name)
            elif selected == _EDIT_TAGS:
                self._edit_tags(name)
            elif selected == _EDIT_GROUPS:
                self._edit_groups(name)
            elif selected == _EDIT_POLICIES:
                self._edit_policies(name)

    def _security_menu(self: Self, name: str) -> None:
        """Security & Access: Console Access / Access Keys / MFA Device / Back.

        Every credential-lifecycle action for this user lives under one menu
        here, each destination state-aware instead of carrying a generic
        "Reset" label regardless of what state the credential is actually in:
        Console Access offers to create a login password when there isn't
        one yet, or to reset/disable the existing one; Access Keys offers to
        generate, activate/deactivate (rotate), or delete; MFA Device offers
        to reset or deactivate a registered device.
        """
        while True:
            clear_and_banner(self.ctx)
            selected = self.prompter.select(
                f"Security & Access for '{name}' -- what do you want to manage?",
                [
                    Choice(title="Console Access", value=_SECURITY_CONSOLE),
                    Choice(title="Access Keys", value=_SECURITY_KEYS),
                    Choice(title="MFA Device", value=_SECURITY_MFA),
                    Separator(),
                    Choice(title="↩️  Back", value=NAV_BACK),
                ],
            )
            if selected is None or selected == NAV_BACK:
                return
            if selected == _SECURITY_CONSOLE:
                self._edit_console(name)
            elif selected == _SECURITY_KEYS:
                self._edit_keys(name)
            elif selected == _SECURITY_MFA:
                self._manage_mfa(name)

    def _edit_name(self: Self, name: str) -> str | None:
        """Rename ``name`` -- entirely independent of Path, never asks about it.

        Blank ENTER means Back (the global "Enter vacío = Back" convention),
        same as every other free-text prompt in this module -- it does NOT
        mean "apply some other field's change", because there is no other
        field here to apply.
        """
        clear_and_banner(self.ctx)
        current = build_iam_use_cases(self.ctx).get_user.execute(name)
        self.ctx.err_console.print(f"  Nombre actual: {current.user_name}")
        new_name = prompt_text_or_cancel(self.prompter, self.ctx.err_console, "New name")
        if not new_name:
            return None
        updated = build_iam_use_cases(self.ctx).update_user.execute(
            UpdateUserRequest(name=name, new_name=new_name)
        )
        if updated.user_name != new_name:
            self.ctx.err_console.print(
                f"[yellow]Name sanitized: '{new_name}' -> '{updated.user_name}' "
                "(IAM doesn't allow spaces).[/]"
            )
        announce_result(self.ctx, self.prompter, f"[green]User renamed: '{updated.user_name}'.[/]")
        return updated.user_name

    def _edit_path(self: Self, name: str) -> None:
        """Change ``name``'s IAM path -- entirely independent of Name, never asks about it.

        Blank ENTER means Back, same convention as ``_edit_name`` -- no
        structure like ``/team/`` is ever required or assumed; the user
        types whatever path they actually want, or backs out unchanged.
        """
        clear_and_banner(self.ctx)
        current = build_iam_use_cases(self.ctx).get_user.execute(name)
        self.ctx.err_console.print(f"  Path actual: {current.path}")
        new_path = prompt_text_or_cancel(self.prompter, self.ctx.err_console, "New path")
        if not new_path:
            return
        updated = build_iam_use_cases(self.ctx).update_user.execute(
            UpdateUserRequest(name=name, new_path=new_path)
        )
        if updated.path != new_path:
            self.ctx.err_console.print(
                f"[yellow]Path sanitized: '{new_path}' -> '{updated.path}' "
                "(IAM paths must start and end with '/').[/]"
            )
        announce_result(self.ctx, self.prompter, f"[green]Path updated to '{updated.path}'.[/]")

    def _edit_tags(self: Self, name: str) -> None:
        while True:
            clear_and_banner(self.ctx)
            user = build_iam_use_cases(self.ctx).get_user.execute(name)
            tags = _user_tags(user)
            render(tags or {"(no tags)": ""}, ctx=self.ctx, title=f"tags for {name}")
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
                self._add_user_tag(name)
            elif selected == _TAG_DELETE:
                self._delete_user_tag(name, tags)

    def _add_user_tag(self: Self, name: str) -> None:
        clear_and_banner(self.ctx)
        key = prompt_text_or_cancel(self.prompter, self.ctx.err_console, "Tag key")
        if not key:
            return
        value = prompt_text_or_cancel(self.prompter, self.ctx.err_console, "Tag value")
        if value is None:
            return
        build_iam_use_cases(self.ctx).set_user_tags.execute(
            SetUserTagsRequest(name=name, tags={key: value})
        )
        announce_result(self.ctx, self.prompter, f"[green]Tag '{key}={value}' applied.[/]")

    def _delete_user_tag(self: Self, name: str, tags: dict[str, str]) -> None:
        choices: list[Choice | Separator] = [
            Choice(title=f"{k} = {v}", value=k) for k, v in tags.items()
        ]
        choices.append(Separator())
        choices.append(Choice(title="<- Cancel", value=NAV_BACK))
        key = self.prompter.select("Which tag do you want to delete?", choices)
        if key is None or key == NAV_BACK:
            return
        if not confirm_yes_no(self.prompter, f"Delete the tag '{key}'?"):
            return
        build_iam_use_cases(self.ctx).delete_user_tags.execute(
            DeleteUserTagsRequest(name=name, keys=(key,))
        )
        announce_result(self.ctx, self.prompter, f"[green]Tag '{key}' deleted.[/]")

    def _edit_groups(self: Self, name: str) -> None:
        while True:
            clear_and_banner(self.ctx)
            use_cases = build_iam_use_cases(self.ctx)
            current = use_cases.list_groups_for_user.execute(name)
            current_names = {g.group_name for g in current}
            render(
                {"Groups": ", ".join(g.group_name for g in current) or "(none)"},
                ctx=self.ctx,
                title=f"groups for {name}",
            )
            selected = self.prompter.select(
                "Groups -- what do you want to do?",
                [
                    Choice(title="Add to a group", value=_GROUP_ADD),
                    Choice(
                        title="Remove from a group",
                        value=_GROUP_REMOVE,
                        disabled=None if current else "not a member of any group",
                    ),
                    Separator(),
                    Choice(title="↩️  Back", value=NAV_BACK),
                ],
            )
            if selected is None or selected == NAV_BACK:
                return
            if selected == _GROUP_ADD:
                self._add_to_group(name, current_names)
            elif selected == _GROUP_REMOVE:
                self._remove_from_group(name, current)

    def _add_to_group(self: Self, name: str, current_names: set[str]) -> None:
        use_cases = build_iam_use_cases(self.ctx)
        all_groups = use_cases.list_groups.execute(None)
        candidates = [g for g in all_groups if g.group_name not in current_names]
        choices: list[Choice | Separator] = [Choice(title="<- Create new group", value=_NEW_GROUP)]
        choices.extend(Choice(title=g.group_name, value=g.group_name) for g in candidates)
        choices.append(Separator())
        choices.append(Choice(title="<- Cancel", value=NAV_BACK))
        selected = self.prompter.select("Which group do you want to add them to?", choices)
        if selected is None or selected == NAV_BACK:
            return
        group_name = selected
        if selected == _NEW_GROUP:
            new_group = prompt_text_or_cancel(self.prompter, self.ctx.err_console, "New group name")
            if not new_group:
                return
            use_cases.create_group.execute(CreateGroupRequest(name=new_group))
            group_name = new_group
        use_cases.add_user_to_group.execute(
            AddUserToGroupRequest(group_name=group_name, user_name=name)
        )
        announce_result(self.ctx, self.prompter, f"[green]'{name}' added to '{group_name}'.[/]")

    def _remove_from_group(self: Self, name: str, current: list[IamGroup]) -> None:
        choices: list[Choice | Separator] = [
            Choice(title=g.group_name, value=g.group_name) for g in current
        ]
        choices.append(Separator())
        choices.append(Choice(title="<- Cancel", value=NAV_BACK))
        group_name = self.prompter.select("Which group do you want to remove them from?", choices)
        if group_name is None or group_name == NAV_BACK:
            return
        if not confirm_yes_no(self.prompter, f"Remove '{name}' from '{group_name}'?"):
            return
        build_iam_use_cases(self.ctx).remove_user_from_group.execute(
            RemoveUserFromGroupRequest(group_name=group_name, user_name=name)
        )
        announce_result(self.ctx, self.prompter, f"[green]'{name}' removed from '{group_name}'.[/]")

    def _edit_policies(self: Self, name: str) -> None:
        """Attach/detach managed policies directly on the user (independent of Groups).

        Excludes the shared deny-all policy from both the current list and
        the attach picker -- that one is Enable/Disable User's own internal
        marker, never a policy an admin attaches or detaches by hand here.
        """
        while True:
            clear_and_banner(self.ctx)
            use_cases = build_iam_use_cases(self.ctx)
            current = [
                p
                for p in use_cases.list_attached_policies.execute(name, principal_type="user")
                if p.policy_name != DENY_ALL_POLICY_NAME
            ]
            render(
                {"Attached Policies": ", ".join(p.policy_name for p in current) or "(none)"},
                ctx=self.ctx,
                title=f"policies for {name}",
            )
            selected = self.prompter.select(
                "Policies -- what do you want to do?",
                [
                    Choice(title="Attach a policy", value=_POLICY_ATTACH),
                    Choice(
                        title="Detach a policy",
                        value=_POLICY_DETACH,
                        disabled=None if current else "no policies attached",
                    ),
                    Separator(),
                    Choice(title="↩️  Back", value=NAV_BACK),
                ],
            )
            if selected is None or selected == NAV_BACK:
                return
            if selected == _POLICY_ATTACH:
                self._attach_policy(name, use_cases, {p.policy_arn for p in current})
            elif selected == _POLICY_DETACH:
                self._detach_policy(name, use_cases, current)

    def _attach_policy(
        self: Self, name: str, use_cases: IamUseCases, current_arns: set[str]
    ) -> None:
        """Customer-managed policies only.

        ``scope="Local"`` -- AWS's own hundreds of managed policies would
        swamp this picker, same reasoning ``_create_user_step_policies``
        applies to the create wizard's own policy checkbox.
        """
        candidates = [
            p
            for p in use_cases.list_policies.execute(scope="Local", only_attached=False)
            if p.arn not in current_arns and p.policy_name != DENY_ALL_POLICY_NAME
        ]
        if not candidates:
            self.ctx.err_console.print(
                "[yellow]No customer-managed policies available to attach.[/]"
            )
            self.prompter.pause()
            return
        choices: list[Choice | Separator] = [
            Choice(title=p.policy_name, value=p.arn) for p in candidates
        ]
        choices.append(Separator())
        choices.append(Choice(title="<- Cancel", value=NAV_BACK))
        arn = self.prompter.select("Which policy do you want to attach?", choices)
        if arn is None or arn == NAV_BACK:
            return
        use_cases.attach_policy.execute(
            AttachPolicyRequest(principal_name=name, policy_arn=arn, principal_type="user")
        )
        policy_name = next(p.policy_name for p in candidates if p.arn == arn)
        announce_result(self.ctx, self.prompter, f"[green]'{policy_name}' attached to '{name}'.[/]")

    def _detach_policy(
        self: Self, name: str, use_cases: IamUseCases, current: list[AttachedPolicy]
    ) -> None:
        choices: list[Choice | Separator] = [
            Choice(title=p.policy_name, value=p.policy_arn) for p in current
        ]
        choices.append(Separator())
        choices.append(Choice(title="<- Cancel", value=NAV_BACK))
        arn = self.prompter.select("Which policy do you want to detach?", choices)
        if arn is None or arn == NAV_BACK:
            return
        policy_name = next(p.policy_name for p in current if p.policy_arn == arn)
        if not confirm_yes_no(self.prompter, f"Detach '{policy_name}' from '{name}'?"):
            return
        use_cases.detach_policy.execute(
            DetachPolicyRequest(principal_name=name, policy_arn=arn, principal_type="user")
        )
        announce_result(
            self.ctx, self.prompter, f"[green]'{policy_name}' detached from '{name}'.[/]"
        )

    def _edit_console(self: Self, name: str) -> None:
        """Console Access: create a login password if there isn't one, else reset/disable it.

        State-aware, not a generic "Reset" label shown regardless of whether
        there's actually anything to reset -- when there's no login profile
        yet, the only offered action is creating one.
        """
        while True:
            clear_and_banner(self.ctx)
            use_cases = build_iam_use_cases(self.ctx)
            login_profile = use_cases.gateway.get_login_profile(name)
            if login_profile is None:
                choices: list[Choice | Separator] = [
                    Choice(title="Create login password", value=_CONSOLE_GRANT)
                ]
            else:
                flag = "yes" if login_profile.password_reset_required else "no"
                render(
                    {"Console Access": "Yes", "Must-change Password": flag},
                    ctx=self.ctx,
                    title=f"console for {name}",
                )
                choices = [
                    Choice(title="Reset password", value=_CONSOLE_UPDATE_PASSWORD),
                    Choice(
                        title="Toggle must-change-password flag",
                        value=_CONSOLE_TOGGLE_RESET,
                    ),
                    Choice(title="Disable console access", value=_CONSOLE_REVOKE),
                ]
            choices.append(Separator())
            choices.append(Choice(title="↩️  Back", value=NAV_BACK))
            selected = self.prompter.select("Console Access -- what do you want to do?", choices)
            if selected is None or selected == NAV_BACK:
                return
            if selected in (_CONSOLE_GRANT, _CONSOLE_UPDATE_PASSWORD):
                self._set_console_password(name)
            elif selected == _CONSOLE_TOGGLE_RESET:
                self._toggle_console_reset(name, login_profile)
            elif selected == _CONSOLE_REVOKE:
                self._revoke_console(name)

    def _set_console_password(self: Self, name: str) -> None:
        """Grant or refresh console access with an auto-generated password.

        Confirms first -- this immediately invalidates any password the user
        already has, so an accidental Enter on this menu entry must not
        silently lock them out. No manual password entry anywhere in this
        flow -- see `_generate_secure_password`. `password_reset_required`
        stays hardcoded True for the same reason as `_create_new_user`: a
        generated password is one-time-use by design.
        """
        confirm_choice = self.prompter.select(
            f"Are you sure you want to reset the password for '{name}'?",
            [
                Choice(title="Yes, reset password", value=_YES),
                Choice(title="No, do not reset", value=_NO),
                Separator(),
                Choice(title=_WIZARD_BACK_LABEL, value=NAV_BACK),
            ],
            default=_NO,
        )
        if confirm_choice != _YES:
            return
        password = _generate_secure_password()
        run_with_spinner(
            self.ctx.err_console,
            "[bold green]Resetting user password...[/bold green]",
            lambda: build_iam_use_cases(self.ctx).set_login_profile.execute(
                SetLoginProfileRequest(
                    user_name=name, password=password, password_reset_required=True
                )
            ),
        )
        announce_result(
            self.ctx,
            self.prompter,
            "[green]Console password updated.[/]",
            "[bold red]Save this password NOW -- it will not be shown again:[/]",
            f"  ✅ Auto-generated password: {password}",
        )

    def _toggle_console_reset(self: Self, name: str, login_profile: LoginProfile | None) -> None:
        current = login_profile.password_reset_required if login_profile is not None else False
        password = prompt_text_or_cancel(
            self.prompter,
            self.ctx.err_console,
            "Current password (IAM requires re-entering it to change just the flag)",
        )
        if not password:
            return
        build_iam_use_cases(self.ctx).set_login_profile.execute(
            SetLoginProfileRequest(
                user_name=name, password=password, password_reset_required=not current
            )
        )
        announce_result(self.ctx, self.prompter, "[green]Must-change-password flag updated.[/]")

    def _revoke_console(self: Self, name: str) -> None:
        if not confirm_yes_no(self.prompter, "Revoke this user's console access?"):
            return
        build_iam_use_cases(self.ctx).delete_login_profile.execute(
            DeleteLoginProfileRequest(user_name=name)
        )
        announce_result(self.ctx, self.prompter, "[green]Console access revoked.[/]")

    def _edit_keys(self: Self, name: str) -> None:
        while True:
            clear_and_banner(self.ctx)
            use_cases = build_iam_use_cases(self.ctx)
            keys = use_cases.list_access_keys.execute(name)
            render(
                [{"AccessKeyId": k.access_key_id, "Status": k.status} for k in keys]
                or [{"(no access keys)": ""}],
                ctx=self.ctx,
                title=f"access keys for {name}",
            )
            selected = self.prompter.select(
                "Access Keys -- what do you want to do?",
                [
                    Choice(title="Create new", value=_KEY_CREATE),
                    Choice(
                        title="Activate / Deactivate",
                        value=_KEY_TOGGLE,
                        disabled=None if keys else "no keys",
                    ),
                    Choice(
                        title="Delete",
                        value=_KEY_DELETE,
                        disabled=None if keys else "no keys",
                    ),
                    Separator(),
                    Choice(title="↩️  Back", value=NAV_BACK),
                ],
            )
            if selected is None or selected == NAV_BACK:
                return
            if selected == _KEY_CREATE:
                self._create_key(name)
            elif selected == _KEY_TOGGLE:
                self._toggle_key(name, keys)
            elif selected == _KEY_DELETE:
                self._delete_key(name, keys)

    def _create_key(self: Self, name: str) -> None:
        key = run_with_spinner(
            self.ctx.err_console,
            "[bold green]Generating API credentials...[/bold green]",
            lambda: build_iam_use_cases(self.ctx).create_access_key.execute(
                CreateAccessKeyRequest(user_name=name)
            ),
        )
        credentials_path = _save_credentials_file(
            name, key.access_key_id, key.secret_access_key, is_local=self.ctx.settings.is_local
        )
        announce_result(
            self.ctx,
            self.prompter,
            "[bold red]Save this key NOW -- it will not be shown again:[/]",
            f"  AccessKeyId:     {key.access_key_id}",
            f"  SecretAccessKey: {key.secret_access_key}",
            f"[bold green]✔ Credentials saved to: {relative_path_display(credentials_path)}[/]",
        )

    def _pick_key(self: Self, keys: list[AccessKeyMetadata], message: str) -> str | None:
        choices: list[Choice | Separator] = [
            Choice(title=f"{k.access_key_id} ({k.status})", value=k.access_key_id) for k in keys
        ]
        choices.append(Separator())
        choices.append(Choice(title="<- Cancel", value=NAV_BACK))
        selected = self.prompter.select(message, choices)
        if selected is None or selected == NAV_BACK:
            return None
        return selected

    def _toggle_key(self: Self, name: str, keys: list[AccessKeyMetadata]) -> None:
        key_id = self._pick_key(keys, "Which access key do you want to activate/deactivate?")
        if key_id is None:
            return
        current = next(k for k in keys if k.access_key_id == key_id)
        new_active = current.status != "Active"
        build_iam_use_cases(self.ctx).update_access_key.execute(
            UpdateAccessKeyRequest(user_name=name, access_key_id=key_id, active=new_active)
        )
        state = "activated" if new_active else "deactivated"
        announce_result(self.ctx, self.prompter, f"[green]Access key {state}.[/]")

    def _delete_key(self: Self, name: str, keys: list[AccessKeyMetadata]) -> None:
        key_id = self._pick_key(keys, "Which access key do you want to delete?")
        if key_id is None:
            return
        if not confirm_yes_no(self.prompter, f"Delete access key '{key_id}'?"):
            return
        build_iam_use_cases(self.ctx).delete_access_key.execute(
            DeleteAccessKeyRequest(user_name=name, access_key_id=key_id)
        )
        announce_result(self.ctx, self.prompter, "[green]Access key deleted.[/]")

    def _manage_mfa(self: Self, name: str) -> None:
        """MFA Device: list ``name``'s registered devices, then reset or deactivate one.

        IAM only exposes a delete API for VIRTUAL devices (serial numbers
        shaped ``arn:...:mfa/...``) -- a hardware device has no such call and
        can only ever be deactivated. "Reset" is therefore "deactivate, and
        delete outright if virtual" (``DeactivateMfaDeviceRequest.
        delete_virtual_device=True``) -- the closest this admin-side tool
        gets to letting the user register a fresh device from scratch;
        "Deactivate" always just unlinks, leaving any device object in place.
        """
        while True:
            clear_and_banner(self.ctx)
            serials = build_iam_use_cases(self.ctx).gateway.list_mfa_devices(name)
            render(
                [{"Serial Number": s} for s in serials] or [{"(no MFA devices)": ""}],
                ctx=self.ctx,
                title=f"MFA devices for {name}",
            )
            selected = self.prompter.select(
                "MFA Device -- what do you want to do?",
                [
                    Choice(
                        title="Reset a device",
                        value=_MFA_RESET,
                        disabled=None if serials else "no MFA devices",
                    ),
                    Choice(
                        title="Deactivate a device",
                        value=_MFA_DEACTIVATE,
                        disabled=None if serials else "no MFA devices",
                    ),
                    Separator(),
                    Choice(title="↩️  Back", value=NAV_BACK),
                ],
            )
            if selected is None or selected == NAV_BACK:
                return
            if selected == _MFA_RESET:
                self._reset_or_deactivate_mfa(name, serials, delete_virtual=True)
            elif selected == _MFA_DEACTIVATE:
                self._reset_or_deactivate_mfa(name, serials, delete_virtual=False)

    def _pick_mfa_device(self: Self, serials: list[str], message: str) -> str | None:
        choices: list[Choice | Separator] = [Choice(title=s, value=s) for s in serials]
        choices.append(Separator())
        choices.append(Choice(title="<- Cancel", value=NAV_BACK))
        selected = self.prompter.select(message, choices)
        if selected is None or selected == NAV_BACK:
            return None
        return selected

    def _reset_or_deactivate_mfa(
        self: Self, name: str, serials: list[str], *, delete_virtual: bool
    ) -> None:
        verb = "reset" if delete_virtual else "deactivate"
        serial = self._pick_mfa_device(serials, f"Which MFA device do you want to {verb}?")
        if serial is None:
            return
        if not confirm_yes_no(self.prompter, f"{verb.capitalize()} MFA device '{serial}'?"):
            return
        build_iam_use_cases(self.ctx).deactivate_mfa_device.execute(
            DeactivateMfaDeviceRequest(
                user_name=name, serial_number=serial, delete_virtual_device=delete_virtual
            )
        )
        message = (
            "[green]MFA device reset -- a new one can now be registered.[/]"
            if delete_virtual
            else "[green]MFA device deactivated.[/]"
        )
        announce_result(self.ctx, self.prompter, message)

    # -- Create ---------------------------------------------------------------------

    def _create_user(self: Self) -> None:
        clear_and_banner(self.ctx)
        selected = self.prompter.select(
            "Create user -- how?",
            [
                Choice(title="New", value=_CREATE_NEW),
                Choice(title="Copy", value=_CREATE_COPY),
                Separator(),
                Choice(title="<- Cancel", value=NAV_BACK),
            ],
        )
        if selected is None or selected == NAV_BACK:
            return
        if selected == _CREATE_NEW:
            self._create_new_user()
        else:
            self._copy_user()

    def _create_new_user(self: Self) -> None:
        """Create User (New): a steppable wizard.

        Name / Console / Programmatic / Groups / Tags / Confirm, driven by
        ``_create_user_next_step``/``_create_user_prev_step`` --
        exactly the same "resolved state, single driving loop" architecture
        ``ec2_flow.py``'s creation wizards use. "<- Back" at any step
        re-shows its immediate predecessor with every previously-collected answer intact;
        backing out of the very first step (Name) exits the whole wizard, back to
        "Create user -- how?". Nothing is created in AWS until the final CONFIRM step's
        "Yes" -- see ``_create_user_execute``.
        """
        use_cases = build_iam_use_cases(self.ctx)
        _ensure_demo_iam_resources(self.ctx, use_cases)
        state = _CreateUserState(available_groups=use_cases.list_groups.execute(None))
        step = _CreateUserStep.NAME

        while True:
            clear_and_banner(self.ctx)
            if step is _CreateUserStep.NAME:
                nav = self._create_user_step_name(state)
            elif step is _CreateUserStep.CONSOLE_CONFIRM:
                nav = self._access_prefs_step_console_confirm(state.access)
            elif step is _CreateUserStep.PROGRAMMATIC_CONFIRM:
                nav = self._access_prefs_step_programmatic(state.access)
            elif step is _CreateUserStep.GROUPS:
                nav = self._create_user_step_groups(state)
            elif step is _CreateUserStep.TAGS:
                nav = self._create_user_step_tags(state)
            else:
                nav = self._create_user_step_confirm(state)
                if nav is _WizardNav.NEXT:
                    self._create_user_execute(use_cases, state)
                    return

            if nav is _WizardNav.CANCEL:
                return
            if nav is _WizardNav.BACK:
                prev_step = _create_user_prev_step(step, state)
                if prev_step is None:
                    return
                step = prev_step
                continue
            step = _create_user_next_step(step, state)

    def _create_user_step_name(self: Self, state: _CreateUserState) -> _WizardNav:
        """Step 1: Username. No predecessor -- backing out here exits the whole wizard."""
        name = prompt_available_name(
            self.prompter,
            self.ctx.err_console,
            "Username:",
            validate=_validate_username,
            exists=self._user_exists,
        )
        if name is None:
            return _WizardNav.CANCEL
        state.name = name
        return _WizardNav.NEXT

    # -- The shared Console/Programmatic access sub-flow: reused VERBATIM by Create User
    # (New) and Copy User -- see ``_AccessPrefsState``.

    def _access_prefs_step_console_confirm(self: Self, access: _AccessPrefsState) -> _WizardNav:
        """"Grant console access?" -- no follow-up question about the reset flag.

        Requiring a password reset on next login is a compliance rule, not
        an admin choice: every console grant made here forces it, so there's
        nothing left to ask once "Yes" is picked.
        """
        choice = self.prompter.select(
            "Grant console access?",
            [
                Choice(title="No", value=_NO),
                Choice(title="Yes", value=_YES),
                Separator(),
                Choice(title=_WIZARD_BACK_LABEL, value=NAV_BACK),
            ],
            default=_YES if access.wants_console else _NO,
        )
        if choice is None or choice == NAV_BACK:
            return _WizardNav.BACK
        access.wants_console = choice == _YES
        access.reset_required = True
        return _WizardNav.NEXT

    def _access_prefs_step_programmatic(self: Self, access: _AccessPrefsState) -> _WizardNav:
        choice = self.prompter.select(
            "Enable programmatic access? (creates an Access Key)",
            [
                Choice(title="No", value=_NO),
                Choice(title="Yes", value=_YES),
                Separator(),
                Choice(title=_WIZARD_BACK_LABEL, value=NAV_BACK),
            ],
            default=_YES if access.wants_key else _NO,
        )
        if choice is None or choice == NAV_BACK:
            return _WizardNav.BACK
        access.wants_key = choice == _YES
        return _WizardNav.NEXT

    def _create_user_step_groups(self: Self, state: _CreateUserState) -> _WizardNav:
        """Only reached when the account actually has groups -- see ``_create_user_next_step``.

        A single checkbox for the groups, with "<- Atrás" appended as its own
        tickable row -- the same shape ``ec2_flow.py``'s
        ``_quick_pick_security_groups`` uses. Ticking that row (with <space>)
        and hitting Enter reads back the sentinel in the result list, so it's
        treated as Back regardless of what else was ticked alongside it;
        leaving it unticked and hitting Enter submits the groups chosen so
        far and advances straight to Tags -- no separate "N selected,
        Continue?" screen in between.

        Back here is a genuine ONE-STEP retreat to Programmatic Access, like
        every other step in this wizard -- not an abort. Backing up repeatedly
        therefore walks Tags -> Groups -> Programmatic -> Console -> Username
        and only then leaves for the main IAM menu, so "<- Atrás" means the
        same thing on every screen of the wizard.

        Groups are only committed to ``state.group_names`` on a real forward
        submission, so stepping back and forward again re-shows the same
        ticks (``checked=`` reads ``state.group_names``) -- the wizard's
        "never reset on Back" contract.
        """
        choices: list[Choice | Separator] = [
            Choice(
                title=g.group_name, value=g.group_name, checked=g.group_name in state.group_names
            )
            for g in state.available_groups
        ]
        choices.append(Separator())
        choices.append(Choice(title=_WIZARD_BACK_LABEL, value=NAV_BACK))
        selected = self.prompter.checkbox("Which groups do they belong to? (optional)", choices)
        if selected is None or NAV_BACK in selected:
            return _WizardNav.BACK
        state.group_names = selected
        return _WizardNav.NEXT

    def _create_user_step_tags(self: Self, state: _CreateUserState) -> _WizardNav:
        """Yes/No gated tag capture, same shape "empty key to finish" never uses.

        The "Do you want to add tags?" gate, the Tag key/value prompts, and
        "Add another tag?" all resolve Back the same way now: straight to
        Groups, the wizard's real previous step -- not a re-show of this
        step's own gate. A tag typed so far in this pass (a key with no value
        yet, or a value the admin changed their mind about) is a genuinely
        half-finished entry, so there's nothing worth preserving by stopping
        short of the actual previous step.
        """
        choice = self.prompter.select(
            "Do you want to add tags to this user?",
            [
                Choice(title="No", value=_NO),
                Choice(title="Yes", value=_YES),
                Separator(),
                Choice(title=_WIZARD_BACK_LABEL, value=NAV_BACK),
            ],
            default=_YES if state.tags else _NO,
        )
        if choice is None or choice == NAV_BACK:
            return _WizardNav.BACK
        if choice == _NO:
            state.tags = {}
            return _WizardNav.NEXT

        tags: dict[str, str] = dict(state.tags)
        while True:
            clear_and_banner(self.ctx)
            key = self._prompt_tag_field("Tag key")
            if key is None:
                return _WizardNav.BACK
            value = self._prompt_tag_field(f"Value for '{key}'")
            if value is None:
                return _WizardNav.BACK
            tags[key] = value

            again = self.prompter.select(
                "Add another tag?",
                [
                    Choice(title="No", value=_NO),
                    Choice(title="Yes", value=_YES),
                    Separator(),
                    Choice(title=_WIZARD_BACK_LABEL, value=NAV_BACK),
                ],
                default=_NO,
            )
            if again is None or again == NAV_BACK:
                return _WizardNav.BACK
            if again == _NO:
                state.tags = tags
                return _WizardNav.NEXT

    def _prompt_tag_field(self: Self, message: str) -> str | None:
        """One tag key or value prompt; ``None`` means "go back to Groups".

        Blank input or typing "back"/"atras"/"cancel" (case-insensitive) all
        mean the same thing here -- there's no reason to make an admin
        remember one specific escape word. This is a LOCAL, scoped trigger
        set -- not the shared ``_ABORT_WORDS`` (``_shared.py``) that every
        other free-text prompt in this TUI uses (search queries, renames,
        passwords...) -- so tag entry's extra "atras"/blank-means-back
        behavior never leaks into those other prompts.
        """
        raw = self.prompter.text(f"{message} (blank or 'back' to return to Groups):")
        if raw is None or not raw.strip() or raw.strip().lower() in _TAG_ENTRY_BACK_WORDS:
            return None
        return raw

    def _create_user_step_confirm(self: Self, state: _CreateUserState) -> _WizardNav:
        """Render the pending-creation summary and ask the final Sí/No/Back confirmation."""
        assert state.name is not None
        table = Table(title="Pending User Creation", show_header=False)
        table.add_column("Field", style="bold cyan")
        table.add_column("Value")
        table.add_row("Name", state.name)
        table.add_row("Console Access", "Yes" if state.access.wants_console else "No")
        if state.access.wants_console:
            # Always "Yes" -- a compliance rule, not an admin choice; see
            # ``_access_prefs_step_console_confirm``. Shown here only so the admin
            # sees it's in effect, not because it was ever asked about.
            table.add_row("Require Password Reset", "Yes (mandatory)")
        table.add_row("Programmatic Access", "Yes" if state.access.wants_key else "No")
        table.add_row("Groups", ", ".join(state.group_names) if state.group_names else "None")
        table.add_row("Tags", ", ".join(state.tags) if state.tags else "None")
        self.ctx.console.print(table)

        confirm_choice = self.prompter.select(
            "Create this user?",
            [
                Choice(title="Yes, create user", value=_YES),
                Choice(title="No, cancel", value=_NO),
                Separator(),
                Choice(title=_WIZARD_BACK_LABEL, value=NAV_BACK),
            ],
            default=_YES,
        )
        if confirm_choice == _YES:
            return _WizardNav.NEXT
        if confirm_choice == NAV_BACK:
            return _WizardNav.BACK
        return _WizardNav.CANCEL

    def _create_user_execute(self: Self, use_cases: IamUseCases, state: _CreateUserState) -> None:
        """Fully resolved state -> ``CreateUser`` + every follow-on attachment call."""
        assert state.name is not None
        password: str | None = (
            _generate_secure_password() if state.access.wants_console else None
        )

        user = use_cases.create_user.execute(CreateUserRequest(name=state.name, tags=state.tags))
        # The name was already sanitized/validated/checked-for-availability by
        # prompt_available_name above, so this is normally a no-op -- kept as the
        # source of truth for every downstream call, same defense as before.
        created_name = user.user_name

        for group_name in state.group_names:
            use_cases.add_user_to_group.execute(
                AddUserToGroupRequest(group_name=group_name, user_name=created_name)
            )
        if state.access.wants_console and password:
            use_cases.set_login_profile.execute(
                SetLoginProfileRequest(
                    user_name=created_name,
                    password=password,
                    password_reset_required=state.access.reset_required,
                )
            )

        access_key_id: str | None = None
        secret_access_key: str | None = None
        credentials_path: Path | None = None
        if state.access.wants_key:
            key = run_with_spinner(
                self.ctx.err_console,
                "[bold green]Generating API credentials...[/bold green]",
                lambda: use_cases.create_access_key.execute(
                    CreateAccessKeyRequest(user_name=created_name)
                ),
            )
            access_key_id = key.access_key_id
            secret_access_key = key.secret_access_key
            credentials_path = _save_credentials_file(
                created_name,
                access_key_id,
                secret_access_key,
                is_local=self.ctx.settings.is_local,
            )

        self._render_creation_success(
            name=created_name,
            password=password,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            credentials_path=credentials_path,
            wants_console=state.access.wants_console,
            wants_key=state.access.wants_key,
            group_names=state.group_names,
            policy_names=[],
            tags=state.tags,
        )
        self.ctx.console.print()
        self.prompter.pause()

    def _render_creation_success(
        self: Self,
        *,
        name: str,
        password: str | None,
        access_key_id: str | None,
        secret_access_key: str | None,
        credentials_path: Path | None = None,
        wants_console: bool,
        wants_key: bool,
        group_names: list[str],
        policy_names: list[str],
        tags: dict[str, str],
    ) -> None:
        """The closing success screen shared by Create User (New) and Copy User.

        Password and Secret Access Key get their own rows, right here --
        AWS shows both exactly once, at creation, and nowhere else in this
        CLI can ever show them again, so they're never buried in a separate
        print statement above the table, always right alongside everything
        else the admin needs to save. ``credentials_path`` -- where
        Programmatic Access's key was saved to disk (see
        ``_save_credentials_file``) -- is ``None`` only when Programmatic
        Access wasn't enabled; both Create User (New) and Copy User write one
        whenever it was.
        """
        table = Table(title="User Created", show_header=False)
        table.add_column("Field", style="bold cyan")
        table.add_column("Value")
        table.add_row("Name", name)
        table.add_row("Console Access", "Yes" if wants_console else "No")
        table.add_row("Password", password if password else "N/A")
        table.add_row("Programmatic Access", "Yes" if wants_key else "No")
        table.add_row("Access Key ID", access_key_id if access_key_id else "N/A")
        table.add_row("Secret Access Key", secret_access_key if secret_access_key else "N/A")
        if credentials_path is not None:
            table.add_row("Credentials File", relative_path_display(credentials_path))
        table.add_row("Groups", ", ".join(group_names) if group_names else "None")
        table.add_row("Policies", ", ".join(policy_names) if policy_names else "None")
        table.add_row("Tags", ", ".join(tags) if tags else "None")
        self.ctx.console.print(table)
        if password or secret_access_key:
            self.ctx.console.print(
                "[bold red]⚠ Save the Password/Secret Access Key above NOW -- "
                "they will not be shown again.[/]"
            )
        if credentials_path is not None:
            self.ctx.console.print(
                f"[bold green]✔ Credentials saved to: "
                f"{relative_path_display(credentials_path)}[/]"
            )

    def _copy_user(self: Self) -> None:
        """Copy User: a steppable wizard (Source / New Name / Console / Programmatic / Confirm).

        Tags, Groups, and Attached Policies are cloned from the source
        automatically (see ``CopyUserUseCase``) -- this wizard only asks
        what the source's own detail can't answer: console/programmatic
        access for the NEW user, which is never copied blindly (a
        password/access key can't be cloned; granting either is a fresh,
        deliberate choice the spec calls for).
        """
        use_cases = build_iam_use_cases(self.ctx)
        state = _CopyUserState()
        step = _CopyUserStep.SOURCE

        while True:
            clear_and_banner(self.ctx)
            if step is _CopyUserStep.SOURCE:
                nav = self._copy_user_step_source(state)
            elif step is _CopyUserStep.NEW_NAME:
                nav = self._copy_user_step_new_name(state)
            elif step is _CopyUserStep.CONSOLE_CONFIRM:
                nav = self._access_prefs_step_console_confirm(state.access)
            elif step is _CopyUserStep.PROGRAMMATIC_CONFIRM:
                nav = self._access_prefs_step_programmatic(state.access)
            else:
                nav = self._copy_user_step_confirm(state)
                if nav is _WizardNav.NEXT:
                    self._copy_user_execute(use_cases, state)
                    return

            if nav is _WizardNav.CANCEL:
                return
            if nav is _WizardNav.BACK:
                prev_step = _copy_user_prev_step(step, state)
                if prev_step is None:
                    return
                step = prev_step
                continue
            step = _copy_user_next_step(step, state)

    def _copy_user_step_source(self: Self, state: _CopyUserState) -> _WizardNav:
        """Step 1: pick the source user. No predecessor -- backing out exits the wizard.

        Query and pick are one self-contained sub-interaction: "<- Back" at
        the picker re-shows the query prompt (not a step back to the driving
        loop), same convention the Tags step's inner loop uses. Only backing
        out of the query prompt itself returns BACK to the driving loop.
        """
        while True:
            matches = self._query_users("Search for the user to copy:")
            if matches is None:
                return _WizardNav.BACK
            if not matches:
                self.ctx.err_console.print("[yellow]No matches.[/]")
                self.prompter.pause()
                continue
            source_name = self._pick_user(matches, "Which user do you want to copy?")
            if source_name is None:
                continue
            state.source_name = source_name
            return _WizardNav.NEXT

    def _copy_user_step_new_name(self: Self, state: _CopyUserState) -> _WizardNav:
        new_name = prompt_available_name(
            self.prompter,
            self.ctx.err_console,
            "New username:",
            validate=_validate_username,
            exists=self._user_exists,
        )
        if new_name is None:
            return _WizardNav.BACK
        state.new_name = new_name
        return _WizardNav.NEXT

    def _copy_user_step_confirm(self: Self, state: _CopyUserState) -> _WizardNav:
        assert state.source_name is not None
        assert state.new_name is not None
        table = Table(title="Pending User Copy", show_header=False)
        table.add_column("Field", style="bold cyan")
        table.add_column("Value")
        table.add_row("Source User", state.source_name)
        table.add_row("New Name", state.new_name)
        table.add_row("Console Access", "Yes" if state.access.wants_console else "No")
        if state.access.wants_console:
            # Always "Yes" -- a compliance rule, not an admin choice; see
            # ``_access_prefs_step_console_confirm``. Shown here only so the admin
            # sees it's in effect, not because it was ever asked about.
            table.add_row("Require Password Reset", "Yes (mandatory)")
        table.add_row("Programmatic Access", "Yes" if state.access.wants_key else "No")
        table.add_row("Clones from source", "Tags, Groups, and Attached Policies")
        self.ctx.console.print(table)

        confirm_choice = self.prompter.select(
            "Create this copy?",
            [
                Choice(title="Yes, create user", value=_YES),
                Choice(title="No, cancel", value=_NO),
                Separator(),
                Choice(title=_WIZARD_BACK_LABEL, value=NAV_BACK),
            ],
            default=_YES,
        )
        if confirm_choice == _YES:
            return _WizardNav.NEXT
        if confirm_choice == NAV_BACK:
            return _WizardNav.BACK
        return _WizardNav.CANCEL

    def _copy_user_execute(self: Self, use_cases: IamUseCases, state: _CopyUserState) -> None:
        assert state.source_name is not None
        assert state.new_name is not None
        source_name, new_name = state.source_name, state.new_name
        new_user = run_with_spinner(
            self.ctx.err_console,
            "[bold green]Cloning user permissions...[/bold green]",
            lambda: use_cases.copy_user.execute(
                CopyUserRequest(source_name=source_name, new_name=new_name)
            ),
        )
        created_name = new_user.user_name

        password: str | None = None
        if state.access.wants_console:
            password = _generate_secure_password()
            use_cases.set_login_profile.execute(
                SetLoginProfileRequest(
                    user_name=created_name,
                    password=password,
                    password_reset_required=state.access.reset_required,
                )
            )

        access_key_id: str | None = None
        secret_access_key: str | None = None
        credentials_path: Path | None = None
        if state.access.wants_key:
            key = run_with_spinner(
                self.ctx.err_console,
                "[bold green]Generating API credentials...[/bold green]",
                lambda: use_cases.create_access_key.execute(
                    CreateAccessKeyRequest(user_name=created_name)
                ),
            )
            access_key_id = key.access_key_id
            secret_access_key = key.secret_access_key
            credentials_path = _save_credentials_file(
                created_name,
                access_key_id,
                secret_access_key,
                is_local=self.ctx.settings.is_local,
            )

        cloned_groups = [
            g.group_name for g in use_cases.list_groups_for_user.execute(created_name)
        ]
        cloned_policies = [
            p.policy_name
            for p in use_cases.list_attached_policies.execute(created_name, principal_type="user")
        ]
        cloned_tags = _user_tags(new_user)

        self.ctx.console.print(
            f"[green]User '{created_name}' created from '{state.source_name}'.[/]"
        )
        self._render_creation_success(
            name=created_name,
            password=password,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            credentials_path=credentials_path,
            wants_console=state.access.wants_console,
            wants_key=state.access.wants_key,
            group_names=cloned_groups,
            policy_names=cloned_policies,
            tags=cloned_tags,
        )
        self.ctx.console.print()
        self.prompter.pause()

    # -- Delete: intentionally absent ----------------------------------------------
    #
    # User deletion is disabled in this TUI entirely -- no menu path anywhere in this
    # module (main menu, user detail screen, Disabled-Users audit) offers it. Still
    # available, if ever genuinely needed, through the non-interactive `iam user
    # delete` CLI command (`presentation/cli/iam_app.py`), which this module does
    # not touch.
