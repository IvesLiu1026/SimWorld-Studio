import { expect, test } from "@playwright/test";

const ACCESS_TOKEN = "vista-ui-test-access-token-000000000000";

async function openAuthenticated(page, host = "127.0.0.1") {
  await page.goto(`http://${host}:3002/?token=${ACCESS_TOKEN}`);
  await expect(page).toHaveURL(`http://${host}:3002/`);
}

async function playerFrame(page) {
  const iframe = page.locator('iframe[title="UE Pixel Streaming"]');
  await expect(iframe).toBeAttached();
  const handle = await iframe.elementHandle();
  const frame = await handle.contentFrame();
  await frame.waitForLoadState("domcontentloaded");
  return frame;
}

async function recordKeys(frame) {
  await frame.evaluate(() => {
    window.__vistaTestKeys = [];
    const record = (event) => {
      window.__vistaTestKeys.push({
        type: event.type,
        key: event.key,
        code: event.code,
        keyCode: event.keyCode,
        which: event.which,
        altKey: event.altKey,
      });
    };
    document.addEventListener("keydown", record, true);
    document.addEventListener("keyup", record, true);
  });
}

function stoppedStatePayload() {
  return {
    schema: "vista-runtime-state/v1",
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

test.describe("VISTA Pixel Streaming controls", () => {
  test("loads the demo UI without a non-loopback browser request", async ({ page }) => {
    const external = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (
        ["http:", "https:", "ws:", "wss:"].includes(url.protocol) &&
        !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      ) external.push(request.url());
    });
    await openAuthenticated(page);
    await page.waitForTimeout(1_000);
    expect(external).toEqual([]);
  });

  test("propagates the reviewed 30 FPS fallback into the player URL", async ({ page }) => {
    await page.route("**/api/pixel-streaming-url", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ url: "http://127.0.0.1:8585", detectedPort: 8585, webRtcFps: 30 }),
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
      body: JSON.stringify(stoppedStatePayload()),
    }));

    await openAuthenticated(page);
    const iframe = page.locator('iframe[title="UE Pixel Streaming"]');
    await expect(iframe).toBeAttached();
    const src = await iframe.getAttribute("src");
    expect(new URL(src, page.url()).searchParams.get("WebRTCFPS")).toBe("30");
  });

  test("cancels a pending reconnect when the same stream recovers", async ({ page }) => {
    await page.route("**/api/pixel-streaming-url", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ url: "http://127.0.0.1:8585", detectedPort: 8585, webRtcFps: 60 }),
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
      body: JSON.stringify(stoppedStatePayload()),
    }));
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
    let stopped = false;
    let stopRequests = 0;
    await page.route("**/api/pixel-streaming-url", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ url: "http://127.0.0.1:8585", detectedPort: 8585, webRtcFps: 60 }),
    }));
    await page.route("**/api/training/datahub/latest", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "idle", run: null }),
    }));
    await page.route("**/api/screenshot/latest**", (route) => route.fulfill({ status: 404, body: "" }));
    await page.route("**/api/vista/stop_vista_play_mode", async (route) => {
      stopRequests += 1;
      stopped = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          schema: "vista-runtime-stop/v1",
          phase: "stop_requested",
          stop_requested: true,
          was_playing: true,
          retry_after_ms: 500,
        }),
      });
    });
    await page.route("**/api/vista/get_vista_state", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(stopped ? stoppedStatePayload() : {
        schema: "vista-runtime-state/v1",
        pie: true,
        possessed: true,
        pawn_class: "/Game/FixedPawn.FixedPawn_C",
        location: [0, 0, 96],
        rotation: [0, 0, 0],
        velocity: [0, 0, 0],
        on_ground: true,
        engine_time: 5,
      }),
    }));

    await openAuthenticated(page);
    const toggle = page.getByTestId("vista-demo-toggle");
    await expect(page.getByTestId("vista-demo-status")).toContainText("Stop remains available");
    await expect(toggle).toBeEnabled();
    await expect(toggle).toHaveText("Stop VISTA Demo");
    await toggle.click();
    await expect(page.getByTestId("vista-demo-status")).toHaveText("VISTA Demo stopped — UE confirmed");
    expect(stopRequests).toBe(1);
  });

  test("announces input readiness only after both a decoded frame and data channel", async ({ page }) => {
    await openAuthenticated(page);
    await page.setContent('<iframe id="player" title="UE Pixel Streaming" src="/ue-player.html?cirrus=8585"></iframe>');
    const frame = await playerFrame(page);
    await page.evaluate(() => {
      window.__vistaReadyMessages = 0;
      window.__vistaDisconnectedMessages = 0;
      window.addEventListener("message", (event) => {
        if (
          event.origin === location.origin &&
          event.source === document.getElementById("player").contentWindow &&
          event.data?.type === "sw-stream-connected"
        ) {
          window.__vistaReadyMessages += 1;
        }
        if (
          event.origin === location.origin &&
          event.source === document.getElementById("player").contentWindow &&
          event.data?.type === "sw-stream-disconnected"
        ) {
          window.__vistaDisconnectedMessages += 1;
        }
      });
    });

    await frame.evaluate(() => {
      window._readyPosted = false;
      window._videoFrameReady = true;
      window._swInputDataChannelOpen = false;
      window.swMaybeSignalReady();
    });
    await page.waitForTimeout(50);
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
    expect(await frame.evaluate(() => window._readyPosted)).toBe(false);
  });

  test("server rejects a non-configured signaling destination", async ({ page }) => {
    await openAuthenticated(page);
    const allowed = await page.request.get("/ue-player.html?cirrus=8585");
    expect(allowed.status()).toBe(200);
    expect(allowed.headers()["content-security-policy"]).toContain("ws://127.0.0.1:8585");
    expect(allowed.headers()["content-security-policy"]).not.toContain("127.0.0.1:*");
    const response = await page.request.get(
      "/ue-player.html?ss=wss%3A%2F%2Fexample.com%3A443&cirrus=99999",
    );
    expect(response.status()).toBe(400);
    expect(await response.text()).toContain("configured signaling port");
  });

  test("accepts only exact commands from the same-origin direct parent", async ({ page }) => {
    await openAuthenticated(page);
    await page.setContent(`
      <iframe id="player" title="UE Pixel Streaming" src="/ue-player.html?cirrus=8585"></iframe>
      <iframe id="sibling" src="about:blank"></iframe>
    `);

    const frame = await playerFrame(page);
    await recordKeys(frame);

    await page.evaluate(() => {
      const player = document.getElementById("player");
      player.contentWindow.postMessage({ type: "sw-vista-play", keyCode: 65 }, location.origin);
      player.contentWindow.postMessage({ type: "sw-focus-stream" }, location.origin);
    });
    const sibling = page.frames().find((candidate) => candidate !== page.mainFrame() && candidate.url() === "about:blank");
    await sibling.evaluate(() => {
      parent.document.getElementById("player").contentWindow.postMessage({ type: "sw-vista-play" }, parent.location.origin);
    });
    await page.waitForTimeout(50);
    await expect.poll(() => frame.evaluate(() => window.__vistaTestKeys)).toEqual([]);

    await page.evaluate(() => {
      document.getElementById("player").contentWindow.postMessage({ type: "sw-vista-play" }, location.origin);
    });
    await expect.poll(() => frame.evaluate(() => window.__vistaTestKeys)).toEqual([
      { type: "keydown", key: "Alt", code: "AltLeft", keyCode: 18, which: 18, altKey: true },
      { type: "keydown", key: "p", code: "KeyP", keyCode: 80, which: 80, altKey: true },
      { type: "keyup", key: "p", code: "KeyP", keyCode: 80, which: 80, altKey: true },
      { type: "keyup", key: "Alt", code: "AltLeft", keyCode: 18, which: 18, altKey: false },
    ]);

    await page.evaluate(() => {
      document.getElementById("player").contentWindow.postMessage({ type: "sw-vista-stop" }, location.origin);
    });
    await expect.poll(() => frame.evaluate(() => window.__vistaTestKeys)).toEqual([
      { type: "keydown", key: "Alt", code: "AltLeft", keyCode: 18, which: 18, altKey: true },
      { type: "keydown", key: "p", code: "KeyP", keyCode: 80, which: 80, altKey: true },
      { type: "keyup", key: "p", code: "KeyP", keyCode: 80, which: 80, altKey: true },
      { type: "keyup", key: "Alt", code: "AltLeft", keyCode: 18, which: 18, altKey: false },
      { type: "keydown", key: "Escape", code: "Escape", keyCode: 27, which: 27, altKey: false },
      { type: "keyup", key: "Escape", code: "Escape", keyCode: 27, which: 27, altKey: false },
    ]);
  });

  test("rejects a valid-looking command from a cross-origin parent", async ({ page }) => {
    await openAuthenticated(page, "localhost");
    await page.setContent(`
      <iframe
        id="player"
        title="UE Pixel Streaming"
        src="http://127.0.0.1:3002/ue-player.html?token=${ACCESS_TOKEN}&cirrus=8585"
      ></iframe>
    `);
    const frame = await playerFrame(page);
    await recordKeys(frame);

    await page.evaluate(() => {
      document.getElementById("player").contentWindow.postMessage(
        { type: "sw-vista-play" },
        "http://127.0.0.1:3002",
      );
    });
    await page.waitForTimeout(50);
    await expect.poll(() => frame.evaluate(() => window.__vistaTestKeys)).toEqual([]);
  });

  test("sets up, sends Play, verifies state, and uses backend Stop after stream loss", async ({ page }) => {
    let setupRequest = null;
    let stopRequest = null;
    let stateRequests = 0;

    // Hold the canonical 30s broker grace timer so the test can prove the exact
    // delay without sleeping for 30 real seconds. Other timers remain native.
    await page.addInitScript(() => {
      const nativeSetTimeout = window.setTimeout.bind(window);
      const nativeClearTimeout = window.clearTimeout.bind(window);
      const held = new Map();
      let nextId = -10_000;
      window.__vistaHeldDelays = [];
      window.setTimeout = (callback, delay, ...args) => {
        if (delay !== 30_000) return nativeSetTimeout(callback, delay, ...args);
        const id = nextId;
        nextId -= 1;
        held.set(id, { callback, args });
        window.__vistaHeldDelays.push(delay);
        return id;
      };
      window.clearTimeout = (id) => {
        if (held.delete(id)) return;
        nativeClearTimeout(id);
      };
      window.__releaseVistaGrace = () => {
        const first = held.entries().next().value;
        if (!first) return false;
        const [id, timer] = first;
        held.delete(id);
        timer.callback(...timer.args);
        return true;
      };
    });

    await page.route("**/api/pixel-streaming-url", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ url: "http://127.0.0.1:8585", detectedPort: 8585, webRtcFps: 60 }),
    }));
    await page.route("**/api/training/datahub/latest", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "idle", run: null }),
    }));
    await page.route("**/api/screenshot/latest**", (route) => route.fulfill({ status: 404, body: "" }));
    await page.route("**/api/vista/setup_vista_play_mode", async (route) => {
      setupRequest = route.request();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          schema: "vista-runtime-setup/v1",
          phase: "prepared",
          prepared: true,
          game_mode_class: "/Game/FixedGameMode.FixedGameMode_C",
          pawn_class: "/Game/FixedPawn.FixedPawn_C",
          player_start_present: true,
          requires_operator_play: true,
          play_lease_granted: true,
          retry_after_ms: 30_000,
          state_probe_grace_ms: 30_000,
        }),
      });
    });
    await page.route("**/api/vista/stop_vista_play_mode", async (route) => {
      stopRequest = route.request();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          schema: "vista-runtime-stop/v1",
          phase: "stop_requested",
          stop_requested: true,
          was_playing: true,
          retry_after_ms: 500,
        }),
      });
    });
    await page.route("**/api/vista/get_vista_state", async (route) => {
      stateRequests += 1;
      if (stateRequests === 1) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: { "Cache-Control": "no-store" },
          body: JSON.stringify(stoppedStatePayload()),
        });
        return;
      }
      if (stateRequests === 2) {
        await route.fulfill({
          status: 425,
          contentType: "application/json",
          headers: { "Cache-Control": "no-store", "Retry-After": "1" },
          body: JSON.stringify({
            code: "VISTA_PLAY_START_GRACE",
            error: "VISTA Play mode is still starting",
            retry_after_ms: 300,
          }),
        });
        return;
      }
      const payload = stateRequests === 3 ? {
        schema: "vista-runtime-state/v1",
        pie: true,
        possessed: true,
        pawn_class: "/Game/FixedPawn.FixedPawn_C",
        location: [0, 0, 96],
        rotation: [0, 0, 0],
        velocity: [0, 0, 0],
        on_ground: true,
        engine_time: 1,
      } : stoppedStatePayload();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store" },
        body: JSON.stringify(payload),
      });
    });

    await openAuthenticated(page);
    const frame = await playerFrame(page);
    await recordKeys(frame);

    const toggle = page.getByTestId("vista-demo-toggle");
    await expect(toggle).toBeDisabled();
    await page.evaluate(() => window.postMessage({ type: "sw-stream-connected" }, location.origin));
    await expect(toggle).toBeDisabled();
    await frame.evaluate(() => window.parent.postMessage(
      { type: "sw-stream-connected", extra: true },
      location.origin,
    ));
    await expect(toggle).toBeDisabled();
    await frame.evaluate(() => window.parent.postMessage({ type: "sw-stream-connected" }, location.origin));
    await expect(toggle).toBeEnabled();

    await toggle.click();
    await expect(page.getByTestId("vista-demo-status")).toContainText("verifying after 30s");
    expect(setupRequest).not.toBeNull();
    expect(setupRequest.method()).toBe("POST");
    expect(setupRequest.postData()).toBeNull();
    expect(setupRequest.headers()["content-type"]).toBeUndefined();
    expect(setupRequest.headers().cookie).toContain("vista_studio_access=");

    await expect.poll(() => frame.evaluate(() => window.__vistaTestKeys)).toEqual([
      { type: "keydown", key: "Alt", code: "AltLeft", keyCode: 18, which: 18, altKey: true },
      { type: "keydown", key: "p", code: "KeyP", keyCode: 80, which: 80, altKey: true },
      { type: "keyup", key: "p", code: "KeyP", keyCode: 80, which: 80, altKey: true },
      { type: "keyup", key: "Alt", code: "AltLeft", keyCode: 18, which: 18, altKey: false },
    ]);
    await page.waitForTimeout(150);
    expect(stateRequests).toBe(1);
    expect(await page.evaluate(() => window.__vistaHeldDelays)).toContain(30_000);
    expect(await page.evaluate(() => window.__releaseVistaGrace())).toBe(true);

    await expect.poll(() => stateRequests).toBe(2);
    await expect(page.getByTestId("vista-demo-status")).toContainText("Entering Play mode");
    await page.waitForTimeout(100);
    expect(stateRequests).toBe(2);
    await expect(page.getByTestId("vista-demo-status")).toContainText("VISTA Demo live");
    expect(stateRequests).toBe(3);
    await expect(toggle).toHaveText("Stop VISTA Demo");
    await expect(page.getByRole("button", { name: "Agent", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Shot" })).toBeDisabled();

    await frame.evaluate(() => window.parent.postMessage({ type: "sw-stream-disconnected" }, location.origin));
    await expect(page.getByTestId("vista-demo-status")).toContainText("disconnected");
    await expect(toggle).toBeEnabled();
    await expect(toggle).toHaveText("Stop VISTA Demo");
    await toggle.click();
    await expect(page.getByTestId("vista-demo-status")).toContainText("Stop sent");
    expect(stopRequest).not.toBeNull();
    expect(stopRequest.method()).toBe("POST");
    expect(stopRequest.postData()).toBeNull();
    expect(stopRequest.headers()["content-type"]).toBeUndefined();
    expect(stopRequest.headers().cookie).toContain("vista_studio_access=");
    await expect.poll(() => frame.evaluate(() => window.__vistaTestKeys)).toEqual([
      { type: "keydown", key: "Alt", code: "AltLeft", keyCode: 18, which: 18, altKey: true },
      { type: "keydown", key: "p", code: "KeyP", keyCode: 80, which: 80, altKey: true },
      { type: "keyup", key: "p", code: "KeyP", keyCode: 80, which: 80, altKey: true },
      { type: "keyup", key: "Alt", code: "AltLeft", keyCode: 18, which: 18, altKey: false },
    ]);
    await expect(page.getByTestId("vista-demo-status")).toHaveText("VISTA Demo stopped — UE confirmed");
    expect(stateRequests).toBe(4);
    await expect(toggle).toHaveText("Start VISTA Demo");
    await expect(page.getByRole("button", { name: "Agent", exact: true })).toBeEnabled();
  });
});
