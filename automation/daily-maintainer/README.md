# VISTA World Daily Maintainer Safety Core

This package implements the deterministic trust boundary used before a daily
maintenance patch can be published. It does not schedule runs, invoke a model,
write to GitHub, or manage production runtime.

The core provides:

- a digest-bound, strict YAML contract for human-reviewed candidate backlogs;
- code-owned validation profile IDs with fixed cwd and argv;
- deterministic candidate selection;
- a Git diff guard for path, symlink, binary, secret, size, and test-weakening
  policy;
- a credential-free verifier that never enables a subprocess shell; and
- canonical receipt serialization, SHA-256 binding, and journal markers.

## V1 fail-closed boundaries

Only reviewed Tier 0 and Tier 1 candidates are accepted. Tier 0 owns
documentation and test-only paths; Tier 1 is limited to the approved pure
Python, Node, contract, frontend, and test roots. Unreal, runtime, network,
auth, deployment, data, evidence, receipt, ledger, asset, and automation
authority is rejected. Unreal validation profiles are not eligible in V1.

Any deletion from an existing test and any modification to an existing schema
or validation configuration requires human review. The unattended verifier is
deliberately unable to decide that these edits are safe.

Validation requires an operator-produced IsolationAttestation proving that
network access is blocked and publisher/model credentials are absent. Python
cannot establish that sandbox boundary itself. The verifier additionally uses
an explicit executable allowlist, ignores inherited PATH and HOME, creates
empty XDG/npm/uv configuration roots, disables global Git configuration, and
kills the validation process group on timeout. A detached descendant still
belongs to the outer UID/container/network-namespace containment boundary.

The verifier re-runs the complete diff guard after every command. Exact patch
digest and changed-file metadata must remain identical; validation-time
tracked, untracked, or executable-mode mutation rejects the run.

From the repository root, run the complete test suite with:

    uv run --project automation/daily-maintainer python -m unittest discover \
      -s automation/daily-maintainer/tests -p 'test_*.py'

For package-local development:

    cd automation/daily-maintainer
    uv run --locked python -m unittest discover -s tests -t . -p 'test_*.py'

Both discovery forms are intentionally supported.
