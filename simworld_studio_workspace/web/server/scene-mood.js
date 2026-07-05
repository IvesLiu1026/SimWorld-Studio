"use strict";
// ── Mood / lighting / camera stage (Phase A1) ─────────────────────────────────
// Scenes render at flat noon even when the prompt says "foggy medieval market" or "dusk gas station".
// Derive lighting+atmosphere from the prompt (one LLM call) and apply it DETERMINISTICALLY in UE — the
// builder ignores injected scripts (~3 of 4 times), so mood is applied server/harness-side, not by the LLM.
const { oneshotJSON } = require("./llm-oneshot");

// No-LLM keyword fallback (coarse but scene-appropriate) — also the sanitizer's per-key default source.
const NOON = { sun_elevation_deg: 55, sun_azimuth_deg: 150, sun_intensity_lux: 10, color_temp_k: 6500, fog_density: 0.004, fog_color: [0.62, 0.68, 0.75], exposure_comp: 0.0, saturation: 1.0, mood_tag: "noon" };
const FALLBACK = [
  { re: /\bnight\b|midnight|neon|after ?dark/, p: { sun_elevation_deg: -8, sun_azimuth_deg: 205, sun_intensity_lux: 0.4, color_temp_k: 9000, fog_density: 0.02, fog_color: [0.05, 0.07, 0.13], exposure_comp: 1.1, saturation: 1.12, mood_tag: "night" } },
  { re: /dusk|sunset|evening|golden ?hour|twilight/, p: { sun_elevation_deg: 5, sun_azimuth_deg: 255, sun_intensity_lux: 3.0, color_temp_k: 4200, fog_density: 0.014, fog_color: [0.55, 0.4, 0.32], exposure_comp: 0.4, saturation: 1.16, mood_tag: "dusk" } },
  { re: /dawn|sunrise|early morning/, p: { sun_elevation_deg: 11, sun_azimuth_deg: 95, sun_intensity_lux: 5.0, color_temp_k: 5200, fog_density: 0.012, fog_color: [0.6, 0.6, 0.62], exposure_comp: 0.2, saturation: 1.05, mood_tag: "dawn" } },
  { re: /fog|foggy|mist|misty|\bhaze\b/, p: { sun_elevation_deg: 28, sun_azimuth_deg: 150, sun_intensity_lux: 6.0, color_temp_k: 6800, fog_density: 0.05, fog_color: [0.72, 0.74, 0.78], exposure_comp: 0.2, saturation: 0.88, mood_tag: "foggy" } },
  { re: /overcast|cloudy|\bgrey\b|\bgray\b|storm|\brain/, p: { sun_elevation_deg: 40, sun_azimuth_deg: 180, sun_intensity_lux: 5.0, color_temp_k: 7000, fog_density: 0.02, fog_color: [0.68, 0.7, 0.73], exposure_comp: 0.1, saturation: 0.85, mood_tag: "overcast" } },
  { re: /snow|winter|frost|blizzard|arctic/, p: { sun_elevation_deg: 22, sun_azimuth_deg: 160, sun_intensity_lux: 7.5, color_temp_k: 7600, fog_density: 0.02, fog_color: [0.8, 0.83, 0.88], exposure_comp: 0.3, saturation: 0.9, mood_tag: "snowy" } },
];
function _fallback(scene) { const s = String(scene || "").toLowerCase(); for (const f of FALLBACK) if (f.re.test(s)) return f.p; return NOON; }

function _moodPrompt(scene) {
  return [
    "You are a lighting/atmosphere director for a 3D outdoor scene. From the description, choose realistic",
    "lighting + atmosphere that matches its MOOD and time of day. Output ONLY JSON with exactly these keys:",
    '{ "sun_elevation_deg": <number -10..80, angle above horizon; low=dawn/dusk, negative=night>,',
    '  "sun_azimuth_deg": <0..360 compass>, "sun_intensity_lux": <0.3..12, low at night>,',
    '  "color_temp_k": <3000 warm .. 12000 cool>, "fog_density": <0.002 clear .. 0.06 thick>,',
    '  "fog_color": [r,g,b] each 0..1, "exposure_comp": <-1..1.5>, "saturation": <0.8..1.3>,',
    '  "mood_tag": "<one word e.g. noon|dusk|dawn|night|foggy|overcast|snowy>" }',
    "Honor explicit cues in the prompt (foggy, dusk, night, snowy, golden hour, neon). If none stated, pick",
    "a flattering, slightly-directional daylight (not flat noon). JSON only, no prose, no markdown.",
    "",
    "SCENE: " + String(scene || ""),
  ].join("\n");
}

function _clamp(v, lo, hi, d) { v = Number(v); return isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d; }
function _sanitize(r, scene) {
  const f = _fallback(scene);
  const c = (Array.isArray(r && r.fog_color) && r.fog_color.length === 3) ? r.fog_color.map(x => _clamp(x, 0, 1, 0.65)) : f.fog_color;
  return {
    sun_elevation_deg: _clamp(r && r.sun_elevation_deg, -10, 80, f.sun_elevation_deg),
    sun_azimuth_deg: _clamp(r && r.sun_azimuth_deg, 0, 360, f.sun_azimuth_deg),
    sun_intensity_lux: _clamp(r && r.sun_intensity_lux, 0.2, 14, f.sun_intensity_lux),
    color_temp_k: _clamp(r && r.color_temp_k, 2800, 12000, f.color_temp_k),
    fog_density: _clamp(r && r.fog_density, 0.001, 0.08, f.fog_density),
    fog_color: c,
    exposure_comp: _clamp(r && r.exposure_comp, -1.5, 1.8, f.exposure_comp),
    saturation: _clamp(r && r.saturation, 0.7, 1.4, f.saturation),
    mood_tag: String((r && r.mood_tag) || f.mood_tag || "noon").slice(0, 20),
  };
}

async function deriveMood(scene, opts) {
  try {
    const raw = await oneshotJSON(_moodPrompt(scene), Object.assign({ telemetryComponent: "scene_mood" }, opts || {}));
    return _sanitize(raw, scene);
  } catch (e) { return Object.assign({ _fallback: true }, _fallback(scene)); }
}

// Deterministic UE python that applies the mood. Every step wrapped in try/except so one failure
// (e.g. missing PostProcessVolume API) never aborts the rest. Idempotent (reuses existing actors).
function buildMoodScript(m) {
  const P = m || NOON;
  return [
    "import unreal",
    "eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)",
    "acts = eas.get_all_level_actors()",
    "def _find(cn):",
    "    for a in acts:",
    "        try:",
    "            if a.get_class().get_name()==cn: return a",
    "        except Exception: pass",
    "    return None",
    "log=[]",
    "# --- Directional (sun) light ---",
    "try:",
    "    dl=_find('DirectionalLight') or eas.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0,0,300), unreal.Rotator(0,0,0))",
    `    dl.set_actor_rotation(unreal.Rotator(${(-P.sun_elevation_deg).toFixed(2)}, ${P.sun_azimuth_deg.toFixed(2)}, 0.0), False)`,
    "    c=dl.get_component_by_class(unreal.DirectionalLightComponent)",
    `    c.set_intensity(${P.sun_intensity_lux.toFixed(3)})`,
    `    c.set_temperature(${Math.round(P.color_temp_k)}); c.set_editor_property('use_temperature', True)`,
    "    log.append('sun ok')",
    "except Exception as e: log.append('sun FAIL '+str(e))",
    "# --- Sky light (scale ambient with sun; dim at night) ---",
    "try:",
    "    sl=_find('SkyLight')",
    "    if sl:",
    `        sc=sl.get_component_by_class(unreal.SkyLightComponent); sc.set_intensity(${Math.max(0.05, Math.min(3, P.sun_intensity_lux / 8)).toFixed(3)}); sc.recapture_sky()`,
    "        log.append('skylight ok')",
    "except Exception as e: log.append('skylight FAIL '+str(e))",
    "# --- Exponential height fog ---",
    "try:",
    "    fg=_find('ExponentialHeightFog') or eas.spawn_actor_from_class(unreal.ExponentialHeightFog, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))",
    "    fc=fg.get_component_by_class(unreal.ExponentialHeightFogComponent)",
    `    fc.set_editor_property('fog_density', ${P.fog_density.toFixed(4)})`,
    `    fc.set_editor_property('fog_inscattering_luminance', unreal.LinearColor(${P.fog_color[0].toFixed(3)}, ${P.fog_color[1].toFixed(3)}, ${P.fog_color[2].toFixed(3)}, 1.0))`,
    "    log.append('fog ok')",
    "except Exception as e: log.append('fog FAIL '+str(e))",
    "# --- Post-process (exposure + saturation), unbound volume ---",
    "try:",
    "    pp=_find('PostProcessVolume') or eas.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))",
    "    pp.set_editor_property('unbound', True)",
    "    s=pp.get_editor_property('settings')",
    "    s.set_editor_property('override_auto_exposure_bias', True)",
    `    s.set_editor_property('auto_exposure_bias', ${P.exposure_comp.toFixed(3)})`,
    "    s.set_editor_property('override_color_saturation', True)",
    `    s.set_editor_property('color_saturation', unreal.Vector4(${P.saturation.toFixed(3)}, ${P.saturation.toFixed(3)}, ${P.saturation.toFixed(3)}, 1.0))`,
    "    pp.set_editor_property('settings', s)",
    "    log.append('postprocess ok')",
    "except Exception as e: log.append('postprocess FAIL '+str(e))",
    `print('[MOOD] ${P.mood_tag} | ' + ' | '.join(log))`,
  ].join("\n");
}

module.exports = { deriveMood, buildMoodScript };
