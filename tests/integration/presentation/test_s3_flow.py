"""Tests for ``S3Flow``: search, the bucket detail/object screen, and the
arrow-key destructive confirmation on delete.
"""

import io
from dataclasses import replace
from pathlib import Path

from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.presentation.tui.flows.s3_flow import S3Flow
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT, Choice
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.wiring import build_s3_use_cases
from moto import mock_aws
from rich.console import Console

from tests.fakes.prompter import FakePrompter


def _app_ctx() -> AppContext:
    return AppContext.build(Settings(profile="testprofile"))


# -- Main menu shape and navigation ------------------------------------------------


def test_choices_match_the_unified_service_screen_template() -> None:
    """The service screen is one consolidated menu -- no List/Create/Delete picker.

    Guards the shared template: search, create and delete sit at the root of
    every service screen, in this order -- IAM and EC2 assert the same shape
    with their own nouns.
    """
    ctx = _app_ctx()
    choices = S3Flow(ctx, FakePrompter())._choices()
    labels = [c.title if isinstance(c, Choice) else c.line for c in choices]
    assert labels == [
        "🔍 Search / Filter Buckets",
        "+ Create Hardened Bucket",
        "❌ Delete Bucket",
        "\u2500" * 66,
        "↩️  Back",
    ]


# -- Bucket stats: Total / Public / Private counts ----------------------------------


@mock_aws
def test_bucket_stats_shows_public_and_private_counts() -> None:
    ctx = _app_ctx()
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, err_console=err_console)
    gateway = ctx.client_factory.s3()

    gateway.create_bucket(Bucket="private-one")
    gateway.put_public_access_block(
        Bucket="private-one",
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    gateway.create_bucket(Bucket="public-one")
    gateway.put_public_access_block(
        Bucket="public-one",
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": False,
            "IgnorePublicAcls": False,
            "BlockPublicPolicy": False,
            "RestrictPublicBuckets": False,
        },
    )
    gateway.put_bucket_policy(
        Bucket="public-one",
        Policy=(
            '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
            '"Principal": "*", "Action": "s3:GetObject", '
            '"Resource": "arn:aws:s3:::public-one/*"}]}'
        ),
    )

    action = S3Flow(ctx, FakePrompter([NAV_EXIT])).menu()

    assert action is NavAction.EXIT
    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "Buckets: 2 Total  |  1 Public 🔓  |  1 Private 🔒" in output


@mock_aws
def test_bucket_stats_shows_zero_public_when_every_bucket_is_private() -> None:
    """A bucket only counts as Private once Block Public Access is fully locked
    down -- not merely because it was created.
    """
    ctx = _app_ctx()
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, err_console=err_console)
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="quiet-bucket")
    gateway.put_public_access_block(
        Bucket="quiet-bucket",
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )

    action = S3Flow(ctx, FakePrompter([NAV_EXIT])).menu()

    assert action is NavAction.EXIT
    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "Buckets: 1 Total  |  0 Public 🔓  |  1 Private 🔒" in output


@mock_aws
def test_bucket_stats_shows_public_with_no_public_access_block_configuration_at_all() -> None:
    """No ``PublicAccessBlockConfiguration`` at all (``NoSuchPublicAccessBlockConfiguration``)
    -- e.g. a bucket created with ``--allow-public`` -- counts as Public immediately,
    with no separate bucket-policy grant required.
    """
    ctx = _app_ctx()
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, err_console=err_console)
    ctx.client_factory.s3().create_bucket(Bucket="never-configured")

    action = S3Flow(ctx, FakePrompter([NAV_EXIT])).menu()

    assert action is NavAction.EXIT
    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "Buckets: 1 Total  |  1 Public 🔓  |  0 Private 🔒" in output


@mock_aws
def test_menu_cancelled_at_top_level_exits() -> None:
    ctx = _app_ctx()
    prompter = FakePrompter([None])

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.EXIT


@mock_aws
def test_menu_back_returns_back() -> None:
    ctx = _app_ctx()
    prompter = FakePrompter([NAV_BACK])

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.BACK


@mock_aws
def test_menu_exit_choice_returns_exit() -> None:
    ctx = _app_ctx()
    prompter = FakePrompter([NAV_EXIT])

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.EXIT


# -- Listar (Resource Explorer): empty state, cancellation, and search --------------
#
# No mode picker anymore: selecting "Search" asks ONE free-text query directly
# (blank = everyone), matched against the bucket name. There is no more search
# by tag -- that capability was deliberately removed in favor of a single
# unified query box (see s3_flow.py's ``_query_buckets``).


@mock_aws
def test_list_buckets_when_none_exist_shows_message_and_pauses() -> None:
    ctx = _app_ctx()
    # "search" reaches the empty-population message inside the search flow; the
    # service screen offers Search/Create/Delete/Back regardless of Total.
    prompter = FakePrompter(["search"])

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert prompter.asked == [
        "S3 (Buckets) -- What do you want to do?"
    ]




@mock_aws
def test_search_cancelled_at_query_prompt_short_circuits() -> None:
    ctx = _app_ctx()
    ctx.client_factory.s3().create_bucket(Bucket="some-bucket")

    prompter = FakePrompter(["search", None])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert len(prompter.asked) == 2


@mock_aws
def test_search_with_no_match_shows_message_without_offering_a_picker() -> None:
    ctx = _app_ctx()
    ctx.client_factory.s3().create_bucket(Bucket="alpha-bucket")

    prompter = FakePrompter(["search", "zzz-does-not-exist"])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    # No select() for a match list was ever reached -- just the service menu and the query.
    assert len(prompter.asked) == 2


@mock_aws
def test_search_with_a_single_match_still_requires_an_explicit_pick() -> None:
    """A single search hit is never auto-selected -- it's still offered as a pick list."""
    ctx = _app_ctx()
    ctx.client_factory.s3().create_bucket(Bucket="only-match-bucket")

    prompter = FakePrompter(
        [
            "search",
            "only-match",
            "only-match-bucket",  # the picker, even though there's exactly one entry
            NAV_BACK,  # back out of the bucket detail loop
        ]
    )
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    # menu, query, picker, detail-screen action -- the picker step was mandatory.
    assert len(prompter.asked) == 4


@mock_aws
def test_search_with_blank_query_lists_everyone() -> None:
    ctx = _app_ctx()
    ctx.client_factory.s3().create_bucket(Bucket="alpha-bucket")

    prompter = FakePrompter(["search", "", "alpha-bucket", NAV_BACK])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY


@mock_aws
def test_bucket_detail_entry_redraws_the_banner() -> None:
    ctx = _app_ctx()
    ctx.client_factory.s3().create_bucket(Bucket="alpha-bucket")
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, err_console=err_console)

    prompter = FakePrompter(["search", "", "alpha-bucket", NAV_BACK])
    S3Flow(ctx, prompter).menu()

    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "AWS CLOUD ADMIN CLI v1.0" in output


# -- Bucket detail screen: read-only object inspection -------------------------------


@mock_aws
def test_object_list_when_bucket_empty_shows_the_empty_warning() -> None:
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console, err_console=err_console)
    ctx.client_factory.s3().create_bucket(Bucket="empty-objects-bucket")

    prompter = FakePrompter(
        ["search", "", "empty-objects-bucket", "detail_list_objects", NAV_BACK],
    )
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "Bucket 'empty-objects-bucket' is empty (0 objects found)." in output
    # Read-only: no file-modification menu is ever offered.
    assert not any("Upload" in asked or "Download" in asked for asked in prompter.asked)


@mock_aws
def test_object_list_renders_key_size_storage_class_and_last_modified() -> None:
    """"List Objects" is a straight read-only table -- no Upload/Download/Delete
    options are ever offered, and no scripted response is needed for any of them.
    """
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="inspect-bucket")
    gateway.put_object(Bucket="inspect-bucket", Key="report.csv", Body=b"a,b,c\n1,2,3\n")

    prompter = FakePrompter(["search", "", "inspect-bucket", "detail_list_objects", NAV_BACK])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "report.csv" in output
    assert "STANDARD" in output
    assert "Key / Object Name" in output
    assert "Storage Class" in output
    assert "Last Modified" in output
    # Read-only: no file-modification menu is ever offered.
    assert not any("Upload" in asked or "Download" in asked for asked in prompter.asked)


# -- Crear -------------------------------------------------------------------------


@mock_aws
def test_create_bucket_asks_public_access_and_defaults_to_blocked() -> None:
    ctx = _app_ctx()
    prompter = FakePrompter(
        [
            "create_bucket",
            "new-bucket",
            "block_public_access",
            "tags_no",  # "No, skip tags"
            "iam_bind_no",  # skip IAM binding
        ]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    buckets = ctx.client_factory.s3().list_buckets()["Buckets"]
    assert any(b["Name"] == "new-bucket" for b in buckets)


@mock_aws
def test_create_bucket_tags_back_returns_to_public_access_preserving_name() -> None:
    """"↩️ Back" on the "Add Resource Tags to Bucket?" gate must re-show Block Public
    Access -- not abort the wizard -- and the bucket name entered earlier must survive
    the round trip untouched.
    """
    ctx = _app_ctx()
    prompter = FakePrompter(
        [
            "create_bucket",
            "back-to-public-access-bucket",
            "block_public_access",  # Block Public Access (first pass)
            NAV_BACK,  # Tags gate -> Back -> must land on Block Public Access again
            "allow_public_access",  # Block Public Access (re-shown): change of mind
            "tags_no",
            "iam_bind_no",
        ]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    buckets = ctx.client_factory.s3().list_buckets()["Buckets"]
    assert any(b["Name"] == "back-to-public-access-bucket" for b in buckets)
    # The re-shown answer ("Allow Public Access") must be the one that actually took
    # effect -- proving the Back landed on Public Access and not somewhere else.
    # ``put_public_access_block`` is called explicitly either way, so the bucket has
    # a real configuration with every flag off, never a missing one.
    config = ctx.client_factory.s3().get_public_access_block(
        Bucket="back-to-public-access-bucket"
    )["PublicAccessBlockConfiguration"]
    assert not any(config.values())


@mock_aws
def test_create_bucket_iam_bind_back_returns_to_tags_gate_preserving_name() -> None:
    """"↩️ Back" on the "Attach an S3 access policy..." gate must re-show the Tags
    gate -- not abort the wizard -- and the bucket name/public-access choice entered
    earlier must survive the round trip untouched.
    """
    ctx = _app_ctx()
    prompter = FakePrompter(
        [
            "create_bucket",
            "back-to-tags-gate-bucket",
            "block_public_access",
            "tags_no",  # Tags gate (first pass): skip tags
            NAV_BACK,  # IAM Bind gate -> Back -> must land on Tags gate again
            "tags_yes",  # Tags gate (re-shown): change of mind, add a tag this time
            "Environment=dev",
            "",  # blank to finish
            "iam_bind_no",
        ]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    buckets = ctx.client_factory.s3().list_buckets()["Buckets"]
    assert any(b["Name"] == "back-to-tags-gate-bucket" for b in buckets)
    # The re-shown answer ("add a tag") must be the one that actually took effect --
    # proving the Back landed on the Tags gate and not somewhere else (e.g. a full
    # abort, which would leave the bucket never created at all).
    tags = ctx.client_factory.s3().get_bucket_tagging(Bucket="back-to-tags-gate-bucket")[
        "TagSet"
    ]
    assert {"Key": "Environment", "Value": "dev"} in tags


@mock_aws
def test_create_bucket_tags_gate_back_can_be_cancelled_via_ctrl_c_at_public_access() -> None:
    """A genuine cancel (Ctrl+C/Esc -> ``None``) after landing back on Public Access
    aborts the whole wizard, same contract as every other step's own Ctrl+C.
    """
    ctx = _app_ctx()
    prompter = FakePrompter(
        [
            "create_bucket",
            "cancelled-after-tags-back",
            "block_public_access",
            NAV_BACK,  # Tags gate -> Back -> Public Access (re-shown)
            None,  # Ctrl+C at the re-shown Public Access select -> full abort
        ]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    buckets = ctx.client_factory.s3().list_buckets()["Buckets"]
    assert not any(b["Name"] == "cancelled-after-tags-back" for b in buckets)


@mock_aws
def test_create_bucket_tags_yes_invalid_format_reprompts_without_losing_state() -> None:
    """A tag missing '=' shows a clear error and re-asks for that SAME tag -- it never
    aborts the wizard nor silently drops back to Block Public Access.
    """
    ctx = _app_ctx()
    prompter = FakePrompter(
        [
            "create_bucket",
            "invalid-tag-retry-bucket",
            "block_public_access",
            "tags_yes",
            "NoEqualsSignHere",  # invalid -- missing '='
            "Environment=dev",  # retried -- now valid
            "",  # blank to finish
            "iam_bind_no",
        ]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    gateway = ctx.client_factory.s3()
    tags = gateway.get_bucket_tagging(Bucket="invalid-tag-retry-bucket")["TagSet"]
    assert {"Key": "Environment", "Value": "dev"} in tags
    assert len(tags) == 1  # the invalid entry never made it into the tag set


@mock_aws
def test_create_bucket_inherits_the_global_region_with_no_region_prompt() -> None:
    """The bucket must be created in ``ctx.settings.region`` -- no region prompt is
    ever asked, and no scripted response is left unconsumed for one.
    """
    ctx = AppContext.build(Settings(profile="testprofile", region="us-west-2"))
    prompter = FakePrompter(
        [
            "create_bucket",
            "inherited-region-bucket",
            "block_public_access",
            "tags_no",
            "iam_bind_no",
        ]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    location = ctx.client_factory.s3().get_bucket_location(Bucket="inherited-region-bucket")
    assert location["LocationConstraint"] == "us-west-2"


@mock_aws
def test_create_bucket_summary_shows_concise_region_display() -> None:
    """The Bucket Creation Summary shows ``<region_code> (<location_name>)`` -- not
    the old verbose "(Inherited from Global Context)" string.
    """
    ctx = AppContext.build(Settings(profile="testprofile", region="sa-east-1"))
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)
    prompter = FakePrompter(
        [
            "create_bucket",
            "concise-region-bucket",
            "block_public_access",
            "tags_no",
            "iam_bind_no",
        ]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "sa-east-1 (São Paulo)" in output
    assert "Inherited from Global Context" not in output


@mock_aws
def test_create_bucket_summary_shows_blocked_when_block_public_access_chosen() -> None:
    """Choosing "Block Public Access" must show "BLOCKED" in the Creation Summary --
    the wizard's own answer, not a re-fetched value.
    """
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)
    prompter = FakePrompter(
        ["create_bucket", "blocked-summary-bucket", "block_public_access", "tags_no", "iam_bind_no"]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "BLOCKED" in output
    assert "ALLOWED" not in output
    # `put_public_access_block` must actually have been called -- the summary answer
    # and the applied AWS state must agree when the user chose to block.
    config = ctx.client_factory.s3().get_public_access_block(Bucket="blocked-summary-bucket")[
        "PublicAccessBlockConfiguration"
    ]
    assert all(config.values())


@mock_aws
def test_create_bucket_summary_shows_allowed_when_public_access_allowed_chosen() -> None:
    """Choosing "Allow Public Access" must show "ALLOWED" in the Creation Summary --
    not "BLOCKED" -- and ``put_public_access_block`` must have been called EXPLICITLY
    with every flag off, never simply skipped.
    """
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)
    prompter = FakePrompter(
        ["create_bucket", "exposed-summary-bucket", "allow_public_access", "tags_no", "iam_bind_no"]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "ALLOWED" in output
    assert "BLOCKED" not in output
    # `put_public_access_block` must have been called explicitly, with every flag
    # off -- never simply skipped, which would leave no configuration at all.
    config = ctx.client_factory.s3().get_public_access_block(Bucket="exposed-summary-bucket")[
        "PublicAccessBlockConfiguration"
    ]
    assert not any(config.values())


@mock_aws
def test_create_bucket_prints_exactly_one_blank_line_before_the_summary() -> None:
    """Exactly one blank line separates the header dashboard box from the "Bucket
    Creation Summary" subtitle -- the banner prints via ``err_console`` (stderr) and
    the summary table via ``console`` (stdout), so both are pointed at the same
    buffer here to observe how they actually interleave on a real terminal.
    """
    ctx = _app_ctx()
    shared_file = io.StringIO()
    console = Console(file=shared_file, force_terminal=False, width=200)
    err_console = Console(file=shared_file, force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, console=console, err_console=err_console)
    prompter = FakePrompter(
        ["create_bucket", "spacing-bucket", "block_public_access", "tags_no", "iam_bind_no"]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = shared_file.getvalue()
    banner_end = output.rindex("╰")  # the header panel's bottom border
    after_banner = output[banner_end:].splitlines()[1:]
    assert after_banner[0].strip() == ""
    assert after_banner[1].strip() != ""
    assert "Bucket Creation Summary" in "\n".join(after_banner[1:5])


@mock_aws
def test_create_bucket_shows_a_warning_panel_when_public_access_is_allowed() -> None:
    """Choosing "Allow Public Access" shows a clean Rich callout -- not the backend
    use case's own raw, timestamped ``logger.warning()`` line (muted for this one
    call since the callout already conveys the same warning to the user).
    """
    ctx = _app_ctx()
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, err_console=err_console)
    prompter = FakePrompter(
        ["create_bucket", "exposed-warning-bucket", "allow_public_access", "tags_no", "iam_bind_no"]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "WARNING: Bucket 'exposed-warning-bucket' is being created WITHOUT" in output
    assert "Block Public Access" in output
    assert "may be exposed to the internet if a public bucket policy is applied" in output
    # The use case's own raw log line for this same condition must not also appear.
    assert "WARNING" not in output.replace(
        "WARNING: Bucket 'exposed-warning-bucket' is being created WITHOUT", ""
    )


@mock_aws
def test_create_bucket_shows_no_warning_panel_when_public_access_is_blocked() -> None:
    ctx = _app_ctx()
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, err_console=err_console)
    prompter = FakePrompter(
        [
            "create_bucket",
            "blocked-no-warning-bucket",
            "block_public_access",
            "tags_no",
            "iam_bind_no",
        ]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "WARNING" not in output


@mock_aws
def test_create_bucket_public_access_back_reprompts_bucket_name() -> None:
    """"↩️ Back" on the "Block Public Access?" gate (the wizard's first step) re-asks
    for the bucket name -- the prompt immediately before it -- rather than aborting.
    """
    ctx = _app_ctx()
    prompter = FakePrompter(
        [
            "create_bucket",
            "first-name-attempt",
            NAV_BACK,  # Public Access gate -> Back -> must land on the name prompt again
            "second-name-attempt",
            "block_public_access",
            "tags_no",
            "iam_bind_no",
        ]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    buckets = ctx.client_factory.s3().list_buckets()["Buckets"]
    assert not any(b["Name"] == "first-name-attempt" for b in buckets)
    assert any(b["Name"] == "second-name-attempt" for b in buckets)


@mock_aws
def test_create_bucket_public_access_gate_offers_only_back_no_cancel_choice() -> None:
    """The "Block Public Access?" gate offers only "↩️ Back" for navigation/
    cancellation -- the "🚫 Cancel creation" choice available on the later Tags and
    IAM Bind gates is deliberately absent here (Ctrl+C/Esc is still how a genuine
    abort happens at this step).
    """
    captured: dict[str, object] = {}

    class _SpyPrompter(FakePrompter):
        def select(  # type: ignore[override]
            self, message, choices, *, default=None, use_search=False, spacing=True
        ):
            if message == "Block Public Access?":
                captured["choices"] = choices
            return super().select(
                message, choices, default=default, use_search=use_search, spacing=spacing
            )

    ctx = _app_ctx()
    prompter = _SpyPrompter(
        [
            "create_bucket",
            "no-cancel-choice-bucket",
            "block_public_access",
            "tags_no",
            "iam_bind_no",
        ]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    labels = [c.title for c in captured["choices"] if isinstance(c, Choice)]  # type: ignore[union-attr]
    assert "↩️  Back" in labels
    assert not any("Cancel creation" in label for label in labels)


@mock_aws
def test_create_bucket_cancel_at_tags_aborts_to_main_menu() -> None:
    """"🚫 Cancel creation" at the Tags gate aborts the whole wizard -- distinct from
    "↩️ Back", which would only re-show the Public Access gate.
    """
    ctx = _app_ctx()
    prompter = FakePrompter(
        [
            "create_bucket",
            "cancel-at-tags",
            "block_public_access",
            "cancel_creation",
        ]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    buckets = ctx.client_factory.s3().list_buckets()["Buckets"]
    assert not any(b["Name"] == "cancel-at-tags" for b in buckets)


@mock_aws
def test_create_bucket_cancel_at_iam_bind_aborts_to_main_menu() -> None:
    """"🚫 Cancel creation" at the IAM Bind gate aborts the whole wizard -- distinct
    from "↩️ Back", which would only re-show the Tags gate.
    """
    ctx = _app_ctx()
    prompter = FakePrompter(
        [
            "create_bucket",
            "cancel-at-iam-bind",
            "block_public_access",
            "tags_no",
            "cancel_creation",
        ]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    buckets = ctx.client_factory.s3().list_buckets()["Buckets"]
    assert not any(b["Name"] == "cancel-at-iam-bind" for b in buckets)


# -- Borrar: arrow-key destructive confirmation --------------------------------------


@mock_aws
def test_delete_empty_bucket_full_confirmation_deletes() -> None:
    ctx = _app_ctx()
    ctx.client_factory.s3().create_bucket(Bucket="delete-me")

    prompter = FakePrompter(["delete_bucket", "", "delete-me", "yes"])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    remaining = ctx.client_factory.s3().list_buckets()["Buckets"]
    assert not any(b["Name"] == "delete-me" for b in remaining)


@mock_aws
def test_delete_bucket_selecting_no_keeps_bucket() -> None:
    ctx = _app_ctx()
    ctx.client_factory.s3().create_bucket(Bucket="keep-me")

    prompter = FakePrompter(["delete_bucket", "", "keep-me", "no"])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    remaining = ctx.client_factory.s3().list_buckets()["Buckets"]
    assert any(b["Name"] == "keep-me" for b in remaining)


@mock_aws
def test_delete_bucket_selecting_cancel_keeps_bucket() -> None:
    ctx = _app_ctx()
    ctx.client_factory.s3().create_bucket(Bucket="keep-me-typo")

    prompter = FakePrompter(["delete_bucket", "", "keep-me-typo", NAV_BACK])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    remaining = ctx.client_factory.s3().list_buckets()["Buckets"]
    assert any(b["Name"] == "keep-me-typo" for b in remaining)


@mock_aws
def test_delete_bucket_not_empty_offers_to_empty_it_first() -> None:
    ctx = _app_ctx()
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="not-empty-bucket")
    gateway.put_object(Bucket="not-empty-bucket", Key="a.txt", Body=b"hi")

    prompter = FakePrompter(
        [
            "delete_bucket",
            "",
            "not-empty-bucket",
            "yes",  # arrow-key confirm: empty it and continue?
            "yes",  # arrow-key delete confirmation
        ]
    )
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    remaining = gateway.list_buckets()["Buckets"]
    assert not any(b["Name"] == "not-empty-bucket" for b in remaining)


@mock_aws
def test_delete_bucket_not_empty_declining_empty_leaves_bucket_untouched() -> None:
    ctx = _app_ctx()
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="keep-not-empty")
    gateway.put_object(Bucket="keep-not-empty", Key="a.txt", Body=b"hi")

    prompter = FakePrompter(["delete_bucket", "", "keep-not-empty", "no"])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    remaining = gateway.list_buckets()["Buckets"]
    assert any(b["Name"] == "keep-not-empty" for b in remaining)


@mock_aws
def test_delete_bucket_cancelled_at_picker_never_reaches_confirm() -> None:
    ctx = _app_ctx()
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="untouched-bucket")

    prompter = FakePrompter(["delete_bucket", "", NAV_BACK])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    remaining = gateway.list_buckets()["Buckets"]
    assert any(b["Name"] == "untouched-bucket" for b in remaining)


# -- Tag-based search --------------------------------------------------------------


@mock_aws
def test_filter_buckets_matches_exact_tag_key_value() -> None:
    ctx = _app_ctx()
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="prod-bucket")
    gateway.create_bucket(Bucket="dev-bucket")
    gateway.put_bucket_tagging(
        Bucket="prod-bucket", Tagging={"TagSet": [{"Key": "Environment", "Value": "prod"}]}
    )
    gateway.put_bucket_tagging(
        Bucket="dev-bucket", Tagging={"TagSet": [{"Key": "Environment", "Value": "dev"}]}
    )
    buckets = build_s3_use_cases(ctx).list_buckets.execute()

    matches = S3Flow(ctx, FakePrompter())._filter_buckets(buckets, "Environment=prod")

    assert [b.name for b in matches] == ["prod-bucket"]


@mock_aws
def test_filter_buckets_matches_tag_value_substring() -> None:
    ctx = _app_ctx()
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="alpha-bucket")
    gateway.create_bucket(Bucket="beta-bucket")
    gateway.put_bucket_tagging(
        Bucket="alpha-bucket", Tagging={"TagSet": [{"Key": "Owner", "Value": "team-rocket"}]}
    )
    buckets = build_s3_use_cases(ctx).list_buckets.execute()

    matches = S3Flow(ctx, FakePrompter())._filter_buckets(buckets, "rocket")

    assert [b.name for b in matches] == ["alpha-bucket"]


@mock_aws
def test_search_by_tag_end_to_end_reaches_bucket_detail() -> None:
    ctx = _app_ctx()
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="tagged-bucket")
    gateway.put_bucket_tagging(
        Bucket="tagged-bucket", Tagging={"TagSet": [{"Key": "Environment", "Value": "prod"}]}
    )

    prompter = FakePrompter(["search", "Environment=prod", "tagged-bucket", NAV_BACK])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY


# -- Hardened creation: tags + IAM binding -------------------------------------------


@mock_aws
def test_create_bucket_with_tags_and_iam_binding_end_to_end() -> None:
    ctx = _app_ctx()
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(
        [
            "create_bucket",
            "governed-bucket",
            "block_public_access",
            "tags_yes",  # "Yes, add resource tags"
            "Environment=prod",  # first tag
            "",  # blank to finish
            "iam_bind_yes",
            "",  # filter IAM users: blank = show all
            "alice",  # pick user
        ]
    )
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    gateway = ctx.client_factory.s3()
    tags = gateway.get_bucket_tagging(Bucket="governed-bucket")["TagSet"]
    assert {"Key": "Environment", "Value": "prod"} in tags
    # Assigning an IAM user during bucket creation must also tag the bucket with
    # that Owner -- and must never clobber the "Environment" tag entered earlier.
    assert {"Key": "Owner", "Value": "alice"} in tags

    encryption = gateway.get_bucket_encryption(Bucket="governed-bucket")
    algorithm = encryption["ServerSideEncryptionConfiguration"]["Rules"][0][
        "ApplyServerSideEncryptionByDefault"
    ]["SSEAlgorithm"]
    assert algorithm == "AES256"

    attached = ctx.client_factory.iam().list_attached_user_policies(UserName="alice")[
        "AttachedPolicies"
    ]
    assert any(p["PolicyName"] == "s3-access-governed-bucket" for p in attached)

    # Policy binding is pure AWS API -- no local JSON audit file is ever written.
    assert not (Path.cwd() / "keys" / "iam" / "policies").exists()


@mock_aws
def test_create_bucket_declining_iam_binding_leaves_bucket_unassigned() -> None:
    ctx = _app_ctx()
    prompter = FakePrompter(
        ["create_bucket", "solo-bucket", "block_public_access", "tags_no", "iam_bind_no"]
    )

    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    audit_dir = Path.cwd() / "keys" / "iam" / "policies"
    assert not audit_dir.exists()


# -- Owner tag / bucket detail table --------------------------------------------------


@mock_aws
def test_bucket_detail_shows_dash_for_owner_when_unassigned() -> None:
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)
    ctx.client_factory.s3().create_bucket(Bucket="unassigned-bucket")

    prompter = FakePrompter(["search", "", "unassigned-bucket", NAV_BACK])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "Owner" in output
    assert "-" in output


@mock_aws
def test_bucket_detail_shows_the_iam_username_when_owner_tag_is_set() -> None:
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="owned-bucket")
    gateway.put_bucket_tagging(
        Bucket="owned-bucket", Tagging={"TagSet": [{"Key": "Owner", "Value": "Fernando_baza"}]}
    )

    prompter = FakePrompter(["search", "", "owned-bucket", NAV_BACK])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "Fernando_baza" in output


# -- Access column (Public/Private) ---------------------------------------------------


@mock_aws
def test_bucket_detail_access_column_shows_private_between_region_and_versioning() -> None:
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="private-bucket")
    gateway.put_public_access_block(
        Bucket="private-bucket",
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )

    prompter = FakePrompter(["search", "", "private-bucket", NAV_BACK])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "🔒 Private" in output
    assert output.index("Region") < output.index("Access") < output.index("Versioning")


@mock_aws
def test_bucket_detail_access_column_shows_public_for_a_publicly_policied_bucket() -> None:
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="exposed-bucket")
    gateway.put_public_access_block(
        Bucket="exposed-bucket",
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": False,
            "IgnorePublicAcls": False,
            "BlockPublicPolicy": False,
            "RestrictPublicBuckets": False,
        },
    )
    gateway.put_bucket_policy(
        Bucket="exposed-bucket",
        Policy=(
            '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
            '"Principal": "*", "Action": "s3:GetObject", '
            '"Resource": "arn:aws:s3:::exposed-bucket/*"}]}'
        ),
    )

    prompter = FakePrompter(["search", "", "exposed-bucket", NAV_BACK])
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "🔓 Public" in output


@mock_aws
def test_bucket_created_with_allow_public_is_immediately_counted_as_public() -> None:
    """A bucket created via the "Allow Public Access" wizard choice must show as
    Public right away -- in both the stats header and its own detail table --
    with no separate bucket-policy grant needed to make it count.
    """
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)
    ctx = replace(ctx, console=console, err_console=err_console)
    prompter = FakePrompter(
        ["create_bucket", "freshly-public-bucket", "allow_public_access", "tags_no", "iam_bind_no"]
    )

    action = S3Flow(ctx, prompter).menu()
    assert action is NavAction.STAY

    # A fresh top-level menu() call re-renders the stats line against current state.
    action = S3Flow(ctx, FakePrompter([NAV_EXIT])).menu()
    assert action is NavAction.EXIT
    stats_output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "Buckets: 1 Total  |  1 Public 🔓  |  0 Private 🔒" in stats_output

    console.file = io.StringIO()  # type: ignore[attr-defined]
    detail_prompter = FakePrompter(["search", "", "freshly-public-bucket", NAV_BACK])
    S3Flow(ctx, detail_prompter).menu()
    detail_output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "🔓 Public" in detail_output


# -- Toggle Public Access Block (bucket detail) --------------------------------------


@mock_aws
def test_toggle_public_access_from_private_removes_the_block_and_confirms_public() -> None:
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="toggle-to-public")
    gateway.put_public_access_block(
        Bucket="toggle-to-public",
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )

    prompter = FakePrompter(
        ["search", "", "toggle-to-public", "detail_toggle_public_access", NAV_BACK]
    )
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "[✓] Public Access Block removed. Bucket is now PUBLIC." in output
    config = gateway.get_public_access_block(Bucket="toggle-to-public")[
        "PublicAccessBlockConfiguration"
    ]
    assert not any(config.values())


@mock_aws
def test_toggle_public_access_from_public_enables_the_block_and_confirms_private() -> None:
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="toggle-to-private")

    prompter = FakePrompter(
        ["search", "", "toggle-to-private", "detail_toggle_public_access", NAV_BACK]
    )
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "[✓] Block Public Access enabled. Bucket is now PRIVATE." in output
    config = gateway.get_public_access_block(Bucket="toggle-to-private")[
        "PublicAccessBlockConfiguration"
    ]
    assert all(config.values())


# -- Manage IAM Access Policy (bucket detail) ----------------------------------------


@mock_aws
def test_manage_iam_policy_from_bucket_detail_attaches_scoped_policy() -> None:
    ctx = _app_ctx()
    ctx.client_factory.s3().create_bucket(Bucket="secure-bucket")
    ctx.client_factory.iam().create_user(UserName="bob")

    prompter = FakePrompter(
        [
            "search",
            "",
            "secure-bucket",
            "detail_manage_policy",
            "owner_attach",  # unassigned -> "Attach IAM user policy"
            "",  # filter IAM users: blank
            "bob",
            NAV_BACK,  # back out of the bucket detail loop
        ]
    )
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    attached = ctx.client_factory.iam().list_attached_user_policies(UserName="bob")[
        "AttachedPolicies"
    ]
    assert any(p["PolicyName"] == "s3-access-secure-bucket" for p in attached)
    # Policy binding is pure AWS API -- no local JSON audit file is ever written.
    assert not (Path.cwd() / "keys" / "iam" / "policies").exists()
    # Assigning an IAM user automatically tags the bucket with that Owner.
    tags = ctx.client_factory.s3().get_bucket_tagging(Bucket="secure-bucket")["TagSet"]
    assert {"Key": "Owner", "Value": "bob"} in tags


@mock_aws
def test_manage_iam_policy_reassign_reuses_the_policy_and_detaches_the_old_owner() -> None:
    """Reassigning ownership must reuse the existing policy (not duplicate it) and
    fully detach it from the previous owner -- this is the over-assignment guard.
    """
    ctx = _app_ctx()
    ctx.client_factory.s3().create_bucket(Bucket="shared-bucket")
    ctx.client_factory.iam().create_user(UserName="carol")
    ctx.client_factory.iam().create_user(UserName="dave")

    S3Flow(
        ctx,
        FakePrompter(
            [
                "search",
                "",
                "shared-bucket",
                "detail_manage_policy",
                "owner_attach",  # unassigned -> "Attach IAM user policy"
                "",
                "carol",
                NAV_BACK,
            ]
        ),
    ).menu()
    policies_after_first = ctx.client_factory.iam().list_policies(Scope="Local")["Policies"]
    assert len(policies_after_first) == 1
    attached_carol = ctx.client_factory.iam().list_attached_user_policies(UserName="carol")[
        "AttachedPolicies"
    ]
    assert any(p["PolicyName"] == "s3-access-shared-bucket" for p in attached_carol)

    S3Flow(
        ctx,
        FakePrompter(
            [
                "search",
                "",
                "shared-bucket",
                "detail_manage_policy",
                "owner_reassign",  # assigned to carol -> "Reassign to a different IAM user"
                "",
                "dave",
                NAV_BACK,
            ]
        ),
    ).menu()
    policies_after_second = ctx.client_factory.iam().list_policies(Scope="Local")["Policies"]
    assert len(policies_after_second) == 1  # reused, not duplicated

    attached_carol_after = ctx.client_factory.iam().list_attached_user_policies(UserName="carol")[
        "AttachedPolicies"
    ]
    assert not any(p["PolicyName"] == "s3-access-shared-bucket" for p in attached_carol_after)

    tags = ctx.client_factory.s3().get_bucket_tagging(Bucket="shared-bucket")["TagSet"]
    assert {"Key": "Owner", "Value": "dave"} in tags

    attached_dave = ctx.client_factory.iam().list_attached_user_policies(UserName="dave")[
        "AttachedPolicies"
    ]
    assert any(p["PolicyName"] == "s3-access-shared-bucket" for p in attached_dave)


@mock_aws
def test_manage_iam_policy_unassign_detaches_policy_and_clears_owner_tag() -> None:
    ctx = _app_ctx()
    ctx.client_factory.s3().create_bucket(Bucket="unassign-me-bucket")
    ctx.client_factory.iam().create_user(UserName="erin")

    S3Flow(
        ctx,
        FakePrompter(
            [
                "search",
                "",
                "unassign-me-bucket",
                "detail_manage_policy",
                "owner_attach",
                "",
                "erin",
                NAV_BACK,
            ]
        ),
    ).menu()

    S3Flow(
        ctx,
        FakePrompter(
            ["search", "", "unassign-me-bucket", "detail_manage_policy", "owner_unassign", NAV_BACK]
        ),
    ).menu()

    attached_erin = ctx.client_factory.iam().list_attached_user_policies(UserName="erin")[
        "AttachedPolicies"
    ]
    assert not any(p["PolicyName"] == "s3-access-unassign-me-bucket" for p in attached_erin)

    tags = build_s3_use_cases(ctx).get_bucket_tags.execute("unassign-me-bucket")
    assert "Owner" not in tags


# -- Edit Tags ------------------------------------------------------------------------


@mock_aws
def test_edit_tags_add_tag_applies_it() -> None:
    ctx = _app_ctx()
    ctx.client_factory.s3().create_bucket(Bucket="tag-bucket")

    prompter = FakePrompter(
        [
            "search",
            "",
            "tag-bucket",
            "detail_edit_tags",
            "tag_add",
            "Environment",
            "prod",
            NAV_BACK,
            NAV_BACK,
        ]
    )
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    tags = build_s3_use_cases(ctx).get_bucket_tags.execute("tag-bucket")
    assert tags == {"Environment": "prod"}


@mock_aws
def test_edit_tags_delete_tag_removes_it() -> None:
    ctx = _app_ctx()
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="tag-bucket")
    gateway.put_bucket_tagging(
        Bucket="tag-bucket", Tagging={"TagSet": [{"Key": "Environment", "Value": "prod"}]}
    )

    prompter = FakePrompter(
        [
            "search",
            "",
            "tag-bucket",
            "detail_edit_tags",
            "tag_delete",
            "Environment",
            "yes",
            NAV_BACK,
            NAV_BACK,
        ]
    )
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    tags = build_s3_use_cases(ctx).get_bucket_tags.execute("tag-bucket")
    assert tags == {}


# -- Empty Bucket ---------------------------------------------------------------------


@mock_aws
def test_empty_bucket_purges_objects_but_keeps_the_bucket() -> None:
    ctx = _app_ctx()
    gateway = ctx.client_factory.s3()
    gateway.create_bucket(Bucket="fillable-bucket")
    gateway.put_object(Bucket="fillable-bucket", Key="a.txt", Body=b"hi")

    prompter = FakePrompter(
        ["search", "", "fillable-bucket", "detail_empty_bucket", "yes", NAV_BACK]
    )
    action = S3Flow(ctx, prompter).menu()

    assert action is NavAction.STAY
    remaining = gateway.list_objects_v2(Bucket="fillable-bucket")
    assert remaining.get("KeyCount", 0) == 0
    assert any(b["Name"] == "fillable-bucket" for b in gateway.list_buckets()["Buckets"])


# -- Audit ------------------------------------------------------------------------


@mock_aws
def test_audit_lists_public_access_encryption_versioning_and_size() -> None:
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)
    gateway = ctx.client_factory.s3()
    # Created directly via boto3 (bypassing CreateBucketUseCase): no Block Public
    # Access, no default encryption -- exactly the "at risk" state the Audit
    # dashboard exists to surface.
    gateway.create_bucket(Bucket="raw-bucket")
    gateway.put_object(Bucket="raw-bucket", Key="a.txt", Body=b"1234")

    prompter = FakePrompter([])
    S3Flow(ctx, prompter)._run_audit()

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "raw-bucket" in output
    assert "PUBLIC ALERT" in output
    assert "NONE" in output
    assert "DISABLED" in output


@mock_aws
def test_audit_shows_blocked_and_encrypted_for_a_bucket_created_through_the_wizard() -> None:
    ctx = _app_ctx()
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    ctx = replace(ctx, console=console)

    create_prompter = FakePrompter(
        ["create_bucket", "governed-bucket", "block_public_access", "tags_no", "iam_bind_no"]
    )
    S3Flow(ctx, create_prompter).menu()

    audit_prompter = FakePrompter([])
    S3Flow(ctx, audit_prompter)._run_audit()

    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "governed-bucket" in output
    assert "BLOCKED" in output
    assert "SSE-S3" in output
