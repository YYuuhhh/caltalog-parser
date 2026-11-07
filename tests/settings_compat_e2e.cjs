/* Mixed deployment versions and legacy settings cannot send invalid batches. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const base = process.env.PIPELINE_UI_URL || "http://127.0.0.1:17861";
(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
  });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  try {
    const bootResponse = await page.request.get(base + "/api/bootstrap");
    const boot = await bootResponse.json();
    assert.equal(bootResponse.headers()["cache-control"], "no-store");
    for (const path of ["/", "/assets/app.js"])
      assert.equal(
        (await page.request.get(base + path)).headers()["cache-control"],
        "no-store",
      );
    const old = structuredClone(boot);
    for (const field of [
      "baidu_url",
      "baidu_output_tokens",
      "extraction_dpi",
      "local_cpu_threads",
    ])
      delete old.defaults[field];
    let posts = 0;
    await page.route("**/api/batches", (route) => {
      posts++;
      return route.fulfill({
        status: 500,
        json: { detail: "No model execution in this regression" },
      });
    });
    await page.route("**/api/bootstrap", (route) =>
      route.fulfill({ json: old }),
    );
    await page.goto(base);
    await page.locator("#error").waitFor({ state: "visible" });
    assert.match(
      await page.locator("#error").textContent(),
      /API and UI are different versions/,
    );
    assert.match(
      await page.locator("#error").textContent(),
      /force-recreate pipeline-ui/,
    );
    assert.equal(await page.locator("#run").isDisabled(), true);
    assert.equal(posts, 0);
    await page.unroute("**/api/bootstrap");
    await page.goto(base);
    await page.locator("#model-picker").waitFor();
    await page.waitForFunction(
      () => document.getElementById("settings-file").onchange != null,
    );
    await page.locator("#settings-file").setInputFiles({
      name: "old-settings.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          version: 2,
          settings: {
            ocr_check_model: "none",
            ocr_check_url: "",
            ocr_check_tokens: null,
            local_cpu_threads: null,
            model: "none",
            final_model: "none",
          },
        }),
      ),
    });
    await page.waitForFunction(
      () =>
        document.querySelector("input[name=local_cpu_threads]").value !== "" &&
        document.querySelector("select[name=model]").value === "none",
    );
    assert.equal(
      await page.locator("input[name=local_cpu_threads]").inputValue(),
      String(boot.defaults.local_cpu_threads),
    );
    assert.equal(
      await page.locator("input[name=baidu_url]").inputValue(),
      boot.defaults.baidu_url,
    );
    assert.equal(
      await page.locator("input[name=baidu_output_tokens]").inputValue(),
      String(boot.defaults.baidu_output_tokens),
    );
    await page.locator("input[name=local_cpu_threads]").fill("");
    const planned = page.waitForResponse((r) => r.url().endsWith("/api/plan"));
    await page.locator("#preview").click();
    const response = await planned;
    assert.equal(response.status(), 200);
    const payload = response.request().postDataJSON();
    assert.equal(payload.local_cpu_threads, boot.defaults.local_cpu_threads);
    assert.equal(
      payload.baidu_output_tokens,
      boot.defaults.baidu_output_tokens,
    );
    assert.equal(payload.last_page, null);
    for (const name of ["ocr_check_model", "ocr_check_url", "ocr_check_tokens"])
      assert.equal(Object.hasOwn(payload, name), false);
    assert.deepEqual(errors, []);
    console.log(
      JSON.stringify({
        passed: true,
        checks: [
          "mixed backend rejected before queueing",
          "restart instructions",
          "legacy import defaults",
          "blank CPU threads use hardware default",
          "deprecated keys excluded",
          "real plan accepted",
          "uncached UI and bootstrap",
        ],
        modelCalls: 0,
      }),
    );
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
