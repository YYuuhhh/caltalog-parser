/* Model menus against the real install API and a tiny local Ollama HTTP fixture. */
const { chromium } = require("playwright");
const http = require("node:http");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const base = process.env.PIPELINE_UI_URL || "http://127.0.0.1:17861";
const REVIEW = "qwen3-vl:8b-instruct",
  FINAL = "qwen3.5:9b",
  LARGE = "qwen3-vl:32b-thinking-bf16";

(async () => {
  const installed = new Set([REVIEW]);
  const pulls = [];
  const pending = [];
  const deletes = [];
  const pendingDeletes = [];
  let failNext = false,
    failDelete = false;
  const server = http.createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = raw ? JSON.parse(raw) : {};
    if (req.method === "DELETE" && req.url === "/api/delete") {
      deletes.push(body.model);
      if (failDelete) {
        failDelete = false;
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "Deletion denied (test fixture)" }));
      } else pendingDeletes.push({ res, model: body.model });
      return;
    }
    if (req.url === "/api/pull") {
      pulls.push(body);
      assert(body.stream);
      res.writeHead(200, { "Content-Type": "application/x-ndjson" });
      if (failNext) {
        failNext = false;
        res.end(
          JSON.stringify({ error: "Not enough disk space (test fixture)" }) +
            "\n",
        );
      } else {
        res.write(
          JSON.stringify({
            status: "pulling weights",
            digest: "weights",
            total: 1000,
            completed: 200,
          }) + "\n",
        );
        pending.push({ res, model: body.model });
      }
      return;
    }
    res.setHeader("Content-Type", "application/json");
    if (req.url === "/api/tags")
      return res.end(
        JSON.stringify({
          models: [...installed].map((name) => ({ name, size: 6200000000 })),
        }),
      );
    if (req.url === "/api/version")
      return res.end(JSON.stringify({ version: "fixture" }));
    if (req.url === "/api/show" && installed.has(body.model))
      return res.end(JSON.stringify({ capabilities: ["vision", "thinking"] }));
    res.statusCode = 404;
    res.end(JSON.stringify({ error: "not installed" }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  const emptyServer = http.createServer((req, res) => {
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ models: [], version: "fixture" }));
  });
  await new Promise((resolve) => emptyServer.listen(0, "127.0.0.1", resolve));
  const emptyUrl = `http://127.0.0.1:${emptyServer.address().port}`;
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
    reducedMotion: "reduce",
  });
  const errors = [];
  page.on("pageerror", (e) => {
    errors.push(e.message);
    console.error("Browser error:", e.message);
  });
  const menu = (name) => page.locator(`#${name}-model-menu`);
  const row = (name, id) =>
    menu(name).locator(`.model-entry[data-model="${id}"]`);
  async function open(name) {
    if (!(await menu(name).isVisible()))
      await page.locator(`#${name}-picker`).click();
    await menu(name).waitFor({ state: "visible", timeout: 3000 });
  }
  async function waitUntil(check) {
    for (let i = 0; i < 100; i++) {
      if (await check()) return;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error("Timed out waiting for model state");
  }
  function finish() {
    const item = pending.shift();
    assert(item);
    installed.add(item.model);
    item.res.end(JSON.stringify({ status: "success" }) + "\n");
  }
  const output = path.join(root, "logs/model-picker-test/browser");
  await fs.mkdir(output, { recursive: true });
  try {
    await page.goto(base);
    await page.locator("#model-picker").waitFor();
    await page.locator("input[name=local_url]").fill(url);
    await waitUntil(async () =>
      (await page.locator("#local-model-status").textContent()).includes(
        "1 installed vision",
      ),
    );
    await open("model");
    await row("model", REVIEW)
      .getByRole("button", { name: `Choose ${REVIEW}`, exact: true })
      .click();
    assert.equal(pulls.length, 0);
    assert(
      await page.locator("#run").isDisabled(),
      "Selected missing final model must block parsing",
    );
    assert(
      await page.locator("#preview").isEnabled(),
      "Preview does not require installed models",
    );
    await open("final_model");
    assert(
      await row("final_model", REVIEW)
        .getByRole("button", { name: `Choose ${REVIEW}`, exact: true })
        .isEnabled(),
    );
    assert(
      await row("final_model", FINAL)
        .getByRole("button", { name: `Choose ${FINAL}`, exact: true })
        .isDisabled(),
    );
    assert((await row("final_model", REVIEW).textContent()).includes("✓"));
    assert(
      (await row("final_model", FINAL).textContent()).includes("Not installed"),
    );
    await menu("final_model")
      .getByRole("button", { name: "Choose No model", exact: true })
      .click();
    assert(await page.locator("#run").isEnabled());
    await open("final_model");
    await row("final_model", FINAL)
      .getByRole("button", { name: `Install ${FINAL}`, exact: true })
      .click();
    await waitUntil(async () =>
      (await row("final_model", FINAL).textContent()).includes(
        "Installing 20%",
      ),
    );
    assert.equal(pulls.length, 1);
    assert.equal(
      await page.locator("select[name=final_model]").inputValue(),
      "none",
      "Install must not select a model",
    );
    assert(
      await row("final_model", FINAL).locator(".model-install").isDisabled(),
    );
    await open("model");
    await waitUntil(async () =>
      (await row("model", FINAL).textContent()).includes("Installing 20%"),
    );
    await page.waitForTimeout(3500);
    assert(
      await menu("model").isVisible(),
      "Progress polling must not close the menu",
    );
    finish();
    await waitUntil(
      async () =>
        await row("model", FINAL)
          .getByRole("button", { name: `Choose ${FINAL}`, exact: true })
          .isEnabled(),
    );
    assert.equal(pulls.length, 1);
    await page.keyboard.press("Escape");
    assert(!(await menu("model").isVisible()));
    assert.equal(
      await page
        .locator("#model-picker")
        .evaluate((el) => el === document.activeElement),
      true,
    );
    await open("final_model");
    await row("final_model", FINAL)
      .getByRole("button", { name: `Choose ${FINAL}`, exact: true })
      .click();
    assert.equal(
      await page.locator("select[name=final_model]").inputValue(),
      FINAL,
    );
    failNext = true;
    await open("model");
    await row("model", LARGE)
      .getByRole("button", { name: `Install ${LARGE}`, exact: true })
      .click();
    await waitUntil(async () =>
      (await row("model", LARGE).textContent()).includes(
        "Not enough disk space",
      ),
    );
    assert(
      await row("model", LARGE)
        .getByRole("button", { name: `Choose ${LARGE}`, exact: true })
        .isDisabled(),
    );
    await row("model", LARGE)
      .getByRole("button", { name: `Retry installing ${LARGE}`, exact: true })
      .click();
    await waitUntil(async () =>
      (await row("model", LARGE).textContent()).includes("Installing 20%"),
    );
    finish();
    await waitUntil(
      async () =>
        await row("model", LARGE)
          .getByRole("button", { name: `Choose ${LARGE}`, exact: true })
          .isEnabled(),
    );
    assert.equal(pulls.length, 3);
    await page.screenshot({
      path: path.join(output, "desktop.png"),
      fullPage: false,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.keyboard.press("Escape");
    await open("final_model");
    const bounds = await menu("final_model").boundingBox();
    assert(
      bounds.x >= 0 && bounds.x + bounds.width <= 390,
      JSON.stringify(bounds),
    );
    assert(
      bounds.y >= 0 && bounds.y + bounds.height <= 844,
      JSON.stringify(bounds),
    );
    await page.screenshot({
      path: path.join(output, "mobile.png"),
      fullPage: false,
    });
    await page.keyboard.press("Escape");
    await page.locator("#settings-file").setInputFiles({
      name: "local-server-settings.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({ version: 2, settings: { local_url: emptyUrl } }),
      ),
    });
    await waitUntil(async () =>
      (await page.locator("#local-model-status").textContent()).includes(
        "0 installed vision",
      ),
    );
    assert(
      await page.locator("#run").isDisabled(),
      "Imported server settings must clear stale installed state",
    );
    await open("model");
    assert(
      await row("model", REVIEW)
        .getByRole("button", { name: `Choose ${REVIEW}`, exact: true })
        .isDisabled(),
    );
    await page.keyboard.press("Escape");
    await page.locator("input[name=local_url]").fill(url);
    await waitUntil(async () =>
      (await page.locator("#local-model-status").textContent()).includes(
        "3 installed vision",
      ),
    );
    await open("model");
    await row("model", REVIEW)
      .getByRole("button", { name: `Delete ${REVIEW}`, exact: true })
      .click();
    await page.waitForTimeout(1700);
    assert(
      await row("model", REVIEW)
        .getByRole("button", { name: `Confirm delete ${REVIEW}`, exact: true })
        .isVisible(),
      "Polling must preserve the deletion confirmation",
    );
    await row("model", REVIEW)
      .getByRole("button", { name: `Cancel deleting ${REVIEW}`, exact: true })
      .click();
    assert.equal(deletes.length, 0);
    assert(!(await row("model", "none").locator(".model-delete").count()));
    failDelete = true;
    await row("model", REVIEW)
      .getByRole("button", { name: `Delete ${REVIEW}`, exact: true })
      .click();
    await row("model", REVIEW)
      .getByRole("button", { name: `Confirm delete ${REVIEW}`, exact: true })
      .click();
    await waitUntil(async () =>
      (await row("model", REVIEW).textContent()).includes("Deletion denied"),
    );
    assert(
      await row("model", REVIEW)
        .getByRole("button", { name: `Choose ${REVIEW}`, exact: true })
        .isEnabled(),
    );
    await row("model", REVIEW)
      .getByRole("button", { name: `Cancel deleting ${REVIEW}`, exact: true })
      .click();
    const custom = "fixture-vision:custom";
    installed.add(custom);
    await menu("model")
      .getByRole("button", { name: "Refresh", exact: true })
      .click();
    await waitUntil(async () => await row("model", custom).count());
    await row("model", custom)
      .getByRole("button", { name: `Choose ${custom}`, exact: true })
      .click();
    await open("final_model");
    await row("final_model", custom)
      .getByRole("button", { name: `Choose ${custom}`, exact: true })
      .click();
    await open("model");
    await row("model", custom)
      .getByRole("button", { name: `Delete ${custom}`, exact: true })
      .click();
    await row("model", custom)
      .getByRole("button", { name: `Confirm delete ${custom}`, exact: true })
      .click();
    await waitUntil(async () => pendingDeletes.length === 1);
    assert(
      await row("model", custom)
        .getByRole("button", { name: `Choose ${custom}`, exact: true })
        .isDisabled(),
    );
    assert(await page.locator("#run").isDisabled());
    await open("final_model");
    assert(
      (await row("final_model", custom).textContent()).includes(
        "Deleting weights",
      ),
    );
    const removed = pendingDeletes.shift();
    installed.delete(removed.model);
    removed.res.end(); // Native Ollama deletion has an empty 200 response.
    await waitUntil(
      async () =>
        await row("final_model", custom)
          .getByRole("button", { name: `Install ${custom}`, exact: true })
          .isEnabled(),
    );
    for (const name of ["model", "final_model"])
      assert.equal(
        await page.locator(`select[name=${name}]`).inputValue(),
        custom,
        "Deleting a selected custom model must preserve the unavailable selection",
      );
    assert(await page.locator("#run").isDisabled());
    assert(
      installed.has(REVIEW) && installed.has(FINAL) && installed.has(LARGE),
    );
    assert.deepEqual(deletes, [REVIEW, custom]);
    await row("final_model", custom)
      .getByRole("button", { name: `Install ${custom}`, exact: true })
      .click();
    await waitUntil(async () => pending.length === 1);
    finish();
    await waitUntil(
      async () =>
        await row("final_model", custom)
          .getByRole("button", { name: `Choose ${custom}`, exact: true })
          .isEnabled(),
    );
    assert(await page.locator("#run").isEnabled());
    assert.equal(pulls.length, 4);
    await page.keyboard.press("Escape");
    installed.clear();
    await page.locator("#local-model-refresh").click();
    await waitUntil(async () =>
      (await page.locator("#local-model-status").textContent()).includes(
        "0 installed vision",
      ),
    );
    await open("model");
    assert(
      await row("model", REVIEW)
        .getByRole("button", { name: `Choose ${REVIEW}`, exact: true })
        .isDisabled(),
    );
    await page.keyboard.press("Escape");
    await page.locator("select[name=review_provider]").selectOption("work");
    assert(await page.locator("select[name=model]").isVisible());
    assert(!(await page.locator("#model-picker").isVisible()));
    assert.deepEqual(errors, []);
    const report = {
      passed: true,
      pulls: pulls.map((p) => p.model),
      deletes,
      realWeightDownloads: 0,
      workCalls: 0,
      checks: [
        "installed/missing icons",
        "disabled missing choices",
        "inline install",
        "shared progress",
        "polling keeps menus open",
        "no automatic selection/download",
        "verified models selectable",
        "failure/retry",
        "external removal refreshed",
        "delete cancellation",
        "delete failure keeps model installed",
        "deletion state shared between menus",
        "selected custom model retained after deletion",
        "deleted model can be reinstalled",
        "server change clears installed state",
        "keyboard",
        "mobile bounds",
        "Work native choices",
      ],
    };
    await fs.writeFile(
      path.join(output, "report.json"),
      JSON.stringify(report, null, 2),
    );
    console.log(JSON.stringify(report));
  } catch (error) {
    await page.screenshot({
      path: path.join(output, "failure.png"),
      fullPage: false,
    });
    await fs.writeFile(path.join(output, "failure.html"), await page.content());
    throw error;
  } finally {
    for (const item of pending) item.res.end();
    for (const item of pendingDeletes) item.res.end();
    await browser.close();
    server.closeAllConnections();
    emptyServer.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    await new Promise((resolve) => emptyServer.close(resolve));
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
