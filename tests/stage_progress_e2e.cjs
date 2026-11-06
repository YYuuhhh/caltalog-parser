/* Stage counters and estimates, with no model inference. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const base = process.env.PIPELINE_UI_URL || "http://127.0.0.1:17861";
const stages = [
  "catalog",
  "parse",
  "raw",
  "correctness",
  "completeness",
  "internet",
  "mapping",
  "structure",
  "duplicates",
  "final",
  "validate",
];
(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
    reducedMotion: "reduce",
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const run = {
    id: "f".repeat(32),
    source_name: "Stage progress fixture.pdf",
    status: "running",
    current_stage: "parse",
    config: {},
    review_tasks: [],
    files: [],
    pages: { total: 10, parsed: 5 },
    stages: stages.map((id) => ({
      id,
      title: id,
      status:
        id === "catalog" ? "completed" : id === "parse" ? "running" : "queued",
    })),
  };
  run.stages[1].progress = {
    percent: 40,
    estimated: true,
    detail: "5/10 pages saved",
  };
  const output = path.resolve(__dirname, "../logs/stage-progress-test/browser");
  await fs.mkdir(output, { recursive: true });
  const bar = (id) =>
    page.locator(`.stage-result[data-stage-id="${id}"] [role=progressbar]`);
  const sectionBar = (id) =>
    page.locator(`#${id} > .stage-mini-progress [role=progressbar]`);
  async function waitFor(check) {
    for (let i = 0; i < 70; i++) {
      if (await check()) return;
      await page.waitForTimeout(100);
    }
    throw new Error("Stage progress did not update");
  }
  try {
    run.config = (
      await (await page.request.get(base + "/api/bootstrap")).json()
    ).defaults;
    await page.route("**/api/runs", (route) => route.fulfill({ json: [run] }));
    await page.route("**/api/queue", (route) =>
      route.fulfill({
        json: {
          paused: run.status === "paused",
          active_run: run.id,
          queued_runs: [],
          status: run.status,
        },
      }),
    );
    await page.route(`**/api/runs/${run.id}/stoppage`, (route) =>
      route.fulfill({
        json: {
          stoppage: run.stoppage || null,
          diagnostics: [],
          log_url: null,
        },
      }),
    );
    await page.goto(base);
    await bar("parse").waitFor();
    assert.equal(
      await page.locator(".stage-card > .stage-mini-progress").count(),
      7,
    );
    assert.equal(
      await page.locator(".stage-result [role=progressbar]").count(),
      11,
    );
    assert.equal(await bar("catalog").getAttribute("aria-valuenow"), "100");
    assert.equal(await bar("parse").getAttribute("aria-valuenow"), "40");
    assert.equal(
      await sectionBar("extract").getAttribute("aria-valuenow"),
      "20",
    );
    assert.equal(await bar("final").getAttribute("aria-valuenow"), "0");
    assert(
      (await bar("parse").getAttribute("aria-valuetext")).includes(
        "5/10 pages",
      ),
    );
    run.stages[1].progress = {
      percent: 90,
      estimated: true,
      detail: "Quality check · 5/10 pages",
    };
    await waitFor(
      async () => (await bar("parse").getAttribute("aria-valuenow")) === "90",
    );
    assert.equal(
      await sectionBar("extract").getAttribute("aria-valuenow"),
      "45",
    );
    run.stages[1].status = "completed";
    run.stages[2].status = "running";
    run.stages[2].started_at = new Date(Date.now() - 30000).toISOString();
    run.current_stage = "raw";
    await waitFor(
      async () => Number(await bar("raw").getAttribute("aria-valuenow")) > 0,
    );
    assert((await bar("raw").getAttribute("aria-valuetext")).includes("≈"));
    assert(Number(await bar("raw").getAttribute("aria-valuenow")) < 100);
    await page.locator("#extract").scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, "desktop.png") });
    run.status = "paused";
    run.stoppage = {
      kind: "paused",
      stage: "raw",
      stage_title: "RAW export",
      occurred_at: new Date().toISOString(),
      reason: "Paused for test",
    };
    await waitFor(
      async () =>
        await page
          .locator('[data-stage-id="raw"] .stage-mini-progress.paused')
          .count(),
    );
    const pausedValue = await bar("raw").getAttribute("aria-valuenow");
    await page.waitForTimeout(3400);
    assert.equal(
      await bar("raw").getAttribute("aria-valuenow"),
      pausedValue,
      "Paused estimates must freeze",
    );
    run.status = "failed";
    run.stages[2].status = "failed";
    run.stoppage = {
      ...run.stoppage,
      kind: "failed",
      reason: "Fixture failure",
    };
    await waitFor(
      async () =>
        await page
          .locator('[data-stage-id="raw"] .stage-mini-progress.failed')
          .count(),
    );
    assert(Number(await bar("raw").getAttribute("aria-valuenow")) < 100);
    run.status = "running";
    run.stoppage = null;
    for (const stage of run.stages)
      stage.status = stage.id === "raw" ? "running" : "completed";
    run.stages[2].progress = {
      percent: 99,
      estimated: true,
      detail: "Finishing",
    };
    await waitFor(
      async () => (await bar("raw").getAttribute("aria-valuenow")) === "99",
    );
    assert.equal(
      await sectionBar("extract").getAttribute("aria-valuenow"),
      "99",
      "Section rounding must not report completion early",
    );
    assert(
      (
        await page
          .locator("#catalog-progress-list .progress-caption")
          .textContent()
      ).includes("99%"),
    );
    for (const stage of run.stages)
      stage.status =
        stage.id === "internet" || stage.id === "final"
          ? "skipped"
          : "completed";
    run.status = "quality_blocked";
    run.stoppage = null;
    await waitFor(async () =>
      (await bar("final").getAttribute("aria-valuetext")).includes("Skipped"),
    );
    assert.equal(await bar("final").getAttribute("aria-valuenow"), "0");
    assert(
      (await bar("final").getAttribute("aria-valuetext")).includes("Skipped"),
    );
    assert.equal(
      await sectionBar("review").getAttribute("aria-valuenow"),
      "100",
    );
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("#results").scrollIntoViewIfNeeded();
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    );
    await page.screenshot({ path: path.join(output, "mobile.png") });
    for (const value of await page
      .locator(".stage-mini-track")
      .evaluateAll((items) =>
        items.map((item) => Number(item.getAttribute("aria-valuenow"))),
      ))
      assert(value >= 0 && value <= 100);
    assert.deepEqual(errors, []);
    const report = {
      passed: true,
      checks: [
        "seven pipeline sections",
        "eleven individual stages",
        "page counters",
        "quality phase",
        "partial overall progress",
        "approximate elapsed progress",
        "pause freezes estimates",
        "failed stage below 100",
        "skipped checks distinct",
        "completion",
        "mobile layout",
        "accessible counters",
      ],
      modelCalls: 0,
    };
    await fs.writeFile(
      path.join(output, "report.json"),
      JSON.stringify(report, null, 2),
    );
    console.log(JSON.stringify(report));
  } catch (error) {
    await page.screenshot({ path: path.join(output, "failure.png") });
    await fs.writeFile(path.join(output, "failure.html"), await page.content());
    throw error;
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
