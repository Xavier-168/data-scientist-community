const { chromium } = require("playwright");
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.MONITOR_BASE_URL || "http://127.0.0.1:8811";
const UNWANTED_PATTERNS = [
  /本次正常完成，但结果为 0 条/,
  /最近已同步.+（明细 \d+，作品 \d+）/,
  /空数据会正常显示为“完成 0 条”，不再误判成失败。/,
];

async function readText(page, selector) {
  return ((await page.locator(selector).innerText().catch(() => "")) || "").trim();
}

function testUpdateCheckClassification() {
  const html = fs.readFileSync(
    path.resolve(__dirname, "..", "frontend", "progress.html"),
    "utf8",
  );
  const match = html.match(/function classifyUpdateCheck\(payload\)\s*\{[\s\S]*?\n  \}/);
  assert(match, "progress page should define classifyUpdateCheck");
  const classifyUpdateCheck = new Function(`${match[0]}; return classifyUpdateCheck;`)();
  assert.strictEqual(
    classifyUpdateCheck({ update_available: false, error: "network_error" }),
    "unavailable",
  );
  assert.strictEqual(
    classifyUpdateCheck({
      ok: false,
      unavailable: true,
      error: "update_service_not_configured",
      update_status: "not_configured",
    }),
    "not_configured",
  );
  assert.strictEqual(classifyUpdateCheck({ update_available: false }), "current");
  assert.strictEqual(
    classifyUpdateCheck({ update_available: true, latest: { version: "20260710" } }),
    "available",
  );
  assert(
    html.includes("当前版本未配置自动更新服务；源码版本请从项目发布页获取更新。"),
    "not-configured update state should explain source release updates without showing a connection failure",
  );
}

async function main() {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage();
  try {
    await page.goto(`${BASE_URL}/monitor`, { waitUntil: "networkidle" });
    const snapshot = {
      hero: await readText(page, "#heroSummaryCard"),
      scrape: await readText(page, "#scrapeSummary"),
      feishu: await readText(page, "#feishuSummary"),
      platformSection: await readText(page, "#platformCards"),
      platformHeader: await readText(page, "#platformStatusTag"),
    };
    const serialized = JSON.stringify(snapshot, null, 2);
    const hit = UNWANTED_PATTERNS.find((pattern) => pattern.test(serialized));
    if (hit) {
      throw new Error(`dashboard contains unwanted copy: ${hit}`);
    }
    console.log(JSON.stringify(snapshot, null, 2));
  } finally {
    await browser.close();
  }
}

const selectedRun = process.argv.includes("--classification-only")
  ? async () => testUpdateCheckClassification()
  : main;

selectedRun().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
