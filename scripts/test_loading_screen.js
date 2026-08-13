const assert = require("assert");
const fs = require("fs");
const path = require("path");

const loadingHtml = fs.readFileSync(path.resolve(__dirname, "..", "frontend", "loading.html"), "utf8");

function testLoadingScreenHasSlowStartActions() {
  assert(
    loadingHtml.includes('id="timeoutActions"'),
    "loading screen should expose timeout actions when startup is slow",
  );
  assert(
    loadingHtml.includes("打开启动日志"),
    "loading screen should offer a launcher log shortcut",
  );
  assert(
    loadingHtml.includes("重新打开工作台"),
    "loading screen should offer a manual reopen action",
  );
}

function testLoadingScreenCommunicatesSlowStartupClearly() {
  assert(
    loadingHtml.includes("启动偏慢"),
    "loading screen should tell the user when startup is slow instead of looking frozen",
  );
  assert(
    loadingHtml.includes("首次启动会额外准备运行环境"),
    "loading screen should explain why startup can take longer on first launch",
  );
}

function main() {
  testLoadingScreenHasSlowStartActions();
  testLoadingScreenCommunicatesSlowStartupClearly();
}

main();
