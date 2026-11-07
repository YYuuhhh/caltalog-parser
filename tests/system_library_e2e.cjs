/* Live diagnostics, machine defaults, library inventory and optional CPU OCR. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const base = process.env.PIPELINE_UI_URL || "http://127.0.0.1:17861";
(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const boot = await (await page.request.get(base + "/api/bootstrap")).json();
  const run = {
    id: "f".repeat(32),
    source_name: "Performance fixture.pdf",
    status: "running",
    created_at: new Date().toISOString(),
    current_stage: "correctness",
    config: boot.defaults,
    review_tasks: [],
    files: [],
    official_sources: {
      status: "ready",
      official_urls: ["https://guehring.com/de-en/"],
      snapshot_sha256: "e".repeat(64),
    },
    stages: [
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
    ].map((id, i) => ({
      id,
      title: id,
      status: i < 3 ? "completed" : i === 3 ? "running" : "queued",
    })),
    performance: {
      hardware: {
        cpu: "Fixture guest CPU",
        logical_cpus: 50,
        physical_cores: 50,
        virtualized: true,
        memory_bytes: 117 * 1024 ** 3,
      },
      current: {
        cpu_percent: 93,
        memory_available_bytes: 40 * 1024 ** 3,
        swap_used_bytes: 1024 ** 3,
        swap_in_bytes_sec: 0,
        swap_out_bytes_sec: 1024,
        iowait_percent: 12,
      },
      peak: { cpu_percent: 97, memory_used_bytes: 80 * 1024 ** 3 },
      bottlenecks: [
        {
          kind: "swap",
          text: "Active host swap I/O; model-specific attribution unavailable.",
        },
      ],
      scope: "Host-wide resources",
    },
    generation: {
      model: "qwen3-vl:32b-thinking-bf16",
      status: "completed",
      tokens: 516,
      tokens_per_second: 10,
      tokens_estimated: false,
      prompt_tokens_per_second: 12,
      first_token_seconds: 45,
      thinking_chunks: 10,
      thinking_chars: 100,
      answer_chunks: 5,
      answer_chars: 50,
    },
  };
  let installed = false,
    installs = 0;
  run.performance.inference_process = {
    available: true,
    rss_bytes: 63 * 1024 ** 3,
    swap_bytes: 2 * 1024 ** 3,
    cpu_cores_used: 12,
    major_faults_per_second: 4,
  };
  run.performance.peak.inference_rss_bytes = 64 * 1024 ** 3;
  await page.route("**/api/runs", (r) => r.fulfill({ json: [run] }));
  await page.route("**/api/runs/" + run.id, (r) => r.fulfill({ json: run }));
  await page.route("**/api/runs/" + run.id + "/log", (r) =>
    r.fulfill({ json: { text: "" } }),
  );
  await page.route("**/api/queue", (r) =>
    r.fulfill({ json: { paused: false, active_run: run.id, queued_runs: [] } }),
  );
  await page.route("**/api/library", (r) =>
    r.fulfill({
      json: {
        manufacturers: boot.manufacturers,
        target_manufacturer_id: "guhring",
        duplicates: [],
        catalogs: [
          {
            id: "loaded",
            name: "Waiting.pdf",
            manufacturer: "Gühring",
            revision: "unknown",
            pages: 6,
            product_count: 0,
            status: "loaded",
            technical: null,
          },
          {
            id: "done",
            name: "Reviewed.pdf",
            manufacturer: "Gühring",
            revision: "2026",
            pages: 6,
            product_count: 12,
            status: "validated",
            last_run: run.id,
            technical: {
              completed_at: "2026-11-06T13:00:00Z",
              confidence: { average: 0.96, worst: 0.88 },
              extraction_model: "mineru-3.4.5-pipeline",
              checks: [
                {
                  stage: "correctness",
                  model: "qwen3-vl:32b-thinking-bf16",
                  reasoning: "medium",
                  provider: "ollama_local",
                },
              ],
              parameters: { local_temperature: 0 },
              stages: [],
            },
          },
        ],
      },
    }),
  );
  await page.route("**/api/ocr/baidu?*", (r) =>
    r.fulfill({
      json: { available: true, installed, device: "cpu", experimental: true },
    }),
  );
  await page.route("**/api/ocr/baidu/install", (r) => {
    installs++;
    installed = true;
    return r.fulfill({ status: 202, json: { status: "completed" } });
  });
  try {
    await page.goto(base);
    await page.locator("#model-picker").waitFor();
    assert.equal(
      await page.locator("input[name=local_temperature]").inputValue(),
      "0",
    );
    assert.equal(
      await page.locator("#reasoning-value").textContent(),
      "Thinking on",
    );
    await page.locator("#performance-status").waitFor({ state: "visible" });
    assert.match(
      await page.locator("#performance-status").textContent(),
      /10 tokens\/s \(measured\)/,
    );
    assert.match(
      await page.locator("#performance-status").textContent(),
      /ACTIVE PAGING/,
    );
    assert.match(
      await page.locator("#performance-status").textContent(),
      /sampled peak 64.0 GiB · swapped 2.0 GiB/,
    );
    assert.match(
      await page.locator("#performance-status").textContent(),
      /50 guest cores/,
    );
    assert.match(
      await page.locator("#performance-status").textContent(),
      /Thinking: 10 fragments \/ 100 characters · answer: 5 fragments \/ 50 characters/,
    );
    await page.locator("#performance-status summary").click();
    run.performance.current.cpu_percent = 84;
    await page.waitForTimeout(3400);
    assert.equal(
      await page.locator("#performance-status").getAttribute("open"),
      null,
    );
    await page.locator("#sidebar-library").click();
    const loaded = page.locator("#library-catalogs tr.catalog-row.loaded");
    await loaded.waitFor();
    assert.match(await loaded.getAttribute("title"), /Loaded, not parsed/);
    assert.notEqual(
      await loaded.evaluate((el) => getComputedStyle(el).backgroundColor),
      "rgba(0, 0, 0, 0)",
    );
    assert.equal(
      await page.locator("#library-catalogs .catalog-state").count(),
      0,
    );
    assert.equal(
      await page
        .locator("#library-catalogs th")
        .filter({ hasText: /^Status$/ })
        .count(),
      0,
    );
    assert.match(
      await page.locator("#library-catalogs").textContent(),
      /96.0% \/ 88.0%/,
    );
    await page.locator(".catalog-technical summary").click();
    assert.match(
      await page.locator(".catalog-technical").textContent(),
      /qwen3-vl:32b-thinking-bf16/,
    );
    await fs.mkdir("logs/system-library-test/browser", { recursive: true });
    await page.screenshot({
      path: "logs/system-library-test/browser/library.png",
      fullPage: true,
    });
    await page.locator("#show-pipeline").click();
    await page.locator(".ocr-runtime-setup summary").click();
    await page
      .getByRole("button", { name: "Check CPU OCR service", exact: true })
      .click();
    await page.waitForFunction(
      () => !document.getElementById("ocr-install").disabled,
    );
    assert.equal(await page.locator("#ocr-install").isEnabled(), true);
    await page.locator("#ocr-install").click();
    await page
      .getByText("CPU extraction model ready.", { exact: false })
      .waitFor();
    assert.equal(installs, 1);
    await page
      .locator("select[name=extraction_model]")
      .selectOption("baidu/Unlimited-OCR-cpu");
    assert.equal(
      await page.locator("#extract select[name=extraction_model]").count(),
      1,
    );
    assert.equal(await page.locator("select[name=ocr_check_model]").count(), 0);
    assert.equal(await page.locator("#baidu-fields").isVisible(), true);
    assert.equal(await page.locator("select[name=method]").isDisabled(), true);
    assert.match(
      await page.locator("#official-source-status").textContent(),
      /fixed for this run/,
    );
    assert.equal(
      await page.locator("#official-source-status a").getAttribute("href"),
      "https://guehring.com/de-en/",
    );
    await page.locator("#performance-status summary").click();
    await page.screenshot({
      path: "logs/system-library-test/browser/performance.png",
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    );
    assert.deepEqual(errors, []);
    console.log(
      "System/library/OCR UI checks passed; no inference or downloads performed.",
    );
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
