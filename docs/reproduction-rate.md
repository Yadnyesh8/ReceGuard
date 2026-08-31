# What reproduction rate does and does not mean

This is the page to read before quoting a number from RaceGuard anywhere.

## The definition

```
reproduction_rate = violating_trials / usable_trials
```

A trial is **usable** when at least one request returned a non-5xx status. A
trial where every request 5xx'd is excluded from the denominator and noted,
because an invariant that held because the application crashed is not a pass.

Every rate is printed with a Wilson score interval at 95%. The interval is
**reported, never asserted on**: a gate that fails when a confidence bound
moves is a gate that fails randomly.

## The asymmetry

The two directions are not symmetric and the report says so in these words:

* **One violating trial out of any number is a proof of the defect.** The
  interleaving happened. It is real. The rate beside it describes how often it
  fired under these conditions, not how likely it is in production.
* **Zero violating trials out of T is evidence bounded by T**, the seed, the
  concurrency, the boot mode, and the amplification setting. It is not a proof
  of safety.

This is why 0 of 20 renders as an interval from 0% to 16.1% rather than as
zero. The normal approximation gives (0.0, 0.0), which reads as certainty, and
that is the overclaim this project exists not to make.

## Amplified is not natural

Two phases run per target and both are reported.

**Amplified** inserts a deterministic delay after the first read of the table
under test. A 100% amplified rate means the window exists and can be entered.
It does **not** mean the bug fires on every production request.

**Natural** runs with no help. This is the number closest to an operational
probability, and it is still not one: it was measured against a local container
with a seed chosen to make the collision as likely as possible.

Never quote a single blended figure. There isn't one.

## What the rate is not

* It is **not a probability of production impact.** Production has different
  latency, different connection pooling, different worker counts, and real
  traffic that does not arrive in a synchronised burst.
* It is **not comparable across seeds.** `stock = 1` with concurrency 2 and
  `stock = 100` with concurrency 50 measure different things.
* It is **not comparable across boot modes.** `inprocess` cannot reproduce
  races that depend on more than one process, so a 0% there is much weaker
  evidence than a 0% in `container` mode.
* It is **not stable enough to gate on.** Gate on `violated`, which is a
  boolean and a proof, not on a rate crossing a threshold.

## Flake control

The nightly corpus job records the rate per fixture over time. A fixture whose
rate drifts with no code change is a **harness bug**, not a finding, and it
should be visible as a trend rather than as a surprise the week before a
deadline.
