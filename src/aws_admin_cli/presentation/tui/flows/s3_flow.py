"""The S3 TUI screen: buckets, their lifecycle, security posture, and IAM binding.

Follows the same patterns ``iam_flow.py``/``ec2_flow.py`` share:

* **Unified service screen**: there is no intermediate menu. Entering S3
  lands straight on a live stats line (never cached across redraws) above one
  consolidated ``[Search, Create, Delete, Audit, Back]`` menu -- the same
  shape IAM and EC2 use, plus S3's own Audit dashboard. Returning here after
  any action goes through ``NavAction.STAY``, so ``NavigationStack`` clears
  the screen and re-runs ``menu()``, and the counters are recomputed for
  free. Search asks one free-text query (blank = everyone), matched against
  the bucket name OR its tag keys/values (``Environment=prod`` matches an
  exact tag; anything else is a substring match against the name or any tag),
  then always re-renders the matches (even just one) as its own cancellable
  ``select()`` -- the user's last action is always an explicit pick, never an
  implicit one.
* **Guard rail pattern**: ``bucket delete`` checks emptiness up front via
  ``GetBucketInfoUseCase`` -- knowing the object count is what decides whether
  to offer "empty it and continue", so that check has to happen before the
  delete call, not react to a failure from it.
* **Arrow-key destructive confirmation**: no text prompts -- see
  ``flows/_shared.py::confirm_destructive``, shared with EC2's terminate and
  IAM's delete-user.
* **Bucket detail sub-menu**: List Objects (a read-only inspection table --
  Key, Size, Storage Class, Last Modified -- no upload/download/delete;
  see ``_list_objects``), Manage IAM Access Policy (generates a bucket-scoped
  customer-managed IAM policy and attaches it to a picked IAM user), Edit
  Tags, and Empty Bucket (purges every object/version in place). Bucket
  creation offers the same IAM-binding step inline, right after the bucket
  is created.
"""

import logging
from dataclasses import dataclass
from typing import ClassVar, Self

from rich.panel import Panel

from aws_admin_cli.application.dto.iam import (
    AttachPolicyRequest,
    CreatePolicyRequest,
    DetachPolicyRequest,
)
from aws_admin_cli.application.dto.s3 import (
    CreateBucketRequest,
    DeleteBucketRequest,
    ListObjectsRequest,
    SetBucketTagsRequest,
)
from aws_admin_cli.application.use_cases.s3.get_bucket_info import BucketInfo
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.domain.models.policy import PolicyDocument, PolicyEffect, PolicyStatement
from aws_admin_cli.domain.models.s3 import Bucket, VersioningStatus, validate_bucket_name
from aws_admin_cli.presentation.cli.formatters.render import render
from aws_admin_cli.presentation.tui.flows._shared import (
    announce_result,
    clear_and_banner,
    confirm_destructive,
    confirm_yes_no,
    prompt_available_name,
    prompt_text_or_cancel,
    run_with_spinner,
)
from aws_admin_cli.presentation.tui.flows.error_handler import aws_error_handler
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT, Choice, Separator
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.tui.prompter import Prompter
from aws_admin_cli.presentation.wiring import build_iam_use_cases, build_s3_use_cases

__all__ = ["S3Flow"]

_CREATE_BUCKET = "create_bucket"
_DELETE_BUCKET = "delete_bucket"
_AUDIT = "audit"
_SEARCH_THRESHOLD = 25

_EXPLORE_SEARCH = "search"

_DETAIL_LIST_OBJECTS = "detail_list_objects"
_DETAIL_MANAGE_POLICY = "detail_manage_policy"
_DETAIL_EDIT_TAGS = "detail_edit_tags"
_DETAIL_EMPTY_BUCKET = "detail_empty_bucket"
_DETAIL_TOGGLE_PUBLIC_ACCESS = "detail_toggle_public_access"

_TAG_ADD = "tag_add"
_TAG_DELETE = "tag_delete"

_BLOCK_PUBLIC_ACCESS = "block_public_access"
_ALLOW_PUBLIC_ACCESS = "allow_public_access"

_TAGS_YES = "tags_yes"
_TAGS_NO = "tags_no"

_IAM_BIND_YES = "iam_bind_yes"
_IAM_BIND_NO = "iam_bind_no"

_OWNER_REASSIGN = "owner_reassign"
_OWNER_UNASSIGN = "owner_unassign"
_OWNER_ATTACH = "owner_attach"

_OWNER_TAG_KEY = "Owner"
_NO_OWNER = "-"

_CANCEL_CREATION = "cancel_creation"

# The Region Selector (`environment_flow.py`) is the only place `ctx.settings.region`
# ever changes, so this only ever needs to know the display name for its own five
# regions -- an unrecognized code (there shouldn't be one) just falls back to the
# bare code in `_region_display`.
_REGION_DISPLAY_NAMES: dict[str, str] = {
    "us-east-1": "N. Virginia",
    "us-west-2": "Oregon",
    "eu-west-1": "Ireland",
    "sa-east-1": "São Paulo",
    "ap-northeast-1": "Tokyo",
}

# A standard, scoped read/write grant -- never "s3:*"/"Resource": "*" -- so this can
# never trip CreatePolicyUseCase's full-wildcard guard.
_S3_ACCESS_POLICY_ACTIONS: tuple[str, ...] = (
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:ListBucket",
)


def _bucket_access_policy_document(name: str) -> PolicyDocument:
    """The standard read/write policy document generated for a bucket <-> IAM user binding."""
    return PolicyDocument(
        statement=[
            PolicyStatement(
                effect=PolicyEffect.ALLOW,
                action=list(_S3_ACCESS_POLICY_ACTIONS),
                resource=[f"arn:aws:s3:::{name}", f"arn:aws:s3:::{name}/*"],
            )
        ]
    )


def _bucket_owner_label(tags: dict[str, str]) -> str:
    """The bucket detail table's Owner cell: the ``Owner`` tag's value, or "-"."""
    return tags.get(_OWNER_TAG_KEY) or _NO_OWNER


def _encryption_label(algorithm: str | None) -> str:
    """The Audit table's Encryption cell: SSE-S3/KMS in green, NONE in red."""
    if algorithm is None:
        return "[bold red]NONE[/]"
    if algorithm == "AES256":
        return "[bold green]SSE-S3[/]"
    return "[bold green]KMS[/]"


def _public_access_label(blocked: bool) -> str:
    """The Audit table's Public Access cell.

    ``blocked`` must always come from a live ``GetPublicAccessBlock`` call
    (``S3Gateway.get_public_access_block`` -- True only when all 4 Block
    Public Access flags are on; a missing configuration, same as any flag
    being off, reads as exposed) -- this is the dashboard's own real-time
    risk read on an existing bucket, so it must never be sourced from what
    some earlier wizard step merely asked for.
    """
    return "[bold green]BLOCKED[/]" if blocked else "[bold red]PUBLIC ALERT[/]"


def _public_access_choice_label(allow_public: bool) -> str:
    """The Creation Summary's Public Access cell: the wizard's own Block/Allow answer.

    Deliberately a different signal than ``_public_access_label``: this is a
    receipt of what the wizard is about to do (or just did), styled as a
    heads-up (yellow) rather than an alert (red) when public access was
    allowed -- the live, red-alert read belongs to the Audit dashboard once
    the bucket actually exists.
    """
    if allow_public:
        return "[bold yellow]⚠️  ALLOWED[/]"
    return "[bold green]BLOCKED[/]"


def _access_label(is_public: bool) -> str:
    """The Bucket Detail table's Access cell: 🔓 Public or 🔒 Private.

    ``is_public`` comes from ``GetBucketAccessUseCase`` -- any of the 4
    Block Public Access flags off (or no configuration at all) reads as
    Public, the same signal the stats line's Public/Private split and the
    Audit table's ``public_access_blocked`` use, so this label is plain
    text, not the Audit table's colored BLOCKED/PUBLIC ALERT styling: it's
    a factual state badge here, not a compliance alert.
    """
    return "🔓 Public" if is_public else "🔒 Private"


def _region_display(region: str) -> str:
    """"<region_code> (<location_name>)" for the Bucket Creation Summary.

    Falls back to the bare code if it's not one of the five regions the
    Region Selector offers.
    """
    name = _REGION_DISPLAY_NAMES.get(region)
    return f"{region} ({name})" if name else region


def _human_size(num_bytes: int) -> str:
    """Human-readable total size (e.g. ``"3.4MB"``) -- presentation only."""
    size = float(num_bytes)
    units = ("B", "KB", "MB", "GB", "TB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if size < 1024 or candidate == units[-1]:
            break
        size /= 1024
    return f"{size:.1f}{unit}"


@dataclass(slots=True)
class S3Flow:
    """S3's top-level TUI screen: search/filter, create, delete, audit, and bucket management."""

    title: ClassVar[str] = "📦 S3 Storage & Security Governance"

    ctx: AppContext
    prompter: Prompter

    @aws_error_handler
    def menu(self: Self) -> NavAction:
        """Show S3's menu once."""
        clear_and_banner(self.ctx)
        buckets = build_s3_use_cases(self.ctx).list_buckets.execute()
        self._render_bucket_stats(buckets)
        selected = self.prompter.select("S3 (Buckets) -- What do you want to do?", self._choices())
        if selected is None or selected == NAV_EXIT:
            return NavAction.EXIT
        if selected == NAV_BACK:
            return NavAction.BACK
        if selected == _EXPLORE_SEARCH:
            self._search_buckets()
        elif selected == _CREATE_BUCKET:
            self._create_bucket()
        elif selected == _DELETE_BUCKET:
            self._delete_bucket()
        return NavAction.STAY

    def _choices(self: Self) -> list[Choice | Separator]:
        return [
            Choice(title="🔍 Search / Filter Buckets", value=_EXPLORE_SEARCH),
            Choice(title="+ Create Hardened Bucket", value=_CREATE_BUCKET),
            Choice(title="❌ Delete Bucket", value=_DELETE_BUCKET),
            Separator(),
            Choice(title="↩️  Back", value=NAV_BACK),
        ]

    # -- Search (shared by List and Delete) --------------------------------

    def _query_buckets(self: Self, message: str = "Enter search query:") -> list[Bucket] | None:
        """Ask for a free-text query (blank = everyone); matches name or tags.

        Returns the matches (possibly empty, if the query hit nothing), or
        ``None`` if cancelled or there are no buckets at all to search.
        """
        buckets = build_s3_use_cases(self.ctx).list_buckets.execute()
        if not buckets:
            self.ctx.err_console.print("[yellow]No buckets found.[/]")
            self.prompter.pause()
            return None

        clear_and_banner(self.ctx)
        self._render_bucket_stats(buckets)
        query = prompt_text_or_cancel(self.prompter, self.ctx.err_console, message)
        if query is None:
            return None
        if not query:
            return buckets
        return self._filter_buckets(buckets, query)

    def _filter_buckets(self: Self, buckets: list[Bucket], query: str) -> list[Bucket]:
        """Match ``query`` against a bucket's name, or its tag keys/values.

        ``Key=Value`` (e.g. ``Environment=prod``) matches an exact tag; any
        other text is a substring match against the name or any tag key/value.
        Tags are fetched lazily, per bucket, only when the name check alone
        didn't already match -- ``or`` short-circuits below, so a name hit
        never costs a ``GetBucketTagging`` call.
        """
        needle = query.strip().lower()
        if "=" in needle:
            key, _, value = needle.partition("=")
            key, value = key.strip(), value.strip()
            return [b for b in buckets if self._bucket_has_tag(b.name, key, value)]
        return [b for b in buckets if needle in b.name.lower() or self._tags_match(b.name, needle)]

    def _bucket_has_tag(self: Self, bucket_name: str, key: str, value: str) -> bool:
        tags = build_s3_use_cases(self.ctx).get_bucket_tags.execute(bucket_name)
        return any(k.lower() == key and v.lower() == value for k, v in tags.items())

    def _tags_match(self: Self, bucket_name: str, needle: str) -> bool:
        tags = build_s3_use_cases(self.ctx).get_bucket_tags.execute(bucket_name)
        return any(needle in k.lower() or needle in v.lower() for k, v in tags.items())

    def _filter_and_pick_bucket(self: Self, message: str) -> str | None:
        """Search, always through a selectable list -- never an auto-pick."""
        matches = self._query_buckets()
        if matches is None:
            return None
        if not matches:
            self.ctx.err_console.print("[yellow]No matches.[/]")
            self.prompter.pause()
            return None

        choices: list[Choice | Separator] = [
            Choice(title=f"{b.name}  ({b.creation_date:%Y-%m-%d})", value=b.name) for b in matches
        ]
        choices.append(Separator())
        choices.append(Choice(title="↩️  Back", value=NAV_BACK))
        selected = self.prompter.select(
            message, choices, use_search=len(matches) > _SEARCH_THRESHOLD
        )
        if selected is None or selected == NAV_BACK:
            return None
        return selected

    # -- Stats line + search (the service screen itself) ---------------------

    def _render_bucket_stats(self: Self, buckets: list[Bucket]) -> None:
        """Print "  Buckets: N Total | N Public | N Private" above the service menu.

        The Public/Private split costs one ``GetBucketAccessUseCase`` call
        per bucket (a lightweight ``GetPublicAccessBlock``) -- unlike a full
        ``ListObjects`` per bucket, this is cheap and unbilled,
        so it's paid on every render rather than cached: the whole point of
        this line is to reflect live risk, the same reasoning
        ``resolve_endpoint_status`` -- a different screen's live indicator --
        already documents.
        """
        access_by_bucket = build_s3_use_cases(self.ctx).list_bucket_access.execute(buckets)
        public_count = sum(access_by_bucket.values())
        private_count = len(buckets) - public_count
        self.ctx.err_console.print()  # spacing from the previous prompt's answer line
        self.ctx.err_console.print(
            f"  Buckets: {len(buckets)} Total  |  {public_count} Public 🔓  |  "
            f"{private_count} Private 🔒"
        )

    def _search_buckets(self: Self) -> None:
        name = self._filter_and_pick_bucket("Which bucket do you want to view?")
        if name is None:
            return
        self._bucket_detail_loop(name)

    def _bucket_detail_loop(self: Self, name: str) -> None:
        """Detail view + the bucket-detail sub-menu; loops until the user backs out.

        Clears the screen and redraws the header on every entry into this
        loop, so no trace of the prior search screen lingers.
        """
        while True:
            clear_and_banner(self.ctx)
            buckets = build_s3_use_cases(self.ctx).list_buckets.execute()
            self._render_bucket_stats(buckets)
            self._render_bucket_detail(name)
            selected = self.prompter.select(
                f"Bucket '{name}' -- what do you want to do?",
                [
                    Choice(title="📄 List Objects", value=_DETAIL_LIST_OBJECTS),
                    Choice(title="🔐 Manage IAM Access Policy", value=_DETAIL_MANAGE_POLICY),
                    Choice(title="🏷️  Edit Tags", value=_DETAIL_EDIT_TAGS),
                    Choice(
                        title="🔓 / 🔒 Toggle Public Access Block",
                        value=_DETAIL_TOGGLE_PUBLIC_ACCESS,
                    ),
                    Choice(title="🧹 Empty Bucket", value=_DETAIL_EMPTY_BUCKET),
                    Separator(),
                    Choice(title="↩️  Back", value=NAV_BACK),
                ],
            )
            if selected is None or selected == NAV_BACK:
                return
            if selected == _DETAIL_LIST_OBJECTS:
                self._list_objects(name)
            elif selected == _DETAIL_MANAGE_POLICY:
                self._manage_iam_policy(name)
            elif selected == _DETAIL_EDIT_TAGS:
                self._edit_tags(name)
            elif selected == _DETAIL_TOGGLE_PUBLIC_ACCESS:
                self._toggle_public_access(name)
            elif selected == _DETAIL_EMPTY_BUCKET:
                self._empty_bucket(name)

    def _render_bucket_detail(self: Self, name: str) -> BucketInfo:
        use_cases = build_s3_use_cases(self.ctx)
        info = run_with_spinner(
            self.ctx.err_console,
            "[bold green]Scanning bucket security compliance...[/bold green]",
            lambda: use_cases.get_bucket_info.execute(name),
        )
        is_public = use_cases.get_bucket_access.execute(name)
        # "Owner" gets its own row (read straight off the `Owner` tag, "-" when absent
        # or blank) -- excluded from the generic "Tags" row below so it isn't shown
        # twice, same convention EC2's instance/AMI detail tables already use.
        other_tags = {k: v for k, v in info.tags.items() if k != _OWNER_TAG_KEY}
        render(
            {
                "Name": info.name,
                "Region": info.region,
                "Access": _access_label(is_public),
                "Versioning": info.versioning.status.value,
                "Objects": info.object_count,
                "Owner": _bucket_owner_label(info.tags),
                "Tags": other_tags or "(no tags)",
            },
            ctx=self.ctx,
            title=f"bucket: {name}",
        )
        return info

    def _toggle_public_access(self: Self, name: str) -> None:
        """Flip Block Public Access on ``name``: Public -> blocked, Private -> unblocked."""
        use_cases = build_s3_use_cases(self.ctx)
        is_public = use_cases.get_bucket_access.execute(name)
        use_cases.set_public_access.execute(name, block=is_public)
        if is_public:
            announce_result(
                self.ctx,
                self.prompter,
                "[bold green][✓] Block Public Access enabled. Bucket is now PRIVATE.[/]",
            )
        else:
            announce_result(
                self.ctx,
                self.prompter,
                "[bold yellow][✓] Public Access Block removed. Bucket is now PUBLIC.[/]",
            )

    # -- Object inspection (read-only; "List Objects") ---------------------------

    def _list_objects(self: Self, bucket: str) -> None:
        """Read-only object inspection table: Key, Size, Storage Class, Last Modified.

        No upload/download/delete here -- this view only ever calls
        ``ListObjectsV2``, never a mutating S3 API.
        """
        clear_and_banner(self.ctx)
        listing = build_s3_use_cases(self.ctx).list_objects.execute(
            ListObjectsRequest(bucket=bucket)
        )
        if not listing.objects:
            self.ctx.err_console.print(
                f"[yellow]Bucket '{bucket}' is empty (0 objects found).[/]"
            )
        else:
            render(
                [
                    {
                        "Key / Object Name": obj.key,
                        "Size": obj.human_size,
                        "Storage Class": obj.storage_class.value,
                        "Last Modified": obj.last_modified.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    for obj in listing.objects
                ],
                ctx=self.ctx,
                title=f"objects in {bucket}",
            )
        self.prompter.pause("Press Enter to return to Bucket Menu...")

    # -- IAM access policy binding (Create wizard + bucket detail) -----------------

    def _pick_iam_user(self: Self) -> str | None:
        """Filter-first IAM user picker -- blank query means "show everyone"."""
        clear_and_banner(self.ctx)
        users = build_iam_use_cases(self.ctx).list_users.execute(None)
        if not users:
            self.ctx.err_console.print("[yellow]No IAM users found in this account.[/]")
            self.prompter.pause()
            return None
        query = prompt_text_or_cancel(
            self.prompter, self.ctx.err_console, "Filter IAM users (blank = show all)"
        )
        if query is None:
            return None
        needle = query.strip().lower()
        matches = sorted(u.user_name for u in users if not needle or needle in u.user_name.lower())
        if not matches:
            self.ctx.err_console.print(f"[yellow]No IAM users match '{query}'.[/]")
            self.prompter.pause()
            return None
        choices: list[Choice | Separator] = [Choice(title=name, value=name) for name in matches]
        choices.append(Separator())
        choices.append(Choice(title="↩️  Back", value=NAV_BACK))
        selected = self.prompter.select(
            "Which IAM user?", choices, use_search=len(matches) > _SEARCH_THRESHOLD
        )
        if selected is None or selected == NAV_BACK:
            return None
        return selected

    def _manage_iam_policy(self: Self, bucket: str) -> None:
        """Owner-aware entry point: reassign/unassign when assigned, attach when not.

        Prevents a bucket ever ending up over-assigned (two different users
        both holding its policy at once).
        """
        current_owner = _bucket_owner_label(
            build_s3_use_cases(self.ctx).get_bucket_tags.execute(bucket)
        )
        if current_owner != _NO_OWNER:
            self._manage_iam_policy_assigned(bucket, current_owner)
        else:
            self._manage_iam_policy_unassigned(bucket)

    def _manage_iam_policy_assigned(self: Self, bucket: str, current_owner: str) -> None:
        choice = self.prompter.select(
            f"Bucket '{bucket}' is currently assigned to user '{current_owner}'. "
            "Select action:",
            [
                Choice(title="🔄 Reassign to a different IAM user", value=_OWNER_REASSIGN),
                Choice(title="❌ Unassign current user", value=_OWNER_UNASSIGN),
                Separator(),
                Choice(title="↩️  Back", value=NAV_BACK),
            ],
        )
        if choice is None or choice == NAV_BACK:
            return
        if choice == _OWNER_REASSIGN:
            new_user = self._pick_iam_user()
            if new_user is None:
                return
            self._detach_bucket_policy(bucket, current_owner)
            self._bind_iam_policy(bucket, new_user)
        else:  # _OWNER_UNASSIGN
            self._detach_bucket_policy(bucket, current_owner)
            self._set_bucket_owner_tag(bucket, None)
            announce_result(
                self.ctx,
                self.prompter,
                f"[bold green]User '{current_owner}' unassigned from bucket '{bucket}'.[/]",
            )

    def _manage_iam_policy_unassigned(self: Self, bucket: str) -> None:
        choice = self.prompter.select(
            f"Bucket '{bucket}' is currently unassigned. Select action:",
            [
                Choice(title="➕ Attach IAM user policy", value=_OWNER_ATTACH),  # noqa: RUF001
                Separator(),
                Choice(title="↩️  Back", value=NAV_BACK),
            ],
        )
        if choice is None or choice == NAV_BACK:
            return
        user = self._pick_iam_user()
        if user is None:
            return
        self._bind_iam_policy(bucket, user)

    def _detach_bucket_policy(self: Self, bucket: str, user: str) -> None:
        """Detach the bucket's ``s3-access-<bucket>`` policy from ``user`` in AWS.

        A no-op if ``user`` doesn't actually have it attached -- keeps
        reassign/unassign safe to call even if the ``Owner`` tag and IAM's
        own attachment state have ever drifted apart from each other.
        """
        use_cases = build_iam_use_cases(self.ctx)
        policy_name = f"s3-access-{bucket}"
        attached = use_cases.list_attached_policies.execute(user, principal_type="user")
        policy = next((p for p in attached if p.policy_name == policy_name), None)
        if policy is None:
            return
        use_cases.detach_policy.execute(
            DetachPolicyRequest(
                principal_name=user, policy_arn=policy.policy_arn, principal_type="user"
            )
        )

    def _set_bucket_owner_tag(self: Self, bucket: str, owner: str | None) -> None:
        """Set (or clear) the bucket's ``Owner`` tag -- what ``_manage_iam_policy`` reads.

        Reads the current tag set first: S3's ``PutBucketTagging`` always
        replaces the WHOLE tag set, so every other tag must be carried
        forward unchanged, not just the one being written here.
        """
        use_cases = build_s3_use_cases(self.ctx)
        current = use_cases.get_bucket_tags.execute(bucket)
        if owner is None:
            if _OWNER_TAG_KEY not in current:
                return
            updated = {k: v for k, v in current.items() if k != _OWNER_TAG_KEY}
        else:
            updated = {**current, _OWNER_TAG_KEY: owner}
        use_cases.set_bucket_tags.execute(SetBucketTagsRequest(name=bucket, tags=updated))

    def _bind_iam_policy(self: Self, bucket: str, user: str) -> None:
        """Generate (or reuse) a bucket-scoped policy and attach it -- pure AWS API, no file.

        "Reuse" means: a customer-managed policy already exists under this
        bucket's conventional name (``s3-access-<bucket>``) -- that's this
        flow's own "update existing" case, since ``AttachPolicyUseCase`` is
        already idempotent about re-attaching a policy a user already has.
        """
        use_cases = build_iam_use_cases(self.ctx)
        policy_name = f"s3-access-{bucket}"
        document = _bucket_access_policy_document(bucket)
        existing = next(
            (
                p
                for p in use_cases.list_policies.execute(scope="Local", only_attached=False)
                if p.policy_name == policy_name
            ),
            None,
        )
        if existing is not None:
            policy_arn = existing.arn
        else:
            policy = run_with_spinner(
                self.ctx.err_console,
                "[bold green]Generating IAM access policy...[/bold green]",
                lambda: use_cases.create_policy.execute(
                    CreatePolicyRequest(
                        name=policy_name,
                        document=document,
                        description=f"S3 access policy for bucket '{bucket}' (aws-admin-cli).",
                    )
                ),
            )
            policy_arn = policy.arn

        use_cases.attach_policy.execute(
            AttachPolicyRequest(principal_name=user, policy_arn=policy_arn, principal_type="user")
        )
        self._set_bucket_owner_tag(bucket, user)
        announce_result(
            self.ctx,
            self.prompter,
            f"[bold green]Policy '{policy_name}' attached to IAM user '{user}' in AWS.[/]",
        )

    # -- Tags -----------------------------------------------------------------------

    def _edit_tags(self: Self, name: str) -> None:
        while True:
            clear_and_banner(self.ctx)
            tags = build_s3_use_cases(self.ctx).get_bucket_tags.execute(name)
            render(tags or {"(no tags)": ""}, ctx=self.ctx, title=f"tags: {name}")
            selected = self.prompter.select(
                "Tags -- what do you want to do?",
                [
                    Choice(title="Add / update tag", value=_TAG_ADD),
                    Choice(
                        title="Delete tag", value=_TAG_DELETE, disabled=None if tags else "no tags"
                    ),
                    Separator(),
                    Choice(title="↩️  Back", value=NAV_BACK),
                ],
            )
            if selected is None or selected == NAV_BACK:
                return
            if selected == _TAG_ADD:
                self._add_bucket_tag(name, tags)
            elif selected == _TAG_DELETE:
                self._delete_bucket_tag(name, tags)

    def _add_bucket_tag(self: Self, name: str, tags: dict[str, str]) -> None:
        clear_and_banner(self.ctx)
        key = prompt_text_or_cancel(self.prompter, self.ctx.err_console, "Tag key")
        if not key:
            return
        value = prompt_text_or_cancel(self.prompter, self.ctx.err_console, f"Value for '{key}'")
        if value is None:
            return
        updated = {**tags, key: value}
        build_s3_use_cases(self.ctx).set_bucket_tags.execute(
            SetBucketTagsRequest(name=name, tags=updated)
        )
        announce_result(self.ctx, self.prompter, f"[green]Tag '{key}={value}' applied.[/]")

    def _delete_bucket_tag(self: Self, name: str, tags: dict[str, str]) -> None:
        choices: list[Choice | Separator] = [
            Choice(title=f"{k} = {v}", value=k) for k, v in tags.items()
        ]
        choices.append(Separator())
        choices.append(Choice(title="↩️  Back", value=NAV_BACK))
        key = self.prompter.select("Which tag do you want to delete?", choices)
        if key is None or key == NAV_BACK:
            return
        if not confirm_destructive(self.prompter, kind="tag", name=key):
            return
        remaining = {k: v for k, v in tags.items() if k != key}
        build_s3_use_cases(self.ctx).set_bucket_tags.execute(
            SetBucketTagsRequest(name=name, tags=remaining)
        )
        announce_result(self.ctx, self.prompter, f"[green]Tag '{key}' deleted.[/]")

    # -- Empty bucket -----------------------------------------------------------------

    def _empty_bucket(self: Self, name: str) -> None:
        if not confirm_destructive(self.prompter, kind="contents of bucket", name=name):
            return
        run_with_spinner(
            self.ctx.err_console,
            "[bold green]Purging bucket objects...[/bold green]",
            lambda: build_s3_use_cases(self.ctx).empty_bucket.execute(name),
        )
        announce_result(self.ctx, self.prompter, f"[green]Bucket '{name}' emptied.[/]")

    # -- Audit ------------------------------------------------------------------------

    def _run_audit(self: Self) -> None:
        clear_and_banner(self.ctx)
        entries = run_with_spinner(
            self.ctx.err_console,
            "[bold green]Scanning bucket security compliance...[/bold green]",
            lambda: build_s3_use_cases(self.ctx).audit_buckets.execute(),
        )
        if not entries:
            self.ctx.err_console.print("[yellow]No buckets found.[/]")
            self.prompter.pause()
            return
        render(
            [
                {
                    "Bucket Name": entry.name,
                    "Public Access": _public_access_label(entry.public_access_blocked),
                    "Encryption": _encryption_label(entry.encryption),
                    "Versioning": (
                        "[bold green]ENABLED[/]"
                        if entry.versioning is VersioningStatus.ENABLED
                        else "[grey50]DISABLED[/]"
                    ),
                    "Objects": entry.object_count,
                    "Size": _human_size(entry.total_size),
                }
                for entry in entries
            ],
            ctx=self.ctx,
            title="S3 Security & Compliance Audit",
        )
        self.prompter.pause()

    # -- Create -----------------------------------------------------------------

    def _create_bucket(self: Self) -> None:
        clear_and_banner(self.ctx)
        gateway = build_s3_use_cases(self.ctx).gateway

        # No region prompt here -- a bucket always inherits the session's global
        # region (the Region Selector's own switch, `ctx.settings.region`), so a
        # bucket can never end up in a different region than the session that
        # created it targets.
        region = self.ctx.settings.region

        # Name -> Public Access -> Tags -> IAM Bind is one back-navigable step chain.
        # "↩️  Back" at any of the three wizard gates re-shows the prompt immediately
        # before it -- for Public Access, that's the bucket name prompt, so `name` is
        # cleared and re-asked rather than carried across that hop the way
        # `allow_public`/`tags`/`iam_user` are. "🚫  Cancel creation" (Tags and IAM
        # Bind gates only -- Public Access offers only "↩️  Back") aborts the whole
        # wizard and drops straight back to the S3 menu.
        name: str | None = None
        allow_public = False
        tags: dict[str, str] = {}
        iam_user: str | None = None
        step = 0
        while True:
            if step == 0:
                if name is None:
                    clear_and_banner(self.ctx)
                    name = prompt_available_name(
                        self.prompter,
                        self.ctx.err_console,
                        "Bucket name (globally unique):",
                        validate=validate_bucket_name,
                        exists=gateway.bucket_exists,
                    )
                    if name is None:
                        return
                clear_and_banner(self.ctx)
                public_choice = self.prompter.select(
                    "Block Public Access?",
                    [
                        Choice(
                            title="🔒 Yes, Block All Public Access (Recommended)",
                            value=_BLOCK_PUBLIC_ACCESS,
                        ),
                        Choice(title="⚠️  No, Allow Public Access", value=_ALLOW_PUBLIC_ACCESS),
                        Separator(),
                        Choice(title="↩️  Back", value=NAV_BACK),
                    ],
                    default=_BLOCK_PUBLIC_ACCESS,
                )
                if public_choice is None or public_choice == _CANCEL_CREATION:
                    return
                if public_choice == NAV_BACK:
                    name = None
                    continue
                allow_public = public_choice == _ALLOW_PUBLIC_ACCESS
                step = 1
            elif step == 1:
                clear_and_banner(self.ctx)
                tags_choice = self.prompter.select(
                    "Add Resource Tags to Bucket?",
                    [
                        Choice(title="🏷️  Yes, add resource tags", value=_TAGS_YES),
                        Choice(title="➡️  No, skip tags", value=_TAGS_NO),
                        Separator(),
                        Choice(title="↩️  Back", value=NAV_BACK),
                        Choice(title="🚫  Cancel creation", value=_CANCEL_CREATION),
                    ],
                    default=_TAGS_NO,
                )
                if tags_choice is None or tags_choice == _CANCEL_CREATION:
                    return
                if tags_choice == NAV_BACK:
                    step = 0
                    continue
                if tags_choice == _TAGS_YES:
                    clear_and_banner(self.ctx)
                    tags = self._prompt_new_bucket_tags()
                else:
                    tags = {}
                step = 2
            else:
                clear_and_banner(self.ctx)
                bind_choice = self.prompter.select(
                    "Attach an S3 access policy to an IAM user for this bucket?",
                    [
                        Choice(
                            title="Yes, attach access policy to an IAM user",
                            value=_IAM_BIND_YES,
                        ),
                        Choice(title="No, keep unassigned", value=_IAM_BIND_NO),
                        Separator(),
                        Choice(title="↩️  Back", value=NAV_BACK),
                        Choice(title="🚫  Cancel creation", value=_CANCEL_CREATION),
                    ],
                    default=_IAM_BIND_NO,
                )
                if bind_choice is None or bind_choice == _CANCEL_CREATION:
                    return
                if bind_choice == NAV_BACK:
                    step = 1
                    continue
                iam_user = self._pick_iam_user() if bind_choice == _IAM_BIND_YES else None
                break

        # Reaching here means step 0 committed a name and the loop advanced past it --
        # `name` is only ever `None` while still inside step 0, spelled out for mypy.
        assert name is not None
        clear_and_banner(self.ctx)
        self.ctx.err_console.print()  # spacing between the header box and this subtitle
        render(
            {
                "Bucket Name": name,
                "Region": _region_display(region),
                "Public Access": _public_access_choice_label(allow_public),
                "Encryption": "SSE-S3",
                "Tags": ", ".join(f"{k}={v}" for k, v in tags.items()) or "(none)",
                "IAM Binding": iam_user or "(unassigned)",
            },
            ctx=self.ctx,
            title="Bucket Creation Summary",
        )
        if allow_public:
            self.ctx.err_console.print(
                Panel(
                    f"⚠️  WARNING: Bucket '{name}' is being created WITHOUT Block "
                    "Public Access.\nIt may be exposed to the internet if a public "
                    "bucket policy is applied.",
                    border_style="yellow",
                    style="yellow",
                    expand=False,
                )
            )
        use_cases = build_s3_use_cases(self.ctx)
        previous_log_level = self.ctx.logger.level
        if allow_public:
            # The use case's own logger.warning() for this same condition renders as
            # a raw timestamped log line via RichHandler -- redundant now that this
            # screen shows the callout above, so it's muted for this one call only.
            self.ctx.logger.setLevel(logging.ERROR)
        try:
            bucket = run_with_spinner(
                self.ctx.err_console,
                "[bold green]Applying lifecycle policies...[/bold green]",
                lambda: use_cases.create_bucket.execute(
                    CreateBucketRequest(name=name, region=region, allow_public=allow_public)
                ),
            )
        finally:
            self.ctx.logger.setLevel(previous_log_level)
        if tags:
            use_cases.set_bucket_tags.execute(SetBucketTagsRequest(name=bucket.name, tags=tags))
        announce_result(self.ctx, self.prompter, f"[green]Bucket '{bucket.name}' created.[/]")

        if iam_user is not None:
            self._bind_iam_policy(bucket.name, iam_user)

    def _prompt_new_bucket_tags(self: Self) -> dict[str, str]:
        """Repeated ``Key=Value`` capture for the create wizard; blank line finishes."""
        tags: dict[str, str] = {}
        while True:
            raw = prompt_text_or_cancel(
                self.prompter, self.ctx.err_console, "Tag (Key=Value, blank to finish)"
            )
            if not raw:
                return tags
            key, sep, value = raw.partition("=")
            if not sep or not key.strip():
                self.ctx.err_console.print(
                    f"[red]Invalid tag '{raw}' -- use the Key=Value format.[/]"
                )
                continue
            tags[key.strip()] = value.strip()

    # -- Delete ----------------------------------------------------------------

    def _delete_bucket(self: Self) -> None:
        name = self._filter_and_pick_bucket("Which bucket do you want to delete?")
        if name is None:
            return
        clear_and_banner(self.ctx)
        buckets = build_s3_use_cases(self.ctx).list_buckets.execute()
        self._render_bucket_stats(buckets)
        info = self._render_bucket_detail(name)

        force = False
        if info.object_count > 0:
            if not confirm_yes_no(
                self.prompter,
                f"'{name}' contains {info.object_count} object(s). Empty it and continue?",
            ):
                return
            force = True

        if not confirm_destructive(self.prompter, kind="bucket", name=name):
            return

        run_with_spinner(
            self.ctx.err_console,
            f"[bold green]Deleting bucket '{name}'...[/bold green]",
            lambda: build_s3_use_cases(self.ctx).delete_bucket.execute(
                DeleteBucketRequest(name=name, force=force)
            ),
        )
        announce_result(self.ctx, self.prompter, f"[green]Bucket '{name}' deleted.[/]")
