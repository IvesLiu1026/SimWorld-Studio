# Builder Sandbox Production Runbook

This runbook covers the Claude and Codex processes started by the Studio scene
builder. It does not authorize a live provider call, UE mutation, deployment,
or public-network change.

## Enforced source contract

- Claude runs without `--dangerously-skip-permissions`. `--strict-mcp-config`
  loads exactly one server named `simworld`; `--tools` and `--allowedTools`
  contain only the reviewed SimWorld MCP names. Bash, file, search, web,
  subagent, task, and write tools are explicitly denied. `--safe-mode` is not
  used for the builder because Claude 2.1.215 suppresses even the explicit MCP
  config in that mode; empty setting sources, strict MCP config, exact tool
  lists, no plugins/slash commands/browser, and the OS boundary provide the
  composable restriction instead. Caller prompts travel only over stdin.
- Codex runs with `--sandbox read-only`, `approval_policy="never"`,
  `--ephemeral`, `--ignore-user-config`, `--ignore-rules`, and disabled web
  search. Its required `simworld` MCP server has an exact `enabled_tools`
  allowlist and approval mode `approve`; no bypass flag is used.
- The child environment is built from an empty object. It contains locale and
  production-policy values, a lease-scoped run capability, loopback broker
  routing, and fixed private HOME/config paths. Provider keys, Studio root
  bearer tokens, Postgres/Qdrant credentials, cloud credentials, proxy
  credentials, SSH agent state, and host HOME are not inherited.
- `search_assets` crosses a dedicated loopback `/api/internal/assets` endpoint
  using only the same short-lived lease/run capability. PostgreSQL, Qdrant,
  embedding secrets, verified snapshot pins, and retrieval clients stay in the
  main server process. The nested-provider `verify_scene` MCP handler is not
  exposed to builders; Text/Visual Review remains owned by the main-process
  Review coordinator.
- Automatic skill selection is a separate tool-free Claude process. Safe mode,
  an empty tool list, no MCP config, a minimal environment, the same OS
  namespace, and stdin-only prompts are enforced. Legacy Coding Arena,
  `/api/agent-chat`, and `/api/agent-broadcast` execution are disabled whenever
  the production safety policy is locked; their older AgentManager/
  AgentController launchers are not part of the trusted builder path.
- `visualFeedbackImages` never accepts a filesystem path. Non-empty input is
  blocked until the caller supplies opaque `simworld-image:<id>` references and
  a reviewed server-side resolver returns bounded PNG/JPEG bytes. The current
  CLI byte-to-sandbox transport is intentionally not installed.

## OS boundary

`agent-sandbox.js` runs a real bubblewrap namespace probe before it will build
any command. The process starts from an empty mount namespace and receives only:

- the selected builder executable and the exact Node executable, read-only;
- loader libraries and bounded DNS/CA/identity files, read-only;
- the SimWorld repository, read-only, so the scoped stdio MCP child can load;
- only the active provider's dedicated auth subdirectory, read-only (Claude
  never sees Codex auth and Codex never sees Claude auth);
- private tmpfs `/home`, `/tmp`, and `/work`, plus new `/proc` and `/dev`.

The host root, host HOME, host `/tmp`, devices, runtime sockets, databases, and
cloud credential locations are not mounted. The wrapper uses `--unshare-all`,
retains the network namespace only for the provider and loopback broker,
creates a new session, drops all capabilities, and dies with its parent. It
never uses `--dev-bind / /`. A missing/failed namespace probe or
`AGENT_SANDBOX=0` rejects the launch; there is no unsandboxed fallback.

## Administrator prerequisites

1. Install a reviewed bubblewrap package and enable unprivileged user/mount/PID/
   IPC/UTS/cgroup namespaces for the Studio service account. Do not grant the
   service account sudo, Docker-group, or broad device access.
2. Create a dedicated auth root owned by the Studio service UID with mode
   `0700` or stricter. It must not be a symlink and must contain only
   service-scoped builder authentication, for example:

   ```text
   /run/simworld-builder-auth/
     claude/   # dedicated Claude service credential/config only
     codex/    # dedicated Codex service credential/config only
   ```

   Set `AGENT_SANDBOX_AUTH_ROOT=/run/simworld-builder-auth`. Production launch
   fails when this setting is absent, not private, symlinked, or owned by a
   different UID. The selected provider subdirectory is independently checked
   for the same owner/mode/symlink constraints and is the only auth path
   mounted for that run. Do not copy a human's full `~/.claude`, `~/.codex`, browser
   profile, SSH keys, cloud credentials, or shell configuration into this root.
3. Use a trusted/public streaming profile so every production builder request
   has an active lease and receives only a per-run capability. The root Studio
   bearer is never a child credential. This production branch applies the same
   contract to every real builder launch: direct/loopback MCP configs are
   rejected even under a development `NODE_ENV`. Loopback development may use
   explicit reviewed `mock`/`off` mode; it must not report a real builder as
   ready without a trusted/public lease.
4. Keep the generated brokered MCP config a mode-`0600` regular file. It must
   contain exactly the root key `mcpServers`; `simworld` must contain exactly
   `command`, `args`, and `env`, pin the current Node executable plus the
   server-owned `mcp-server.js`, and use only an exact loopback broker profile.
   The auth root must be outside the repository mount.

## Pre-deployment validation

Run without provider credentials or live UE:

```bash
cd simworld_studio_workspace/web
node --test \
  server/tests/agent-sandbox-security.test.js \
  server/tests/builder-process-policy.test.js \
  server/tests/builder-runtime-authority.test.js \
  server/tests/skill-selector-security.test.js \
  server/tests/internal-run-capability.test.js \
  server/tests/internal-asset-broker.test.js \
  server/tests/claude-mcp-startup-contract.test.js
node --test server/tests/*.test.js
```

Then, in a disposable deployment namespace and only after explicit approval,
perform one no-mutation startup probe per CLI. Confirm:

- the bubblewrap probe is `verified`;
- `/etc/shadow`, human HOME, SSH agent sockets, Postgres/Qdrant secrets, and
  unrelated repository paths are absent;
- shell/file/web tools are unavailable and an extra MCP server is rejected;
- the scoped MCP server is required and fails rather than continuing when its
  config or run capability is missing;
- a direct `UNREAL_HOST`/`UNREAL_PORT` MCP profile is rejected before spawn;
- the network-isolated real-Claude startup contract reports exactly one MCP
  server named `simworld` (not an empty `mcp_servers` list);
- `search_assets` succeeds through the capability broker with no database,
  vector, embedding, or Studio root secret in the child;
- process exit revokes the run capability and leaves no host-writable output;
- provider authentication works from the dedicated service credential only.

## Remaining deployment gates

- Provision and audit the dedicated Claude/Codex service credential root. No
  credential was created or copied by this change.
- Verify the exact production CLI binaries and their dynamic-library needs in
  the empty namespace; the sandbox does not add compatibility mounts on error.
- Provision and validate the main-process verified semantic asset runtime
  (snapshot/audit receipt plus PostgreSQL, Qdrant, and embedding service
  credentials). The child-side secret path is intentionally unavailable.
- Add the reviewed opaque-image byte materialization transport before enabling
  visual builder feedback. Existing caller local paths remain rejected.
- Add destination-level egress policy at the service/container/firewall layer
  if production policy requires provider-domain-only traffic. Bubblewrap shares
  the network namespace so the CLI can reach the provider and loopback broker;
  it does not itself filter destinations.
- Obtain the existing explicit cost/state approval before any real Claude or
  Codex call, and separately approve any live UE mutation.
