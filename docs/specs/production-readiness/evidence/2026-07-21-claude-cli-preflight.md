# Claude review CLI preflight — 2026-07-21

Status: local, read-only preflight passed. No model request was sent.

## Observed runtime

- executable: `/home/yhliu/.local/bin/claude`
- version: `2.1.215 (Claude Code)`
- authentication: logged in through `claude.ai`, first-party provider, Team
  subscription
- credential values, account identity, and tokens were not recorded

`claude --help` in this exact installation advertises every isolation and
protocol option required by `server/review-provider.js`:

- `--print`
- `--input-format stream-json`
- `--output-format stream-json`
- `--safe-mode`
- `--disable-slash-commands`
- `--tools ""`
- `--permission-mode dontAsk`
- `--strict-mcp-config`
- `--mcp-config {}`
- `--no-session-persistence`
- `--json-schema`
- `--system-prompt`
- `--model`
- `--max-budget-usd`

The checks executed were limited to `command -v`, `--version`, `--help`, and
the non-secret fields of `claude auth status --json`. They did not send a
prompt, resolve a model, consume tokens, capture an image, access UE, or write
a review receipt.

## Remaining live proof

This preflight does not prove that `claude-opus-4-8` is enabled for the Team
account, that the CLI result event reports bounded usage/cost in this auth
mode, or that the Text and Visual review receipts pass the production parser.
Those facts require the two explicit cost-gated calls in
`../review-provider-smoke-runbook.md`, using a disposable scene and
shared-broker before/after digests. Until both receipts exist and validate,
T1A.11 and production review readiness remain open.
