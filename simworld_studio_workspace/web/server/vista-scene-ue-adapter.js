"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const { PREFLIGHT_RESPONSE_SCHEMA } = require("./vista-scene-executor");

const SAFE_REVISION = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const MARKER_PREFIX = "VISTA_SCENE_UE_V1";
const DEFAULT_TIMEOUT_MS = 30_000;
const MAX_SCREENSHOT_BYTES = 32 * 1024 * 1024;

class VistaSceneUeAdapterError extends Error {
  constructor(code, message, options = {}) {
    super(message);
    this.name = "VistaSceneUeAdapterError";
    this.code = code;
    this.retryable = options.retryable === true;
  }
}

function fail(code, message, options) {
  throw new VistaSceneUeAdapterError(code, message, options);
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function requireRevision(value, field) {
  const revision = typeof value === "string" ? value.trim() : "";
  if (!SAFE_REVISION.test(revision)) throw new TypeError(`${field} is invalid`);
  return revision;
}

function pythonJson(value) {
  return JSON.stringify(JSON.stringify(value));
}

function pythonLogs(raw) {
  const candidates = [
    raw && raw.result && raw.result.python_logs,
    raw && raw.python_logs,
    raw && raw.result && raw.result.result && raw.result.result.python_logs,
  ];
  const logs = candidates.find((value) => Array.isArray(value));
  if (!logs || !logs.every((line) => typeof line === "string")) {
    fail("SCENE_BUILD_UE_MARKER_MISSING", "UE did not return the fixed scene marker", { retryable: true });
  }
  return logs;
}

function extractMarker(raw, marker) {
  const prefix = `${marker}:`;
  const matches = [];
  for (const line of pythonLogs(raw)) {
    const index = line.indexOf(prefix);
    if (index >= 0) matches.push(line.slice(index + prefix.length).trim());
  }
  if (matches.length !== 1) {
    fail(matches.length ? "SCENE_BUILD_UE_MARKER_DUPLICATE" : "SCENE_BUILD_UE_MARKER_MISSING", "UE fixed scene marker is invalid", { retryable: true });
  }
  try {
    const payload = JSON.parse(matches[0]);
    if (!isPlainObject(payload)) throw new Error("marker payload is not an object");
    return payload;
  } catch (_error) {
    fail("SCENE_BUILD_UE_MARKER_INVALID", "UE fixed scene marker is malformed", { retryable: false });
  }
}

function markerScript(operation, nonce, payload, lines) {
  const marker = `${MARKER_PREFIX}_${operation}:${nonce}`;
  return {
    marker,
    script: [
      "import json",
      "import unreal",
      `REQUEST = json.loads(${pythonJson(payload)})`,
      ...lines,
      `print(${JSON.stringify(`${marker}:`)} + json.dumps(RESULT, separators=(',', ':'), allow_nan=False))`,
    ].join("\n"),
  };
}

function preflightScript(nonce, request, expectedContent) {
  return markerScript("PREFLIGHT", nonce, { request, expected_content: expectedContent }, [
    "req = REQUEST['request']",
    "expected_content = REQUEST['expected_content']",
    "revision_matches = False",
    "try:",
    "    import hashlib",
    "    receipt_path = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir() + 'Content/VISTA/Metadata/content-revision.json')",
    "    with open(receipt_path, 'rb') as handle: receipt_bytes = handle.read(65537)",
    "    receipt = json.loads(receipt_bytes.decode('utf-8')) if len(receipt_bytes) <= 65536 else {}",
    "    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()",
    "    revision_matches = set(receipt.keys()) == {'schema', 'content_revision', 'verification_revision'} and receipt.get('schema') == 'simworld-ue-content-receipt/v1' and receipt.get('content_revision') == expected_content['content_revision'] and receipt.get('verification_revision') == expected_content['verification_revision'] and receipt_digest == expected_content['receipt_sha256']",
    "except Exception: revision_matches = False",
    "asset_rows = []",
    "for item in req['assets']:",
    "    loaded = None",
    "    class_matches = False",
    "    if item['class_path'] == '/Script/Engine.StaticMeshActor':",
    "        loaded = unreal.load_asset(item['ue_path'])",
    "        class_matches = loaded is not None and isinstance(loaded, unreal.StaticMesh)",
    "    else:",
    "        loaded = unreal.load_class(None, item['ue_path'])",
    "        class_matches = loaded is not None and loaded.get_path_name() == item['class_path']",
    "    asset_rows.append({",
    "        'asset_key': item['asset_key'],",
    "        'available': loaded is not None,",
    "        'class_matches': bool(class_matches),",
    "        'revision_matches': bool(revision_matches and item['content_revision'] == expected_content['content_revision'] and item['verification_revision'] == expected_content['verification_revision']),",
    "    })",
    "subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "all_actors = list(subsystem.get_all_level_actors()) if subsystem else []",
    "def actor_label(actor):",
    "    try: return str(actor.get_actor_label())",
    "    except Exception: return str(actor.get_name())",
    "def fingerprint(actor):",
    "    try:",
    "        for tag in list(actor.get_editor_property('tags')):",
    "            text = str(tag)",
    "            if text.startswith('VISTA_FINGERPRINT='): return text.split('=', 1)[1]",
    "    except Exception: pass",
    "    return None",
    "def operation_id(actor):",
    "    try:",
    "        for tag in list(actor.get_editor_property('tags')):",
    "            text = str(tag)",
    "            if text.startswith('VISTA_OPERATION='): return text.split('=', 1)[1]",
    "    except Exception: pass",
    "    return None",
    "def object_guid(actor):",
    "    try: return str(actor.get_actor_guid()).strip('{}')",
    "    except Exception: return None",
    "def close_vectors(left, right):",
    "    return len(left) == len(right) and all(abs(float(a) - float(b)) <= 0.01 for a, b in zip(left, right))",
    "def collision_mode(component):",
    "    text = str(component.get_collision_enabled()).lower().replace('_', '')",
    "    if 'queryandphysics' in text: return 'query_and_physics'",
    "    if 'queryonly' in text: return 'query_only'",
    "    if 'nocollision' in text: return 'disabled'",
    "    return 'unknown'",
    "def mobility_name(component):",
    "    return str(component.get_editor_property('mobility')).split('.')[-1].lower()",
    "def spec_matches(actor, expected):",
    "    try:",
    "        actual_class = str(actor.get_class().get_path_name())",
    "        if expected['asset_kind'] == 'static_mesh':",
    "            if actual_class != expected['class_path']: return False",
    "            mesh_components = list(actor.get_components_by_class(unreal.StaticMeshComponent))",
    "            mesh_paths = [str(component.get_editor_property('static_mesh').get_path_name()) for component in mesh_components if component.get_editor_property('static_mesh') is not None]",
    "            if len(mesh_paths) != 1 or mesh_paths[0] != expected['ue_path']: return False",
    "        elif actual_class != expected['class_path'] or actual_class != expected['ue_path']: return False",
    "        loc = actor.get_actor_location(); rot = actor.get_actor_rotation(); scale = actor.get_actor_scale3d()",
    "        transform = expected['transform']",
    "        if not close_vectors([loc.x, loc.y, loc.z], transform['location_cm']): return False",
    "        if not close_vectors([rot.pitch, rot.yaw, rot.roll], transform['rotation_deg']): return False",
    "        if not close_vectors([scale.x, scale.y, scale.z], transform['scale']): return False",
    "        components = list(actor.get_components_by_class(unreal.PrimitiveComponent))",
    "        if not components: return False",
    "        collision = expected['collision']",
    "        return all(mobility_name(component) == expected['mobility'] and collision_mode(component) == collision['mode'] and str(component.get_collision_profile_name()) == collision['profile_name'] and bool(component.get_editor_property('generate_overlap_events')) == bool(collision['generate_overlap_events']) for component in components)",
    "    except Exception: return False",
    "actor_rows = []",
    "for item in req['actors']:",
    "    matches = [actor for actor in all_actors if actor_label(actor) == item['actor_name'] or str(actor.get_name()) == item['actor_name']]",
    "    actual = fingerprint(matches[0]) if len(matches) == 1 else None",
    "    actual_operation = operation_id(matches[0]) if len(matches) == 1 else None",
    "    guid = object_guid(matches[0]) if len(matches) == 1 else None",
    "    matches_spec = spec_matches(matches[0], item) if len(matches) == 1 else False",
    "    state = 'absent' if len(matches) == 0 else ('exact_match' if len(matches) == 1 and actual == item['fingerprint'] and actual_operation == item['operation_id'] and matches_spec and guid else 'conflict')",
    "    actor_rows.append({'actor_name': item['actor_name'], 'state': state, 'actual_fingerprint': actual, 'actual_operation_id': actual_operation, 'object_guid': guid, 'spec_matches': bool(matches_spec)})",
    "ps_req = req['player_start']",
    "player_matches = [actor for actor in all_actors if isinstance(actor, unreal.PlayerStart) and (actor_label(actor) == ps_req['actor_name'] or str(actor.get_name()) == ps_req['actor_name'])]",
    "player_row = {'actor_name': ps_req['actor_name'], 'class_path': ps_req['class_path'], 'state': 'unavailable', 'current_transform': None, 'object_guid': None}",
    "if len(player_matches) == 1:",
    "    player = player_matches[0]",
    "    loc = player.get_actor_location()",
    "    rot = player.get_actor_rotation()",
    "    scale = player.get_actor_scale3d()",
    "    current = {'location_cm': [float(loc.x), float(loc.y), float(loc.z)], 'rotation_deg': [float(rot.pitch), float(rot.yaw), float(rot.roll)], 'scale': [float(scale.x), float(scale.y), float(scale.z)]}",
    "    desired = ps_req['transform']",
    "    flat_current = current['location_cm'] + current['rotation_deg'] + current['scale']",
    "    flat_desired = desired['location_cm'] + desired['rotation_deg'] + desired['scale']",
    "    exact = all(abs(float(a) - float(b)) <= 0.01 for a, b in zip(flat_current, flat_desired))",
    "    player_row = {'actor_name': ps_req['actor_name'], 'class_path': ps_req['class_path'], 'state': 'exact_match' if exact else 'needs_update', 'current_transform': current, 'object_guid': str(player.get_actor_guid()).strip('{}')}",
    "ok = all(row['available'] and row['class_matches'] and row['revision_matches'] for row in asset_rows) and all(row['state'] != 'conflict' for row in actor_rows) and player_row['state'] != 'unavailable'",
    `RESULT = {'schema': ${JSON.stringify(PREFLIGHT_RESPONSE_SCHEMA)}, 'plan_id': req['plan_id'], 'ok': bool(ok), 'assets': asset_rows, 'actors': actor_rows, 'player_start': player_row}`,
  ]);
}

function spawnScript(nonce, actor) {
  return markerScript("SPAWN", nonce, { actor }, [
    "item = REQUEST['actor']",
    "subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "if subsystem is None: raise RuntimeError('editor actor subsystem unavailable')",
    "def actor_label(value):",
    "    try: return str(value.get_actor_label())",
    "    except Exception: return str(value.get_name())",
    "if any(actor_label(value) == item['actor_name'] or str(value.get_name()) == item['actor_name'] for value in subsystem.get_all_level_actors()):",
    "    raise RuntimeError('deterministic actor name became occupied after preflight')",
    "loc = unreal.Vector(*[float(value) for value in item['transform']['location_cm']])",
    "rotation = [float(value) for value in item['transform']['rotation_deg']]",
    "rot = unreal.Rotator(pitch=rotation[0], yaw=rotation[1], roll=rotation[2])",
    "created = None",
    "try:",
    "    if item['asset']['kind'] == 'static_mesh':",
    "        asset = unreal.load_asset(item['asset']['ue_path'])",
    "        if asset is None or not isinstance(asset, unreal.StaticMesh): raise RuntimeError('pinned static mesh unavailable')",
    "        created = subsystem.spawn_actor_from_object(asset, loc, rot, transient=False)",
    "    else:",
    "        asset = unreal.load_class(None, item['asset']['class_path'])",
    "        if asset is None: raise RuntimeError('pinned blueprint class unavailable')",
    "        created = subsystem.spawn_actor_from_class(asset, loc, rot, transient=False)",
    "    if created is None: raise RuntimeError('actor spawn returned none')",
    "    created.set_actor_label(item['actor_name'])",
    "    created.set_actor_location(loc, False, False)",
    "    created.set_actor_rotation(rot, False)",
    "    created.set_actor_scale3d(unreal.Vector(*[float(value) for value in item['transform']['scale']]))",
    "    mobility = {'static': unreal.ComponentMobility.STATIC, 'stationary': unreal.ComponentMobility.STATIONARY, 'movable': unreal.ComponentMobility.MOVABLE}[item['mobility']]",
    "    collision = {'query_and_physics': unreal.CollisionEnabled.QUERY_AND_PHYSICS, 'query_only': unreal.CollisionEnabled.QUERY_ONLY, 'disabled': unreal.CollisionEnabled.NO_COLLISION}[item['collision']['mode']]",
    "    components = list(created.get_components_by_class(unreal.PrimitiveComponent))",
    "    if not components: raise RuntimeError('spawned actor has no primitive component')",
    "    for component in components:",
    "        component.set_mobility(mobility)",
    "        component.set_collision_enabled(collision)",
    "        component.set_collision_profile_name(item['collision']['profile_name'])",
    "        component.set_editor_property('generate_overlap_events', bool(item['collision']['generate_overlap_events']))",
    "    created.set_editor_property('tags', [unreal.Name('VISTA_FINGERPRINT=' + item['fingerprint']), unreal.Name('VISTA_OPERATION=' + item['operation_id'])])",
    "    guid = str(created.get_actor_guid()).strip('{}')",
    "    if not guid: raise RuntimeError('spawned actor guid unavailable')",
    "    RESULT = {'ok': True, 'status': 'success', 'actor_name': item['actor_name'], 'operation_id': item['operation_id'], 'object_guid': guid}",
    "except Exception:",
    "    if created is not None:",
    "        try: subsystem.destroy_actor(created)",
    "        except Exception: pass",
    "    raise",
  ]);
}

function deleteScript(nonce, actor) {
  return markerScript("DELETE", nonce, { actor }, [
    "item = REQUEST['actor']",
    "name = item['actor_name']",
    "subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "matches = [] if subsystem is None else [actor for actor in subsystem.get_all_level_actors() if str(actor.get_actor_label()) == name or str(actor.get_name()) == name]",
    "if len(matches) == 0:",
    "    RESULT = {'ok': True, 'status': 'success', 'actor_name': name, 'disposition': 'absent'}",
    "elif len(matches) != 1:",
    "    raise RuntimeError('rollback actor identity is not unique')",
    "else:",
    "    target = matches[0]",
    "    tags = [str(tag) for tag in list(target.get_editor_property('tags'))]",
    "    expected = {'VISTA_FINGERPRINT=' + item['fingerprint'], 'VISTA_OPERATION=' + item['operation_id']}",
    "    actual_guid = str(target.get_actor_guid()).strip('{}')",
    "    if not expected.issubset(set(tags)): raise RuntimeError('rollback actor identity mismatch')",
    "    if item.get('object_guid') and actual_guid != item['object_guid']: raise RuntimeError('rollback actor guid mismatch')",
    "    if not subsystem.destroy_actor(target): raise RuntimeError('rollback actor delete failed')",
    "    RESULT = {'ok': True, 'status': 'success', 'actor_name': name, 'disposition': 'deleted'}",
  ]);
}

function playerStartScript(operation, nonce, input) {
  return markerScript(operation, nonce, input, [
    "name = REQUEST['actor_name']",
    "transform = REQUEST['transform']",
    "subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "matches = [] if subsystem is None else [actor for actor in subsystem.get_all_level_actors() if isinstance(actor, unreal.PlayerStart) and (str(actor.get_actor_label()) == name or str(actor.get_name()) == name)]",
    "if len(matches) != 1: raise RuntimeError('PlayerStart identity is not unique')",
    "player = matches[0]",
    "before_loc = player.get_actor_location()",
    "before_rot = player.get_actor_rotation()",
    "before_scale = player.get_actor_scale3d()",
    "expected_guid = REQUEST.get('expected_object_guid')",
    "if expected_guid is not None and str(player.get_actor_guid()).strip('{}') != expected_guid: raise RuntimeError('PlayerStart object guid mismatch')",
    "expected_current = REQUEST.get('expected_current_transform')",
    "if expected_current is not None:",
    "    actual_current = [float(before_loc.x), float(before_loc.y), float(before_loc.z), float(before_rot.pitch), float(before_rot.yaw), float(before_rot.roll), float(before_scale.x), float(before_scale.y), float(before_scale.z)]",
    "    expected_flat = [float(value) for value in expected_current['location_cm'] + expected_current['rotation_deg'] + expected_current['scale']]",
    "    if len(actual_current) != len(expected_flat) or any(abs(left - right) > 0.01 for left, right in zip(actual_current, expected_flat)): raise RuntimeError('PlayerStart compare-and-swap mismatch')",
    "try:",
    "    player.set_actor_location(unreal.Vector(*[float(value) for value in transform['location_cm']]), False, False)",
    "    rotation = [float(value) for value in transform['rotation_deg']]",
    "    player.set_actor_rotation(unreal.Rotator(pitch=rotation[0], yaw=rotation[1], roll=rotation[2]), False)",
    "    player.set_actor_scale3d(unreal.Vector(*[float(value) for value in transform['scale']]))",
    "    RESULT = {'ok': True, 'status': 'success', 'actor_name': name}",
    "except Exception:",
    "    player.set_actor_location(before_loc, False, False)",
    "    player.set_actor_rotation(before_rot, False)",
    "    player.set_actor_scale3d(before_scale)",
    "    raise",
  ]);
}

function evidenceScript(operation, nonce, manifest) {
  return markerScript(operation, nonce, { manifest }, [
    "import hashlib",
    "items = REQUEST['manifest']",
    "subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "actors = list(subsystem.get_all_level_actors()) if subsystem else []",
    "receipt_path = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir() + 'Content/VISTA/Metadata/content-revision.json')",
    "with open(receipt_path, 'rb') as receipt_handle: receipt_bytes = receipt_handle.read(65537)",
    "if len(receipt_bytes) > 65536: raise RuntimeError('content revision receipt is oversized')",
    "receipt = json.loads(receipt_bytes.decode('utf-8'))",
    "content_receipt = {'content_revision': str(receipt.get('content_revision', '')), 'receipt_sha256': hashlib.sha256(receipt_bytes).hexdigest(), 'schema': str(receipt.get('schema', '')), 'verification_revision': str(receipt.get('verification_revision', ''))}",
    "def actor_label(actor):",
    "    try: return str(actor.get_actor_label())",
    "    except Exception: return str(actor.get_name())",
    "selected = []",
    "for item in items:",
    "    matches = [actor for actor in actors if actor_label(actor) == item['actor_name'] or str(actor.get_name()) == item['actor_name']]",
    "    if len(matches) != 1: raise RuntimeError('evidence actor identity is not unique')",
    "    actor = matches[0]",
    "    origin, extent = actor.get_actor_bounds(False)",
    "    selected.append({'actor': actor, 'name': item['actor_name'], 'role': item['role'], 'infrastructure_kind': item['infrastructure_kind'], 'origin': origin, 'extent': extent})",
    "snapshot = []",
    "for row in selected:",
    "    actor = row['actor']",
    "    loc = actor.get_actor_location(); rot = actor.get_actor_rotation(); scale = actor.get_actor_scale3d()",
    "    tags = [str(tag) for tag in list(actor.get_editor_property('tags'))]",
    "    fingerprint = next((tag.split('=', 1)[1] for tag in tags if tag.startswith('VISTA_FINGERPRINT=')), None)",
    "    operation_id = next((tag.split('=', 1)[1] for tag in tags if tag.startswith('VISTA_OPERATION=')), None)",
    "    expected = next(item for item in items if item['actor_name'] == row['name'])",
    "    class_path = str(actor.get_class().get_path_name())",
    "    asset_path = class_path",
    "    if expected['asset_kind'] == 'static_mesh':",
    "        mesh_components = list(actor.get_components_by_class(unreal.StaticMeshComponent))",
    "        mesh_paths = [str(component.get_editor_property('static_mesh').get_path_name()) for component in mesh_components if component.get_editor_property('static_mesh') is not None]",
    "        asset_path = mesh_paths[0] if len(mesh_paths) == 1 else None",
    "    components = list(actor.get_components_by_class(unreal.PrimitiveComponent))",
    "    policies = []",
    "    for component in components:",
    "        collision_text = str(component.get_collision_enabled()).lower().replace('_', '')",
    "        collision_mode = 'query_and_physics' if 'queryandphysics' in collision_text else ('query_only' if 'queryonly' in collision_text else ('disabled' if 'nocollision' in collision_text else 'unknown'))",
    "        policies.append({'mobility': str(component.get_editor_property('mobility')).split('.')[-1].lower(), 'collision_mode': collision_mode, 'profile_name': str(component.get_collision_profile_name()), 'generate_overlap_events': bool(component.get_editor_property('generate_overlap_events'))})",
    "    materials = []",
    "    for component in list(actor.get_components_by_class(unreal.MeshComponent)):",
    "        try: slot_count = int(component.get_num_materials())",
    "        except Exception: slot_count = 0",
    "        for slot_index in range(max(0, slot_count)):",
    "            material = component.get_material(slot_index)",
    "            material_path = str(material.get_path_name()) if material is not None else None",
    "            material_class = str(material.get_class().get_path_name()) if material is not None else None",
    "            pbr_eligible = bool(material is not None and isinstance(material, unreal.MaterialInterface) and material_path.startswith('/Game/') and '/Engine/EngineMaterials/DefaultMaterial' not in material_path)",
    "            materials.append({'component': str(component.get_name()), 'slot_index': slot_index, 'material_path': material_path, 'material_class': material_class, 'pbr_eligible': pbr_eligible})",
    "    snapshot.append({'actor_name': row['name'], 'fingerprint': fingerprint, 'operation_id': operation_id, 'object_guid': str(actor.get_actor_guid()).strip('{}'), 'class_path': class_path, 'asset_path': asset_path, 'location_cm': [float(loc.x), float(loc.y), float(loc.z)], 'rotation_deg': [float(rot.pitch), float(rot.yaw), float(rot.roll)], 'scale': [float(scale.x), float(scale.y), float(scale.z)], 'component_policies': policies, 'materials': materials})",
    "selected_ids = {id(row['actor']) for row in selected}",
    "world_context = []",
    "for actor in actors:",
    "    if id(actor) in selected_ids: continue",
    "    try:",
    "        class_path = str(actor.get_class().get_path_name())",
    "        if any(term in class_path for term in ['WorldSettings', 'PlayerStart', 'Volume', 'Light', 'Camera', 'Sky']): continue",
    "        components = list(actor.get_components_by_class(unreal.PrimitiveComponent))",
    "        active_components = [component for component in components if 'nocollision' not in str(component.get_collision_enabled()).lower().replace('_', '') and str(component.get_editor_property('mobility')).split('.')[-1].lower() in ['static', 'stationary']]",
    "        if not active_components: continue",
    "        origin, extent = actor.get_actor_bounds(False)",
    "        world_context.append({'actor': actor, 'name': actor_label(actor), 'class_path': class_path, 'origin': origin, 'extent': extent})",
    "    except Exception: continue",
    "world_context.sort(key=lambda row: (row['name'], row['class_path']))",
    "collisions = []",
    "for left_index in range(len(selected)):",
    "    left = selected[left_index]",
    "    for right in selected[left_index + 1:]:",
    "        dx = abs(left['origin'].x - right['origin'].x) - (left['extent'].x + right['extent'].x)",
    "        dy = abs(left['origin'].y - right['origin'].y) - (left['extent'].y + right['extent'].y)",
    "        dz = abs(left['origin'].z - right['origin'].z) - (left['extent'].z + right['extent'].z)",
    "        if dx < -5.0 and dy < -5.0 and dz < -5.0:",
    "            collisions.append({'actor_a': left['name'], 'actor_b': right['name'], 'scope': 'generated_generated', 'penetration_cm': round(min(abs(dx), abs(dy), abs(dz)) - 5.0, 3)})",
    "    for right in world_context:",
    "        dx = abs(left['origin'].x - right['origin'].x) - (left['extent'].x + right['extent'].x)",
    "        dy = abs(left['origin'].y - right['origin'].y) - (left['extent'].y + right['extent'].y)",
    "        dz = abs(left['origin'].z - right['origin'].z) - (left['extent'].z + right['extent'].z)",
    "        if dx < -5.0 and dy < -5.0 and dz < -5.0:",
    "            collisions.append({'actor_a': left['name'], 'actor_b': right['name'], 'scope': 'generated_world_static', 'penetration_cm': round(min(abs(dx), abs(dy), abs(dz)) - 5.0, 3)})",
    "floating = []",
    "for row in selected:",
    "    if row['infrastructure_kind'] == 'runtime_ground': continue",
    "    bottom = row['origin'].z - row['extent'].z",
    "    support = None",
    "    for other in selected + world_context:",
    "        if other is row: continue",
    "        top = other['origin'].z + other['extent'].z",
    "        if top > bottom + 5.0: continue",
    "        if abs(other['origin'].x - row['origin'].x) > other['extent'].x + row['extent'].x: continue",
    "        if abs(other['origin'].y - row['origin'].y) > other['extent'].y + row['extent'].y: continue",
    "        support = top if support is None or top > support else support",
    "    gap = bottom if support is None else bottom - support",
    "    if gap > 10.0: floating.append({'actor_name': row['name'], 'gap_cm': round(gap, 3)})",
    "world_snapshot = [{'actor_name': row['name'], 'class_path': row['class_path'], 'origin_cm': [float(row['origin'].x), float(row['origin'].y), float(row['origin'].z)], 'extent_cm': [float(row['extent'].x), float(row['extent'].y), float(row['extent'].z)]} for row in world_context]",
    "validation_scope = {'mode': 'generated_vs_generated_and_world_static', 'world_actor_count': len(world_snapshot), 'excluded_class_terms': ['WorldSettings', 'PlayerStart', 'Volume', 'Light', 'Camera', 'Sky']}",
    "digest_payload = {'actor_snapshot': snapshot, 'content_receipt': content_receipt, 'world_snapshot': world_snapshot, 'collisions': collisions, 'floating': floating, 'validation_scope': validation_scope}",
    "scene_digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()",
    "RESULT = {'ok': True, 'actor_snapshot': snapshot, 'content_receipt': content_receipt, 'world_snapshot': world_snapshot, 'validation_scope': validation_scope, 'scene_digest': scene_digest, 'collisions': collisions, 'collision_count': len(collisions), 'floating': floating, 'floating_count': len(floating)}",
  ]);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForRegularFile(file, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let lastSize = -1;
  while (Date.now() < deadline) {
    try {
      const stat = await fs.promises.stat(file);
      if (stat.isFile() && stat.size > 0 && stat.size === lastSize) return stat;
      lastSize = stat.size;
    } catch (_error) {}
    await sleep(200);
  }
  return null;
}

function validPng(buffer) {
  if (!Buffer.isBuffer(buffer) || buffer.length < 33 || buffer.length > MAX_SCREENSHOT_BYTES) return false;
  const signature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  if (!buffer.subarray(0, 8).equals(signature) || buffer.toString("ascii", 12, 16) !== "IHDR") return false;
  const width = buffer.readUInt32BE(16);
  const height = buffer.readUInt32BE(20);
  return width >= 1 && width <= 16_384 && height >= 1 && height <= 16_384;
}

async function readBoundedRegularFile(file) {
  let handle;
  try {
    handle = await fs.promises.open(file, fs.constants.O_RDONLY | Number(fs.constants.O_NOFOLLOW || 0));
    const before = await handle.stat();
    if (!before.isFile() || before.size < 33 || before.size > MAX_SCREENSHOT_BYTES) return null;
    const buffer = await handle.readFile();
    const after = await handle.stat();
    if (after.size !== before.size || after.mtimeMs !== before.mtimeMs || buffer.length !== before.size) return null;
    return buffer;
  } catch (_error) {
    return null;
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}

function createVistaSceneUeAdapter(options = {}) {
  const fixedUeBroker = options.ueBroker || null;
  const resolveUeBroker = typeof options.resolveUeBroker === "function"
    ? options.resolveUeBroker
    : () => fixedUeBroker;
  if (!fixedUeBroker && typeof options.resolveUeBroker !== "function") {
    throw new TypeError("ueBroker or resolveUeBroker is required");
  }
  if (fixedUeBroker && typeof fixedUeBroker.send !== "function") throw new TypeError("ueBroker.send is required");
  const contentRevision = requireRevision(options.contentRevision, "contentRevision");
  const verificationRevision = requireRevision(options.verificationRevision, "verificationRevision");
  const contentReceiptSha256 = String(options.contentReceiptSha256 || "").trim().toLowerCase();
  if (!SHA256_PATTERN.test(contentReceiptSha256)) throw new TypeError("contentReceiptSha256 is invalid");
  const screenshotDir = path.resolve(options.screenshotDir || "");
  if (!options.screenshotDir || screenshotDir === path.parse(screenshotDir).root) throw new TypeError("screenshotDir must be a non-root absolute directory");
  const nonceFactory = typeof options.nonceFactory === "function"
    ? options.nonceFactory
    : () => crypto.randomBytes(16).toString("hex");
  const timeoutMs = Number.isFinite(options.timeoutMs) && options.timeoutMs >= 1_000
    ? Math.min(options.timeoutMs, 60_000)
    : DEFAULT_TIMEOUT_MS;

  function brokerFor(context) {
    const broker = resolveUeBroker(context || {});
    if (!broker || typeof broker.send !== "function") {
      fail("SCENE_BUILD_UE_SESSION_UNAVAILABLE", "The authorized UE slot is unavailable", { retryable: true });
    }
    return broker;
  }

  async function executeFixed(operation, build, context = {}) {
    const nonce = nonceFactory();
    if (!/^[a-f0-9]{32}$/.test(nonce)) throw new TypeError("nonceFactory must return 128-bit lowercase hex");
    const { marker, script } = build(nonce);
    let raw;
    try {
      const authorizedBroker = brokerFor(context);
      raw = await authorizedBroker.send("execute_python_script", { script }, {
        timeoutMs,
        queueDeadlineMs: timeoutMs * 2,
        maxAttempts: 1,
        ...(context.signal ? { signal: context.signal } : {}),
      });
      if (brokerFor(context) !== authorizedBroker) {
        fail("SCENE_BUILD_UE_SESSION_UNAVAILABLE", "The authorized UE slot changed during the command", { retryable: true });
      }
    } catch (error) {
      if (error instanceof VistaSceneUeAdapterError) throw error;
      fail("SCENE_BUILD_UE_UNAVAILABLE", `UE ${operation} command failed`, {
        retryable: !(error && error.name === "AbortError"),
      });
    }
    return extractMarker(raw, marker);
  }

  const broker = Object.freeze({
    async preflight(request, context = {}) {
      const expectedContent = {
        content_revision: contentRevision,
        verification_revision: verificationRevision,
        receipt_sha256: contentReceiptSha256,
      };
      return executeFixed("preflight", (nonce) => preflightScript(nonce, request, expectedContent), context);
    },
    async spawnActor(actor, context = {}) {
      return executeFixed("spawn", (nonce) => spawnScript(nonce, actor), context);
    },
    async deleteActor(actor, context = {}) {
      return executeFixed("delete", (nonce) => deleteScript(nonce, actor), context);
    },
    async setPlayerStart({
      player_start: playerStart,
      expected_current_transform: expectedCurrentTransform,
      expected_object_guid: expectedObjectGuid,
    }, context = {}) {
      return executeFixed("set PlayerStart", (nonce) => playerStartScript("PLAYER_SET", nonce, {
        actor_name: playerStart.actor_name,
        transform: playerStart.transform,
        expected_current_transform: expectedCurrentTransform,
        expected_object_guid: expectedObjectGuid,
      }), context);
    },
    async restorePlayerStart({
      actor_name: actorName,
      transform,
      expected_current_transform: expectedCurrentTransform,
      expected_object_guid: expectedObjectGuid,
    }, context = {}) {
      return executeFixed("restore PlayerStart", (nonce) => playerStartScript("PLAYER_RESTORE", nonce, {
        actor_name: actorName,
        transform,
        expected_current_transform: expectedCurrentTransform,
        expected_object_guid: expectedObjectGuid,
      }), context);
    },
  });

  async function collectValidation(context, phase = "before") {
    const manifest = context.actor_manifest.map((actor) => ({
      actor_name: actor.actor_name,
      role: actor.role,
      infrastructure_kind: actor.infrastructure_kind,
      fingerprint: actor.fingerprint,
      operation_id: actor.operation_id,
      object_guid: actor.object_guid,
      asset_kind: actor.asset.kind,
      class_path: actor.asset.class_path,
      ue_path: actor.asset.ue_path,
      transform: actor.transform,
      mobility: actor.mobility,
      collision: actor.collision,
    }));
    const cache = context.evidence_cache instanceof Map ? context.evidence_cache : null;
    const key = `validation:${phase}`;
    if (cache && cache.has(key)) return cache.get(key);
    const operation = phase === "after" ? "VALIDATION_AFTER" : "VALIDATION_BEFORE";
    const promise = executeFixed(operation.toLowerCase().replace("_", " "), (
      nonce,
    ) => evidenceScript(operation, nonce, manifest), context).then((evidence) => {
      if (!SHA256_PATTERN.test(String(evidence.scene_digest || ""))
          || !isPlainObject(evidence.content_receipt)
          || evidence.content_receipt.schema !== "simworld-ue-content-receipt/v1"
          || evidence.content_receipt.content_revision !== contentRevision
          || evidence.content_receipt.verification_revision !== verificationRevision
          || evidence.content_receipt.receipt_sha256 !== contentReceiptSha256
          || !isPlainObject(evidence.validation_scope)
          || evidence.validation_scope.mode !== "generated_vs_generated_and_world_static"
          || !Number.isSafeInteger(evidence.validation_scope.world_actor_count)
          || evidence.validation_scope.world_actor_count < 0
          || !Array.isArray(evidence.world_snapshot)
          || evidence.world_snapshot.length !== evidence.validation_scope.world_actor_count) {
        fail("SCENE_BUILD_VALIDATION_EVIDENCE_INVALID", "UE validation bundle is incomplete");
      }
      return evidence;
    });
    if (cache) {
      cache.set(key, promise);
      promise.catch(() => cache.delete(key));
    }
    return promise;
  }

  function closeVector(actual, expected) {
    return Array.isArray(actual) && actual.length === 3
      && actual.every((value, index) => Number.isFinite(value) && Math.abs(value - expected[index]) <= 0.01);
  }

  function actorSnapshotMatches(actual, expected) {
    if (!isPlainObject(actual)
        || actual.actor_name !== expected.actor_name
        || actual.fingerprint !== expected.fingerprint
        || actual.operation_id !== expected.operation_id
        || actual.class_path !== expected.asset.class_path
        || actual.asset_path !== expected.asset.ue_path
        || typeof actual.object_guid !== "string"
        || !/^[A-Fa-f0-9-]{16,64}$/.test(actual.object_guid)
        || (expected.object_guid && actual.object_guid !== expected.object_guid)
        || !closeVector(actual.location_cm, expected.transform.location_cm)
        || !closeVector(actual.rotation_deg, expected.transform.rotation_deg)
        || !closeVector(actual.scale, expected.transform.scale)
        || !Array.isArray(actual.component_policies)
        || actual.component_policies.length < 1
        || !Array.isArray(actual.materials)
        || actual.materials.length < 1
        || actual.materials.some((material) => !isPlainObject(material)
          || !Number.isSafeInteger(material.slot_index) || material.slot_index < 0
          || typeof material.component !== "string" || !material.component
          || typeof material.material_path !== "string" || !material.material_path.startsWith("/Game/")
          || /\/Engine\/EngineMaterials\/DefaultMaterial/i.test(material.material_path)
          || typeof material.material_class !== "string" || !material.material_class
          || material.pbr_eligible !== true)) return false;
    return actual.component_policies.every((policy) => (
      isPlainObject(policy)
      && policy.mobility === expected.mobility
      && policy.collision_mode === expected.collision.mode
      && policy.profile_name === expected.collision.profile_name
      && policy.generate_overlap_events === expected.collision.generate_overlap_events
    ));
  }

  const evidenceHooks = Object.freeze({
    async actor_snapshot(context) {
      const evidence = await collectValidation(context, "before");
      const expected = new Map(context.actor_manifest.map((actor) => [actor.actor_name, actor]));
      if (!Array.isArray(evidence.actor_snapshot) || evidence.actor_snapshot.length !== expected.size
          || evidence.actor_snapshot.some((actor) => !expected.has(actor.actor_name)
            || !actorSnapshotMatches(actor, expected.get(actor.actor_name)))) {
        fail("SCENE_BUILD_ACTOR_EVIDENCE_INVALID", "UE actor snapshot does not match the BuildPlan");
      }
      return {
        schema: "vista-scene-actor-snapshot/v1",
        scene_digest: evidence.scene_digest,
        content_receipt: evidence.content_receipt,
        validation_scope: evidence.validation_scope,
        actors: evidence.actor_snapshot,
      };
    },
    async collision_report(context) {
      const evidence = await collectValidation(context, "before");
      if (!Number.isSafeInteger(evidence.collision_count) || evidence.collision_count < 0
          || !Array.isArray(evidence.collisions) || evidence.collision_count !== evidence.collisions.length) {
        fail("SCENE_BUILD_COLLISION_EVIDENCE_INVALID", "UE collision report is invalid");
      }
      if (evidence.collision_count > 0) fail("SCENE_BUILD_COLLISIONS_DETECTED", "UE validation found actor interpenetration");
      return {
        schema: "vista-scene-collision-report/v1",
        scene_digest: evidence.scene_digest,
        validation_scope: evidence.validation_scope,
        collision_count: 0,
        collisions: [],
      };
    },
    async floating_report(context) {
      const evidence = await collectValidation(context, "before");
      if (!Number.isSafeInteger(evidence.floating_count) || evidence.floating_count < 0
          || !Array.isArray(evidence.floating) || evidence.floating_count !== evidence.floating.length) {
        fail("SCENE_BUILD_FLOATING_EVIDENCE_INVALID", "UE floating report is invalid");
      }
      if (evidence.floating_count > 0) fail("SCENE_BUILD_FLOATING_ACTORS_DETECTED", "UE validation found unsupported floating actors");
      return {
        schema: "vista-scene-floating-report/v1",
        scene_digest: evidence.scene_digest,
        validation_scope: evidence.validation_scope,
        floating_count: 0,
        floating: [],
      };
    },
    async screenshot(context) {
      const before = await collectValidation(context, "before");
      await fs.promises.mkdir(screenshotDir, { recursive: true, mode: 0o700 });
      const directoryStat = await fs.promises.lstat(screenshotDir);
      const canonicalDirectory = await fs.promises.realpath(screenshotDir);
      if (!directoryStat.isDirectory() || directoryStat.isSymbolicLink()
          || (directoryStat.mode & 0o077) !== 0 || canonicalDirectory !== screenshotDir) {
        fail("SCENE_BUILD_SCREENSHOT_STORAGE_INVALID", "Screenshot evidence storage is not private and canonical");
      }
      const filename = `${context.plan.plan_id}-${crypto.randomBytes(8).toString("hex")}.png`;
      const filepath = path.resolve(screenshotDir, filename);
      if (path.dirname(filepath) !== screenshotDir) fail("SCENE_BUILD_SCREENSHOT_PATH_INVALID", "Screenshot path is invalid");
      try {
        const authorizedBroker = brokerFor(context);
        await authorizedBroker.send("take_screenshot", { filepath }, {
          timeoutMs,
          queueDeadlineMs: timeoutMs * 2,
          maxAttempts: 1,
          ...(context.signal ? { signal: context.signal } : {}),
        });
        if (brokerFor(context) !== authorizedBroker) {
          fail("SCENE_BUILD_UE_SESSION_UNAVAILABLE", "The authorized UE slot changed during screenshot capture", { retryable: true });
        }
      } catch (error) {
        if (error instanceof VistaSceneUeAdapterError) throw error;
        fail("SCENE_BUILD_SCREENSHOT_FAILED", "UE screenshot capture failed", { retryable: true });
      }
      const stat = await waitForRegularFile(filepath, Math.min(timeoutMs, 15_000));
      const bytes = stat ? await readBoundedRegularFile(filepath) : null;
      if (!bytes || !validPng(bytes)) {
        await fs.promises.unlink(filepath).catch(() => {});
        fail("SCENE_BUILD_SCREENSHOT_FAILED", "UE screenshot file was not a bounded PNG", { retryable: true });
      }
      const digest = crypto.createHash("sha256").update(bytes).digest("hex");
      const after = await collectValidation(context, "after");
      if (after.scene_digest !== before.scene_digest) {
        await fs.promises.unlink(filepath).catch(() => {});
        fail("SCENE_BUILD_SCENE_CHANGED_DURING_EVIDENCE", "UE scene changed while screenshot evidence was captured", { retryable: true });
      }
      return {
        schema: "vista-scene-screenshot/v1",
        url: `/screenshots/${encodeURIComponent(filename)}`,
        sha256: digest,
        bytes: bytes.length,
        scene_digest: after.scene_digest,
        validation_scope: after.validation_scope,
      };
    },
  });

  return Object.freeze({ broker, evidenceHooks });
}

module.exports = {
  MARKER_PREFIX,
  VistaSceneUeAdapterError,
  createVistaSceneUeAdapter,
  extractMarker,
  preflightScript,
  spawnScript,
};
