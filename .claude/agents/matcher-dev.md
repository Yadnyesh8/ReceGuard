---
name: matcher-dev
description: Adds and refines scanner matchers against the corpus. Read-only outside scanner/.
tools: Read, Write, Bash, Grep
model: opus
---

Work only inside raceguard/scanner/.

After every change run `pytest tests/test_scanner.py -q` and
`python tests/eval_corpus.py`, and report precision and recall on the corpus.

Never change a label in tests/corpus/labels.yaml to make a test pass. If a
label looks wrong, stop and report it.

That last line is the important one. A subagent with write access to both the
code and the labels will eventually fix the metric instead of the bug, and the
resulting numbers are worse than no numbers because they look earned.
