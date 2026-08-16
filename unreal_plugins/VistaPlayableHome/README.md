# VistaPlayableHome

Runtime-only Unreal plugin for the VISTA Playable Home contract.

## Included runtime surface

- third-person Manny pawn with Enhanced Input asset hooks and legacy
  `WASD`/mouse, jump, sprint, crouch, interact, and drop bindings;
- typed interactable, pickup, door, container, and appliance actors;
- visible Manny NPC with a bounded typed action queue, room-anchor patrol,
  navigation timeouts, and dynamically gated doorway links;
- atomic data-only event apply/reset and command-generation checks;
- restrained game HUD with friendly context actions, carried-item feedback,
  and public scenario objectives; player-facing text never exposes semantic or
  event identifiers, while the underlying typed IDs, APIs, and receipts remain
  unchanged;
- fixed `vista_world_action` TCP adapter enabled only by
  `-VistaWorldPort=<port>`. The socket binds `127.0.0.1`, accepts one
  newline-delimited request per connection, caps request/response payloads at
  64 KiB, and dispatches only closed typed commands on the game thread.

The non-mutating readiness request is:

```json
{"type":"vista_world_action","params":{"operation":"status","command_id":"vwc-000000000000000000000000"}}
```

A ready response has `status: "success"`, `code: "READY"`, the current
`world_revision`, `session_generation`, and `event_status`. Readiness does not
advance the generation counter. `active_event` is the authoritative event ID
or JSON `null`. `health` is an exact alias for `status`.

The adapter does not expose Python, console commands, object paths, classes,
functions, or filesystem operations. A port argument is intentionally
required; without it the plugin opens no listener.

## Build and proof boundary

The source targets UE 5.7. BuildPlugin, disposable-project installation, map
composition, PIE/game launch, navigation, input, and transport behavior each
require separate retained receipts. A source test is not a runtime claim, and
the Studio must never simulate missing plugin behavior.
