"""Shared enums used across configuration, CLI options, and formatters."""

from enum import Enum


class OutputFormat(str, Enum):
    """Rendering format for command output."""

    TABLE = "table"
    JSON = "json"


class LogLevel(str, Enum):
    """Logging verbosity level."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
