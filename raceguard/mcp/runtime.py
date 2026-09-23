"""What every tool, resource and endpoint shares: settings, the sandbox, the
budget, and the counters ``/metrics`` renders."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .budget import Budget
from .sandbox import Sandbox
from .settings import MCPSettings


@dataclass
class Metrics:
    """Prometheus text format without the dependency. Small enough to be honest."""

    tool_calls: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    verify_seconds_sum: float = 0.0
    verify_seconds_count: int = 0
    verify_buckets: dict[float, int] = field(
        default_factory=lambda: {b: 0 for b in (5.0, 15.0, 30.0, 60.0, 120.0, 300.0)}
    )
    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def observe_call(self, tool: str, outcome: str) -> None:
        with self._lock:
            self.tool_calls[(tool, outcome)] += 1

    def observe_verify(self, seconds: float) -> None:
        with self._lock:
            self.verify_seconds_sum += seconds
            self.verify_seconds_count += 1
            for bound in self.verify_buckets:
                if seconds <= bound:
                    self.verify_buckets[bound] += 1

    def render(self, *, in_flight: int, refusals: int) -> str:
        with self._lock:
            lines = [
                "# HELP raceguard_mcp_tool_calls_total Tool calls by name and outcome.",
                "# TYPE raceguard_mcp_tool_calls_total counter",
            ]
            for (tool, outcome), count in sorted(self.tool_calls.items()):
                lines.append(
                    f'raceguard_mcp_tool_calls_total{{tool="{tool}",outcome="{outcome}"}} {count}'
                )
            lines += [
                "# HELP raceguard_mcp_verify_seconds Wall clock per verify_handler call.",
                "# TYPE raceguard_mcp_verify_seconds histogram",
            ]
            for bound, count in self.verify_buckets.items():
                lines.append(f'raceguard_mcp_verify_seconds_bucket{{le="{bound:g}"}} {count}')
            lines.append(
                f'raceguard_mcp_verify_seconds_bucket{{le="+Inf"}} {self.verify_seconds_count}'
            )
            lines.append(f"raceguard_mcp_verify_seconds_sum {self.verify_seconds_sum:.3f}")
            lines.append(f"raceguard_mcp_verify_seconds_count {self.verify_seconds_count}")
            lines += [
                "# HELP raceguard_mcp_verify_in_flight Verifications running now.",
                "# TYPE raceguard_mcp_verify_in_flight gauge",
                f"raceguard_mcp_verify_in_flight {in_flight}",
                "# HELP raceguard_mcp_budget_refusals_total Verify calls refused by a budget.",
                "# TYPE raceguard_mcp_budget_refusals_total counter",
                f"raceguard_mcp_budget_refusals_total {refusals}",
                "# HELP raceguard_mcp_uptime_seconds Seconds since the server started.",
                "# TYPE raceguard_mcp_uptime_seconds gauge",
                f"raceguard_mcp_uptime_seconds {time.time() - self.started_at:.0f}",
            ]
        return "\n".join(lines) + "\n"


@dataclass
class Runtime:
    settings: MCPSettings
    sandbox: Sandbox
    budget: Budget
    metrics: Metrics = field(default_factory=Metrics)

    @classmethod
    def from_settings(cls, settings: MCPSettings) -> Runtime:
        enforce = not settings.allow_any_path or settings.transport == "http"
        return cls(
            settings=settings,
            sandbox=Sandbox(settings.project_root, enforce=enforce),
            budget=Budget(
                session_cap=settings.session_cap,
                target_cap_per_hour=settings.target_cap_per_hour,
                max_concurrent=settings.max_concurrent_verifies,
            ),
        )
