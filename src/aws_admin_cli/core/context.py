"""Runtime CLI context: the Composition Root.

``AppContext`` is the only place in the project that wires concrete
implementations together (``Boto3SessionFactory``, ``ClientFactory``, the Rich
consoles, the logger). Building one must never touch the network or resolve
credentials — it only wires objects together lazily, so it can run with no
network and no credentials at all.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

from rich.console import Console

from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.enums import OutputFormat
from aws_admin_cli.core.logging import configure_logging
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.models.stack import StackState
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.session_factory import Boto3SessionFactory
from aws_admin_cli.infrastructure.persistence.json_repository import JsonRepository
from aws_admin_cli.infrastructure.persistence.paths import resource_store_path, stack_store_path

if TYPE_CHECKING:
    from aws_admin_cli.domain.ports.repository import Repository


@dataclass(frozen=True, slots=True)
class AppContext:
    """Composition root: wires settings to concrete session/client/logging objects."""

    settings: Settings
    client_factory: ClientFactory
    console: Console
    err_console: Console
    logger: logging.Logger
    # Mutated in place (never reassigned) by the `resource_repository` property below --
    # the same pattern ClientFactory uses for its own client cache -- so this stays
    # compatible with `frozen=True`.
    _resource_repository_cache: dict[str, "Repository[ResourceRecord]"] = field(
        default_factory=dict, repr=False, compare=False
    )
    _stack_repository_cache: dict[str, "Repository[StackState]"] = field(
        default_factory=dict, repr=False, compare=False
    )

    @classmethod
    def build(cls: type[Self], settings: Settings) -> Self:
        """Wire a fresh ``AppContext`` for ``settings``.

        Performs no network I/O and resolves no credentials: it only constructs
        the console/logger/session-factory/client-factory objects a command
        needs to run, deferring any actual AWS contact to first use.

        Args:
            settings: Fully resolved configuration for this invocation.

        Returns:
            A ready-to-use ``AppContext``.
        """
        console = Console()
        err_console = Console(stderr=True)
        logger = configure_logging(settings.log_level, console=err_console)
        session_factory = Boto3SessionFactory(
            profile=settings.profile,
            region=settings.region,
            endpoint_url=settings.endpoint_url,
        )
        client_factory = ClientFactory(session_factory=session_factory, settings=settings)
        return cls(
            settings=settings,
            client_factory=client_factory,
            console=console,
            err_console=err_console,
            logger=logger,
        )

    @property
    def output(self: Self) -> OutputFormat:
        """Configured output format, delegated from ``settings``."""
        return self.settings.output

    def set_region(self: Self, region: str) -> None:
        """Switch this session's AWS region globally, for every client built from now on.

        ``Settings`` is normally immutable (``frozen=True``), but the TUI's
        Region Selector (``EnvironmentFlow``) needs one live session-wide
        switch rather than a brand new ``AppContext``, so this mutates
        ``settings.region`` in place via ``object.__setattr__`` -- the same
        escape hatch ``Settings._apply_local_profile_endpoint`` already uses
        on this frozen model. ``client_factory`` shares this exact ``Settings``
        instance, so every gateway that reads ``ctx.settings.region`` (or
        passes it as a client's ``region_name``) sees the new value
        immediately; ``clear_cache()`` on top of that ensures a client already
        built under the old region gets rebuilt instead of reused stale.
        """
        object.__setattr__(self.settings, "region", region)
        self.client_factory.clear_cache()

    @property
    def resource_repository(self: Self) -> "Repository[ResourceRecord]":
        """The local, per-profile ledger of resources this CLI has provisioned.

        Lazily constructed and cached on first access -- `build()` never reads
        or creates anything on disk, so a bare `--help`/`--version` invocation
        never touches the filesystem. The directory (and file) under
        `settings.data_dir` only appear the first time a command actually reads
        or writes a `ResourceRecord`.
        """
        cached = self._resource_repository_cache.get("default")
        if cached is not None:
            return cached
        repository = JsonRepository(resource_store_path(self.settings), ResourceRecord)
        self._resource_repository_cache["default"] = repository
        return repository

    @property
    def stack_repository(self: Self) -> "Repository[StackState]":
        """The local, per-profile ledger of stack apply state (Fase 6).

        Same lazy-construction rules as ``resource_repository`` -- a
        separate file (``stacks.json``), a separate cache, never touched by
        a command that isn't a ``stack`` one.
        """
        cached = self._stack_repository_cache.get("default")
        if cached is not None:
            return cached
        repository = JsonRepository(stack_store_path(self.settings), StackState)
        self._stack_repository_cache["default"] = repository
        return repository
