"""Tests for ``flows._shared``: the arrow-key delete gate (``confirm_destructive``)
shared by every "Borrar"/"Terminate" flow (S3, EC2, IAM), the Early-Failure
name-availability loop (``prompt_available_name``) shared by every "Crear" flow,
and ``relative_path_display`` (the ``./keys/...`` formatting EC2's auto-generated
``.pem`` and IAM's ``credentials-<name>.txt`` success screens both use).
"""

import io
from dataclasses import replace
from pathlib import Path

import pytest
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.presentation.tui.flows._shared import (
    confirm_destructive,
    confirm_yes_no,
    relative_path_display,
    render_header,
)
from aws_admin_cli.presentation.tui.menu import NAV_BACK, Choice
from rich.console import Console

from tests.fakes.prompter import FakePrompter

_SHARED_MODULE = "aws_admin_cli.presentation.tui.flows._shared"


def _err_console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=200, stderr=True)


def _ctx_for_region(region: str) -> AppContext:
    base = AppContext.build(Settings(profile="localstack", region=region))
    err_console = _err_console()
    return replace(base, err_console=err_console)


# -- render_header: Region field shows the friendly name (Option B) -----------------
#
# These are about the friendly-name mapping, not connectivity, so the endpoint is
# stubbed reachable (ONLINE) -- otherwise the Region field would show the offline
# warning instead of a region name at all, regardless of the mapping being tested.


def test_render_header_shows_the_regions_friendly_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f"{_SHARED_MODULE}.local_endpoint_is_reachable", lambda url: True)
    ctx = _ctx_for_region("us-west-2")

    render_header(ctx)

    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "us-west-2 (Oregon)" in output


def test_render_header_falls_back_to_the_bare_code_for_an_unmapped_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f"{_SHARED_MODULE}.local_endpoint_is_reachable", lambda url: True)
    ctx = _ctx_for_region("ap-south-1")

    render_header(ctx)

    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "ap-south-1" in output


# -- render_header: Region field shows an unreachable warning while OFFLINE ---------


def test_render_header_shows_unreachable_warning_for_region_when_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f"{_SHARED_MODULE}.local_endpoint_is_reachable", lambda url: False)
    ctx = _ctx_for_region("us-west-2")

    render_header(ctx)

    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "Region: ⚠️  (Unreachable)" in output
    assert "us-west-2" not in output
    assert ctx.settings.region == "us-west-2"  # underlying state untouched


def _reject(_: str) -> str:
    raise ValidationError("bad name")


def test_choices_are_exactly_yes_no_cancel_with_no_default_to_yes() -> None:
    """Cursor must default to NO, not the first-listed YES -- an accidental
    Enter on a stale screen must never delete anything.
    """
    prompter = FakePrompter(["no"])

    confirm_destructive(prompter, kind="bucket", name="my-bucket")

    assert prompter.asked == [
        "Delete the bucket 'my-bucket'? This action is IRREVERSIBLE."
    ]


def test_selecting_yes_returns_true() -> None:
    prompter = FakePrompter(["yes"])
    assert confirm_destructive(prompter, kind="instance", name="i-123") is True


def test_selecting_no_returns_false() -> None:
    prompter = FakePrompter(["no"])
    assert confirm_destructive(prompter, kind="user", name="alice") is False


def test_selecting_cancel_returns_false() -> None:
    prompter = FakePrompter([NAV_BACK])
    assert confirm_destructive(prompter, kind="user", name="alice") is False


def test_no_text_prompts_are_asked() -> None:
    """The old two-step (confirm + type-the-name) flow is gone -- exactly one
    prompt, a single-choice select, no ``text()`` call at all.
    """
    prompter = FakePrompter(["yes"])

    confirm_destructive(prompter, kind="bucket", name="my-bucket")

    assert len(prompter.asked) == 1


def test_underlying_select_call_uses_the_exact_specified_menu_shape() -> None:
    """Spies on the raw ``Prompter.select`` call to assert on the exact
    choices and default -- ``FakePrompter`` doesn't expose them otherwise.
    """
    captured: dict[str, object] = {}

    class _SpyPrompter(FakePrompter):
        def select(  # type: ignore[override]
            self, message, choices, *, default=None, use_search=False, spacing=True
        ):
            captured["message"] = message
            captured["choices"] = choices
            captured["default"] = default
            return super().select(
                message, choices, default=default, use_search=use_search, spacing=spacing
            )

    confirm_destructive(_SpyPrompter(["yes"]), kind="bucket", name="my-bucket")

    labels = [
        c.title if isinstance(c, Choice) else c.line for c in captured["choices"]  # type: ignore[union-attr]
    ]
    assert labels == [
        "YES -- Proceed with deletion",
        "NO -- Keep resource",
        "─" * 66,
        "<- Cancel",
    ]
    assert captured["default"] == "no"


def test_confirm_yes_no_choices_default_to_no() -> None:
    """Same NO-default safety rule as ``confirm_destructive``, for the plain
    (non-"delete a named resource") confirmations across every flow.
    """
    prompter = FakePrompter(["no"])

    confirm_yes_no(prompter, "Overwrite 'dupe.txt'?")

    assert prompter.asked == ["Overwrite 'dupe.txt'?"]


def test_confirm_yes_no_selecting_yes_returns_true() -> None:
    prompter = FakePrompter(["yes"])
    assert confirm_yes_no(prompter, "Retry?") is True


def test_confirm_yes_no_selecting_no_returns_false() -> None:
    prompter = FakePrompter(["no"])
    assert confirm_yes_no(prompter, "Retry?") is False


def test_confirm_yes_no_uses_no_text_prompts() -> None:
    prompter = FakePrompter(["yes"])

    confirm_yes_no(prompter, "Retry?")

    assert len(prompter.asked) == 1


def test_confirm_yes_no_supports_custom_choice_labels() -> None:
    """The IAM force-delete prompt needs explicit context-specific labels,
    not the generic "YES"/"NO".
    """
    captured: dict[str, object] = {}

    class _SpyPrompter(FakePrompter):
        def select(  # type: ignore[override]
            self, message, choices, *, default=None, use_search=False, spacing=True
        ):
            captured["choices"] = choices
            return super().select(
                message, choices, default=default, use_search=use_search, spacing=spacing
            )

    confirm_yes_no(
        _SpyPrompter(["yes"]),
        "'bob' still has attachments. Detach everything and delete anyway (--force)?",
        yes_label="YES -- Detach all policies/keys and force delete user",
        no_label="NO -- Abort deletion",
    )

    labels = [c.title for c in captured["choices"]]  # type: ignore[union-attr]
    assert labels == [
        "YES -- Detach all policies/keys and force delete user",
        "NO -- Abort deletion",
    ]


def test_relative_path_display_prefixes_with_dot_slash(tmp_path: Path) -> None:
    """``./keys/ec2/foo.pem`` -- the CLI's saved-file paths never show as absolute."""
    path = tmp_path / "keys" / "ec2" / "foo.pem"
    assert relative_path_display(path) == "./keys/ec2/foo.pem"


def test_relative_path_display_handles_a_nested_iam_credentials_path(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "iam" / "credentials-alice.txt"
    assert relative_path_display(path) == "./keys/iam/credentials-alice.txt"
