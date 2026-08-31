"""Typed exceptions mapped to CLI exit codes.

| Code | Meaning                                                        |
|------|----------------------------------------------------------------|
| 0    | Completed, nothing above the configured gate                   |
| 1    | Findings or violations above the gate                          |
| 2    | Configuration or usage error                                   |
| 3    | Infrastructure failure (Docker, image pull, budget exceeded)   |
| 4    | Interrupted                                                    |

CI systems distinguish these. Conflating them makes a broken runner look like a
security finding, which is how a tool loses a team's trust in one afternoon.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_CONFIG = 2
EXIT_INFRA = 3
EXIT_INTERRUPTED = 4


class RaceGuardError(Exception):
    """Base for everything the tool raises deliberately."""

    exit_code: int = EXIT_CONFIG

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def render(self) -> str:
        if self.hint:
            return f"{self.message}\n  hint: {self.hint}"
        return self.message


class ConfigError(RaceGuardError):
    """raceguard.yaml is missing, malformed, or internally inconsistent."""

    exit_code = EXIT_CONFIG


class UsageError(RaceGuardError):
    """The command line arguments do not make sense together."""

    exit_code = EXIT_CONFIG


class TargetConfigError(RaceGuardError):
    """The target itself is misconfigured: bad payload, wrong auth, no 2xx ever.

    Distinct from a verdict. Reporting "not reproduced" when no request ever
    reached the handler would be a lie, so this aborts the target instead.
    """

    exit_code = EXIT_CONFIG


class InvariantRejected(RaceGuardError):
    """A candidate invariant failed the four step validation gate."""

    exit_code = EXIT_CONFIG

    def __init__(self, message: str, *, step: int = 0, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.step = step


class InfrastructureError(RaceGuardError):
    """Docker unavailable, image pull failed, port in use, container died."""

    exit_code = EXIT_INFRA


class BudgetExceeded(InfrastructureError):
    """A watchdog fired: per trial, per target, per run, or request count."""


class GovernorRefusal(InfrastructureError):
    """The governor refused to fire: ownership check, advertised limit, backpressure."""


class Interrupted(RaceGuardError):
    exit_code = EXIT_INTERRUPTED
