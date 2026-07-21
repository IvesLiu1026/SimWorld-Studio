import { expect, test } from "@playwright/test";

const ACCESS_TOKEN = "vista-ui-test-access-token-000000000000";
const TEST_PORT = process.env.VISTA_UI_TEST_PORT || "3002";
const PAWN_CLASS = "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/BP_ThirdPersonCharacter.BP_ThirdPersonCharacter_C";

function studioOrigin(host = "127.0.0.1") {
  return `http://${host}:${TEST_PORT}`;
}

async function openAuthenticated(page, host = "127.0.0.1") {
  await page.goto(`${studioOrigin(host)}/?token=${ACCESS_TOKEN}`);
  await expect(page).toHaveURL(`${studioOrigin(host)}/`);
}

async function playerFrame(page) {
  const iframe = page.locator('iframe[title="UE Pixel Streaming"]');
  await expect(iframe).toBeAttached();
  const handle = await iframe.elementHandle();
  const frame = await handle.contentFrame();
  await frame.waitForLoadState("domcontentloaded");
  return frame;
}

function stoppedStatePayload() {
  return {
    schema: "vista-runtime-state/v2",
    pie: false,
    possessed: false,
    pawn_class: null,
    location: null,
    rotation: null,
    velocity: null,
    on_ground: null,
    engine_time: null,
  };
}

function liveStatePayload() {
  return {
    schema: "vista-runtime-state/v2",
    pie: true,
    possessed: true,
    pawn_class: PAWN_CLASS,
    location: [0, 0, 96],
    rotation: [0, 0, 0],
    velocity: [0, 0, 0],
    on_ground: true,
    engine_time: 5,
  };
}

function setupPayload() {
  return {
    schema: "vista-runtime-setup/v2",
    phase: "live",
    prepared: true,
    play_requested: true,
    already_playing: false,
    pie: true,
    possessed: true,
    game_mode_class: "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/BP_ThirdPersonGameMode.BP_ThirdPersonGameMode_C",
    pawn_class: PAWN_CLASS,
    player_start_present: true,
    scene_proof_digest: "a".repeat(64),
    play_lease_granted: true,
  };
}

function stopPayload() {
  return {
    schema: "vista-runtime-stop/v2",
    phase: "stopped",
    stop_requested: true,
    was_playing: true,
    confirmed_stopped: true,
    ended_pie: true,
    binding_cleaned: true,
  };
}

async function mockStudioShell(page, state = stoppedStatePayload(), fps = 60) {
  await page.route("**/api/pixel-streaming-url", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      schema: "pixel-streaming-endpoint/v1",
      path: "/pixel-stream/session/ps1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
      expiresAt: Date.now() + 300000,
      webRtcFps: fps,
    }),
  }));
  await page.route("**/api/training/datahub/latest", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ status: "idle", run: null }),
  }));
  await page.route("**/api/screenshot/latest**", (route) => route.fulfill({ status: 404, body: "" }));
  await page.route("**/api/vista/get_vista_state", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    headers: { "Cache-Control": "no-store" },
    body: JSON.stringify(state),
  }));
}

async function recordPixelInputs(frame) {
  await frame.waitForSelector("#videoElementParent");
  await frame.evaluate(() => {
    window.__vistaTestInputs = [];
    const record = (event) => window.__vistaTestInputs.push({
      type: event.type,
      key: event.key,
      button: event.button,
      buttons: event.buttons,
      offsetX: event.offsetX,
      offsetY: event.offsetY,
    });
    for (const type of ["keydown", "keyup"]) document.addEventListener(type, record, true);
    const target = document.getElementById("videoElementParent");
    for (const type of ["mouseenter", "mousemove", "mousedown", "mouseup"]) {
      target.addEventListener(type, record);
    }
  });
}

test.describe("VISTA Pixel Streaming controls", () => {
  test("loads the demo UI without a non-loopback browser request", async ({ page }) => {
    const external = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (["http:", "https:", "ws:", "wss:"].includes(url.protocol)
          && !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
        external.push(request.url());
      }
    });
    await openAuthenticated(page);
    await page.waitForTimeout(1_000);
    expect(external).toEqual([]);
  });

  test("propagates the reviewed 30 FPS fallback into the player URL", async ({ page }) => {
    await mockStudioShell(page, stoppedStatePayload(), 30);
    await openAuthenticated(page);
    const iframe = page.locator('iframe[title="UE Pixel Streaming"]');
    const src = await iframe.getAttribute("src");
    expect(new URL(src, page.url()).searchParams.get("WebRTCFPS")).toBe("30");
  });

  test("cancels a pending reconnect when the same stream recovers", async ({ page }) => {
    await mockStudioShell(page);
    await openAuthenticated(page);
    const frame = await playerFrame(page);
    const iframe = page.locator('iframe[title="UE Pixel Streaming"]');
    const originalSrc = await iframe.getAttribute("src");

    await frame.evaluate(() => window.parent.postMessage({ type: "sw-stream-connected" }, location.origin));
    await expect(page.getByTestId("vista-demo-toggle")).toBeEnabled();
    await frame.evaluate(() => window.parent.postMessage({ type: "sw-stream-disconnected" }, location.origin));
    await expect(page.getByTestId("vista-demo-toggle")).toBeDisabled();
    await frame.evaluate(() => window.parent.postMessage({ type: "sw-stream-connected" }, location.origin));
    await expect(page.getByTestId("vista-demo-toggle")).toBeEnabled();
    await page.waitForTimeout(2_300);
    expect(await iframe.getAttribute("src")).toBe(originalSrc);
  });

  test("recovers and stops an existing PIE without a WebRTC input channel", async ({ page }) => {
    let stopRequests = 0;
    await mockStudioShell(page, liveStatePayload());
    await page.route("**/api/vista/stop_vista_play_mode", async (route) => {
      stopRequests += 1;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(stopPayload()) });
    });

    await openAuthenticated(page);
    const toggle = page.getByTestId("vista-demo-toggle");
    await expect(page.getByTestId("vista-demo-status")).toContainText("Stop remains available");
    await expect(toggle).toHaveText("Stop Simulation");
    await toggle.click();
    await expect(page.getByTestId("vista-demo-status")).toHaveText("Simulation stopped — UE confirmed");
    expect(stopRequests).toBe(1);
  });

  test("announces input readiness only after both a decoded frame and data channel", async ({ page }) => {
    await openAuthenticated(page);
    await page.setContent('<iframe id="player" title="UE Pixel Streaming" src="/ue-player.html?endpoint=%2Fpixel-stream%2Fsession%2Fps1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"></iframe>');
    const frame = await playerFrame(page);
    await page.evaluate(() => {
      window.__vistaReadyMessages = 0;
      window.__vistaDisconnectedMessages = 0;
      window.addEventListener("message", (event) => {
        if (event.origin !== location.origin || event.source !== document.getElementById("player").contentWindow) return;
        if (event.data?.type === "sw-stream-connected") window.__vistaReadyMessages += 1;
        if (event.data?.type === "sw-stream-disconnected") window.__vistaDisconnectedMessages += 1;
      });
    });
    await frame.evaluate(() => {
      window._readyPosted = false;
      window._videoFrameReady = true;
      window._swInputDataChannelOpen = false;
      window.swMaybeSignalReady();
    });
    expect(await page.evaluate(() => window.__vistaReadyMessages)).toBe(0);
    await frame.evaluate(() => {
      window._swInputDataChannelOpen = true;
      window.swMaybeSignalReady();
      window.swMaybeSignalReady();
    });
    await expect.poll(() => page.evaluate(() => window.__vistaReadyMessages)).toBe(1);
    await frame.evaluate(() => {
      window.swMarkStreamDisconnected();
      window.swMarkStreamDisconnected();
    });
    await expect.poll(() => page.evaluate(() => window.__vistaDisconnectedMessages)).toBe(1);
  });

  test("server accepts only the configured opaque same-origin signaling destination", async ({ page }) => {
    await openAuthenticated(page);
    const allowed = await page.request.get(
      `${studioOrigin()}/ue-player.html?endpoint=%2Fpixel-stream%2Fsession%2Fps1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA`,
    );
    expect(allowed.status()).toBe(200);
    expect(allowed.headers()["content-security-policy"]).toContain("ws://127.0.0.1:8585");
    expect(allowed.headers()["content-security-policy"]).not.toContain("127.0.0.1:*");
    const response = await page.request.get(
      `${studioOrigin()}/ue-player.html?endpoint=%2Fpixel-stream%2Fsession%2Fps1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&ss=wss%3A%2F%2Fexample.com%2Fpixel-stream%2Fsession%2Fps1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA`,
    );
    expect(response.status()).toBe(400);
  });

  test("player exposes no toolbar-coordinate or Escape command surface", async ({ page }) => {
    await openAuthenticated(page);
    const source = await (await page.request.get(`${studioOrigin()}/ue-player.html?endpoint=%2Fpixel-stream%2Fsession%2Fps1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA`)).text();
    expect(source).not.toContain("sw-vista-play");
    expect(source).not.toContain("sw-vista-stop");
    expect(source).not.toContain("swClickVistaPlayToolbar");
    expect(source).not.toMatch(/\b486\b|\b78\b/);

    await page.setContent('<iframe id="player" title="UE Pixel Streaming" src="/ue-player.html?endpoint=%2Fpixel-stream%2Fsession%2Fps1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"></iframe>');
    const frame = await playerFrame(page);
    await recordPixelInputs(frame);
    await page.evaluate(() => {
      const player = document.getElementById("player");
      player.contentWindow.postMessage({ type: "sw-vista-play" }, location.origin);
      player.contentWindow.postMessage({ type: "sw-vista-stop" }, location.origin);
    });
    await page.waitForTimeout(200);
    expect(await frame.evaluate(() => window.__vistaTestInputs)).toEqual([]);
  });

  test("backend Start and authoritative Stop never synthesize Pixel Streaming input", async ({ page }) => {
    let setupRequest = null;
    let stopRequest = null;
    await mockStudioShell(page);
    await page.route("**/api/vista/setup_vista_play_mode", async (route) => {
      setupRequest = route.request();
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(setupPayload()) });
    });
    await page.route("**/api/vista/stop_vista_play_mode", async (route) => {
      stopRequest = route.request();
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(stopPayload()) });
    });

    await openAuthenticated(page);
    const frame = await playerFrame(page);
    await recordPixelInputs(frame);
    await frame.evaluate(() => window.parent.postMessage({ type: "sw-stream-connected" }, location.origin));
    const toggle = page.getByTestId("vista-demo-toggle");
    await expect(toggle).toBeEnabled();
    await toggle.click();
    await expect(page.getByTestId("vista-demo-status")).toContainText("Simulation running");
    expect(setupRequest.method()).toBe("POST");
    expect(setupRequest.postData()).toBeNull();
    expect(setupRequest.headers()["content-type"]).toBeUndefined();
    expect(await frame.evaluate(() => window.__vistaTestInputs)).toEqual([]);

    await frame.evaluate(() => window.parent.postMessage({ type: "sw-stream-disconnected" }, location.origin));
    await expect(toggle).toHaveText("Stop Simulation");
    await toggle.click();
    await expect(page.getByTestId("vista-demo-status")).toHaveText("Simulation stopped — UE confirmed");
    expect(stopRequest.method()).toBe("POST");
    expect(stopRequest.postData()).toBeNull();
    expect(stopRequest.headers()["content-type"]).toBeUndefined();
    expect(await frame.evaluate(() => window.__vistaTestInputs)).toEqual([]);
  });
});
