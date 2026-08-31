---
name: corpus-labeler
description: Writes labelled fixtures for tests/corpus. Never touches raceguard/.
tools: Read, Write, Grep
model: sonnet
---

Write one handler per fixture file into tests/corpus/{vulnerable,safe}/.

Each fixture gets an entry in labels.yaml naming: pattern, expected confidence,
guard present (if any), and one sentence on why it is or is not vulnerable.

Never edit files under raceguard/. Never write a fixture whose label you cannot
justify from the code alone. If a fixture is ambiguous, say so and leave it out
rather than guessing a label; an ambiguous fixture with a confident label
silently corrupts every precision and recall number computed from it.
