const assert = require("assert");
const fs = require("fs");
const path = require("path");

const script = fs.readFileSync(
  path.resolve(__dirname, "bilibili_export.mjs"),
  "utf8",
);

function testEnsureDashboardReturnsPageWhenAlreadyReady() {
  // ensureDashboard uses findReadyDashboardPage to check if dashboard is
  // already accessible and returns it immediately, rather than forcing a
  // re-login cycle.
  assert(
    script.includes("async function ensureDashboard(context, page)"),
    "bilibili should define ensureDashboard function"
  );
  assert(
    script.includes("if (initialReadyPage) return initialReadyPage;"),
    "bilibili ensureDashboard should return the ready page immediately when dashboard is already accessible"
  );
}

function main() {
  testEnsureDashboardReturnsPageWhenAlreadyReady();
}

main();
