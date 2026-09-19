"""Enforces the TUI layer's own boundaries -- see ``docs/architecture.md``'s TUI section.

Three separate rules, each with its own reasoning:

1. ``domain``/``application`` never import ``questionary``/``prompt_toolkit``.
   Those two layers are the ONE part of this codebase every presentation
   layer (CLI, and now the TUI) shares -- see ``presentation/wiring.py``'s
   module docstring. If either started importing a terminal-UI library, that
   sharing breaks: a non-interactive caller (a script, the JSON-mode CLI,
   another presentation layer entirely) would now transitively depend on a
   library it never asked for.
2. ``presentation/tui/`` never imports ``boto3``/``botocore``. Same
   Separation-of-Duties spirit as ``test_vpc_readonly.py``: a TUI flow talks
   to AWS exclusively through ``presentation.wiring.build_*_use_cases(ctx)``
   -- never a raw client -- so it inherits the same guard rails, error
   mapping, and (once Fase 8 e.g. adds one) audit logging as the CLI, with
   zero duplication.
3. Every flow registered in ``flows/registry.py`` actually implements the
   ``Flow`` Protocol and has a non-empty ``title``; the registry lists
   exactly the five domain flows this Fase built, by class name (no fewer,
   no silently duplicated/forgotten one); and ``vpc_flow.py`` contains no
   network-mutation calls -- the exact same regex ``test_vpc_readonly.py``
   already enforces against the CLI/gateway, applied here to the TUI's own
   VPC screen, because "read-only" is a property of the ``vpc`` MODULE, not
   of one particular presentation layer.

If you're reading this because a check below just failed: the fix is almost
never to adjust this test -- it's to move whatever import/call you just
added back out of the layer that isn't supposed to have it.
"""

import re
from pathlib import Path

from aws_admin_cli.presentation.tui.flows.registry import FLOWS

_REPO_ROOT = Path(__file__).parents[3]
_SRC = _REPO_ROOT / "src" / "aws_admin_cli"

_TERMINAL_UI_IMPORT_RE = re.compile(
    r"^\s*(import|from)\s+(questionary|prompt_toolkit)\b", re.MULTILINE
)
_BOTO_IMPORT_RE = re.compile(r"^\s*(import|from)\s+(boto3|botocore)\b", re.MULTILINE)

# Same regex `test_vpc_readonly.py` uses against the CLI/gateway -- see that file's
# module docstring for the full Separation-of-Duties reasoning.
_FORBIDDEN_NETWORK_MUTATION_RE = re.compile(
    r"create_security_group|authorize_security_group|create_subnet|create_vpc|"
    r"modify_subnet|revoke_security_group"
)

_VPC_FLOW_PATH = _SRC / "presentation" / "tui" / "flows" / "vpc_flow.py"

# Checked by class name, not by `.title` -- the title is free-form UI copy (subject to
# wording tweaks in docs/README) and not what this test is actually verifying; the class
# name is the stable identifier of "which of the five domain flows is this".
_EXPECTED_FLOW_CLASS_NAMES = {"S3Flow", "IamFlow", "VpcFlow", "Ec2Flow", "StackFlow"}


def _py_files(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.py"))


def test_domain_never_imports_a_terminal_ui_library() -> None:
    offenders = {
        str(path.relative_to(_REPO_ROOT)): _TERMINAL_UI_IMPORT_RE.findall(
            path.read_text(encoding="utf-8")
        )
        for path in _py_files(_SRC / "domain")
    }
    offenders = {path: hits for path, hits in offenders.items() if hits}
    assert not offenders, f"domain/ importa una librería de terminal: {offenders}"


def test_application_never_imports_a_terminal_ui_library() -> None:
    offenders = {
        str(path.relative_to(_REPO_ROOT)): _TERMINAL_UI_IMPORT_RE.findall(
            path.read_text(encoding="utf-8")
        )
        for path in _py_files(_SRC / "application")
    }
    offenders = {path: hits for path, hits in offenders.items() if hits}
    assert not offenders, f"application/ importa una librería de terminal: {offenders}"


def test_presentation_tui_never_imports_boto() -> None:
    offenders = {
        str(path.relative_to(_REPO_ROOT)): _BOTO_IMPORT_RE.findall(
            path.read_text(encoding="utf-8")
        )
        for path in _py_files(_SRC / "presentation" / "tui")
    }
    offenders = {path: hits for path, hits in offenders.items() if hits}
    assert not offenders, (
        f"presentation/tui/ importa boto3/botocore directamente: {offenders}. "
        "Un flow debe hablar con AWS solo a través de "
        "presentation.wiring.build_*_use_cases(ctx)."
    )


def test_registry_lists_exactly_the_five_domain_flows() -> None:
    class_names = {flow_cls.__name__ for flow_cls in FLOWS}
    assert class_names == _EXPECTED_FLOW_CLASS_NAMES, (
        f"flows/registry.py.FLOWS tiene {class_names}, se esperaba "
        f"{_EXPECTED_FLOW_CLASS_NAMES}."
    )
    assert len(FLOWS) == len(set(FLOWS)), "flows/registry.py.FLOWS tiene una entrada duplicada."


def test_every_registered_flow_implements_the_flow_protocol_and_has_a_title() -> None:
    # `Flow` has a non-method member (`title`), so `issubclass(cls, Flow)` isn't usable
    # here -- typing itself refuses it ("Protocols with non-method members don't support
    # issubclass()"), even with @runtime_checkable. Checked structurally by hand instead.
    for flow_cls in FLOWS:
        assert isinstance(flow_cls.title, str) and flow_cls.title, (
            f"{flow_cls.__name__}.title debe ser un str no vacío."
        )
        assert callable(getattr(flow_cls, "menu", None)), (
            f"{flow_cls.__name__} no implementa el Protocol Flow (falta .menu())."
        )


def test_vpc_flow_source_has_no_forbidden_network_mutation_calls() -> None:
    source = _VPC_FLOW_PATH.read_text(encoding="utf-8")
    offenders = _FORBIDDEN_NETWORK_MUTATION_RE.findall(source)
    assert not offenders, (
        f"vpc_flow.py contiene llamada(s) de mutación de red prohibidas: {offenders}. "
        "El flow de VPC es exclusivamente de consulta -- ver "
        "docs/least-privilege.md, sección 'Separation of Duties'."
    )
