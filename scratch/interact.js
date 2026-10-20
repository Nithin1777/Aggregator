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

  const check = async (label, fn) => {
    try {
      const detail = await fn();
      console.log(`[PASS] ${label}${detail ? " - " + detail : ""}`);
    } catch (err) {
      console.log(`[FAIL] ${label} - ${err.message}`);
      errors.push(label + ": " + err.message);
    }
  };

  await page.goto(BASE + "/?q=esp32", { waitUntil: "networkidle" });
  await page.waitForSelector("[data-state='results'] .card", { timeout: 60000 });

  await check("category filter from the rail", async () => {
    await page.click("[data-view='results'] .cat-rail .cat-pill:nth-child(2)");
    await page.waitForTimeout(6000);
    const url = page.url();
    if (!url.includes("category=resistors")) throw new Error("url not updated: " + url);
    const active = await page.$eval(
      "[data-view='results'] .cat-rail .cat-pill.is-active",
      (el) => el.getAttribute("data-category-slug")
    );
    if (active !== "resistors") throw new Error("wrong active pill: " + active);
    return url.replace(BASE, "");
  });

  await check("active filter chip is rendered", async () => {
    const chip = await page.$eval(
      "[data-active-filters] .active-filter",
      (el) => el.textContent.trim()
    );
    return chip;
  });

  await check("vendor filter", async () => {
    await page.click("[data-view='results'] [data-vendor-slug='robu']");
    await page.waitForTimeout(6000);
    const url = page.url();
    if (!url.includes("vendor=robu")) throw new Error("url not updated: " + url);
    return url.replace(BASE, "");
  });

  await check("in-stock toggle", async () => {
    await page.click("[data-view='results'] .switch");
    await page.waitForTimeout(6000);
    const url = page.url();
    if (!url.includes("in_stock=1")) throw new Error("url not updated: " + url);
    const oos = await page.$$eval(".offers .tag--off", (els) => els.length);
    return `url ok, out-of-stock rows in table: ${oos}`;
  });

  await check("reset clears every filter", async () => {
    await page.click("[data-clear-filters]");
    await page.waitForTimeout(6000);
    const url = page.url();
    if (url.includes("category=") || url.includes("vendor=") || url.includes("in_stock="))
      throw new Error("filters not cleared: " + url);
    return url.replace(BASE, "");
  });

  await check("suggestion click runs the search", async () => {
    const input = await page.$("[data-view='results'] [data-search-input]");
    await input.click();
    await input.fill("");
    await input.type("dht", { delay: 60 });
    await page.waitForFunction(
      () => {
        const items = document.querySelectorAll(
          "[data-view='results'] .suggest.is-open .suggest__item"
        );
        return (
          items.length > 0 &&
          Array.from(items).some((el) =>
            (el.getAttribute("data-suggest-value") || "").toLowerCase().startsWith("dht")
          )
        );
      },
      { timeout: 8000 }
    );
    const before = await page.evaluate(() => ({
      url: location.href,
      panels: Array.from(document.querySelectorAll(".suggest")).map((p) => ({
        open: p.classList.contains("is-open"),
        items: p.querySelectorAll(".suggest__item").length,
        visible: !!p.offsetParent,
      })),
    }));
    console.log("   before click:", JSON.stringify(before));

    await page.click("[data-view='results'] .suggest.is-open .suggest__item");
    await page.waitForTimeout(8000);
    const after = await page.evaluate(() => ({
      url: location.href,
      title: (document.querySelector("[data-results-title]") || {}).textContent,
      input: (document.querySelector("[data-view='results'] [data-search-input]") || {}).value,
    }));
    console.log("   after click:", JSON.stringify(after));

    const title = await page.$eval("[data-results-title]", (el) => el.textContent.trim());
    if (!title.toLowerCase().includes("dht")) throw new Error("title: " + title);
    return title;
  });

  await check("browser back returns to the previous state", async () => {
    await page.goBack();
    await page.waitForTimeout(6000);
    const title = await page.$eval("[data-results-title]", (el) => el.textContent.trim());
    return title;
  });

  await check("category browse page lists parts", async () => {
    await page.goto(BASE + "/?category=sensors", { waitUntil: "networkidle" });
    await page.waitForSelector("[data-state='results'] .card", { timeout: 60000 });
    const count = await page.$$eval("[data-state='results'] .card", (els) => els.length);
    if (!count) throw new Error("no cards");
    return count + " cards";
  });

  console.log("\nconsole/page errors:", errors.length ? errors : "none");
  await browser.close();
})();
