# SimWorld remote migration package

Target: `yhliu@140.113.215.82`
Updated: 2026-07-15

Read in this order:

1. `requirements.md` — behavioral requirements and gates.
2. `design.md` — source/target mapping, transfer strategy and rollback.
3. `tasks.md` — executable status checklist.
4. `runbook.md` — exact remote Codex continuation instructions.
5. `HANDOFF.md` — current transfer and validation ledger.

The target host has enough GPU/RAM/disk. Source, the checksummed UE runtime, sanitized evidence, Node dependencies, Playwright Chromium and the Python environment have been migrated and verified; offline acceptance tests pass. Asset data services and the stateful loopback runtime smoke remain gated. The migration preserves dirty source without inventing a mixed commit, excludes secrets, and keeps runtime activation separate from file transfer.
