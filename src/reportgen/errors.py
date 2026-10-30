"""Typed, user-actionable errors.

Every error raised by reportgen for an *expected* failure (bad input,
missing file, invalid config, ...) derives from :class:`ReportGenError`.
The CLI catches these, prints a concise message, and exits non-zero
without a stack trace. Programming errors still raise normally.
"""

from __future__ import annotations


class ReportGenError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code = 1


class ConfigError(ReportGenError):
    """A report definition or configuration file is invalid."""

    exit_code = 2


class SourceError(ReportGenError):
    """A data source could not be read or is malformed."""

    exit_code = 3


class ProcessingError(ReportGenError):
    """A transformation step failed or referenced invalid data."""

    exit_code = 4


class ValidationError(ReportGenError):
    """Dataset validation failed (missing columns, bad types, empty data)."""

    exit_code = 4


class ChartError(ReportGenError):
    """A chart could not be rendered from the supplied data."""

    exit_code = 5


class ExportError(ReportGenError):
    """An export could not be written safely."""

    exit_code = 6


class PartialExportError(ExportError):
    """One format failed after earlier formats succeeded.

    Carries the files that *were* created so callers (CLI --json, job
    history) can report them for safe retries and cleanup instead of
    pretending nothing was produced."""

    def __init__(self, message: str, outputs: list) -> None:
        super().__init__(message)
        self.outputs = list(outputs)


class JobError(ReportGenError):
    """A saved job is missing, locked, or failed to run."""

    exit_code = 7


class SecurityError(ReportGenError):
    """An operation was refused for security reasons."""

    exit_code = 8
