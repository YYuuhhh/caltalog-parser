/* Browser contract for the vertical, manufacturer-based catalog workflow. */
const { chromium } = require("playwright");
const fs = require("node:fs/promises");
const path = require("node:path");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "..");
const testCatalogsRoot =
  process.env.PIPELINE_TEST_CATALOGS_ROOT || path.join(root, "catalogs");
const base = process.env.PIPELINE_UI_URL || "http://127.0.0.1:7861";
(async () => {
  const output = path.join(
    root,
    "logs/ui-tests",
    new Date().toISOString().replaceAll(":", "-"),
  );
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    reducedMotion: "reduce",
    acceptDownloads: true,
  });
  const page = await context.newPage();
  const errors = [];
  const uploadedPaths = [];
  page.on("response", async (response) => {
    if (response.url().includes("/api/upload?name=ui-test-catalog-")) {
      try {
        const uploaded = await response.json();
        if (uploaded.path) uploadedPaths.push(uploaded.path);
      } catch {}
    }
  });
  page.on("pageerror", (e) => errors.push(e.message));
  try {
    for (let i = 0; i < 30; i++) {
      try {
        const ready = await page.request.get(base + "/health");
        if (ready.ok()) break;
      } catch {}
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    await page.goto(base);
    await page.locator("#model-picker").waitFor();
    const boot = await (await page.request.get(base + "/api/bootstrap")).json();
    assert(boot.manufacturers.length >= 50);
    assert.equal(boot.defaults.languages, "en,ru,zh");
    for (const language of ["en", "ru", "zh"])
      assert(
        await page.locator(`input[data-language=${language}]`).isChecked(),
      );
    assert.equal(
      await page.locator("input[data-language=de]").isChecked(),
      false,
    );
    assert.equal(
      await page.locator("#catalog-progress").evaluate((el) => el.open),
      false,
    );
    const windowsSources = boot.catalogs.filter(
      (c) => c.origin === "Windows · GUHRING",
    );
    if (windowsSources.length) {
      assert(boot.defaults.source_pdf.startsWith("/catalogs/imports/guhring/"));
      assert(
        (await page.locator("#source-list").textContent()).includes(
          "Windows · GUHRING",
        ),
      );
    }
    assert.equal(
      await page.locator("#target-manufacturer option").count(),
      boot.manufacturers.length,
    );
    for (const id of [
      "catalog",
      "extract",
      "review",
      "mapping",
      "structure",
      "final",
      "results",
    ])
      assert(await page.locator("#" + id).isVisible(), id);
    assert.equal(
      await page.locator("input[name=page_selection]").isChecked(),
      false,
    );
    assert.equal(await page.locator("#page-fields").isVisible(), false);
    assert.equal(
      await page.locator("select[name=model]").inputValue(),
      boot.defaults.model,
    );
    assert.equal(
      await page.locator("select[name=final_model]").inputValue(),
      boot.defaults.final_model,
    );
    assert.equal(
      await page.locator("select[name=review_provider]").inputValue(),
      "local",
    );
    assert.equal(
      await page.locator("select[name=final_provider]").inputValue(),
      "local",
    );
    assert.equal(await page.locator("input[name=local_use_gpu]").count(), 0);
    for (const name of ["model", "final_model"]) {
      await page.locator(`#${name}-picker`).click();
      await page
        .locator(`#${name}-model-menu`)
        .getByRole("button", { name: "Choose No model", exact: true })
        .click();
    }
    assert(await page.locator("input[name=reasoning]").isDisabled());
    assert(await page.locator("input[name=final_reasoning]").isDisabled());
    assert(
      (await page.locator("#review-mode-note").textContent()).includes(
        "remains unvalidated",
      ),
    );
    const noModelResponse = page.waitForResponse("**/api/plan");
    await page.locator("#preview").click();
    const noModelPlan = await (await noModelResponse).json();
    assert.equal(
      noModelPlan.stages.filter((stage) => !stage.enabled).length,
      5,
    );
    assert(noModelPlan.stages.find((stage) => stage.id === "validate").enabled);
    await page.locator("#close-preview").click();
    await page.locator("select[name=local_ram_gb]").selectOption("750");
    assert.equal(
      await page.locator("select[name=model]").inputValue(),
      "qwen3-vl:235b-a22b-instruct-bf16",
    );
    await page.locator("select[name=local_ram_gb]").selectOption("32");
    await page.locator("select[name=review_provider]").selectOption("work");
    await page.locator("select[name=final_provider]").selectOption("work");
    assert(await page.locator("input[name=automatic_review]").isChecked());
    for (const name of ["reasoning", "final_reasoning"])
      assert.equal(
        await page
          .locator(`input[name=${name}]`)
          .getAttribute("aria-valuetext"),
        "Medium",
      );
    const restrictedModels = structuredClone(boot.models);
    restrictedModels.work_models.find(
      (m) => m.id === "gpt-6-luna",
    ).reasoning_levels = ["medium", "high"];
    await page.route("**/api/models", (route) =>
      route.fulfill({ json: restrictedModels }),
    );
    await page.locator("#model-refresh").click();
    await page.locator("select[name=model]").selectOption("gpt-6-luna");
    await page.waitForFunction(
      () => document.querySelector("input[name=reasoning]").max === "1",
    );
    assert.equal(
      await page.locator("input[name=reasoning]").getAttribute("max"),
      "1",
    );
    assert.equal(
      await page.locator("#reasoning-value").textContent(),
      "Medium",
    );
    await page.locator("input[name=reasoning]").focus();
    await page.keyboard.press("Home");
    assert.equal(
      await page.locator("#reasoning-value").textContent(),
      "Medium",
    );
    await page.unroute("**/api/models");
    await page.locator("#model-refresh").click();
    await page.locator("select[name=model]").selectOption("gpt-6-sol");
    await page.locator("input[name=reasoning]").focus();
    await page.keyboard.press("Home");
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    assert.equal(await page.locator("input[name=work_mode]").count(), 0);
    assert.equal(await page.locator('option[value="chatgpt"]').count(), 0);
    assert.equal(
      await page.locator("select[name=extraction_model] option").count(),
      1,
    );
    assert.equal(await page.locator("input[name=review_rows]").count(), 0);
    assert.equal(await page.locator("input[name=catalog_id]").count(), 0);
    assert.equal(
      await page.locator("input[name=automatic_review]").isChecked(),
      true,
    );
    await page.locator("input[name=automatic_review]").uncheck();
    assert.equal(
      await page.locator("select[name=model] option").count(),
      boot.models.work_models.length + 1,
    );
    await page.locator("input[name=final_reasoning]").focus();
    await page.keyboard.press("End");
    assert.equal(
      await page.locator("#final_reasoning-value").textContent(),
      "Maximum",
    );
    await page.locator("select[name=final_model]").selectOption("none");
    assert(await page.locator("input[name=final_reasoning]").isDisabled());
    assert.equal(
      await page.locator("#final_reasoning-value").textContent(),
      "Off",
    );
    assert(
      (await page.locator("#final-model-note").textContent()).includes(
        "unvalidated",
      ),
    );
    await page.locator("select[name=final_model]").selectOption("gpt-6-astra");
    assert.equal(
      await page.locator("#final_reasoning-value").textContent(),
      "Maximum",
    );
    assert.equal(
      await page.locator("input[name=final_reasoning]").isDisabled(),
      false,
    );
    await page.locator("input[name=reasoning]").focus();
    await page.keyboard.press("Home");
    assert.equal(await page.locator("#reasoning-value").textContent(), "Low");
    await page
      .locator("#extract details > summary")
      .filter({ hasText: "Extraction quality & limits" })
      .click();
    await page
      .getByRole("button", { name: "About Required confidence", exact: true })
      .focus();
    assert(await page.locator("#parameter-tooltip").isVisible());
    assert(
      (await page.locator("#parameter-tooltip").textContent()).includes(
        "not a calibrated probability",
      ),
    );
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#parameter-tooltip").isVisible(), false);
    const reasoningPlanResponse = page.waitForResponse((r) =>
      r.url().endsWith("/api/plan"),
    );
    await page.locator("#preview").click();
    await page.locator("#plan-dialog").waitFor({ state: "visible" });
    const reasoningPlan = await (await reasoningPlanResponse).json();
    assert.equal(reasoningPlan.reasoning, "low");
    assert.equal(reasoningPlan.final_reasoning, "max");
    assert.equal(reasoningPlan.config.model, "gpt-6-sol");
    assert.equal(reasoningPlan.config.final_model, "gpt-6-astra");
    assert.equal(reasoningPlan.config.automatic_review, false);
    assert.equal("work_mode" in reasoningPlan.config, false);
    assert(
      (await page.locator("#plan-body").textContent()).includes("36/36 pages"),
    );
    await page.locator("#close-preview").click();
    await page.locator("input[name=page_selection]").check();
    assert(await page.locator("#page-fields").isVisible());
    await page.locator("input[name=first_page]").fill("37");
    await page.locator("#preview").click();
    await page.locator("#error").waitFor({ state: "visible" });
    assert((await page.locator("#error").textContent()).includes("36 pages"));
    await page.locator("input[name=first_page]").fill("1");
    await page.locator("input[name=page_selection]").uncheck();
    await page.locator("select[name=model]").selectOption("gpt-6-luna");
    assert.equal(
      await page.locator("select[name=model]").inputValue(),
      "gpt-6-luna",
    );
    assert.equal(
      await page.locator("select[name=final_model]").inputValue(),
      "gpt-6-astra",
    );
    assert(
      (await page.locator("#review-mode-note").textContent()).includes(
        "Manual",
      ),
    );
    await page.locator("select[name=model]").selectOption("gpt-6-astra");
    assert.equal(
      await page.locator("select[name=final_model]").inputValue(),
      "gpt-6-astra",
    );
    assert.equal(
      await page
        .locator("input[name=reasoning]")
        .getAttribute("aria-valuetext"),
      "Low",
    );
    await page.locator("select[name=model]").selectOption("gpt-6-sol");
    await page.locator("select[name=final_model]").selectOption("gpt-6-astra");
    assert.equal(
      await page.locator("select[name=model]").inputValue(),
      "gpt-6-sol",
    );
    await page.locator("#mapping-manufacturer").selectOption("sowin");
    await page.waitForFunction(() =>
      document
        .getElementById("dictionary-summary")
        .textContent.startsWith("SOWIN"),
    );
    assert(
      (await page.locator("#dictionary").textContent()).includes(
        "own empty draft",
      ),
    );
    await page.locator("#mapping-manufacturer").selectOption("guhring");
    await page.locator("#theme-toggle").click();
    assert.equal(
      await page.locator("html").getAttribute("data-theme"),
      "light",
    );
    await page.reload();
    await page.locator("#model-picker").waitFor();
    assert.equal(
      await page.locator("html").getAttribute("data-theme"),
      "light",
    );
    await page.locator("select[name=review_provider]").selectOption("work");
    await page.locator("select[name=final_provider]").selectOption("work");
    await page.locator("#theme-toggle").click();
    // Multi-file upload is real; each source keeps its own manufacturer control.
    const uploadFile = path.join(
      root,
      "catalogs/input/GTC_Milling_Cutters 2018.pdf",
    );
    const content = await fs.readFile(uploadFile);
    await page.locator("#pdf-upload").setInputFiles([
      {
        name: "ui-test-catalog-a.pdf",
        mimeType: "application/pdf",
        buffer: content,
      },
      {
        name: "ui-test-catalog-b.pdf",
        mimeType: "application/pdf",
        buffer: content,
      },
    ]);
    await page.waitForFunction(() =>
      document
        .getElementById("upload-status")
        .textContent.includes("2 PDFs added"),
    );
    assert(
      (await page.locator("#selected-count").textContent()).includes(
        "3 selected",
      ),
    );
    const newSources = await page.evaluate(() =>
      [...document.querySelectorAll(".source-row")]
        .filter((row) => row.textContent.includes("ui-test-catalog-"))
        .map((row) => ({
          manufacturer: row.querySelector("select").value,
          languages: row.querySelector(".catalog-language").value,
        })),
    );
    assert(
      newSources.length === 2 &&
        newSources.every(
          (s) => s.manufacturer === "guhring" && s.languages.includes("en"),
        ),
    );
    const overrideLanguage = page
      .locator(".source-row")
      .filter({ hasText: "ui-test-catalog-a.pdf" })
      .locator(".catalog-language");
    await overrideLanguage.fill("ru");
    await overrideLanguage.blur();
    const overrideResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/plan") &&
        response
          .request()
          .postDataJSON()
          .source_pdf.endsWith("-ui-test-catalog-a.pdf"),
    );
    await page.locator("#preview").click();
    const overridePlan = await (await overrideResponse).json();
    assert.equal(overridePlan.config.languages, "ru");
    assert.equal(overridePlan.config.parser_language, "east_slavic");
    assert.equal(overridePlan.config.automatic_review, true);
    await page.locator("#plan-dialog").waitFor({ state: "visible" });
    assert.equal(await page.locator("#plan-body>.notice").count(), 3);
    await page.locator("#close-preview").click();
    await page.locator("#show-library").click();
    await page.locator("#library-view").waitFor({ state: "visible" });
    await page.waitForFunction(
      () => document.querySelectorAll(".manufacturer-tile").length >= 50,
    );
    assert(
      (await page.locator("#library-manufacturers").textContent()).includes(
        "Gühring",
      ),
    );
    await page.locator("#library-search").fill("SOWIN");
    await page.waitForFunction(
      () => document.querySelectorAll(".manufacturer-tile").length === 1,
    );
    assert(
      (await page.locator("#library-manufacturers").textContent()).includes(
        "SOWIN",
      ),
    );
    await page.locator("#show-pipeline").click();
    const savedRuns = await (await page.request.get(base + "/api/runs")).json();
    const evidenceRun = savedRuns.find(
      (r) =>
        r.config.manufacturer_id &&
        r.status === "quality_blocked" &&
        r.files.some((f) => f.filename === "master_canonical.xlsx"),
    );
    if (evidenceRun) {
      await page.locator("#run-select").selectOption(evidenceRun.id);
      await page.waitForFunction(
        (id) =>
          document
            .getElementById("run-results")
            .textContent.includes("Catalog ID: " + id),
        evidenceRun.catalog_id,
      );
      const findings = page.locator(
        "#run-results details[data-panel=findings]",
      );
      await findings.locator("summary").click();
      await page.waitForTimeout(4200);
      assert(
        await findings.evaluate((d) => d.open),
        "Findings closed during a poll",
      );
      await page.route("**/api/runs", (route) =>
        route.fulfill({
          json: [
            {
              ...evidenceRun,
              metrics: {
                ...evidenceRun.metrics,
                blocking_issues:
                  (evidenceRun.metrics?.blocking_issues || 0) + 1,
              },
            },
          ],
        }),
      );
      await page.waitForTimeout(4200);
      assert(
        await page
          .locator("#run-results details[data-panel=findings]")
          .evaluate((d) => d.open),
        "Findings closed after progress changed",
      );
      await page.unroute("**/api/runs");
      // Persist UI decisions in an isolated browser fixture; backend persistence
      // and stale-source rejection are covered by the temporary-workspace tests.
      const decisions = new Map();
      await page.route(
        `**/api/runs/${evidenceRun.id}/manual-review/decision`,
        (route) => {
          const decision = {
            ...route.request().postDataJSON(),
            saved_at: new Date().toISOString(),
          };
          decisions.set(decision.product_version_id, decision);
          return route.fulfill({ json: { decision } });
        },
      );
      await page.route(
        `**/api/runs/${evidenceRun.id}/manual-review`,
        async (route) => {
          const response = await route.fetch();
          const data = await response.json();
          for (const tool of data.tools || [])
            if (decisions.has(tool.product.product_version_id))
              tool.decision = decisions.get(tool.product.product_version_id);
          await route.fulfill({ json: data });
        },
      );
      await page
        .getByRole("button", { name: "Open manual tool review", exact: true })
        .click();
      await page.locator("#manual-dialog").waitFor({ state: "visible" });
      assert((await page.locator("#manual-tool-select option").count()) > 0);
      const labels = await page
        .locator("#manual-tool-list strong")
        .allTextContents();
      assert.equal(
        new Set(labels).size,
        labels.length,
        "Tool labels must distinguish unresolved candidates",
      );
      assert(await page.locator("#manual-tab-internet").isVisible());
      await page.locator("#manual-tab-internet").click();
      assert(
        (await page.locator("#manual-detail").textContent()).includes(
          "https://",
        ),
      );
      await page.locator("#manual-tab-findings").click();
      assert(
        (await page.locator("#manual-detail").textContent()).includes(
          "Findings from these source pages",
        ),
      );
      await page.locator("#manual-tab-parameters").click();
      await page.locator("#manual-zoom-in").click();
      assert.equal(await page.locator("#manual-fit").textContent(), "125%");
      await page.locator("#manual-fit").click();
      await page
        .locator("#manual-notes")
        .fill("Browser fixture: compare dimension letters with drawing.");
      await page.locator("#manual-correction").click();
      await page.waitForFunction(() =>
        document
          .getElementById("manual-save-status")
          .textContent.startsWith("Saved "),
      );
      await page.locator("#manual-filter").selectOption("needs_changes");
      assert.equal(await page.locator("#manual-tool-list button").count(), 1);
      await page.locator("#manual-search").fill("no-such-tool-name");
      assert.equal(await page.locator("#manual-tool-list button").count(), 0);
      await page.locator("#manual-search").fill("");
      await page.locator("#manual-filter").selectOption("all");
      await page.locator("#manual-next-pending").click();
      assert.equal(await page.locator("#manual-tool-select").inputValue(), "1");
      await page.locator("#manual-content img").first().waitFor();
      assert(
        (
          await page.locator("#manual-content img").first().getAttribute("src")
        ).includes("/source-page/"),
      );
      await page.locator("#close-manual").click();
      await page
        .getByRole("button", { name: "Open manual tool review", exact: true })
        .click();
      assert.equal(await page.locator("#manual-tool-select").inputValue(), "1");
      await page.locator("#manual-prev").click();
      assert.equal(
        await page.locator("#manual-notes").inputValue(),
        "Browser fixture: compare dimension letters with drawing.",
      );
      assert(
        (await page.locator("#manual-identity").textContent()).includes(
          "needs changes",
        ),
      );
      await page.locator("#close-manual").click();
      assert(
        (await page.locator("#run-results").textContent()).includes(
          "Average confidence",
        ),
      );
      assert(
        (await page.locator("#run-results").textContent()).includes(
          "Worst confidence",
        ),
      );
      if (await findings.evaluate((d) => d.open))
        await findings.locator("summary").click();
    }
    const layouts = [];
    for (const width of [360, 390, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      const layout = await page.evaluate(() => ({
        viewport: innerWidth,
        document: document.documentElement.scrollWidth,
        visible: [
          ...document.querySelectorAll(".vertical-pipeline>.stage-card"),
        ].filter((e) => getComputedStyle(e).display !== "none").length,
      }));
      assert(layout.document <= width, JSON.stringify(layout));
      assert.equal(layout.visible, 7);
      layouts.push(layout);
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.screenshot({
      path: path.join(output, "pipeline-desktop.png"),
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 900 });
    await page.screenshot({
      path: path.join(output, "pipeline-mobile.png"),
      fullPage: true,
    });
    // Exercise progress rendering with a paused multi-catalog queue without
    // invoking a second model run while the real integration test is active.
    const fixture = {
      id: "a".repeat(32),
      catalog_id: "guhring-fixture",
      manufacturer: "Gühring",
      source_name: "Paused catalog.pdf",
      status: "awaiting_review",
      config: {
        manufacturer_id: "guhring",
        model: "gpt-6-sol",
        reasoning: "low",
        final_model: "gpt-6-astra",
        final_reasoning: "max",
        automatic_review: false,
      },
      stages: boot.defaults
        ? [
            "Register",
            "Extract",
            "RAW",
            "Correctness",
            "Completeness",
            "Internet",
            "Mapping",
            "Structure",
            "Duplicates",
            "Final",
            "Validate",
          ].map((title, i) => ({
            id: [
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
            ][i],
            title,
            status: i < 3 ? "completed" : i === 3 ? "running" : "queued",
          }))
        : [],
      files: [],
      review_tasks: [
        {
          id: "d".repeat(32),
          stage: "correctness",
          page: 7,
          chunk: 1,
          status: "waiting",
        },
      ],
    };
    const queuedFixture = {
      ...fixture,
      id: "b".repeat(32),
      status: "queued",
      source_name: "Queued catalog.pdf",
      catalog_id: "guhring-other",
      stages: fixture.stages.map((s) => ({ ...s, status: "queued" })),
    };
    const fixtureQueue = {
      paused: false,
      active_run: fixture.id,
      queued_runs: [queuedFixture.id],
      status: fixture.status,
    };
    const controlCalls = [];
    const fixtureStop = (kind, reason, stage = "correctness", page = 7) => ({
      kind,
      stage,
      stage_title: stage === "parse" ? "Extract" : "Correctness",
      page,
      reason,
      occurred_at: "2026-10-12T07:00:00Z",
    });
    await page.route(`**/api/runs/${fixture.id}/stoppage`, (route) =>
      route.fulfill({
        json: {
          stoppage: fixture.stoppage || null,
          diagnostics:
            fixture.status === "failed"
              ? [
                  {
                    label: "Latest saved stage output",
                    text: "MinerU upstream request failed: HTTP 502.",
                  },
                ]
              : [],
          log_url: null,
        },
      }),
    );
    await page.route("**/api/queue", (route) =>
      route.fulfill({ json: fixtureQueue }),
    );
    for (const action of ["pause", "resume", "stop"])
      await page.route(`**/api/queue/${action}`, (route) => {
        controlCalls.push(`queue/${action}`);
        fixtureQueue.paused = action === "pause";
        fixtureQueue.status =
          action === "pause"
            ? "paused"
            : action === "stop"
              ? "idle"
              : "awaiting_review";
        fixture.status =
          action === "pause"
            ? "paused"
            : action === "stop"
              ? "cancelled"
              : "awaiting_review";
        queuedFixture.status =
          action === "pause"
            ? "paused"
            : action === "stop"
              ? "cancelled"
              : "queued";
        fixture.paused_by_queue = queuedFixture.paused_by_queue =
          action === "pause";
        if (action === "resume") delete fixture.stoppage;
        else
          fixture.stoppage = fixtureStop(
            fixture.status,
            action === "pause"
              ? "The processing queue was paused by the user."
              : "The processing queue was stopped by the user.",
          );
        if (action === "stop") {
          fixtureQueue.active_run = null;
          fixtureQueue.queued_runs = [];
        }
        return route.fulfill({ json: fixtureQueue });
      });
    for (const action of ["pause", "resume", "cancel"])
      await page.route(`**/api/runs/${fixture.id}/${action}`, (route) => {
        controlCalls.push(`catalog/${action}`);
        fixture.status =
          action === "pause"
            ? "paused"
            : action === "cancel"
              ? "cancelled"
              : "awaiting_review";
        fixture.paused_individually = action === "pause";
        if (action === "resume") delete fixture.stoppage;
        else
          fixture.stoppage = fixtureStop(
            fixture.status,
            action === "pause"
              ? "This catalog was paused by the user."
              : "This catalog was stopped by the user.",
          );
        fixtureQueue.status = fixture.status;
        if (action === "cancel") fixtureQueue.active_run = null;
        return route.fulfill({ json: fixture });
      });
    await page.route("**/api/runs", (route) =>
      route.fulfill({
        json: [fixture, queuedFixture],
      }),
    );
    await page.waitForFunction(() =>
      document
        .getElementById("catalog-progress")
        .textContent.includes("Paused catalog.pdf"),
    );
    assert(
      (await page.locator("#catalog-progress").textContent()).includes("27%"),
    );
    assert(
      (await page.locator("#catalog-progress").textContent()).includes(
        "Queued catalog.pdf",
      ),
    );
    assert.equal(
      await page.locator("#catalog-progress").evaluate((el) => el.open),
      false,
    );
    await page.locator("#catalog-progress > summary").click();
    const positions = await page
      .locator("#catalog-progress-list .progress-catalog")
      .evaluateAll((cards) =>
        cards.map((el) => ({
          top: el.getBoundingClientRect().top,
          left: el.getBoundingClientRect().left,
        })),
      );
    assert.equal(positions[0].left, positions[1].left);
    assert(positions[1].top > positions[0].top);
    await page.waitForTimeout(3300);
    assert(await page.locator("#catalog-progress").evaluate((el) => el.open));
    assert(
      (await page.locator("#pipeline-current").textContent()).includes(
        "03 Correctness",
      ),
    );
    assert(await page.locator("#review.processing").isVisible());
    assert(
      await page.locator('#navigation a[href="#review"].processing').count(),
    );
    await page.locator("#go-current-stage").click();
    assert(
      (await page.locator("#pipeline-viewing").textContent()).includes("03"),
    );
    await page.route("**/api/review/tasks/" + "d".repeat(32), (route) =>
      route.fulfill({
        json: {
          request: {
            task_id: "d".repeat(32),
            model: "gpt-6-sol",
            reasoning: "low",
            stage: "correctness",
            context: { page: 7 },
          },
          prompt: "Check page correctness against its source.",
          schema: { type: "object" },
          image_url: evidenceRun
            ? `/api/runs/${evidenceRun.id}/source-page/7`
            : "/assets/mark.svg",
        },
      }),
    );
    await page
      .locator("#review-pending")
      .getByRole("button", { name: "Continue review" })
      .click();
    await page.locator("#task-dialog").waitFor({ state: "visible" });
    assert(
      (await page.locator("#task-content").textContent()).includes(
        "Low reasoning",
      ),
    );
    assert(
      (
        await page
          .getByRole("link", { name: "Download review packet (.zip)" })
          .getAttribute("href")
      ).endsWith("/packet"),
    );
    await page.locator("#task-result-file").setInputFiles({
      name: "result.json",
      mimeType: "application/json",
      buffer: Buffer.from('{"fixture":"browser only"}'),
    });
    await page.waitForFunction(() =>
      document.getElementById("task-result").value.includes("browser only"),
    );
    await page.locator("#close-task").click();
    const catalogCard = page.locator(
      `.progress-catalog[data-run-id="${fixture.id}"]`,
    );
    await catalogCard.locator(".progress-catalog-select").click();
    await catalogCard
      .getByRole("button", { name: "Pause catalog", exact: true })
      .click();
    await catalogCard
      .getByRole("button", { name: "Resume catalog", exact: true })
      .waitFor();
    const pauseInfo = page.locator("#review .stoppage-info");
    assert.equal(await pauseInfo.evaluate((d) => d.open), false);
    await pauseInfo.locator("summary").click();
    assert((await pauseInfo.textContent()).includes("This catalog was paused"));
    assert((await pauseInfo.textContent()).includes("PDF page 7"));
    await page.waitForTimeout(3300);
    assert(await pauseInfo.evaluate((d) => d.open));
    assert(
      (await page.locator("#pipeline-current").textContent()).includes(
        "paused",
      ),
    );
    await catalogCard
      .getByRole("button", { name: "Resume catalog", exact: true })
      .click();
    await catalogCard
      .getByRole("button", { name: "Pause catalog", exact: true })
      .waitFor();
    assert.equal(await page.locator(".stoppage-info").count(), 0);
    await page
      .locator("#queue-controls")
      .getByRole("button", { name: "Pause parsing", exact: true })
      .click();
    await page
      .locator("#queue-controls")
      .getByRole("button", { name: "Resume parsing", exact: true })
      .waitFor();
    assert(
      (await page.locator("#queue-control-note").textContent()).includes(
        "paused",
      ),
    );
    await page
      .locator("#queue-controls")
      .getByRole("button", { name: "Resume parsing", exact: true })
      .click();
    await page
      .locator("#queue-controls")
      .getByRole("button", { name: "Pause parsing", exact: true })
      .waitFor();
    await catalogCard
      .getByRole("button", { name: "Stop catalog", exact: true })
      .click();
    await page.waitForFunction(() =>
      document
        .getElementById("pipeline-current")
        .textContent.includes("cancelled"),
    );
    await page
      .locator("#queue-controls")
      .getByRole("button", { name: "Stop parsing", exact: true })
      .click();
    await page.waitForFunction(
      () => document.querySelectorAll("#queue-controls button")[1].disabled,
    );
    assert.deepEqual(controlCalls, [
      "catalog/pause",
      "catalog/resume",
      "queue/pause",
      "queue/resume",
      "catalog/cancel",
      "queue/stop",
    ]);
    assert(
      (await page.locator("#review .stoppage-info").textContent()).includes(
        "Why stopped",
      ),
    );
    fixture.status = "failed";
    fixture.error = "CPU extraction failed: HTTP 502.";
    fixture.review_tasks = [];
    fixture.stoppage = fixtureStop("failed", fixture.error, "parse", 8);
    fixture.stages = fixture.stages.map((s) => ({
      ...s,
      status:
        s.id === "catalog"
          ? "completed"
          : s.id === "parse"
            ? "failed"
            : "not_run",
    }));
    await page.waitForFunction(() =>
      Boolean(document.querySelector("#extract .stoppage-info")),
    );
    assert.equal(await page.locator("#review .stoppage-info").count(), 0);
    const failureInfo = page.locator("#extract .stoppage-info");
    await failureInfo.locator("summary").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#extract .stoppage-info pre")
        ?.textContent.includes("HTTP 502"),
    );
    assert((await failureInfo.textContent()).includes("PDF page 8"));
    assert(
      (await page.locator("#pipeline-current").textContent()).includes(
        "02 Extract",
      ),
    );
    await page.waitForTimeout(3300);
    assert(await failureInfo.evaluate((d) => d.open));
    await page.screenshot({
      path: path.join(output, "stoppage-desktop.png"),
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 900 });
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    await failureInfo.screenshot({
      path: path.join(output, "stoppage-mobile.png"),
    });
    await page.setViewportSize({ width: 1440, height: 1000 });
    fixture.stoppage = {
      ...fixture.stoppage,
      stage: "legacy_ai",
      stage_title: "Legacy model review",
      reason: "Recorded legacy failure.",
      page: null,
    };
    fixture.stages = [
      { id: "legacy_ai", title: "Legacy model review", status: "failed" },
    ];
    await page.waitForFunction(() =>
      Boolean(document.querySelector("#results .stoppage-info")),
    );
    assert.equal(await page.locator("#extract .stoppage-info").count(), 0);
    assert(
      (await page.locator("#results .stoppage-info").textContent()).includes(
        "Recorded legacy failure",
      ),
    );
    await page.locator("#catalog-progress > summary").click();
    assert.equal(errors.length, 0, errors.join("\n"));
    await fs.writeFile(
      path.join(output, "report.json"),
      JSON.stringify(
        {
          passed: true,
          work_generation_calls: 0,
          upload_metadata_detection: true,
          help_popovers: true,
          findings_preserved_during_poll: Boolean(evidenceRun),
          manual_tool_review: Boolean(evidenceRun),
          manual_search_filters_notes: Boolean(evidenceRun),
          catalog_dropdown_vertical_and_persistent: true,
          current_stage_navigation: true,
          default_languages: ["en", "ru", "zh"],
          windows_catalogs: windowsSources.length,
          fresh_task_packet_and_json_import: true,
          independent_models_and_reasoning: true,
          normal_mode_removed: true,
          no_model_disables_reasoning: true,
          catalog_pause_resume_stop: true,
          queue_pause_resume_stop: true,
          stoppage_reason_dropdown: true,
          stoppage_panel_persists_during_poll: true,
          legacy_stoppage_context: true,
          default_review: {
            model: boot.defaults.model,
            final_model: boot.defaults.final_model,
            reasoning: boot.defaults.reasoning,
            final_reasoning: boot.defaults.final_reasoning,
            automatic_review: boot.defaults.automatic_review,
          },
          manufacturers: boot.manufacturers.length,
          layouts,
          browserErrors: errors,
          paidApiCalls: 0,
        },
        null,
        2,
      ),
    );
    console.log(
      JSON.stringify({
        passed: true,
        report: path.join(output, "report.json"),
      }),
    );
  } finally {
    await browser.close();
    for (const uploaded of uploadedPaths) {
      const filename = path.basename(uploaded);
      if (/^[a-f0-9]{12}-ui-test-catalog-[ab]\.pdf$/.test(filename))
        await fs
          .unlink(path.join(testCatalogsRoot, "uploads", filename))
          .catch(() => {});
    }
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
