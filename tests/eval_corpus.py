"""Corpus evaluation: the quality gates from section 17.2, and the paper's table.

    Scanner recall on vulnerable/ at high plus medium : at least 90 percent
    Scanner false positives on safe/ at high          : under 5 percent

Run with `python tests/eval_corpus.py` for the table, or via pytest for the gate.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raceguard.contracts import Confidence, Finding  # noqa: E402
from raceguard.scanner import ScanOptions, scan  # noqa: E402

CORPUS = Path(__file__).parent / "corpus"
RECALL_GATE = 0.90
FP_GATE = 0.05


@dataclass
class Evaluation:
    expected_vulnerable: int = 0
    detected: int = 0
    missed: list[str] = field(default_factory=list)
    wrong_pattern: list[str] = field(default_factory=list)
    wrong_confidence: list[str] = field(default_factory=list)
    safe_total: int = 0
    false_positives_high: list[str] = field(default_factory=list)
    false_positives_any: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.detected / self.expected_vulnerable if self.expected_vulnerable else 0.0

    @property
    def fp_rate_high(self) -> float:
        return len(self.false_positives_high) / self.safe_total if self.safe_total else 0.0

    @property
    def precision_high(self) -> float:
        tp = self.detected
        fp = len(self.false_positives_high)
        return tp / (tp + fp) if (tp + fp) else 0.0


def load_labels() -> list[dict]:
    data = yaml.safe_load((CORPUS / "labels.yaml").read_text())
    return data["fixtures"]


def run() -> Evaluation:
    result = scan([str(CORPUS)], ScanOptions(min_confidence=Confidence.low, root=CORPUS))
    by_file: dict[str, list[Finding]] = {}
    for finding in result.findings:
        by_file.setdefault(finding.read_site.file, []).append(finding)

    ev = Evaluation(errors=list(result.errors))
    for label in load_labels():
        rel = label["file"]
        found = by_file.get(rel, [])
        if label.get("vulnerable"):
            ev.expected_vulnerable += 1
            usable = [f for f in found if f.confidence.rank() >= Confidence.medium.rank()]
            expected_conf = label.get("expected_confidence", "high")
            if expected_conf == "low":
                usable = found  # a low-confidence label is satisfied by any finding
            if not usable:
                ev.missed.append(f"{rel} (expected {label.get('pattern')})")
                continue
            ev.detected += 1
            want = label.get("pattern")
            if want and not any(f.pattern == want for f in usable):
                ev.wrong_pattern.append(
                    f"{rel}: expected {want}, got {sorted({f.pattern for f in usable})}"
                )
            if want and expected_conf:
                match = [f for f in usable if f.pattern == want]
                if match and match[0].confidence.value != expected_conf:
                    ev.wrong_confidence.append(
                        f"{rel}: expected {expected_conf}, got {match[0].confidence.value}"
                    )
        else:
            ev.safe_total += 1
            high = [f for f in found if f.confidence is Confidence.high]
            if high:
                ev.false_positives_high.append(
                    f"{rel}: {sorted({f.pattern for f in high})} (guard={label.get('guard')})"
                )
            elif found:
                ev.false_positives_any.append(
                    f"{rel}: {sorted({f.pattern for f in found})} at "
                    f"{sorted({f.confidence.value for f in found})}"
                )
    return ev


def report(ev: Evaluation) -> int:
    print("=" * 78)
    print("RaceGuard scanner evaluation (section 17.2 gates)")
    print("=" * 78)
    print(f"  vulnerable fixtures      {ev.expected_vulnerable}")
    print(f"  safe fixtures            {ev.safe_total}")
    print()
    recall_ok = ev.recall >= RECALL_GATE
    fp_ok = ev.fp_rate_high <= FP_GATE
    print(
        f"  recall (high+medium)     {ev.recall:6.1%}  gate >= {RECALL_GATE:.0%}   "
        f"{'PASS' if recall_ok else 'FAIL'}"
    )
    print(
        f"  false positives (high)   {ev.fp_rate_high:6.1%}  gate <= {FP_GATE:.0%}    "
        f"{'PASS' if fp_ok else 'FAIL'}"
    )
    print(f"  precision (high)         {ev.precision_high:6.1%}")
    print()
    for title, items in (
        ("MISSED (recall failures)", ev.missed),
        ("FALSE POSITIVES at high", ev.false_positives_high),
        ("flagged on safe/ below high (not a gate failure)", ev.false_positives_any),
        ("pattern mismatches (detected, wrong label)", ev.wrong_pattern),
        ("confidence mismatches", ev.wrong_confidence),
        ("scan errors", ev.errors),
    ):
        if items:
            print(f"{title}: {len(items)}")
            for item in items:
                print(f"    - {item}")
            print()
    return 0 if (recall_ok and fp_ok) else 1


if __name__ == "__main__":
    raise SystemExit(report(run()))
