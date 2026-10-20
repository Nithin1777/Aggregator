const { chromium } = require("playwright");
const path = require("path");

const BASE = "http://127.0.0.1:8000";
const OUT = path.join(__dirname, "shots");

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

  // 1. Home
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(OUT, "01-home.png"), fullPage: true });

  // 2. Search from the landing page -> results view
  await page.click(".hero__search [data-search-input]");
  await page.type(".hero__search [data-search-input]", "dht", { delay: 70 });
  await page.waitForSelector(".suggest.is-open .suggest__item", { timeout: 8000 });
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(OUT, "02-suggest.png") });

  await page.keyboard.press("Enter");
  await page.waitForSelector("[data-view='results']:not([hidden])", { timeout: 10000 });
  await page.waitForSelector("[data-state='results'] .card", { timeout: 60000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: path.join(OUT, "03-results.png"), fullPage: false });

  // 3. Category filter (client side, no page reload)
  const before = page.url();
  await page.click("[data-view='results'] .cat-rail .cat-pill:nth-child(4)");
  await page.waitForTimeout(9000);
  console.log("url before:", before, "-> after:", page.url());
  await page.screenshot({ path: path.join(OUT, "04-category.png"), fullPage: false });

  // 4. Direct browse link (server rendered)
  await page.goto(BASE + "/?category=resistors", { waitUntil: "networkidle" });
  await page.waitForSelector("[data-state='results'] .card", { timeout: 60000 });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: path.join(OUT, "09-browse-category.png"), fullPage: false });

  // 5. Browse pages
  await page.goto(BASE + "/categories", { waitUntil: "networkidle" });
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(OUT, "05-categories.png"), fullPage: true });

  await page.goto(BASE + "/vendors", { waitUntil: "networkidle" });
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(OUT, "06-vendors.png"), fullPage: true });

  // 6. Mobile
  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await mobile.goto(BASE + "/", { waitUntil: "networkidle" });
  await mobile.waitForTimeout(500);
  await mobile.screenshot({ path: path.join(OUT, "07-mobile.png"), fullPage: true });

  await mobile.goto(BASE + "/?q=dht11", { waitUntil: "networkidle" });
  await mobile.waitForSelector("[data-state='results'] .card", { timeout: 60000 });
  await mobile.waitForTimeout(1200);
  await mobile.screenshot({ path: path.join(OUT, "08-mobile-results.png"), fullPage: false });

  console.log("console errors:", errors.length ? errors : "none");
  await browser.close();
})();
