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

`patcher.py` describes a shell-free `codex exec` invocation for `gpt-5.6-sol` with Ultra reasoning,
ephemeral history and pinned prompt, schema, Codex and Git builds. It does not launch Codex. It can
return an executable invocation only after a root-owned deployment manifest agrees with live kernel,
mount, cgroup, UID, credential-inode, managed-policy, reviewed-backlog and clean-Git evidence.

The managed permission profile denies the filesystem root by default, permits only minimal runtime
reads plus the worktree and a separate scratch mount, disables command network, and leaves credential
and final-output state outside command authority. Fixed launch flags additionally disable web search,
apps, browsers, Computer Use, plugins, hooks, MCP-related features and other hosted surfaces. This is
intentional: command network controls do not govern hosted tools.

Activation still requires administrator-provisioned, non-nested mounts; a dedicated non-root UID;
`NoNewPrivileges`, seccomp, namespace and cgroup containment; a root-owned managed
`requirements.toml`; a root-owned install of the pinned binaries; and separate scratch/state paths.
The credential metadata must identify an explicitly approved API key, access token or workload
identity for public-repository automation. Personal ChatGPT-managed auth is rejected. Unit tests
prove that the ordinary developer login fails closed; they do not fabricate a successful UID or
container boundary. A real positive sandbox acceptance test remains a deployment gate.
Provider-only model egress is also an outer network-policy acceptance item; a namespace inode proves
identity, not the destinations reachable through that namespace.

The request is rebound to a root-owned, read-only backlog file by digest, revision, approver, exact
canonical candidate payload and the built-in V1 profile registry. The final result path is reserved
with `O_EXCL`/`O_NOFOLLOW`, revalidated around execution and parsed only after the subprocess exits.
Until the real outer boundary and compliant credential exist, the maintainer remains report-only or
attended-canary only; this implementation is a fail-closed contract, not deployment readiness.

This follows the official Codex guidance for [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode.md),
which documents `codex exec`, `--ephemeral`, structured output, least-privilege sandboxing, and the
credential warning for public/open-source CI/CD.
