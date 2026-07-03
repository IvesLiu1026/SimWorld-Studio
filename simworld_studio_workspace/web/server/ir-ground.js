"use strict";
// ── Deterministic ground-carpet pass (fix #1) ─────────────────────────────────
// Scenes came out with bare-grey / striped ground because the retrieved palette is ground-poor and
// often picks a ROAD/PATTERN STRIP (e.g. roadsnowy 8×4m) as the carpet — tiling a strip leaves gaps.
// Instead: pick the best FLAT, SQUARE-ish, theme-appropriate ground tile from the FULL library and lay
// it GAPLESS (step = exact footprint) as a guaranteed first step. Roads/paths stay as accents.

// scene text → ground-material keywords (first match wins; falls through to a neutral paving default)
const THEMES = [
  { re: /snow|winter|frost|\bice\b|arctic|blizzard/, keys: ["snow", "ice", "frost", "winter"] },
  { re: /desert|bazaar|sand|dune|oasis|arab|middle[- ]?east|souk/, keys: ["sand", "desert", "dune", "gravel"] },
  { re: /medieval|old town|village|rustic|fantasy/, keys: ["cobble", "stone", "dirt", "mud", "paving"] },
  { re: /temple|shrine|zen|pagoda|monastery|courtyard/, keys: ["stone", "paving", "pavement", "tile", "gravel", "flagstone"] },
  { re: /park|garden|lawn|grass|meadow|field|forest|nature/, keys: ["grass", "lawn", "turf", "meadow", "dirt"] },
  { re: /harbor|harbour|dock|port|industrial|factory|construction/, keys: ["concrete", "asphalt", "pavement", "paving"] },
  { re: /city|urban|street|market|plaza|town|road/, keys: ["asphalt", "concrete", "pavement", "paving", "cobble"] },
];
function themeKeys(scene) {
  const s = String(scene || "").toLowerCase();
  for (const t of THEMES) if (t.re.test(s)) return t.keys;
  return ["pavement", "paving", "concrete", "stone"];
}

const _FLAT = /floor|ground|tile|paving|pavement|slab|terrain|field|patch|plaza|court|surface|grass|sand|snow|gravel|dirt|cobble|road/i;
// Hard-reject non-ground structural meshes. NOTE: 'road' is intentionally NOT here — a snowy/dirt road
// is a valid fallback carpet for a snow/rustic scene when no square ground tile exists; overlap-tiling
// (step = min dimension) hides the strip seams, and squares still outscore strips for other scenes.
const _REJECT = /bridge|wall|stair|ramp|curb|kerb|fence|pipe|rail\b|roof|ceiling|pillar|column|beam|door|window|sign|lamp|pole|drain|hatch|manhole|sewer|marking|barrier|planter|bench|statue|pile|mound|heap|stack|rubble|boulder|\brock\b|pebble|debris/i;

// Pick the best carpet tile from the full DB. Returns {a, w, d, sz, ar, themeHit, score} or null.
function pickGroundTile(scene, db) {
  if (!db || !db.assets) return null;
  const keys = themeKeys(scene);
  const cands = [];
  for (const a of db.assets.values()) {
    if (a.category !== "ground_and_road" && a.category !== "nature_terrain") continue;
    const fp = a.footprint || a.dims || {};
    const w = Number(fp.width) || 0, d = Number(fp.depth) || 0;
    if (!w || !d) continue;
    const ar = Math.max(w, d) / Math.max(0.1, Math.min(w, d));
    if (ar > 2.6) continue;                                    // reject only extreme strips (planks/bridges)
    const sz = Math.max(w, d);
    if (sz < 1.5 || sz > 14) continue;                         // sane tile size
    const text = `${a.name || ""} ${a.id || ""} ${(a.tags || []).join(" ")} ${a.subcategory || ""}`.toLowerCase();
    if (_REJECT.test(text)) continue;                          // structural, not a ground surface
    if (!_FLAT.test(text)) continue;                           // must read as a floor/ground surface
    const themeHit = keys.filter(k => text.includes(k)).length;
    // theme match dominates; squareness + good size are tie-breakers so squares beat strips when both match.
    const score = themeHit * 10 + (ar <= 1.15 ? 4 : ar <= 1.6 ? 2 : 0) + (sz >= 3 && sz <= 9 ? 2 : 0) - Math.abs(sz - 6) * 0.15;
    cands.push({ a, w, d, sz, ar, themeHit, score });
  }
  if (!cands.length) return null;
  cands.sort((x, y) => y.score - x.score);
  return cands[0];
}

// Deterministic, gapless tiling python. Steps by min(W,D) on BOTH axes so even a mild strip overlaps
// into full coverage (no stripes, no gaps). Places each tile CENTER on the grid point.
function _carpetScript(pick, halfM) {
  const W = Math.round(pick.w * 100), D = Math.round(pick.d * 100), HALF = Math.round(halfM * 100);
  const S = Math.max(50, Math.min(W, D));   // step (cm), overlap for strips
  return [
    "import unreal",
    "_eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    `_m = unreal.EditorAssetLibrary.load_asset(${JSON.stringify(pick.a.path)})`,
    `S, HALF = ${S}, ${HALF}   # cm; step = min(footprint) => gapless (overlaps strips)`,
    "n = 0; x = -HALF",
    "while x <= HALF:",
    "    y = -HALF",
    "    while y <= HALF:",
    "        try:",
    "            _eas.spawn_actor_from_object(_m, unreal.Vector(x, y, 0.0), unreal.Rotator(0,0,0)); n += 1",
    "        except Exception as _e:",
    "            pass",
    "        y += S",
    "    x += S",
    'print("[GROUND] carpet tiles spawned:", n)',
  ].join("\n");
}

// The IR-block section for the ground carpet. Returns "" if no suitable tile found (pipeline unchanged).
function groundCarpetBlock(scene, db, halfM) {
  const pick = pickGroundTile(scene, db);
  if (!pick) return "";
  const stepM = Math.max(0.5, Math.min(pick.w, pick.d));
  const nx = Math.ceil((2 * halfM) / stepM), ny = nx;
  return [
    "### GROUND CARPET — lay this FIRST, before any props (deterministic, gapless):",
    `Base ground for this scene = "${pick.a.name}" (${pick.a.spawnTool || "spawn_actor"} "${pick.a.path}"), a flat ${pick.w}×${pick.d} m tile chosen to match the setting.`,
    `Carpet the WHOLE build area with it — ~${nx}×${ny} tiles, edge-to-edge (step = footprint) so NO bare grey and NO stripes show. Run this EXACT loop as your first spawn step:`,
    "```python",
    _carpetScript(pick, halfM),
    "```",
    "Any ground/road/path/pattern assets listed in the plan below are ACCENTS to place ON TOP of this carpet (walkways, plazas, patches) — do NOT rely on them for full coverage.",
    "",
  ].join("\n");
}

module.exports = { pickGroundTile, groundCarpetBlock, themeKeys };
