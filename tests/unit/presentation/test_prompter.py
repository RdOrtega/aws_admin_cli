"""Tests for ``QuestionaryPrompter``: the real ``questionary``-backed adapter.

Every other TUI test drives a flow through ``FakePrompter``, which never
touches ``questionary`` at all -- exactly why this bug (EC2's "Search /
Filter Instances" crashing once LocalStack had more than 25 instances) was
invisible to the rest of the suite. ``use_search=True`` must disable
``use_jk_keys``, or questionary raises ``ValueError`` at construction time,
before ``.ask()`` is ever called.
"""

import pytest
import questionary
from aws_admin_cli.presentation.tui.menu import Choice
from aws_admin_cli.presentation.tui.prompter import _CHECKBOX_INSTRUCTION, QuestionaryPrompter
from prompt_toolkit.application import create_app_session
from prompt_toolkit.formatted_text import split_lines, to_formatted_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

# Raw keystrokes fed to the real widget below. Space toggles the entry under the
# cursor, the arrow keys move, Enter confirms whatever is ticked.
_SPACE = " "
_DOWN = "\x1b[B"
_ENTER = "\r"
_TOGGLE_ALL = "a"


def _drive_checkbox(choices: list[Choice], keys: str) -> list[str] | None:
    """Run the REAL ``QuestionaryPrompter.checkbox`` against a scripted keystroke stream.

    ``create_pipe_input`` + ``DummyOutput`` give ``prompt_toolkit`` a fake
    terminal, so the widget actually runs its own key bindings instead of
    being mocked away. This is the only way to assert that multi-selection
    still works: a monkeypatched ``questionary.checkbox`` would happily
    "pass" even if the real widget stopped toggling entirely.
    """
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            return QuestionaryPrompter().checkbox("Security groups:", choices)


def _sg_choices() -> list[Choice]:
    """The EC2 launch wizard's Step 5 shape: the VPC default plus the seeded demo SGs."""
    return [
        Choice(title=f"{name} ({group_id})", value=group_id)
        for name, group_id in (
            ("default", "sg-000"),
            ("web-sg", "sg-111"),
            ("db-sg", "sg-222"),
            ("ssh-only", "sg-333"),
        )
    ]


def test_questionary_itself_raises_on_search_filter_with_jk_keys_enabled() -> None:
    """Documents the exact upstream failure this adapter must never trigger."""
    with pytest.raises(ValueError, match="j/k keys"):
        questionary.select(
            "pick one",
            choices=[questionary.Choice(title="a", value="a")],
            use_search_filter=True,
            use_jk_keys=True,
        )

    # The actual fix: the same call, with j/k keys off, must not raise.
    questionary.select(
        "pick one",
        choices=[questionary.Choice(title="a", value="a")],
        use_search_filter=True,
        use_jk_keys=False,
    )


def test_select_with_use_search_disables_jk_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeQuestion:
        def ask(self) -> str | None:
            return "picked"

    def _fake_select(message: str, **kwargs: object) -> _FakeQuestion:
        captured.update(kwargs)
        return _FakeQuestion()

    monkeypatch.setattr(
        "aws_admin_cli.presentation.tui.prompter.questionary.select", _fake_select
    )

    result = QuestionaryPrompter().select(
        "pick one", [Choice(title="a", value="a")], use_search=True
    )

    assert result == "picked"
    assert captured["use_search_filter"] is True
    assert captured["use_jk_keys"] is False


def test_checkbox_forwards_the_checked_flag_per_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Choice.checked`` must reach ``questionary.Choice`` unchanged, per entry.

    This is what lets a flow re-show a checkbox with a previous selection
    already ticked (e.g. the EC2 launch wizard's Security Groups step,
    after the user picks "<- Back" and returns to it) instead of losing it.
    """
    captured: dict[str, object] = {}

    class _FakeQuestion:
        def ask(self) -> list[str] | None:
            return ["a"]

    def _fake_checkbox(message: str, **kwargs: object) -> _FakeQuestion:
        captured.update(kwargs)
        return _FakeQuestion()

    monkeypatch.setattr(
        "aws_admin_cli.presentation.tui.prompter.questionary.checkbox", _fake_checkbox
    )

    QuestionaryPrompter().checkbox(
        "pick some",
        [Choice(title="a", value="a", checked=True), Choice(title="b", value="b")],
    )

    choices = captured["choices"]
    assert isinstance(choices, list)
    assert choices[0].checked is True
    assert choices[1].checked is False


def test_checkbox_forwards_the_shared_multiline_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every checkbox prompt gets the same two-line instruction -- see ``_CHECKBOX_INSTRUCTION``."""
    captured: dict[str, object] = {}

    class _FakeQuestion:
        def ask(self) -> list[str] | None:
            return []

    def _fake_checkbox(message: str, **kwargs: object) -> _FakeQuestion:
        captured.update(kwargs)
        return _FakeQuestion()

    monkeypatch.setattr(
        "aws_admin_cli.presentation.tui.prompter.questionary.checkbox", _fake_checkbox
    )

    QuestionaryPrompter().checkbox("pick some", [Choice(title="a", value="a")])

    assert captured["instruction"] == _CHECKBOX_INSTRUCTION


def test_checkbox_instruction_renders_on_its_own_line_not_beside_a_long_question() -> None:
    """Proven the same way prompt_toolkit's own renderer consumes it: split the
    formatted-text token stream (question, then instruction) into screen lines. A
    same-line instruction (questionary's default) would collide horizontally with a
    long question like the IAM policy-attachment step's -- the leading ``\\n`` inside
    ``_CHECKBOX_INSTRUCTION`` is what prevents that.
    """
    tokens = [
        ("class:qmark", "?"),
        ("class:question", " Which policies do you want to attach? (optional) "),
        ("class:instruction", _CHECKBOX_INSTRUCTION),
    ]

    lines = [
        "".join(text for _style, text in line)
        for line in split_lines(to_formatted_text(tokens))
    ]

    assert len(lines) == 2
    assert lines[0] == "? Which policies do you want to attach? (optional) "
    assert lines[1] == "  (Use arrow keys to move, <space> to select, <a> to toggle, <i> to invert)"


def test_select_without_use_search_keeps_jk_keys_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common case (no search) must keep vi-style j/k navigation -- this
    fix must not regress ordinary short menus.
    """
    captured: dict[str, object] = {}

    class _FakeQuestion:
        def ask(self) -> str | None:
            return "picked"

    def _fake_select(message: str, **kwargs: object) -> _FakeQuestion:
        captured.update(kwargs)
        return _FakeQuestion()

    monkeypatch.setattr(
        "aws_admin_cli.presentation.tui.prompter.questionary.select", _fake_select
    )

    QuestionaryPrompter().select("pick one", [Choice(title="a", value="a")])

    assert captured["use_search_filter"] is False
    assert captured["use_jk_keys"] is True


# -- Multi-selection: the EC2 launch wizard's Security Groups step ------------------
#
# Every one of these drives the real questionary widget end to end. The rest of the
# suite reaches this step through ``FakePrompter``, which returns whatever list the
# test scripted -- so it can prove what the FLOW does with several security groups,
# but never that the WIDGET lets a human tick more than one in the first place.


def test_checkbox_returns_every_ticked_entry_not_just_the_last_one() -> None:
    """Three entries ticked with space must come back as three values, in list order."""
    result = _drive_checkbox(_sg_choices(), _SPACE + _DOWN + _SPACE + _DOWN + _SPACE + _ENTER)

    assert result == ["sg-000", "sg-111", "sg-222"]


def test_checkbox_can_tick_non_adjacent_entries() -> None:
    """Selection is not a contiguous range: skipping past an entry leaves it unticked."""
    result = _drive_checkbox(_sg_choices(), _DOWN + _SPACE + _DOWN + _DOWN + _SPACE + _ENTER)

    assert result == ["sg-111", "sg-333"]


def test_checkbox_toggle_all_selects_every_entry() -> None:
    """questionary's ``a`` binding ticks the whole list -- 4 of 4 security groups."""
    result = _drive_checkbox(_sg_choices(), _TOGGLE_ALL + _ENTER)

    assert result == ["sg-000", "sg-111", "sg-222", "sg-333"]


def test_checkbox_preserves_pre_checked_entries_and_adds_to_them() -> None:
    """``Choice.checked`` survives into the answer, so Back-then-return keeps the selection.

    Two entries arrive pre-ticked (what the wizard passes when the user
    returns to Step 5 via "<- Atrás"); ticking a third must yield all three,
    not replace the previous two.
    """
    choices = [
        Choice(title=f"sg ({gid})", value=gid, checked=gid in {"sg-111", "sg-222"})
        for gid in ("sg-000", "sg-111", "sg-222", "sg-333")
    ]

    result = _drive_checkbox(choices, _SPACE + _ENTER)

    assert result == ["sg-000", "sg-111", "sg-222"]


def test_checkbox_with_nothing_ticked_returns_empty_list_not_none() -> None:
    """Empty selection is a real answer (-> VPC default SG); ``None`` means cancelled.

    ``ec2_flow`` branches on exactly this distinction, so the two must never
    collapse into one another.
    """
    result = _drive_checkbox(_sg_choices(), _ENTER)

    assert result == []
    assert result is not None
