"""Structured logging setup: a Rich-backed handler wired to STDERR.

Logging must never touch STDOUT: that stream is reserved for command output
(tables or JSON), so ``configure_logging`` always renders through the
``stderr``-mode :class:`rich.console.Console` it is given.
"""

import logging

from rich.console import Console
from rich.logging import RichHandler

from aws_admin_cli.core.enums import LogLevel

_LOGGER_NAME = "aws_admin_cli"
_NOISY_THIRD_PARTY_LOGGERS = ("botocore", "boto3", "urllib3", "s3transfer")


def configure_logging(level: LogLevel, *, console: Console) -> logging.Logger:
    """Configure and return the ``aws_admin_cli`` application logger.

    Args:
        level: Verbosity for the ``aws_admin_cli`` namespace.
        console: A STDERR-mode Rich console to render log records through.

    Returns:
        The configured, namespaced application logger.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.handlers.clear()  # idempotent: safe to call more than once per process

    handler = RichHandler(
        console=console,
        rich_tracebacks=True,
        show_path=False,
        markup=True,
    )
    handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))
    logger.addHandler(handler)
    logger.setLevel(level.value)
    logger.propagate = False

    third_party_level = logging.DEBUG if level is LogLevel.DEBUG else logging.WARNING
    for name in _NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(third_party_level)

    return logger
