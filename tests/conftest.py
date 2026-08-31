from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "tests"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

CORPUS = ROOT / "tests" / "corpus"
SHOP = ROOT / "examples" / "shop"


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def corpus() -> Path:
    return CORPUS


@pytest.fixture(scope="session")
def shop() -> Path:
    return SHOP


@pytest.fixture(scope="session")
def shop_scan():
    from raceguard.contracts import Confidence
    from raceguard.scanner import ScanOptions, scan

    return scan([str(SHOP / "shop")], ScanOptions(min_confidence=Confidence.low, root=SHOP))


def docker_available() -> bool:
    import subprocess

    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=20
        ).returncode == 0
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not docker_available(), reason="needs a running Docker daemon"
)
