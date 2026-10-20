const { chromium } = require("playwright");
const path = require("path");

const BASE = "http://127.0.0.1:8000";
const OUT = path.join(__dirname, "shots");

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

  // Browse a category that carries spec filters
  await page.goto(BASE + "/?category=motor", { waitUntil: "networkidle" });
  await page.waitForSelector("[data-state='results'] .card", { timeout: 60000 });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: path.join(OUT, "20-spec-filters.png"), fullPage: false });

  const before = await page.$eval("[data-results-sub]", (el) => el.textContent.trim());

  // Apply an RPM filter
  await page.fill("[data-spec-max='rpm']", "500");
  await page.press("[data-spec-max='rpm']", "Enter");
  await page.waitForTimeout(9000);

  const after = await page.$eval("[data-results-sub]", (el) => el.textContent.trim());
  const url = page.url();
  await page.screenshot({ path: path.join(OUT, "21-spec-filtered.png"), fullPage: false });

  console.log("before:", before);
  console.log("after: ", after);
  console.log("url:   ", url.replace(BASE, ""));
  console.log("errors:", errors.length ? errors : "none");

  await browser.close();
})();
