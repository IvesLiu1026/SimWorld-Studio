"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const WEB_ROOT = path.resolve(__dirname, "../..");

function source(relativePath) {
  return fs.readFileSync(path.join(WEB_ROOT, relativePath), "utf8");
}

test("production viewport uses only backend-confirmed VISTA Start and Stop", () => {
  const player = source("public/ue-player.html");
  const component = source("src/PixelStreamPlayer.jsx");
  const viewport = source("src/features/viewport/ViewportPanel.jsx");
  const combined = `${player}\n${component}\n${viewport}`;

  assert.doesNotMatch(combined, /sw-vista-play|sw-vista-stop|swClickVistaPlayToolbar/);
  assert.doesNotMatch(player, /\b486\b|\b78\b|KeyboardEvent\([^)]*Escape/);
  assert.doesNotMatch(component, /useImperativeHandle|playVistaDemo|stopVistaDemo/);
  assert.match(viewport, /\/vista\/setup_vista_play_mode/);
  assert.match(viewport, /vista-runtime-setup\/v2/);
  assert.match(viewport, /payload\?\.phase !== "live"/);
  assert.match(viewport, /payload\?\.pie !== true \|\| payload\?\.possessed !== true/);
  assert.match(viewport, /\/vista\/stop_vista_play_mode/);
  assert.match(viewport, /vista-runtime-stop\/v2/);
  assert.match(viewport, /payload\?\.confirmed_stopped === true/);
});
