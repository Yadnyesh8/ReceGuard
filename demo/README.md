# RaceGuard live demo

A real shop, a real PostgreSQL and real concurrent requests. Nothing is mocked or
pre-recorded. It takes about 70 seconds of machine time, or 5 to 7 minutes if you
talk through each step.

The script works on a throwaway copy of `examples/shop`, so you can run it as
often as you like and your code never changes.

## Before the meeting

1. Rehearse once, on internet, from the repository root:

   ```bash
   make demo
   ```

   The first run may download images and Python packages. Later runs reuse Docker's
   cache. Keep your internet on for the meeting anyway.
2. If `.venv` is missing, run `make setup` first.
3. Make the terminal wide (120 columns or more) and increase the font size.

## During the meeting

```bash
make demo
```

It pauses at every step. Press Enter when you have finished talking. Ctrl-C stops
it at any time and removes its containers.

| Step | What the screen shows | What to say |
|---|---|---|
| 1. The bug | The checkout handler | "This looks correct. It checks the stock, then sells. The problem is the gap between those two lines. No compiler, linter or normal unit test sees it, because they run one request at a time." |
| 2. Many buyers | 10 buyers, 1 item, all accepted | "This is the real app with no delay added, and RaceGuard is not running yet. We sold one item ten times. The second endpoint is correctly written and accepts exactly one, so the bug is in that handler, not the test." |
| 3. Scan | 6 findings in about 0.1s | "RaceGuard read the source without running it and gives the read, decide and write lines. It did not flag the correct endpoint, so it is not just flagging everything that touches the database." |
| 4. Verify | VIOLATED, 7 of 7 trials | "A static tool can only suspect. Here RaceGuard starts its own database and app, fires concurrent requests and checks the rule in the database. One violation is proof." |
| 5. Fix | A diff | "It moves the check into the UPDATE statement, so the decision and the write become one atomic step. Nothing is written yet." |
| 6. Apply | `applied: the race did not reproduce...` | "It applies the patch, repeats the concurrent test and runs the tests. If either fails, the change is rolled back by itself." |
| 7. Same buyers again | 1 accepted, 9 rejected | "Same buyers, same script. Before the patch nearly every round oversold, and after it none did." |

The HTML report opens at the end. It holds the evidence from step 4, trial by trial.

## Questions your guide may ask

**Is this faked or scripted?** No. `demo/concurrent_buyers.py` is short, uses only HTTP and SQL
and never imports RaceGuard. Open it and show them.

**Why not 20 of 20 every time?** It is a timing bug, so a round can occasionally miss.
Real runs here showed 19 or 20 of 20. If the first round misses, the script says so
and tries again.

**Why did step 4 run only 7 trials when the config says 30?** One violation already
proves the bug, so RaceGuard stops early, after a quarter of the trials, once it has
enough for a rate. When no violation appears, it runs all 30.

**Is the code safe now?** Do not say yes. RaceGuard never does. Say: "The race did not
reproduce in these trials under these conditions." One failing trial proves a bug, but
passing trials are only evidence.

**Does the fix use AI?** No. Patches are deterministic libcst syntax-tree rewrites. A
language model is optional, and is only used to read invariants written in English.

**What does it support?** Python HTTP handlers against PostgreSQL. This demo uses
FastAPI with async SQLAlchemy.

**Can it handle other bugs?** Yes. The scan finds five vulnerable endpoints: oversell,
wallet transfer, coupon reuse, duplicate signup and team size. Try
`raceguard fix --target transfer` inside the workspace. `signup` is a good one to
show: its fix needs a unique index, and RaceGuard says plainly that the code change
alone is not enough.

## If something goes wrong

| Problem | Fix |
|---|---|
| `Docker did not start` | Open Docker Desktop yourself, wait until it is running, run again |
| `missing raceguard, uvicorn...` | Run `make setup`, and use `.venv/bin/python` rather than `python` |
| Docker build or pull fails | You are offline with a cold cache: connect and run once |
| Code lines wrap in step 1 | Widen the terminal |

If Docker cannot start at all, `scan` and the `fix` dry run still work without it:

```bash
cd examples/shop
../../.venv/bin/raceguard scan .
../../.venv/bin/raceguard fix --target purchase
```

The patched copy stays in the path printed at the end, so you can open it and show
the changed file. Run the demo again to reset it.
