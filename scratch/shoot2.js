const { chromium } = require("playwright");
const path = require("path");

const BASE = "http://127.0.0.1:8000";
const OUT = path.join(__dirname, "shots");

(async () => {
  const browser = await chromium.launch();

  // ---- light mode --------------------------------------------------------
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

  await page.goto(BASE + "/?q=esp32", { waitUntil: "networkidle" });
  await page.waitForSelector("[data-state='results'] .card", { timeout: 60000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: path.join(OUT, "10-results-light.png"), fullPage: false });

  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(OUT, "11-home-light.png"), fullPage: false });

  // ---- dark mode ---------------------------------------------------------
  const dark = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
    colorScheme: "dark",
  });
  dark.on("pageerror", (e) => errors.push("dark pageerror: " + e.message));

  await dark.goto(BASE + "/?q=esp32", { waitUntil: "networkidle" });
  await dark.waitForSelector("[data-state='results'] .card", { timeout: 60000 });
  await dark.waitForTimeout(1500);
  await dark.screenshot({ path: path.join(OUT, "12-results-dark.png"), fullPage: false });

  await dark.goto(BASE + "/", { waitUntil: "networkidle" });
  await dark.waitForTimeout(600);
  await dark.screenshot({ path: path.join(OUT, "13-home-dark.png"), fullPage: false });

  // ---- lightbox ----------------------------------------------------------
  await page.goto(BASE + "/?q=esp32", { waitUntil: "networkidle" });
  await page.waitForSelector("[data-state='results'] .card", { timeout: 60000 });
  await page.waitForTimeout(1200);
  const thumb = await page.$(".card__thumb.zoom-trigger");
  if (thumb) {
    await thumb.click();
    await page.waitForTimeout(600);
    await page.screenshot({ path: path.join(OUT, "14-lightbox.png"), fullPage: false });
  }

  console.log("console errors:", errors.length ? errors : "none");
  await browser.close();
})();
