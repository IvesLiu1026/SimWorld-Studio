import { expect, test } from "@playwright/test";

async function expectNoDocumentOverflow(page) {
  const overflow = await page.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }));
  expect(overflow.width).toBeLessThanOrEqual(overflow.viewport + 4);
}

async function expectNoTopbarOverlap(page) {
  const overlaps = await page.evaluate(() => {
    const header = document.querySelector("header");
    if (!header) return ["missing header"];
    const selectors = [".sw-brand", "select", ".pipeline-stepper", ".sec-nav-btn", ".primary-cta"];
    const rects = [...header.querySelectorAll(selectors.join(","))]
      .filter((node) => node.offsetParent !== null)
      .map((node) => {
        const rect = node.getBoundingClientRect();
        return {
          label: node.className || node.tagName,
          left: rect.left,
          right: rect.right,
          top: rect.top,
          bottom: rect.bottom,
          width: rect.width,
          height: rect.height,
        };
      })
      .filter((rect) => rect.width > 2 && rect.height > 2);

    const collisions = [];
    for (let i = 0; i < rects.length; i += 1) {
      for (let j = i + 1; j < rects.length; j += 1) {
        const a = rects[i];
        const b = rects[j];
        const xOverlap = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const yOverlap = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (xOverlap > 3 && yOverlap > 3) collisions.push(`${a.label} overlaps ${b.label}`);
      }
    }
    return collisions;
  });

  expect(overlaps).toEqual([]);
}

test.describe("SimWorld Studio shell", () => {
  test("renders and switches core modes without topbar overflow", async ({ page }) => {
    await page.goto("/");

    const header = page.locator("header");
    await expect(header.locator(".sw-brand-name")).toHaveText("SimWorld Studio");
    await expect(header.getByRole("button", { name: /Studio/ })).toBeVisible();
    await expect(header.getByRole("button", { name: /Library/ })).toBeVisible();
    await expect(header.getByRole("button", { name: /Results/ })).toBeVisible();
    await expectNoDocumentOverflow(page);
    await expectNoTopbarOverlap(page);

    const modes = [
      [1, "Task Builder"],
      [2, "Training Config"],
      [3, "Curriculum Builder"],
      [0, "Intent + SimCoder"],
    ];

    for (const [modeIndex, panelTitle] of modes) {
      await header.locator(".pipeline-tab").nth(modeIndex).click();
      await expect(
        page.locator(".sw-panel-header .sw-section-title").filter({ hasText: panelTitle }).first(),
      ).toBeVisible();
      await expectNoDocumentOverflow(page);
      await expectNoTopbarOverlap(page);
    }
  });
});
