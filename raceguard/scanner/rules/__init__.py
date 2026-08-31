"""Pattern rules. Importing this package registers every rule."""

from . import (  # noqa: F401
    aggregate_limit,
    duplicate_insert,
    dynamic_sql,
    lost_update,
    oversell,
    single_use,
)
from .base import RuleContext, all_rules, register  # noqa: F401

__all__ = ["RuleContext", "all_rules", "register"]
