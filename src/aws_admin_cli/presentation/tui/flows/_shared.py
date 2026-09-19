"""Small pieces of TUI-flow behavior shared across services (S3, EC2, IAM, ...).

Deliberately NOT a base class flows inherit from -- see ``flows/base.py``'s
own docstring for why ``Flow`` stays a bare Protocol. These are plain
functions a flow calls, nothing more.

``render_header``/``resolve_endpoint_status`` live here (not in ``app.py``,
where the header was originally written) because ``app.py`` already imports
every flow module (to build the main menu's ``_MAIN_MENU_FLOWS``) -- a flow
importing back from ``app.py`` would be a circular import. This module sits
below both, so both can depend on it. ``app.py``'s ``_MainMenu`` keeps its
own ``_render_banner``/``_resolve_status`` methods as thin delegates to
these, so existing callers/tests of that class are unaffected.
"""

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.exceptions import AwsAdminCliError
from aws_admin_cli.domain.constants import get_region_display_name
from aws_admin_cli.infrastructure.aws.connectivity import local_endpoint_is_reachable
from aws_admin_cli.presentation.cli.diagnostics_app import resolve_current_user
from aws_admin_cli.presentation.tui.menu import NAV_BACK, Choice, Separator
from aws_admin_cli.presentation.tui.navigation import clear_terminal
from aws_admin_cli.presentation.tui.prompter import Prompter

__all__ = [
    "announce_result",
    "clear_and_banner",
    "confirm_destructive",
    "confirm_yes_no",
    "prompt_available_name",
    "prompt_text_or_cancel",
    "relative_path_display",
    "render_header",
    "resolve_endpoint_status",
    "run_with_spinner",
]

_T = TypeVar("_T")

# The typed-word abort contract, shared by every free-text prompt in a create wizard
# (Username, Tag key/value, bucket name, ...): unlike Ctrl+C/Esc -- which every
# ``Prompter`` method already folds into a silent ``None``, per its own docstring --
# typing one of these words gets a VISIBLE notice, since the user typed something
# rather than reaching for an existing escape hatch.
_ABORT_WORDS = frozenset({"cancel", "back"})


def _print_cancelled(prompter: Prompter, err_console: Console) -> None:
    """The shared "typed cancel/back" notice.

    Red print, a pause so it's actually read, then a clear so the wizard's
    leftovers don't linger under the next menu.
    """
    err_console.print("[red]❌ Operation cancelled[/]")
    prompter.pause("Press ENTER to return to the menu...")
    if err_console.is_terminal:
        clear_terminal()

def resolve_endpoint_status(endpoint_url: str | None) -> tuple[str, str]:
    """The banner's (text, Rich style) pair for the Status field.

    A local endpoint gets an actual reachability probe (ONLINE/OFFLINE);
    a real-AWS target gets a fixed, uncolored "N/A" -- no cheap, universal
    reachability probe exists for it, and the loud "AWS REAL" warning
    already signals that state distinctly. See
    ``infrastructure.aws.aws_client.aws_connection_is_healthy`` for the real,
    cloud-agnostic (LocalStack AND real AWS) STS-based check -- deliberately
    NOT used here: this field is read on every header redraw, and a real
    signed AWS request on that hot path would make backing out of any menu
    against real AWS cost real network latency for no benefit real AWS users
    would want paid on every screen transition.
    """
    if endpoint_url is None:
        return ("N/A", "dim")
    if local_endpoint_is_reachable(endpoint_url):
        return ("ONLINE", "green")
    return ("OFFLINE", "red")


_STATUS_ICONS: dict[str, str] = {"ONLINE": "🟢", "OFFLINE": "🔴"}
_STATUS_FALLBACK_ICON = "⚪"  # N/A (real AWS, unchecked -- see resolve_endpoint_status)


def _status_icon(status_text: str) -> str:
    """The Status field's leading glyph: 🟢/🔴 for a determined state, ⚪ for N/A."""
    return _STATUS_ICONS.get(status_text, _STATUS_FALLBACK_ICON)


_HEADER_TITLE = "[bold white]AWS CLOUD ADMIN CLI v1.0[/]"
_HEADER_BORDER_STYLE = "dodger_blue1"


_HEADER_MAX_WIDTH = 84


def render_header(ctx: AppContext) -> None:
    """Print the "Deep Enterprise" Governance Grid header: a bordered 2x2 panel.

    Replaces the old fixed-width ``"=" * 70`` ASCII banner with a
    ``rich.panel.Panel`` (``box.ROUNDED``, ``dodger_blue1`` border) wrapped
    around a borderless ``Table.grid`` -- Target/Region on the first row,
    User/Status on the second. A free function so any screen -- the main
    menu, or a nested flow screen after a clear (e.g. IAM's user detail
    view) -- can redraw the same consistent chrome, not just the root menu.

    The panel itself is capped at ``_HEADER_MAX_WIDTH`` (falling back to the
    terminal's own width when the terminal is narrower) instead of the
    Rich-panel default of stretching to fill the console: on a wide monitor
    an ``expand=True`` panel drags the Target/Region/User/Status fields far
    apart from each other, which reads as noise rather than a compact status
    strip. Menu screens and wide table views (EC2 lists, S3 audits) both call
    this same function, so the header stays this compact size everywhere
    while a table printed right after it is free to use the full terminal
    width -- the two are independent ``console.print`` calls.

    Target is the dynamic LocalStack-vs-real-AWS distinction
    ``diagnostics_app.target_label`` already draws off ``settings.is_local``.
    Status stays dynamic (ONLINE/OFFLINE/N/A, from ``resolve_endpoint_status``)
    rather than a hardcoded "ONLINE": a governance header that always claims
    ONLINE regardless of actual reachability would defeat the one thing this
    field exists to warn about.
    """
    settings = ctx.settings
    if settings.is_local:
        target_icon, target_name = "\U0001f4bb", "LOCALSTACK (Local)"
    else:
        target_icon, target_name = "☁️", "AWS CLOUD (Live)"
    current_user = resolve_current_user(settings)
    status_text, status_style = resolve_endpoint_status(settings.endpoint_url)

    if status_text == "OFFLINE":
        # Region is unreachable to confirm during an outage -- show a warning
        # in its place instead of a region name that may be stale. Only the
        # header's rendering changes; ``settings.region`` itself is untouched.
        region_field = "[grey70]Region:[/] [bold cyan]⚠️  (Unreachable)[/]"
    else:
        region_name = get_region_display_name(settings.region)
        region_field = f"[grey70]Region:[/] [bold cyan]\U0001f30d {region_name}[/]"

    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_row(
        f"[grey70]Target:[/] [bold cyan]{target_icon} {target_name}[/]",
        region_field,
    )
    grid.add_row(
        f"[grey70]User:[/]   [bold cyan]\U0001f511 {current_user}[/]",
        f"[grey70]Status:[/] [bold {status_style}]{_status_icon(status_text)} {status_text}[/]",
    )

    # NOTE: no ``expand=False`` here -- with a ``Table.grid(expand=True, ...)``
    # child, Rich's Panel.expand=False shrinks to the grid's *minimum* column
    # width (its ratio columns report a small measured minimum), wrapping
    # "LOCALSTACK (Local)" onto its own line even though there's room. An
    # explicit ``width=`` already forces this exact size regardless of
    # ``expand``, so the grid's ratio columns split that fixed width evenly
    # instead of being measured down to a wrap.
    ctx.err_console.print(
        Panel(
            grid,
            title=_HEADER_TITLE,
            border_style=_HEADER_BORDER_STYLE,
            box=box.ROUNDED,
            width=min(ctx.err_console.width, _HEADER_MAX_WIDTH),
            padding=(0, 1),
        )
    )


def clear_and_banner(ctx: AppContext) -> None:
    """The one screen-transition primitive: wipe the terminal, then redraw the header.

    THE centralized "clear -> print header -> render UI" building block --
    every flow (S3, IAM, EC2, VPC, Stack) calls this instead of duplicating
    the ``if is_terminal: clear_terminal()`` + ``render_header(ctx)`` pair
    inline. Call it at the START of every distinct screen/step (a menu
    redraw, a detail view, or one step of a multi-step wizard) so a prior
    step's prompts and tables never visually stack up ("amontonamiento")
    underneath the next one -- the header is the only thing that's supposed
    to persist across a transition, everything else is a fresh page.

    A no-op clear under a non-terminal (``CliRunner``, a pipe, this test
    suite's ``FakePrompter`` runs): there's no real screen to wipe there,
    same guard ``render_header``'s own callers already use. The header
    itself still prints unconditionally -- only the actual terminal wipe is
    gated on ``is_terminal``.
    """
    if ctx.err_console.is_terminal:
        clear_terminal()
    render_header(ctx)


_CONFIRM_YES = "yes"
_CONFIRM_NO = "no"


def confirm_destructive(prompter: Prompter, *, kind: str, name: str) -> bool:
    """Arrow-key destructive confirmation -- no text prompts.

    Cursor defaults to NO (not the first-listed YES), so an accidental
    Enter on a stale screen never deletes anything. Used by every
    "Borrar"/"Terminate" flow (S3 buckets, EC2 instances, IAM users, ...).
    """
    selected = prompter.select(
        f"Delete the {kind} '{name}'? This action is IRREVERSIBLE.",
        [
            Choice(title="YES -- Proceed with deletion", value=_CONFIRM_YES),
            Choice(title="NO -- Keep resource", value=_CONFIRM_NO),
            Separator(),
            Choice(title="<- Cancel", value=NAV_BACK),
        ],
        default=_CONFIRM_NO,
    )
    return selected == _CONFIRM_YES


def confirm_yes_no(
    prompter: Prompter,
    message: str,
    *,
    yes_label: str = "YES",
    no_label: str = "NO",
    default: bool = False,
) -> bool:
    """Arrow-key Yes/No confirmation -- drop-in replacement for a raw ``(y/N)`` text prompt.

    Unlike ``confirm_destructive``, this has no fixed "Delete the X 'Y'?"
    wording or Cancel option -- it's for the plain "Overwrite?"/"Retry?"/
    "Apply this change?" confirmations scattered across every flow, so
    ``message``, and optionally the two choice labels, are fully caller
    supplied. Cursor still defaults to NO unless the caller opts in, and a
    cancelled prompt (``None``) is treated the same as NO.
    """
    selected = prompter.select(
        message,
        [
            Choice(title=yes_label, value=_CONFIRM_YES),
            Choice(title=no_label, value=_CONFIRM_NO),
        ],
        default=_CONFIRM_YES if default else _CONFIRM_NO,
    )
    return selected == _CONFIRM_YES


_RETRY = "retry"


def prompt_available_name(
    prompter: Prompter,
    err_console: Console,
    message: str,
    *,
    validate: Callable[[str], str],
    exists: Callable[[str], bool],
) -> str | None:
    """Ask for a resource name; validate and check availability immediately (Early Failure).

    Loops on an invalid or already-taken name: prints a red rejection line
    plus the arrow-key "Try another name" / "<- Back" menu below, so a bad
    name never lets a create wizard go on to ask Region/Access/etc. first --
    the exact bug this exists to prevent (a bucket/user name rejected only
    after the whole form was filled in).

    ``validate`` may both sanitize and validate (e.g. IAM's
    ``sanitize_user_name`` + ``validate_resource_name``) -- if it changes the
    input, a yellow notice is printed before the availability check runs, so
    sanitization is never silent even when it happens this early.

    Returns ``None`` if the user backs out entirely: blank input, cancelling
    a prompt, choosing "<- Back" from the retry menu, or typing "cancel"/"back"
    (see ``_ABORT_WORDS`` -- unlike the other three, this one prints a visible
    notice first, since the user actively typed a cancel word).
    """
    hinted_message = f"{message.rstrip(':')} (type 'cancel' to abort):"
    while True:
        raw = prompter.text(hinted_message)
        if not raw:
            return None
        if raw.strip().lower() in _ABORT_WORDS:
            _print_cancelled(prompter, err_console)
            return None
        try:
            candidate = validate(raw)
        except AwsAdminCliError as exc:
            err_console.print(f"[red]❌ Name '{raw}' is unavailable/invalid: {exc}[/]")
        else:
            if candidate != raw:
                err_console.print(
                    f"[yellow]Name sanitized: '{raw}' -> '{candidate}'.[/]"
                )
            try:
                taken = exists(candidate)
            except AwsAdminCliError as exc:
                err_console.print(
                    f"[red]❌ Name '{candidate}' is unavailable/invalid: {exc}[/]"
                )
            else:
                if not taken:
                    return candidate
                err_console.print(
                    f"[red]❌ Name '{candidate}' is unavailable/invalid: "
                    "already in use.[/]"
                )

        retry = prompter.select(
            "What do you want to do?",
            [
                Choice(title="Try another name", value=_RETRY),
                Separator(),
                Choice(title="↩️  Back", value=NAV_BACK),
            ],
        )
        if retry != _RETRY:
            return None


def relative_path_display(path: Path) -> str:
    """``./keys/ec2/foo.pem`` -- a saved-file path, as shown to the admin.

    Every "here's the local file this CLI just wrote for you" message (EC2's
    auto-generated ``.pem``, IAM's ``credentials-<name>.txt``) shows this
    instead of ``str(path)``'s absolute form: shorter, and it reads the same
    on any machine regardless of where the project checkout happens to live.
    Assumes ``path`` was built from ``Path.cwd()`` in the first place (both
    callers' own destination-dir helpers guarantee that), so ``relative_to``
    can't fail here.
    """
    return f"./{path.relative_to(Path.cwd())}"


def announce_result(ctx: AppContext, prompter: Prompter, *lines: str) -> None:
    """Print one or more already-styled result line(s), padded, then pause.

    One blank line before the first line and one after the last -- the
    shared shape every "created/deleted/updated/reset" confirmation follows.
    Centralized so a status line (e.g. "Termination requested for 'x'.") can
    never collide with a table or the previous prompt's answer above it, nor
    with the "Press Enter..." pause directly below it -- the exact collision
    bug this exists to prevent.
    """
    ctx.console.print()
    for line in lines:
        ctx.console.print(line)
    ctx.console.print()
    prompter.pause()


def run_with_spinner(err_console: Console, description: str, action: Callable[[], _T]) -> _T:
    """Run ``action`` under a spinner on ``err_console`` -- disabled without a real TTY.

    Same ``rich.progress`` spinner the CLI's ``ec2_app._run_with_spinner``
    already used (extracted here so both layers share one implementation);
    ``transient=True`` means the spinner line disappears once ``action``
    finishes, leaving no trace above whatever the flow prints next.
    """
    disable = not err_console.is_terminal
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=err_console,
        disable=disable,
        transient=True,
    ) as progress:
        progress.add_task(description, total=None)
        return action()


def prompt_text_or_cancel(
    prompter: Prompter,
    err_console: Console,
    message: str,
    *,
    default: str = "",
) -> str | None:
    """Ask for free text with the same typed "cancel"/"back" abort word.

    Same as ``prompt_available_name``, for a field that needs no name
    validation or availability check (e.g. a tag key/value). Ctrl+C/Esc keep
    the ordinary silent ``None`` contract; only the typed word gets the
    visible notice.
    """
    raw = prompter.text(f"{message.rstrip(':')} (type 'cancel' to abort):", default=default)
    if raw is None:
        return None
    if raw.strip().lower() in _ABORT_WORDS:
        _print_cancelled(prompter, err_console)
        return None
    return raw
