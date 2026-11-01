"""Live RaceGuard demo: rush a real shop, find the bug, prove it, patch it, rush it again.

    .venv/bin/python demo/demo.py          # pauses at every step, for presenting
    .venv/bin/python demo/demo.py --auto   # runs straight through, for rehearsal

Everything happens on a throwaway copy of examples/shop, so examples/ is never
modified and the demo can be run any number of times. See demo/README.md.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path

import concurrent_buyers
import httpx
import psycopg
import yaml
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "examples" / "shop"
WORKSPACE = Path(tempfile.gettempdir()) / "raceguard-demo"
CONTAINER = "raceguard-demo-postgres"
BIN = Path(sys.executable).parent
TOTAL_STEPS = 7
ROUNDS = 20
FIRST_ROUND_RETRIES = 4
DOCKER_WAIT_SECONDS = 120

console = Console(highlight=False)


class Presenter:
    def __init__(self, auto: bool) -> None:
        self.auto = auto
        self.step_number = 0

    def step(self, title: str, explanation: str) -> None:
        self.step_number += 1
        console.print()
        console.rule(f"[bold]Step {self.step_number} of {TOTAL_STEPS}: {title}", align="left")
        console.print(f"{explanation}\n")

    def pause(self, next_action: str) -> None:
        if self.auto:
            return
        console.print()
        console.input(f"[dim]Enter to {next_action}...[/]")


def run(*command: str | Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(part) for part in command], **kwargs)


def raceguard(*args: str) -> int:
    """Run the real CLI with its output on the terminal, exactly as a user sees it."""
    console.print(f"[bold cyan]$ raceguard {' '.join(args)}[/]\n")
    return run(BIN / "raceguard", *args, cwd=WORKSPACE, env=app_env()).returncode


def app_env(**extra: str) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{BIN}{os.pathsep}{os.environ['PATH']}",
        # `pytest` inside `fix --apply` has to import the shop from this copy.
        "PYTHONPATH": str(WORKSPACE),
        "PYTHONDONTWRITEBYTECODE": "1",
        **extra,
    }


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def fail(message: str) -> None:
    console.print(f"\n[bold red]Cannot continue:[/] {message}")
    raise SystemExit(1)


def docker_ready() -> bool:
    return run("docker", "info", capture_output=True).returncode == 0


def ensure_docker() -> None:
    if docker_ready():
        return
    console.print("[dim]Starting Docker Desktop, this can take a minute...[/]")
    run("open", "-a", "Docker", capture_output=True)
    for _ in range(DOCKER_WAIT_SECONDS // 3):
        time.sleep(3)
        if docker_ready():
            return
    fail("Docker did not start. Open Docker Desktop, wait for it to start, try again.")


def preflight() -> None:
    ensure_docker()
    required = ("raceguard", "uvicorn", "alembic", "pytest")
    missing = [name for name in required if not (BIN / name).exists()]
    if missing:
        fail(f"missing {', '.join(missing)}. Run `make setup`, then use .venv/bin/python.")


def prepare_workspace() -> None:
    """Copy the shop into a git repository, because `fix --apply` insists on one."""
    shutil.rmtree(WORKSPACE, ignore_errors=True)
    shutil.copytree(SOURCE, WORKSPACE, ignore=shutil.ignore_patterns(".raceguard", "__pycache__"))
    ignored = ("__pycache__/", "*.pyc", ".raceguard/", ".pytest_cache/", "*.html")
    (WORKSPACE / ".gitignore").write_text("\n".join(ignored) + "\n")
    git = ("git", "-c", "user.name=demo", "-c", "user.email=demo@example.com")
    for command in (("init", "-q"), ("add", "-A"), ("commit", "-qm", "shop before the fix")):
        run(*git, *command, cwd=WORKSPACE, check=True)


@contextmanager
def postgres():
    """A real PostgreSQL for the buyers, separate from the ones RaceGuard boots itself."""
    image = yaml.safe_load((SOURCE / "raceguard.yaml").read_text())["database"]["image"]
    port = free_port()
    run("docker", "rm", "-f", CONTAINER, capture_output=True)
    run(
        "docker", "run", "-d", "--rm", "--name", CONTAINER,
        "-e", "POSTGRES_PASSWORD=postgres", "-e", "POSTGRES_DB=shop",
        "-p", f"127.0.0.1:{port}:5432", image,
        capture_output=True, check=True,
    )  # fmt: skip
    dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/shop"
    try:
        for _ in range(120):
            try:
                psycopg.connect(dsn, connect_timeout=2).close()
                break
            except psycopg.OperationalError:
                time.sleep(0.5)
        else:
            fail("PostgreSQL did not start within a minute.")
        yield dsn
    finally:
        run("docker", "rm", "-f", CONTAINER, capture_output=True)


@contextmanager
def shop_server(dsn: str):
    """Serve whatever code is in the workspace right now."""
    env = app_env(DATABASE_URL=dsn.replace("postgresql://", "postgresql+asyncpg://"))
    run(BIN / "alembic", "upgrade", "head", cwd=WORKSPACE, env=env, check=True, capture_output=True)
    port = free_port()
    server = subprocess.Popen(
        [BIN / "uvicorn", "shop.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=WORKSPACE,
        env=env,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                httpx.get(f"{base_url}/health", timeout=1).raise_for_status()
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            fail("the shop server did not start.")
        yield base_url
    finally:
        server.send_signal(signal.SIGTERM)
        server.wait(timeout=10)


def show_requests(outcome: concurrent_buyers.Outcome) -> None:
    table = Table(box=None, pad_edge=False, show_header=False)
    for number, status in enumerate(outcome.statuses, start=1):
        result = "[red]order accepted[/]" if status == 200 else "[dim]rejected, out of stock[/]"
        table.add_row(f"buyer {number:>2}", f"HTTP {status}", result)
    console.print(table)


def show_result(outcome: concurrent_buyers.Outcome, label: str) -> None:
    console.print(
        f"\n{label}: stock was [bold]1[/], [bold]{outcome.accepted}[/] accepted, "
        f"[bold]{outcome.rows}[/] sold according to the database, stock now {outcome.stock}"
    )


def repeat(scenario, base_url: str, dsn: str, buyers: int) -> int:
    """Run the scenario ROUNDS times and count how many rounds sold more than existed."""
    return sum(scenario(base_url, dsn, buyers).oversold for _ in range(ROUNDS))


def run_demo(presenter: Presenter, dsn: str, buyers: int) -> None:
    presenter.step(
        "The bug",
        "The real checkout handler. The check (line 30) and the write (line 33) are separate "
        "steps, so two buyers can both pass the check before either one writes.",
    )
    code = WORKSPACE / "shop/routes/orders.py"
    console.print(
        Syntax.from_path(str(code), line_numbers=True, line_range=(20, 37), word_wrap=True)
    )
    presenter.pause("send buyers to the running shop")

    presenter.step(
        "Many buyers at once",
        f"A real server and a real PostgreSQL. {buyers} buyers, 1 unit of stock, no delays "
        "injected. This step is plain HTTP and SQL: RaceGuard is not involved.",
    )
    with shop_server(dsn) as base_url:
        outcome = concurrent_buyers.order_item(base_url, dsn, buyers)
        for _ in range(FIRST_ROUND_RETRIES):
            if outcome.oversold:
                break
            console.print("[dim]This round did not oversell: the bug depends on timing. Again.[/]")
            outcome = concurrent_buyers.order_item(base_url, dsn, buyers)
        show_requests(outcome)
        show_result(outcome, "POST /orders")
        before = repeat(concurrent_buyers.order_item, base_url, dsn, buyers)
        console.print(
            f"Repeated {ROUNDS} times: oversold in [bold red]{before} of {ROUNDS}[/] rounds"
        )

        guarded = concurrent_buyers.purchase_product(base_url, dsn, buyers)
        console.print(
            "\nControl, same buyers on the correctly written endpoint "
            f"POST /products/1/purchase:\n{guarded.accepted} accepted out of {buyers}"
        )
    presenter.pause("scan the source code")

    presenter.step(
        "Find it: static scan",
        "Reads the source only: no Docker, no database, no running app. "
        "Six findings, and the guarded shop/routes/safe.py is correctly not among them.",
    )
    raceguard("scan", ".", "--fail-on", "never")
    presenter.pause("prove it with the verifier")

    presenter.step(
        "Prove it: dynamic verification",
        "A scanner can only suspect. RaceGuard now boots its own PostgreSQL and the app in "
        "containers, fires concurrent requests over repeated trials and checks the "
        "invariant in the database. Under a minute.",
    )
    raceguard("verify", "--target", "purchase")
    # Render now: `report` reads the latest run, and the later `fix` runs hold no results.
    report = WORKSPACE / "raceguard-report.html"
    run(BIN / "raceguard", "report", "--format", "html", "-o", report,
        cwd=WORKSPACE, env=app_env(), capture_output=True)  # fmt: skip
    presenter.pause("generate the fix")

    presenter.step(
        "Propose the fix",
        "Generated from the code by a syntax-tree rewrite, not by an LLM. "
        "Prints a diff and writes nothing.",
    )
    raceguard("fix", "--target", "purchase")
    presenter.pause("apply it")

    presenter.step(
        "Apply and re-prove",
        "--apply patches the code, verifies the race again and runs the test suite. "
        "If either check fails, the change is thrown away automatically. Under a minute.",
    )
    raceguard("fix", "--target", "purchase", "--apply")
    patched = run("git", "diff", "--quiet", "shop/routes/orders.py", cwd=WORKSPACE).returncode != 0
    if not patched:
        fail("the patch was not kept, see the output above.")
    console.print("\n[bold]The patch was kept. The file on disk has changed:[/]\n")
    run("git", "--no-pager", "diff", "--stat", cwd=WORKSPACE)
    presenter.pause("repeat the same test on the patched code")

    presenter.step(
        "Same buyers again",
        "The same concurrent_buyers.py as step 2, with the server restarted on the patched code.",
    )
    with shop_server(dsn) as base_url:
        outcome = concurrent_buyers.order_item(base_url, dsn, buyers)
        show_requests(outcome)
        show_result(outcome, "POST /orders")
        after = repeat(concurrent_buyers.order_item, base_url, dsn, buyers)

    console.print(f"\n[bold]Oversold rounds before the patch: {before} of {ROUNDS}[/]")
    console.print(f"[bold]Oversold rounds after the patch:  {after} of {ROUNDS}[/]")
    console.print(
        f"\n[dim]{after} of {ROUNDS} is evidence under these conditions, not a guarantee. "
        "RaceGuard never claims more than that.[/]"
    )

    if report.exists():
        console.print(f"\nHTML report of the step 4 proof: {report}")
        if not presenter.auto:
            webbrowser.open(report.as_uri())
    console.print(f"Patched copy, to inspect or diff: {WORKSPACE}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live RaceGuard demo.")
    parser.add_argument("--auto", action="store_true", help="do not pause between steps")
    parser.add_argument("--buyers", type=int, default=10, help="concurrent buyers (default 10)")
    args = parser.parse_args()

    preflight()
    console.print("[dim]Preparing a throwaway copy of the shop and a real PostgreSQL...[/]")
    prepare_workspace()
    try:
        with postgres() as dsn:
            run_demo(Presenter(args.auto), dsn, args.buyers)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped. Containers cleaned up.[/]")


if __name__ == "__main__":
    main()
