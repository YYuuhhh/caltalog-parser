/* Startup failures remain readable even when an upstream returns plain text. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const base = process.env.PIPELINE_UI_URL || "http://127.0.0.1:17861";
(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
  });
  const page = await browser.newPage();
  const scenarios = [
    {
      status: 500,
      contentType: "text/plain",
      body: "Internal Server Error",
      expected: "HTTP 500",
    },
    {
      status: 200,
      contentType: "text/html",
      body: "<html>Proxy error</html>",
      expected: "HTTP 200",
    },
    {
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        detail:
          "CatalogFlow cannot access /catalogs/library. Run init-storage.",
      }),
      expected: "cannot access /catalogs/library",
    },
  ];
  try {
    for (const response of scenarios) {
      await page.route("**/api/bootstrap", (route) => route.fulfill(response));
      await page.goto(base);
      await page.locator("#error").waitFor({ state: "visible" });
      const error = await page.locator("#error").textContent();
      assert(
        error.includes("/api/bootstrap") && error.includes(response.expected),
        error,
      );
      assert(
        !error.includes("Unexpected token") &&
          !error.includes("not valid JSON"),
        error,
      );
      await page.unroute("**/api/bootstrap");
    }
    console.log(
      JSON.stringify({
        passed: true,
        checks: [
          "plain HTTP 500",
          "HTML upstream response",
          "actionable storage error",
        ],
      }),
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
