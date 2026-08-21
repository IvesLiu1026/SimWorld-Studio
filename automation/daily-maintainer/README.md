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
