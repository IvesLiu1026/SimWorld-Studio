# SimWorld Studio / VISTA remote production handoff

Target host: `yhliu@140.113.215.82`

Integration branch: `codex/vista-production-completion` on
`git@github.com:IvesLiu1026/SimWorld-Studio.git`

Updated: 2026-07-21 Asia/Taipei

This package is the continuation plan for turning the code-ready VISTA production
foundation into a live, evidence-backed 3D stack on the RTX 5090 host. It is not a
Production-ready declaration.

Read in this order:

1. [requirements.md](requirements.md) — immutable requirements and approval gates.
2. [design.md](design.md) — source, runtime, evidence, ownership, and rollback design.
3. [tasks.md](tasks.md) — checkpointed execution ledger.
4. [runbook.md](runbook.md) — exact remote commands and acceptance checks.
5. [HANDOFF.md](HANDOFF.md) — current facts, historical transfer evidence, and blockers.

The 2026-07-15 dirty-checkout transfer remains historical evidence and must not be
deleted or overwritten. Current source synchronization is exclusively through the
GitHub integration branch: resolve its exact commit at execution time, create a new
clean checkout generation, and record that SHA. Never copy either local dirty
checkout as a new source release.

The last read-only SSH retry on 2026-07-21 failed with `No route to host`. No SSH
session was established and no remote command ran. Therefore every host, UE, asset,
provider, and network fact still requires fresh target-side evidence after
connectivity is restored.
