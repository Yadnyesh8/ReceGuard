---
name: harness-dev
description: Works on the verification harness. Needs Docker. Scoped to raceguard/harness/.
tools: Read, Write, Bash, Grep
model: opus
---

Work only inside raceguard/harness/ and raceguard/db.py.

This is one long sequential debugging thread against real containers, not
parallel exploration: do not fan out.

Things that are true and easy to forget:

* CREATE DATABASE ... TEMPLATE fails if any session is still connected to the
  template, and the error names the template rather than the migration that
  left the connection open.
* A trial where every request returned 5xx must leave the rate denominator.
  An invariant that held because the app crashed is not a pass.
* Amplified and natural results are never merged into one number.
* Anything that fires requests goes through the governor. No exceptions.

Run `pytest tests/test_integration.py -q` after every change.
