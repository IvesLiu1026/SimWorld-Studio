"use strict";

const crypto = require("node:crypto");

const VISTA_SETUP_ROUTE = "/api/vista/setup_vista_play_mode";
const VISTA_STOP_ROUTE = "/api/vista/stop_vista_play_mode";
const VISTA_STATE_ROUTE = "/api/vista/get_vista_state";
const VISTA_SETUP_SCHEMA = "vista-runtime-setup/v2";
const VISTA_STOP_SCHEMA = "vista-runtime-stop/v2";
const VISTA_STATE_SCHEMA = "vista-runtime-state/v2";
const VISTA_CLEANUP_SCHEMA = "vista-runtime-cleanup/v1";
const VISTA_SCENE_PROOF_SCHEMA = "vista-runtime-scene-proof/v1";
const VISTA_SETUP_MARKER = "VISTA_SETUP_V2";
const VISTA_STOP_MARKER = "VISTA_STOP_V2";
const VISTA_STATE_MARKER = "VISTA_STATE_V2";
const VISTA_CLEANUP_MARKER = "VISTA_CLEANUP_V1";
const FIXED_NONCE_PLACEHOLDER = "VISTA_SERVER_NONCE_PLACEHOLDER_V2";
const FIXED_BINDING_PLACEHOLDER = "VISTA_SERVER_BINDING_DIGEST_PLACEHOLDER_V2";
const FIXED_SCENE_PLACEHOLDER = "VISTA_SERVER_SCENE_DIGEST_PLACEHOLDER_V2";
const FIXED_SURFACE_PLACEHOLDER = "VISTA_SERVER_LIVE_SURFACE_DIGEST_PLACEHOLDER_V2";
const VISTA_GAME_MODE_CLASS =
  "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/" +
  "BP_ThirdPersonGameMode.BP_ThirdPersonGameMode_C";
const VISTA_PAWN_CLASS =
  "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/" +
  "BP_ThirdPersonCharacter.BP_ThirdPersonCharacter_C";

const DEFAULT_STATE_CACHE_MS = 500;
const DEFAULT_POLL_INTERVAL_MS = 250;
const DEFAULT_START_TIMEOUT_MS = 30_000;
const DEFAULT_STOP_TIMEOUT_MS = 15_000;
const SCRIPT_TIMEOUT_MS = 15_000;
const MAX_RESPONSE_BYTES = 512 * 1024;
const MAX_CONTROLLERS = 256;
const SHA256_RE = /^[a-f0-9]{64}$/;
const PRINCIPAL_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const PLAN_ID_RE = /^vsp-[a-f0-9]{24}$/;
const ACTOR_FINGERPRINT_RE = /^vsa-[a-f0-9]{24}$/;
const ACTOR_OPERATION_RE = /^vso-[a-f0-9]{24}$/;

const STATE_LIMITS = Object.freeze({
  location: 10_000_000,
  rotation: 360,
  velocity: 100_000,
  engineTime: 315_360_000,
});

// This probe is shared by setup and state reconciliation. It contains no
// caller values: the only dynamic expectation is a server-derived SHA-256.
// The digest binds actor identity to the live asset/class, every exact
// MaterialInterface slot, and the checksum-pinned content revision receipt.
const FIXED_LIVE_SURFACE_PROBE = [
  `EXPECTED_SURFACE = ${JSON.stringify(FIXED_SURFACE_PLACEHOLDER)}`,
  "receipt_path = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir() + 'Content/VISTA/Metadata/content-revision.json')",
  "with open(receipt_path, 'rb') as receipt_handle: receipt_bytes = receipt_handle.read(65537)",
  "if len(receipt_bytes) > 65536:",
  "    raise RuntimeError('VISTA content revision receipt is oversized')",
  "receipt = json.loads(receipt_bytes.decode('utf-8'))",
  "if set(receipt.keys()) != {'schema', 'content_revision', 'verification_revision'} or receipt.get('schema') != 'simworld-ue-content-receipt/v1':",
  "    raise RuntimeError('VISTA content revision receipt is invalid')",
  "content_receipt = {'content_revision': str(receipt['content_revision']), 'receipt_sha256': hashlib.sha256(receipt_bytes).hexdigest(), 'schema': str(receipt['schema']), 'verification_revision': str(receipt['verification_revision'])}",
  "surface_rows = []",
  "for surface_actor in actor_subsystem.get_all_level_actors():",
  "    surface_tags = [str(tag) for tag in list(surface_actor.get_editor_property('tags'))]",
  "    fingerprint_tags = [tag.split('=', 1)[1] for tag in surface_tags if tag.startswith('VISTA_FINGERPRINT=')]",
  "    operation_tags = [tag.split('=', 1)[1] for tag in surface_tags if tag.startswith('VISTA_OPERATION=')]",
  "    if not fingerprint_tags and not operation_tags:",
  "        continue",
  "    if len(fingerprint_tags) != 1 or len(operation_tags) != 1:",
  "        raise RuntimeError('VISTA actor identity tags are not unique')",
  "    try: surface_name = str(surface_actor.get_actor_label())",
  "    except Exception: surface_name = str(surface_actor.get_name())",
  "    surface_class = str(surface_actor.get_class().get_path_name())",
  "    surface_asset = surface_class",
  "    if isinstance(surface_actor, unreal.StaticMeshActor):",
  "        static_components = list(surface_actor.get_components_by_class(unreal.StaticMeshComponent))",
  "        mesh_paths = sorted([str(component.get_editor_property('static_mesh').get_path_name()) for component in static_components if component.get_editor_property('static_mesh') is not None])",
  "        surface_asset = mesh_paths[0] if len(mesh_paths) == 1 else None",
  "    surface_materials = []",
  "    mesh_components = sorted(list(surface_actor.get_components_by_class(unreal.MeshComponent)), key=lambda component: str(component.get_name()))",
  "    for mesh_component in mesh_components:",
  "        try: slot_count = int(mesh_component.get_num_materials())",
  "        except Exception: slot_count = 0",
  "        for slot_index in range(max(0, slot_count)):",
  "            material = mesh_component.get_material(slot_index)",
  "            material_path = str(material.get_path_name()) if material is not None else None",
  "            material_class = str(material.get_class().get_path_name()) if material is not None else None",
  "            pbr_eligible = bool(material is not None and isinstance(material, unreal.MaterialInterface) and material_path.startswith('/Game/') and '/Engine/EngineMaterials/DefaultMaterial' not in material_path)",
  "            surface_materials.append({'component': str(mesh_component.get_name()), 'material_class': material_class, 'material_path': material_path, 'pbr_eligible': pbr_eligible, 'slot_index': slot_index})",
  "    surface_materials.sort(key=lambda row: (row['component'], row['slot_index'], str(row['material_path']), str(row['material_class'])))",
  "    if surface_asset is None or not surface_asset.startswith('/Game/') or not surface_materials or not all(row['pbr_eligible'] for row in surface_materials):",
  "        raise RuntimeError('VISTA live asset/material surface is not production eligible')",
  "    surface_rows.append({'actor_name': surface_name, 'asset_path': surface_asset, 'class_path': surface_class, 'fingerprint': fingerprint_tags[0], 'materials': surface_materials, 'object_guid': str(surface_actor.get_actor_guid()).strip('{}'), 'operation_id': operation_tags[0]})",
  "surface_rows.sort(key=lambda row: (row['actor_name'], row['fingerprint'], row['operation_id'], row['object_guid']))",
  "surface_payload = {'actors': surface_rows, 'content_receipt': content_receipt}",
  "surface_json = json.dumps(surface_payload, separators=(',', ':'), sort_keys=True, allow_nan=False)",
  "live_surface_digest = hashlib.sha256(surface_json.encode('utf-8')).hexdigest()",
  "if not surface_rows or live_surface_digest != EXPECTED_SURFACE:",
  "    raise RuntimeError('verified VISTA live asset/material surface has drifted')",
];

// Every UE program below is a fixed server artifact. Requests cannot provide
// Python, paths, class names, functions, console commands, or filenames. The
// only substitutions are a random response nonce and server-derived SHA-256
// bindings for the already-validated Studio lease and verified scene build.
// Setup never creates geometry and never changes the editor world's GameMode.
const FIXED_SETUP_SCRIPT = [
  "import hashlib",
  "import json",
  "import unreal",
  `GAME_MODE_CLASS = ${JSON.stringify(VISTA_GAME_MODE_CLASS)}`,
  `PAWN_CLASS = ${JSON.stringify(VISTA_PAWN_CLASS)}`,
  `EXPECTED_BINDING = ${JSON.stringify(FIXED_BINDING_PLACEHOLDER)}`,
  `EXPECTED_SCENE = ${JSON.stringify(FIXED_SCENE_PLACEHOLDER)}`,
  "BINDING_TAG_PREFIX = 'SIMWORLD_VISTA_RUNTIME_V2_BINDING='",
  "SCENE_TAG_PREFIX = 'SIMWORLD_VISTA_RUNTIME_V2_SCENE='",
  "game_mode_class = unreal.load_class(None, GAME_MODE_CLASS)",
  "pawn_class = unreal.load_class(None, PAWN_CLASS)",
  "if game_mode_class is None or pawn_class is None:",
  "    raise RuntimeError('fixed VISTA classes are unavailable')",
  "game_mode_default = unreal.get_default_object(game_mode_class)",
  "default_pawn_class = game_mode_default.get_editor_property('default_pawn_class')",
  "if default_pawn_class is None or default_pawn_class.get_path_name() != PAWN_CLASS:",
  "    raise RuntimeError('fixed VISTA game mode has an unexpected default pawn')",
  "editor_subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)",
  "level_editor_subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)",
  "actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
  "if editor_subsystem is None or level_editor_subsystem is None or actor_subsystem is None:",
  "    raise RuntimeError('required editor subsystem is unavailable')",
  "if not hasattr(level_editor_subsystem, 'editor_request_begin_play') or not hasattr(level_editor_subsystem, 'is_in_play_in_editor'):",
  "    raise RuntimeError('UE build does not expose the fixed begin-PIE API')",
  "editor_world = editor_subsystem.get_editor_world()",
  "if editor_world is None:",
  "    raise RuntimeError('editor world is unavailable')",
  "world_settings = editor_world.get_world_settings()",
  "if world_settings is None:",
  "    raise RuntimeError('world settings are unavailable')",
  ...FIXED_LIVE_SURFACE_PROBE,
  "configured_game_mode = world_settings.get_editor_property('default_game_mode')",
  "if configured_game_mode is None or configured_game_mode.get_path_name() != GAME_MODE_CLASS:",
  "    raise RuntimeError('verified scene does not configure the fixed VISTA game mode')",
  "player_starts = [actor for actor in actor_subsystem.get_all_level_actors() if isinstance(actor, unreal.PlayerStart)]",
  "if not player_starts:",
  "    raise RuntimeError('verified scene has no PlayerStart')",
  "manifest_rows = []",
  "surface_count = 0",
  "for actor in actor_subsystem.get_all_level_actors():",
  "    try:",
  "        tags = [str(tag) for tag in list(actor.get_editor_property('tags'))]",
  "        fingerprint = next((tag.split('=', 1)[1] for tag in tags if tag.startswith('VISTA_FINGERPRINT=')), None)",
  "        operation_id = next((tag.split('=', 1)[1] for tag in tags if tag.startswith('VISTA_OPERATION=')), None)",
  "        if not fingerprint or not operation_id:",
  "            continue",
  "        try: actor_name = str(actor.get_actor_label())",
  "        except Exception: actor_name = str(actor.get_name())",
  "        object_guid = str(actor.get_actor_guid()).strip('{}')",
  "        if not actor_name or not object_guid:",
  "            raise RuntimeError('verified scene actor identity is incomplete')",
  "        manifest_rows.append({'actor_name': actor_name, 'fingerprint': fingerprint, 'operation_id': operation_id, 'object_guid': object_guid})",
  "        components = list(actor.get_components_by_class(unreal.PrimitiveComponent))",
  "        origin, extent = actor.get_actor_bounds(False)",
  "        collidable = any('nocollision' not in str(component.get_collision_enabled()).lower().replace('_', '') for component in components)",
  "        if collidable and float(extent.x) >= 25.0 and float(extent.y) >= 25.0:",
  "            surface_count += 1",
  "    except RuntimeError:",
  "        raise",
  "    except Exception:",
  "        continue",
  "manifest_rows.sort(key=lambda row: (row['actor_name'], row['fingerprint'], row['operation_id'], row['object_guid']))",
  "manifest_json = json.dumps(manifest_rows, separators=(',', ':'), sort_keys=True, allow_nan=False)",
  "manifest_digest = hashlib.sha256(manifest_json.encode('utf-8')).hexdigest()",
  "if not manifest_rows or surface_count < 1 or manifest_digest != EXPECTED_SCENE:",
  "    raise RuntimeError('verified scene surface/content proof does not match the live editor world')",
  "tags = [str(tag) for tag in list(world_settings.get_editor_property('tags'))]",
  "binding_tags = [tag for tag in tags if tag.startswith(BINDING_TAG_PREFIX)]",
  "scene_tags = [tag for tag in tags if tag.startswith(SCENE_TAG_PREFIX)]",
  "expected_binding_tag = BINDING_TAG_PREFIX + EXPECTED_BINDING",
  "expected_scene_tag = SCENE_TAG_PREFIX + EXPECTED_SCENE",
  "was_playing = bool(level_editor_subsystem.is_in_play_in_editor())",
  "if was_playing and (binding_tags != [expected_binding_tag] or scene_tags != [expected_scene_tag]):",
  "    raise RuntimeError('PIE belongs to another Studio lease or scene revision')",
  "if not was_playing:",
  "    if binding_tags not in [[], [expected_binding_tag]] or scene_tags not in [[], [expected_scene_tag]]:",
  "        raise RuntimeError('stale runtime tags belong to another Studio lease or scene revision')",
  "    retained = [unreal.Name(tag) for tag in tags if tag not in [expected_binding_tag, expected_scene_tag]]",
  "    retained.extend([unreal.Name(expected_binding_tag), unreal.Name(expected_scene_tag)])",
  "    world_settings.set_editor_property('tags', retained)",
  "    level_editor_subsystem.editor_request_begin_play()",
  "payload = {",
  `    'schema': ${JSON.stringify(VISTA_SETUP_SCHEMA)},`,
  "    'phase': 'play_requested',",
  "    'play_requested': True,",
  "    'was_playing': was_playing,",
  "    'game_mode_class': configured_game_mode.get_path_name(),",
  "    'pawn_class': pawn_class.get_path_name(),",
  "    'default_pawn_class': default_pawn_class.get_path_name(),",
  "    'player_start_count': len(player_starts),",
  "    'scene_actor_count': len(manifest_rows),",
  "    'surface_actor_count': surface_count,",
  "    'scene_manifest_digest': manifest_digest,",
  "}",
  `print(${JSON.stringify(`${VISTA_SETUP_MARKER}:${FIXED_NONCE_PLACEHOLDER}:`)} + json.dumps(payload, separators=(',', ':'), allow_nan=False))`,
].join("\n");

const FIXED_STATE_SCRIPT = [
  "import hashlib",
  "import json",
  "import unreal",
  `PAWN_CLASS = ${JSON.stringify(VISTA_PAWN_CLASS)}`,
  `EXPECTED_BINDING = ${JSON.stringify(FIXED_BINDING_PLACEHOLDER)}`,
  `EXPECTED_SCENE = ${JSON.stringify(FIXED_SCENE_PLACEHOLDER)}`,
  "editor_subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)",
  "level_editor_subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)",
  "actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
  "if editor_subsystem is None or level_editor_subsystem is None or actor_subsystem is None:",
  "    raise RuntimeError('required editor subsystem is unavailable')",
  ...FIXED_LIVE_SURFACE_PROBE,
  "editor_world = editor_subsystem.get_editor_world()",
  "world_settings = editor_world.get_world_settings() if editor_world is not None else None",
  "pie_active = bool(level_editor_subsystem.is_in_play_in_editor())",
  "if not pie_active:",
  "    payload = {",
  `        'schema': ${JSON.stringify(VISTA_STATE_SCHEMA)},`,
  "        'pie': False,",
  "        'possessed': False,",
  "        'pawn_class': None,",
  "        'location': None,",
  "        'rotation': None,",
  "        'velocity': None,",
  "        'on_ground': None,",
  "        'engine_time': None,",
  "    }",
  "else:",
  "    if world_settings is None:",
  "        raise RuntimeError('editor world settings are unavailable')",
  "    tags = [str(tag) for tag in list(world_settings.get_editor_property('tags'))]",
  "    binding_tags = [tag for tag in tags if tag.startswith('SIMWORLD_VISTA_RUNTIME_V2_BINDING=')]",
  "    scene_tags = [tag for tag in tags if tag.startswith('SIMWORLD_VISTA_RUNTIME_V2_SCENE=')]",
  "    if binding_tags != ['SIMWORLD_VISTA_RUNTIME_V2_BINDING=' + EXPECTED_BINDING] or scene_tags != ['SIMWORLD_VISTA_RUNTIME_V2_SCENE=' + EXPECTED_SCENE]:",
  "        raise RuntimeError('PIE binding does not match this Studio lease and scene')",
  "    game_world = editor_subsystem.get_game_world()",
  "    if game_world is None:",
  "        raise RuntimeError('PIE game world is not ready')",
  "    controller = unreal.GameplayStatics.get_player_controller(game_world, 0)",
  "    pawn = unreal.GameplayStatics.get_player_pawn(game_world, 0)",
  "    if controller is None or pawn is None:",
  "        raise RuntimeError('player zero is not possessed')",
  "    if pawn.get_class().get_path_name() != PAWN_CLASS:",
  "        raise RuntimeError('possessed pawn class does not match fixed VISTA content')",
  "    location = pawn.get_actor_location()",
  "    rotation = pawn.get_actor_rotation()",
  "    velocity = pawn.get_velocity()",
  "    movement = pawn.get_component_by_class(unreal.CharacterMovementComponent)",
  "    if movement is None:",
  "        raise RuntimeError('character movement component is unavailable')",
  "    payload = {",
  `        'schema': ${JSON.stringify(VISTA_STATE_SCHEMA)},`,
  "        'pie': True,",
  "        'possessed': True,",
  "        'pawn_class': pawn.get_class().get_path_name(),",
  "        'location': [float(location.x), float(location.y), float(location.z)],",
  "        'rotation': [float(rotation.pitch), float(rotation.yaw), float(rotation.roll)],",
  "        'velocity': [float(velocity.x), float(velocity.y), float(velocity.z)],",
  "        'on_ground': bool(movement.is_moving_on_ground()),",
  "        'engine_time': float(unreal.GameplayStatics.get_time_seconds(game_world)),",
  "    }",
  `print(${JSON.stringify(`${VISTA_STATE_MARKER}:${FIXED_NONCE_PLACEHOLDER}:`)} + json.dumps(payload, separators=(',', ':'), allow_nan=False))`,
].join("\n");

const FIXED_STOP_SCRIPT = [
  "import json",
  "import unreal",
  `EXPECTED_BINDING = ${JSON.stringify(FIXED_BINDING_PLACEHOLDER)}`,
  `EXPECTED_SCENE = ${JSON.stringify(FIXED_SCENE_PLACEHOLDER)}`,
  "level_editor_subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)",
  "editor_subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)",
  "if level_editor_subsystem is None or editor_subsystem is None:",
  "    raise RuntimeError('required editor subsystem is unavailable')",
  "if not hasattr(level_editor_subsystem, 'editor_request_end_play') or not hasattr(level_editor_subsystem, 'is_in_play_in_editor'):",
  "    raise RuntimeError('UE build does not expose the fixed end-PIE API')",
  "editor_world = editor_subsystem.get_editor_world()",
  "world_settings = editor_world.get_world_settings() if editor_world is not None else None",
  "if world_settings is None:",
  "    raise RuntimeError('editor world settings are unavailable')",
  "tags = [str(tag) for tag in list(world_settings.get_editor_property('tags'))]",
  "binding_tags = [tag for tag in tags if tag.startswith('SIMWORLD_VISTA_RUNTIME_V2_BINDING=')]",
  "scene_tags = [tag for tag in tags if tag.startswith('SIMWORLD_VISTA_RUNTIME_V2_SCENE=')]",
  "expected_binding_tag = 'SIMWORLD_VISTA_RUNTIME_V2_BINDING=' + EXPECTED_BINDING",
  "expected_scene_tag = 'SIMWORLD_VISTA_RUNTIME_V2_SCENE=' + EXPECTED_SCENE",
  "was_playing = bool(level_editor_subsystem.is_in_play_in_editor())",
  "if was_playing and (binding_tags != [expected_binding_tag] or scene_tags != [expected_scene_tag]):",
  "    raise RuntimeError('refusing to stop PIE owned by another Studio lease or scene')",
  "if not was_playing and (binding_tags not in [[], [expected_binding_tag]] or scene_tags not in [[], [expected_scene_tag]]):",
  "    raise RuntimeError('refusing to reconcile tags owned by another Studio lease or scene')",
  "if was_playing:",
  "    level_editor_subsystem.editor_request_end_play()",
  "payload = {",
  `    'schema': ${JSON.stringify(VISTA_STOP_SCHEMA)},`,
  "    'phase': 'stop_requested',",
  "    'stop_requested': True,",
  "    'was_playing': was_playing,",
  "}",
  `print(${JSON.stringify(`${VISTA_STOP_MARKER}:${FIXED_NONCE_PLACEHOLDER}:`)} + json.dumps(payload, separators=(',', ':'), allow_nan=False))`,
].join("\n");

const FIXED_CLEANUP_SCRIPT = [
  "import json",
  "import unreal",
  `EXPECTED_BINDING = ${JSON.stringify(FIXED_BINDING_PLACEHOLDER)}`,
  `EXPECTED_SCENE = ${JSON.stringify(FIXED_SCENE_PLACEHOLDER)}`,
  "BINDING_TAG = 'SIMWORLD_VISTA_RUNTIME_V2_BINDING=' + EXPECTED_BINDING",
  "SCENE_TAG = 'SIMWORLD_VISTA_RUNTIME_V2_SCENE=' + EXPECTED_SCENE",
  "editor_subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)",
  "level_editor_subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)",
  "if editor_subsystem is None or level_editor_subsystem is None:",
  "    raise RuntimeError('required editor subsystem is unavailable')",
  "if bool(level_editor_subsystem.is_in_play_in_editor()):",
  "    raise RuntimeError('cannot clean runtime binding before PIE has ended')",
  "editor_world = editor_subsystem.get_editor_world()",
  "world_settings = editor_world.get_world_settings() if editor_world is not None else None",
  "if world_settings is None:",
  "    raise RuntimeError('editor world settings are unavailable')",
  "tags = [str(tag) for tag in list(world_settings.get_editor_property('tags'))]",
  "other_binding = [tag for tag in tags if tag.startswith('SIMWORLD_VISTA_RUNTIME_V2_BINDING=') and tag != BINDING_TAG]",
  "other_scene = [tag for tag in tags if tag.startswith('SIMWORLD_VISTA_RUNTIME_V2_SCENE=') and tag != SCENE_TAG]",
  "if other_binding or other_scene:",
  "    raise RuntimeError('refusing to remove another runtime binding')",
  "removed = BINDING_TAG in tags or SCENE_TAG in tags",
  "retained = [unreal.Name(tag) for tag in tags if tag not in [BINDING_TAG, SCENE_TAG]]",
  "if removed:",
  "    world_settings.set_editor_property('tags', retained)",
  "payload = {",
  `    'schema': ${JSON.stringify(VISTA_CLEANUP_SCHEMA)},`,
  "    'phase': 'cleaned',",
  "    'binding_removed': removed,",
  "}",
  `print(${JSON.stringify(`${VISTA_CLEANUP_MARKER}:${FIXED_NONCE_PLACEHOLDER}:`)} + json.dumps(payload, separators=(',', ':'), allow_nan=False))`,
].join("\n");

class VistaRuntimeError extends Error {
  constructor(code, message, { status = 500, retryable = false, retryAfterMs = null } = {}) {
    super(message);
    this.name = "VistaRuntimeError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    if (Number.isSafeInteger(retryAfterMs) && retryAfterMs > 0) this.retryAfterMs = retryAfterMs;
  }
}

class VistaProtocolError extends Error {
  constructor(code) {
    super(code);
    this.name = "VistaProtocolError";
    this.code = code;
  }
}

function fail(code, message, options) {
  throw new VistaRuntimeError(code, message, options);
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function hasStrictlyEmptyBody(body, headers = {}) {
  if (body === undefined) {
    const entries = headers && typeof headers === "object" ? Object.entries(headers) : [];
    const normalized = Object.fromEntries(entries.map(([key, value]) => [key.toLowerCase(), value]));
    if (normalized["transfer-encoding"] !== undefined) return false;
    const contentLength = normalized["content-length"];
    return contentLength === undefined || contentLength === "0" || contentLength === 0;
  }
  return isPlainObject(body) && Object.keys(body).length === 0;
}

function hasExactKeys(value, expectedKeys) {
  return isPlainObject(value)
    && Object.keys(value).length === expectedKeys.length
    && expectedKeys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function defaultNonceFactory() {
  return crypto.randomBytes(16).toString("hex");
}

function replaceExactlyOnce(source, from, to, label) {
  if (source.split(from).length !== 2) throw new TypeError(`fixed VISTA script must contain exactly one ${label}`);
  return source.replace(from, to);
}

function bindFixedRuntimeScript(script, marker, nonce, bindingDigest, sceneDigest, liveSurfaceDigest) {
  if (!/^[a-f0-9]{32}$/.test(nonce)) throw new TypeError("VISTA server nonce must be 128-bit lowercase hex");
  if (!SHA256_RE.test(bindingDigest) || !SHA256_RE.test(sceneDigest)
      || !SHA256_RE.test(liveSurfaceDigest)) {
    throw new TypeError("VISTA runtime proof digests must be lowercase SHA-256");
  }
  let bound = replaceExactlyOnce(
    script,
    `${marker}:${FIXED_NONCE_PLACEHOLDER}`,
    `${marker}:${nonce}`,
    "nonce placeholder",
  );
  bound = replaceExactlyOnce(bound, FIXED_BINDING_PLACEHOLDER, bindingDigest, "binding placeholder");
  bound = replaceExactlyOnce(bound, FIXED_SCENE_PLACEHOLDER, sceneDigest, "scene placeholder");
  if (bound.includes(FIXED_SURFACE_PLACEHOLDER)) {
    bound = replaceExactlyOnce(bound, FIXED_SURFACE_PLACEHOLDER, liveSurfaceDigest, "live surface placeholder");
  }
  return bound;
}

function pythonLogs(rawReply) {
  const candidates = [
    rawReply && rawReply.result && rawReply.result.python_logs,
    rawReply && rawReply.python_logs,
    rawReply && rawReply.result && rawReply.result.result && rawReply.result.result.python_logs,
  ];
  const logs = candidates.find((value) => Array.isArray(value));
  if (!logs || !logs.every((line) => typeof line === "string")) {
    throw new VistaProtocolError("VISTA_MARKER_MISSING");
  }
  return logs;
}

function extractSingleMarker(rawReply, marker) {
  const prefix = `${marker}:`;
  const matches = [];
  for (const line of pythonLogs(rawReply)) {
    let offset = 0;
    while (offset <= line.length) {
      const index = line.indexOf(prefix, offset);
      if (index < 0) break;
      matches.push(line.slice(index + prefix.length).trim());
      offset = index + prefix.length;
    }
  }
  if (matches.length !== 1) {
    throw new VistaProtocolError(matches.length === 0 ? "VISTA_MARKER_MISSING" : "VISTA_MARKER_DUPLICATE");
  }
  try {
    const payload = JSON.parse(matches[0]);
    if (!isPlainObject(payload)) throw new Error("marker payload must be an object");
    return payload;
  } catch {
    throw new VistaProtocolError("VISTA_MARKER_MALFORMED");
  }
}

function normalizeRuntimeIdentity(value) {
  if (!hasExactKeys(value, ["ownerId", "sessionId", "slotId", "leaseId", "mcpPort"])) {
    fail("VISTA_RUNTIME_IDENTITY_INVALID", "Active Studio lease identity is invalid", { status: 503, retryable: true });
  }
  const identity = {
    ownerId: String(value.ownerId || ""),
    sessionId: String(value.sessionId || ""),
    slotId: Number(value.slotId),
    leaseId: String(value.leaseId || ""),
    mcpPort: Number(value.mcpPort),
  };
  if (!PRINCIPAL_RE.test(identity.ownerId) || !PRINCIPAL_RE.test(identity.sessionId)
      || !PRINCIPAL_RE.test(identity.leaseId)
      || !Number.isSafeInteger(identity.slotId) || identity.slotId < 0 || identity.slotId > 1023
      || !Number.isSafeInteger(identity.mcpPort) || identity.mcpPort < 1 || identity.mcpPort > 65535) {
    fail("VISTA_RUNTIME_IDENTITY_INVALID", "Active Studio lease identity is invalid", { status: 503, retryable: true });
  }
  return Object.freeze(identity);
}

function runtimeBindingDigest(identity) {
  const normalized = normalizeRuntimeIdentity(identity);
  return crypto.createHash("sha256").update(JSON.stringify({
    schema: "vista-runtime-lease-binding/v1",
    owner_id: normalized.ownerId,
    session_id: normalized.sessionId,
    slot_id: String(normalized.slotId),
    lease_id: normalized.leaseId,
    mcp_port: normalized.mcpPort,
  }), "utf8").digest("hex");
}

function normalizeManifestRows(actorManifest) {
  if (!Array.isArray(actorManifest) || actorManifest.length < 1 || actorManifest.length > 10_000) {
    throw new TypeError("verified scene actor manifest is invalid");
  }
  const seen = new Set();
  const rows = actorManifest.map((entry) => {
    if (!isPlainObject(entry)) throw new TypeError("verified scene actor manifest is invalid");
    const row = {
      actor_name: String(entry.actor_name || ""),
      fingerprint: String(entry.fingerprint || ""),
      operation_id: String(entry.operation_id || ""),
      object_guid: String(entry.object_guid || ""),
    };
    if (!row.actor_name || row.actor_name.length > 256 || !ACTOR_FINGERPRINT_RE.test(row.fingerprint)
        || !ACTOR_OPERATION_RE.test(row.operation_id) || !PRINCIPAL_RE.test(row.object_guid)
        || seen.has(row.actor_name)) {
      throw new TypeError("verified scene actor manifest is invalid");
    }
    seen.add(row.actor_name);
    return row;
  });
  return rows.sort((left, right) => (
    left.actor_name.localeCompare(right.actor_name)
    || left.fingerprint.localeCompare(right.fingerprint)
    || left.operation_id.localeCompare(right.operation_id)
    || left.object_guid.localeCompare(right.object_guid)
  ));
}

function sceneManifestDigest(actorManifest) {
  const rows = normalizeManifestRows(actorManifest);
  // Keys are emitted in the same lexicographic order as Python sort_keys=True.
  const canonical = rows.map((row) => ({
    actor_name: row.actor_name,
    fingerprint: row.fingerprint,
    object_guid: row.object_guid,
    operation_id: row.operation_id,
  }));
  return crypto.createHash("sha256").update(JSON.stringify(canonical), "utf8").digest("hex");
}

function createVistaRuntimeSceneProof({
  planId,
  sceneId,
  actorManifest,
  contentRevision,
  verificationRevision,
  assetEvidenceDigest,
  semanticBindingDigest,
  materialPbrEvidenceDigest,
  evidenceBundleDigest,
  liveSurfaceDigest,
}) {
  const rows = normalizeManifestRows(actorManifest);
  const proof = {
    schema: VISTA_SCENE_PROOF_SCHEMA,
    plan_id: String(planId || ""),
    scene_id: String(sceneId || ""),
    actor_manifest_digest: sceneManifestDigest(rows),
    actor_count: rows.length,
    content_revision: String(contentRevision || ""),
    verification_revision: String(verificationRevision || ""),
    asset_evidence_digest: String(assetEvidenceDigest || ""),
    semantic_binding_digest: String(semanticBindingDigest || ""),
    material_pbr_evidence_digest: String(materialPbrEvidenceDigest || ""),
    evidence_bundle_digest: String(evidenceBundleDigest || ""),
    live_surface_digest: String(liveSurfaceDigest || ""),
    start_allowed: true,
  };
  return normalizeSceneProof(proof);
}

function normalizeSceneProof(value) {
  const keys = [
    "schema", "plan_id", "scene_id", "actor_manifest_digest", "actor_count",
    "content_revision", "verification_revision", "asset_evidence_digest",
    "semantic_binding_digest", "material_pbr_evidence_digest",
    "evidence_bundle_digest", "live_surface_digest", "start_allowed",
  ];
  if (!hasExactKeys(value, keys) || value.schema !== VISTA_SCENE_PROOF_SCHEMA
      || !PLAN_ID_RE.test(value.plan_id)
      || !PRINCIPAL_RE.test(value.scene_id)
      || !SHA256_RE.test(value.actor_manifest_digest)
      || !Number.isSafeInteger(value.actor_count) || value.actor_count < 1 || value.actor_count > 10_000
      || !PRINCIPAL_RE.test(value.content_revision)
      || !PRINCIPAL_RE.test(value.verification_revision)
      || !SHA256_RE.test(value.asset_evidence_digest)
      || !SHA256_RE.test(value.semantic_binding_digest)
      || !SHA256_RE.test(value.material_pbr_evidence_digest)
      || !SHA256_RE.test(value.evidence_bundle_digest)
      || !SHA256_RE.test(value.live_surface_digest)
      || value.start_allowed !== true) {
    fail("VISTA_SCENE_PROOF_INVALID", "Verified scene surface/content proof is invalid", { status: 409 });
  }
  return Object.freeze({ ...value });
}

function validateSetupPayload(payload, sceneProof) {
  const keys = [
    "schema", "phase", "play_requested", "was_playing", "game_mode_class",
    "pawn_class", "default_pawn_class", "player_start_count", "scene_actor_count",
    "surface_actor_count", "scene_manifest_digest",
  ];
  if (!hasExactKeys(payload, keys) || payload.schema !== VISTA_SETUP_SCHEMA
      || payload.phase !== "play_requested" || payload.play_requested !== true
      || typeof payload.was_playing !== "boolean"
      || payload.game_mode_class !== VISTA_GAME_MODE_CLASS
      || payload.pawn_class !== VISTA_PAWN_CLASS
      || payload.default_pawn_class !== VISTA_PAWN_CLASS
      || !Number.isSafeInteger(payload.player_start_count) || payload.player_start_count < 1 || payload.player_start_count > 10_000
      || payload.scene_actor_count !== sceneProof.actor_count
      || !Number.isSafeInteger(payload.surface_actor_count) || payload.surface_actor_count < 1
      || payload.surface_actor_count > payload.scene_actor_count
      || payload.scene_manifest_digest !== sceneProof.actor_manifest_digest) {
    throw new VistaProtocolError("VISTA_SETUP_INVALID");
  }
  return Object.freeze({ ...payload });
}

function validateStopPayload(payload) {
  if (!hasExactKeys(payload, ["schema", "phase", "stop_requested", "was_playing"])
      || payload.schema !== VISTA_STOP_SCHEMA || payload.phase !== "stop_requested"
      || payload.stop_requested !== true || typeof payload.was_playing !== "boolean") {
    throw new VistaProtocolError("VISTA_STOP_INVALID");
  }
  return Object.freeze({ ...payload });
}

function validateCleanupPayload(payload) {
  if (!hasExactKeys(payload, ["schema", "phase", "binding_removed"])
      || payload.schema !== VISTA_CLEANUP_SCHEMA || payload.phase !== "cleaned"
      || typeof payload.binding_removed !== "boolean") {
    throw new VistaProtocolError("VISTA_CLEANUP_INVALID");
  }
  return Object.freeze({ ...payload });
}

function validateTriple(value, bound) {
  if (!Array.isArray(value) || value.length !== 3
      || !value.every((item) => Number.isFinite(item) && Math.abs(item) <= bound)) {
    throw new VistaProtocolError("VISTA_STATE_VECTOR_INVALID");
  }
  return value.map(Number);
}

function validateStatePayload(payload) {
  const keys = [
    "schema", "pie", "possessed", "pawn_class", "location", "rotation",
    "velocity", "on_ground", "engine_time",
  ];
  if (!hasExactKeys(payload, keys)) throw new VistaProtocolError("VISTA_STATE_SHAPE_INVALID");
  if (payload.schema !== VISTA_STATE_SCHEMA) throw new VistaProtocolError("VISTA_STATE_NOT_READY");
  if (payload.pie === false && payload.possessed === false) {
    if ([payload.pawn_class, payload.location, payload.rotation, payload.velocity,
      payload.on_ground, payload.engine_time].some((value) => value !== null)) {
      throw new VistaProtocolError("VISTA_STATE_STOPPED_INVALID");
    }
    return Object.freeze({ ...payload });
  }
  if (payload.pie !== true || payload.possessed !== true) {
    throw new VistaProtocolError("VISTA_STATE_NOT_READY");
  }
  if (payload.pawn_class !== VISTA_PAWN_CLASS) throw new VistaProtocolError("VISTA_PAWN_CLASS_MISMATCH");
  const result = {
    schema: VISTA_STATE_SCHEMA,
    pie: true,
    possessed: true,
    pawn_class: VISTA_PAWN_CLASS,
    location: validateTriple(payload.location, STATE_LIMITS.location),
    rotation: validateTriple(payload.rotation, STATE_LIMITS.rotation),
    velocity: validateTriple(payload.velocity, STATE_LIMITS.velocity),
    on_ground: payload.on_ground,
    engine_time: Number(payload.engine_time),
  };
  if (typeof result.on_ground !== "boolean") throw new VistaProtocolError("VISTA_STATE_GROUND_INVALID");
  if (!Number.isFinite(result.engine_time) || result.engine_time < 0 || result.engine_time > STATE_LIMITS.engineTime) {
    throw new VistaProtocolError("VISTA_STATE_TIME_INVALID");
  }
  return Object.freeze(result);
}

function sanitizedTransportError(error) {
  if (error instanceof VistaRuntimeError) return error;
  if (error instanceof VistaProtocolError) {
    return new VistaRuntimeError(
      error.code === "VISTA_PAWN_CLASS_MISMATCH" ? error.code : "VISTA_RUNTIME_PROTOCOL_ERROR",
      "VISTA runtime returned an invalid fixed response",
      { status: 502 },
    );
  }
  if (error && error.code === "UE_COMMAND_AUTHORIZATION_EXPIRED") {
    return new VistaRuntimeError("VISTA_RUNTIME_LEASE_REVOKED", "Studio runtime lease expired before UE dispatch", {
      status: 409,
      retryable: true,
    });
  }
  const retryAfterMs = Number.isFinite(error && error.retryAfterMs)
    ? Math.min(Math.max(1, Math.ceil(error.retryAfterMs)), 60_000)
    : null;
  return new VistaRuntimeError(
    retryAfterMs ? "VISTA_RUNTIME_BUSY" : "VISTA_RUNTIME_UNAVAILABLE",
    retryAfterMs ? "VISTA runtime is busy" : "VISTA runtime is unavailable",
    { status: retryAfterMs ? 429 : 503, retryable: true, retryAfterMs },
  );
}

function delay(ms, signal) {
  if (signal && signal.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    let settled = false;
    const cleanup = () => {
      if (signal) signal.removeEventListener("abort", onAbort);
    };
    const finish = () => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve();
    };
    const timer = setTimeout(finish, ms);
    const onAbort = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      cleanup();
      reject(abortError());
    };
    if (signal) signal.addEventListener("abort", onAbort, { once: true });
  });
}

function abortError() {
  const error = new VistaRuntimeError("VISTA_RUNTIME_ABORTED", "VISTA runtime operation was aborted", {
    status: 409,
    retryable: true,
  });
  error.name = "AbortError";
  return error;
}

function sameIdentity(left, right) {
  return left.ownerId === right.ownerId && left.sessionId === right.sessionId
    && left.slotId === right.slotId && left.leaseId === right.leaseId
    && left.mcpPort === right.mcpPort;
}

class VistaRuntimeController {
  constructor(options) {
    this.identity = normalizeRuntimeIdentity(options.identity);
    this.bindingDigest = runtimeBindingDigest(this.identity);
    this.resolveUeBroker = options.resolveUeBroker;
    this.isActiveSessionBinding = options.isActiveSessionBinding;
    this.nonceFactory = options.nonceFactory || defaultNonceFactory;
    this.now = options.now || (() => Date.now());
    this.delay = options.delay || delay;
    this.pollIntervalMs = options.pollIntervalMs || DEFAULT_POLL_INTERVAL_MS;
    this.startTimeoutMs = options.startTimeoutMs || DEFAULT_START_TIMEOUT_MS;
    this.stopTimeoutMs = options.stopTimeoutMs || DEFAULT_STOP_TIMEOUT_MS;
    this.stateCacheMs = options.stateCacheMs || DEFAULT_STATE_CACHE_MS;
    this.phase = "unknown";
    this.sceneProof = null;
    this.generation = 0;
    this.startInFlight = null;
    this.stopInFlight = null;
    this.stateInFlight = null;
    this.stateCache = null;
    this.lifetime = new AbortController();
  }

  quarantine() {
    if (this.phase === "quarantined") return;
    this.phase = "quarantined";
    this.generation += 1;
    this.stateCache = null;
    this.lifetime.abort("runtime controller quarantined");
  }

  async assertActive() {
    if (this.phase === "quarantined" || this.lifetime.signal.aborted) {
      fail("VISTA_RUNTIME_QUARANTINED", "VISTA runtime ownership must be reconciled", { status: 409 });
    }
    let active = false;
    try { active = await this.isActiveSessionBinding(this.identity); } catch { active = false; }
    if (active !== true) {
      this.quarantine();
      fail("VISTA_RUNTIME_LEASE_REVOKED", "Studio runtime lease is no longer active", {
        status: 409,
        retryable: true,
      });
    }
  }

  async broker() {
    await this.assertActive();
    let broker = null;
    try { broker = await this.resolveUeBroker(this.identity); } catch { broker = null; }
    if (!broker || typeof broker.send !== "function") {
      fail("VISTA_RUNTIME_UNAVAILABLE", "Lease-bound UE runtime is unavailable", {
        status: 503,
        retryable: true,
      });
    }
    return broker;
  }

  async execute(scriptTemplate, marker, sceneProof, validate, { mutation, signal }) {
    const broker = await this.broker();
    const nonce = this.nonceFactory();
    const script = bindFixedRuntimeScript(
      scriptTemplate,
      marker,
      nonce,
      this.bindingDigest,
      sceneProof.actor_manifest_digest,
      sceneProof.live_surface_digest,
    );
    const operationController = new AbortController();
    const abort = () => operationController.abort("runtime operation aborted");
    if (this.lifetime.signal.aborted || (signal && signal.aborted)) abort();
    else {
      this.lifetime.signal.addEventListener("abort", abort, { once: true });
      if (signal) signal.addEventListener("abort", abort, { once: true });
    }
    try {
      const raw = await broker.send("execute_python_script", { script }, {
        timeoutMs: SCRIPT_TIMEOUT_MS,
        queueDeadlineMs: SCRIPT_TIMEOUT_MS * 2,
        maxAttempts: mutation ? 1 : 2,
        maxResponseBytes: MAX_RESPONSE_BYTES,
        signal: operationController.signal,
        preSendAuthorize: async () => {
          if (this.phase === "quarantined" || this.lifetime.signal.aborted) return false;
          let active = false;
          try { active = await this.isActiveSessionBinding(this.identity); } catch { active = false; }
          if (!active) return false;
          try { return await this.resolveUeBroker(this.identity) === broker; } catch { return false; }
        },
      });
      return validate(extractSingleMarker(raw, `${marker}:${nonce}`));
    } catch (error) {
      throw sanitizedTransportError(error);
    } finally {
      this.lifetime.signal.removeEventListener("abort", abort);
      if (signal) signal.removeEventListener("abort", abort);
    }
  }

  async readState({ force = false, signal, sceneProof } = {}) {
    await this.assertActive();
    const proof = normalizeSceneProof(sceneProof || this.sceneProof || {});
    const now = this.now();
    if (!force && this.stateCache && now < this.stateCache.expiresAt) return this.stateCache.value;
    if (!force && this.stateInFlight) return this.stateInFlight;
    const generation = this.generation;
    const read = this.execute(
      FIXED_STATE_SCRIPT,
      VISTA_STATE_MARKER,
      proof,
      validateStatePayload,
      { mutation: false, signal },
    ).then((state) => {
      if (generation === this.generation && !new Set(["starting", "stopping"]).has(this.phase)) {
        this.phase = state.pie ? "live" : "stopped";
        this.stateCache = { value: state, expiresAt: this.now() + this.stateCacheMs };
      }
      return state;
    }).finally(() => {
      if (this.stateInFlight === read) this.stateInFlight = null;
    });
    if (!force) this.stateInFlight = read;
    return read;
  }

  async waitForState(predicate, { deadlineMs, signal, sceneProof }) {
    let lastError = null;
    while (this.now() <= deadlineMs) {
      if (signal && signal.aborted) throw abortError();
      try {
        const state = await this.readState({ force: true, signal, sceneProof });
        if (predicate(state)) return state;
      } catch (error) {
        lastError = error;
        if (error instanceof VistaRuntimeError
            && new Set(["VISTA_RUNTIME_LEASE_REVOKED", "VISTA_RUNTIME_PROTOCOL_ERROR"]).has(error.code)) {
          throw error;
        }
      }
      if (this.now() >= deadlineMs) break;
      await this.delay(this.pollIntervalMs, signal);
    }
    if (lastError && lastError.code === "VISTA_RUNTIME_ABORTED") throw lastError;
    fail("VISTA_RUNTIME_CONFIRMATION_TIMEOUT", "UE runtime transition was not confirmed", {
      status: 503,
      retryable: true,
    });
  }

  async start({ sceneProof, signal } = {}) {
    const proof = normalizeSceneProof(sceneProof);
    if (this.phase === "quarantined") {
      fail("VISTA_RUNTIME_QUARANTINED", "VISTA runtime ownership must be reconciled", { status: 409 });
    }
    await this.assertActive();
    if (this.startInFlight) return this.startInFlight;
    if (this.stopInFlight) {
      fail("VISTA_STOP_IN_PROGRESS", "VISTA Stop must finish before Start", {
        status: 425,
        retryable: true,
        retryAfterMs: this.pollIntervalMs,
      });
    }
    this.sceneProof = proof;
    const generation = ++this.generation;
    this.phase = "starting";
    this.stateCache = null;
    const operation = (async () => {
      let setup = null;
      try {
        const current = await this.readState({ force: true, signal, sceneProof: proof });
        if (current.pie) return this.setupResponse(proof, current, true, false);
        setup = await this.execute(
          FIXED_SETUP_SCRIPT,
          VISTA_SETUP_MARKER,
          proof,
          (payload) => validateSetupPayload(payload, proof),
          { mutation: true, signal },
        );
      } catch (error) {
        // The begin-play mutation may have succeeded before its response was
        // lost. Only an exact lease+scene-bound state read may recover it.
        if (error instanceof VistaRuntimeError
            && new Set(["VISTA_RUNTIME_LEASE_REVOKED", "VISTA_RUNTIME_PROTOCOL_ERROR"]).has(error.code)) throw error;
      }
      const state = await this.waitForState(
        (value) => value.pie === true && value.possessed === true,
        { deadlineMs: this.now() + this.startTimeoutMs, signal, sceneProof: proof },
      );
      if (generation !== this.generation || this.phase === "stopping") {
        fail("VISTA_SETUP_SUPERSEDED", "VISTA Start was superseded by Stop", { status: 409 });
      }
      this.phase = "live";
      return this.setupResponse(proof, state, Boolean(setup && setup.was_playing), Boolean(setup));
    })().finally(() => {
      if (this.startInFlight === operation) this.startInFlight = null;
    });
    this.startInFlight = operation;
    return operation;
  }

  setupResponse(proof, state, wasPlaying, playRequested) {
    return Object.freeze({
      schema: VISTA_SETUP_SCHEMA,
      phase: "live",
      prepared: true,
      play_requested: playRequested,
      already_playing: wasPlaying,
      pie: state.pie,
      possessed: state.possessed,
      game_mode_class: VISTA_GAME_MODE_CLASS,
      pawn_class: VISTA_PAWN_CLASS,
      player_start_present: true,
      scene_proof_digest: proof.actor_manifest_digest,
      play_lease_granted: true,
    });
  }

  async stop({ signal } = {}) {
    if (this.phase === "quarantined") {
      fail("VISTA_RUNTIME_QUARANTINED", "VISTA runtime ownership must be reconciled", { status: 409 });
    }
    await this.assertActive();
    if (this.stopInFlight) return this.stopInFlight;
    if (!this.sceneProof) {
      fail("VISTA_SCENE_PROOF_REQUIRED", "A verified scene proof is required to stop this runtime", { status: 409 });
    }
    // Capture and invalidate Start synchronously, then use its settlement as a
    // serialization barrier. Stop must never overtake a begin-PIE mutation
    // whose transport receipt or exact live-state reconciliation is pending.
    const starting = this.startInFlight;
    const generation = ++this.generation;
    this.phase = "stopping";
    this.stateCache = null;
    const proof = this.sceneProof;
    const operation = (async () => {
      if (starting) await starting.catch(() => {});
      await this.assertActive();
      if (generation !== this.generation || this.phase !== "stopping") {
        fail("VISTA_STOP_SUPERSEDED", "VISTA Stop ownership changed before dispatch", { status: 409 });
      }
      let stopReceipt = null;
      try {
        stopReceipt = await this.execute(
          FIXED_STOP_SCRIPT,
          VISTA_STOP_MARKER,
          proof,
          validateStopPayload,
          { mutation: true, signal },
        );
      } catch (error) {
        if (error instanceof VistaRuntimeError
            && new Set(["VISTA_RUNTIME_LEASE_REVOKED", "VISTA_RUNTIME_PROTOCOL_ERROR"]).has(error.code)) throw error;
      }
      const state = await this.waitForState(
        (value) => value.pie === false && value.possessed === false,
        { deadlineMs: this.now() + this.stopTimeoutMs, signal, sceneProof: proof },
      );
      if (generation !== this.generation) {
        fail("VISTA_STOP_SUPERSEDED", "VISTA Stop ownership changed before confirmation", { status: 409 });
      }
      const cleanupReceipt = await this.execute(
        FIXED_CLEANUP_SCRIPT,
        VISTA_CLEANUP_MARKER,
        proof,
        validateCleanupPayload,
        { mutation: false, signal },
      );
      this.phase = "stopped";
      this.stateCache = { value: state, expiresAt: this.now() + this.stateCacheMs };
      const wasPlaying = stopReceipt ? stopReceipt.was_playing : null;
      return Object.freeze({
        schema: VISTA_STOP_SCHEMA,
        phase: "stopped",
        stop_requested: true,
        was_playing: wasPlaying,
        confirmed_stopped: true,
        ended_pie: stopReceipt !== null && stopReceipt.was_playing === true,
        binding_cleaned: cleanupReceipt.binding_removed,
      });
    })().finally(() => {
      if (this.stopInFlight === operation) this.stopInFlight = null;
    });
    this.stopInFlight = operation;
    return operation;
  }
}

function createVistaRuntimeControllerRegistry(options = {}) {
  for (const method of ["resolveIdentity", "resolveUeBroker", "isActiveSessionBinding", "resolveSceneProof"]) {
    if (typeof options[method] !== "function") throw new TypeError(`${method} must be a function`);
  }
  const controllers = new Map();
  const currentSlotLease = new Map();

  function identityKey(identity) {
    return runtimeBindingDigest(identity);
  }

  function slotKey(identity) {
    return crypto.createHash("sha256").update(JSON.stringify({
      slot_id: identity.slotId,
      mcp_port: identity.mcpPort,
    }), "utf8").digest("hex");
  }

  function prune() {
    while (controllers.size > (options.maxControllers || MAX_CONTROLLERS)) {
      const [key, controller] = controllers.entries().next().value;
      controller.quarantine();
      controllers.delete(key);
    }
  }

  function controllerFor(rawIdentity) {
    const identity = normalizeRuntimeIdentity(rawIdentity);
    const key = identityKey(identity);
    const slot = slotKey(identity);
    const previous = currentSlotLease.get(slot);
    if (previous && previous !== key && controllers.has(previous)) {
      controllers.get(previous).quarantine();
      controllers.delete(previous);
    }
    currentSlotLease.set(slot, key);
    if (!controllers.has(key)) {
      controllers.set(key, new VistaRuntimeController({
        identity,
        resolveUeBroker: options.resolveUeBroker,
        isActiveSessionBinding: options.isActiveSessionBinding,
        nonceFactory: options.nonceFactory,
        now: options.now,
        delay: options.delay,
        pollIntervalMs: options.pollIntervalMs,
        startTimeoutMs: options.startTimeoutMs,
        stopTimeoutMs: options.stopTimeoutMs,
        stateCacheMs: options.stateCacheMs,
      }));
      prune();
    }
    return controllers.get(key);
  }

  async function exactIdentityFromRequest(request) {
    let identity = null;
    try { identity = await options.resolveIdentity(request); } catch { identity = null; }
    return normalizeRuntimeIdentity(identity || {});
  }

  async function sceneProofFor(identity) {
    let proof = null;
    try { proof = await options.resolveSceneProof(identity); } catch (error) {
      if (error instanceof VistaRuntimeError) throw error;
      proof = null;
    }
    if (!proof) {
      fail("VISTA_SCENE_PROOF_REQUIRED", "Build and verify a scene before starting VISTA Play", {
        status: 409,
      });
    }
    return normalizeSceneProof(proof);
  }

  async function startForIdentity(rawIdentity, { sceneProof, signal } = {}) {
    const identity = normalizeRuntimeIdentity(rawIdentity);
    const proof = sceneProof ? normalizeSceneProof(sceneProof) : await sceneProofFor(identity);
    return controllerFor(identity).start({ sceneProof: proof, signal });
  }

  async function stateForIdentity(rawIdentity, { sceneProof, signal, force = false } = {}) {
    const identity = normalizeRuntimeIdentity(rawIdentity);
    const controller = controllerFor(identity);
    const proof = sceneProof ? normalizeSceneProof(sceneProof)
      : (controller.sceneProof || await sceneProofFor(identity));
    controller.sceneProof = proof;
    return controller.readState({ force, signal, sceneProof: proof });
  }

  async function stopForIdentity(rawIdentity, { signal } = {}) {
    const identity = normalizeRuntimeIdentity(rawIdentity);
    const controller = controllerFor(identity);
    if (!controller.sceneProof) controller.sceneProof = await sceneProofFor(identity);
    return controller.stop({ signal });
  }

  return Object.freeze({
    controllerFor,
    exactIdentityFromRequest,
    sceneProofFor,
    startForIdentity,
    stateForIdentity,
    stopForIdentity,
  });
}

// Compatibility entrypoint for focused tests and internal composition. Unlike
// the removed global singleton, every instance is permanently bound to one
// validated lease identity and one verified scene proof.
function createVistaRuntimeBroker(options = {}) {
  const identity = normalizeRuntimeIdentity(options.identity);
  const sceneProof = normalizeSceneProof(options.sceneProof);
  const controller = new VistaRuntimeController({ ...options, identity });
  controller.sceneProof = sceneProof;
  return Object.freeze({
    start: ({ signal } = {}) => controller.start({ sceneProof, signal }),
    state: ({ signal, force = false } = {}) => controller.readState({ signal, force, sceneProof }),
    stop: ({ signal } = {}) => controller.stop({ signal }),
    quarantine: () => controller.quarantine(),
  });
}

module.exports = {
  DEFAULT_POLL_INTERVAL_MS,
  DEFAULT_START_TIMEOUT_MS,
  DEFAULT_STATE_CACHE_MS,
  DEFAULT_STOP_TIMEOUT_MS,
  FIXED_BINDING_PLACEHOLDER,
  FIXED_CLEANUP_SCRIPT,
  FIXED_NONCE_PLACEHOLDER,
  FIXED_SCENE_PLACEHOLDER,
  FIXED_SURFACE_PLACEHOLDER,
  FIXED_SETUP_SCRIPT,
  FIXED_STATE_SCRIPT,
  FIXED_STOP_SCRIPT,
  STATE_LIMITS,
  VISTA_GAME_MODE_CLASS,
  VISTA_PAWN_CLASS,
  VISTA_SCENE_PROOF_SCHEMA,
  VISTA_CLEANUP_MARKER,
  VISTA_CLEANUP_SCHEMA,
  VISTA_SETUP_MARKER,
  VISTA_SETUP_ROUTE,
  VISTA_SETUP_SCHEMA,
  VISTA_STATE_MARKER,
  VISTA_STATE_ROUTE,
  VISTA_STATE_SCHEMA,
  VISTA_STOP_MARKER,
  VISTA_STOP_ROUTE,
  VISTA_STOP_SCHEMA,
  VistaProtocolError,
  VistaRuntimeError,
  bindFixedRuntimeScript,
  createVistaRuntimeBroker,
  createVistaRuntimeControllerRegistry,
  createVistaRuntimeSceneProof,
  extractSingleMarker,
  hasStrictlyEmptyBody,
  normalizeRuntimeIdentity,
  normalizeSceneProof,
  runtimeBindingDigest,
  sceneManifestDigest,
  validateSetupPayload,
  validateCleanupPayload,
  validateStatePayload,
  validateStopPayload,
};
