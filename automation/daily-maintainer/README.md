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

Validation accepts digest-bound `IsolationEvidence` only as caller-provided
evidence. It is not a self-authenticating attestation: Python cannot establish
the required network, credential, filesystem, UID, cgroup, or read-only-mount
boundary from inside the verifier. The verifier report exposes
`checks_passed`, fixes `publication_authorized` to `false`, and is never itself
a publication authorization. T13 must add an immutable outer sandbox and issue
a separately authenticated artifact before unattended publication is enabled.

The verifier additionally uses an explicit executable allowlist, ignores
inherited PATH and HOME, creates empty XDG/npm/uv configuration roots, disables
global Git configuration, and kills the validation process group on timeout.
Executable path, owner, mode, inode, metadata, and content digest are pinned and
revalidated before use. A final check-to-exec race and detached descendants
still belong to the outer T13 UID/container/cgroup/mount/network boundary.

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

## Patcher activation boundary

`patcher.py` builds a shell-free `codex exec` invocation for `gpt-5.6-sol` with Ultra reasoning,
ephemeral history, a pinned prompt/output schema, workspace-write sandboxing, no command network,
and a fresh credential-separated worktree. It deliberately does not launch Codex or claim that an
ordinary worktree is an isolation boundary.

Activation requires an outer dedicated UID/container that can attest all of the following: the
operator home and publisher material are absent; model-generated commands have no network; Codex's
own model transport has provider-only egress; policy files are mounted read-only; and the dedicated
Codex credential is not the operator's personal login. Until that boundary and a credential approved
for public-repository automation exist, the maintainer remains report-only or attended-canary only.

This follows the official Codex guidance for [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode.md),
which documents `codex exec`, `--ephemeral`, structured output, least-privilege sandboxing, and the
credential warning for public/open-source CI/CD.
