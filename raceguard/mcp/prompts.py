"""Prompt templates a host can surface as slash commands.

They exist so the asymmetry survives the handoff. An agent asked to "explain
this verdict" will happily turn "not reproduced in 20 trials" into "the
handler is safe" unless the prompt it is handed carries the bound with it.
"""

from __future__ import annotations

from .runtime import Runtime


def register(mcp, runtime: Runtime) -> None:
    @mcp.prompt()
    def triage_finding(finding_id: str) -> str:
        """Decide whether a finding is reachable under concurrent load and what the tightest seed is."""
        return (
            f"Call get_finding with finding_id={finding_id!r} and read the read, decision and "
            f"write sites it names. Then answer, in order:\n"
            f"1. Under what concurrent request pattern can a second request enter the gap "
            f"between the read and the write? Name the two requests and their payloads.\n"
            f"2. What is the TIGHTEST seed that exposes it: the smallest stock, balance, "
            f"count or row set where two successes are one too many?\n"
            f"3. Which invariant kind fits (non_negative, conservation, single_use, "
            f"duplicate_insert, aggregate_limit) and what SQL returns rows ONLY when it is "
            f"violated?\n"
            f"4. If a raceguard.yaml target already covers this route, name its id so "
            f"verify_handler can be called; otherwise write the target block.\n"
            f"Do not say the code is safe. A finding that does not reproduce is bounded "
            f"evidence under stated conditions, not a proof."
        )

    @mcp.prompt()
    def explain_verdict(target_id: str) -> str:
        """Explain a verification verdict without dropping the bound on what it proves."""
        return (
            f"Call verify_handler for target {target_id!r} if there is no recent verdict, "
            f"otherwise use the latest one, then explain it to a reviewer.\n"
            f"If violated: one violating trial is a proof of the defect. State the final "
            f"database state and which invariant was broken; the reproduction rate describes "
            f"how often it fired under these conditions, not how often it will in production.\n"
            f"If not reproduced: say exactly that, with the trial count, concurrency, mode, "
            f"amplification setting and the 95% interval. The true rate is anywhere in that "
            f"interval. Never write 'safe', 'secure', 'verified correct' or 'race-free'; "
            f"the tool does not say those things and neither should you.\n"
            f"Read raceguard://docs/reproduction-rate for the wording of the asymmetry."
        )
