const assert = require("assert");
const fs = require("fs");
const path = require("path");

const loadingHtml = fs.readFileSync(path.resolve(__dirname, "..", "frontend", "loading.html"), "utf8");
const progressHtml = fs.readFileSync(path.resolve(__dirname, "..", "frontend", "progress.html"), "utf8");

function testLoadingScreenAvoidsRemoteRuntimeAssets() {
  assert(!loadingHtml.includes("https://unpkg.com/"), "loading screen should not depend on unpkg");
  assert(!loadingHtml.includes("https://lottie.host/"), "loading screen should not depend on remote lottie assets");
  assert(!/<dotlottie-player/i.test(loadingHtml), "loading screen should not use remote dotlottie runtime");
}

function testProgressHtmlAvoidsInlineStyleBlocks() {
  assert(!/<style[\s>]/i.test(progressHtml), "progress html should not keep inline style blocks");
}

function testOnboardingWizardEscapesPersistedConfigValues() {
  assert(
    progressHtml.includes("escapeHtml(state.config?.customer_name||'')"),
    "wizard should escape persisted customer_name before injecting HTML",
  );
  assert(
    progressHtml.includes("escapeHtml(state.config?.workspace_name||'')"),
    "wizard should escape persisted workspace_name before injecting HTML",
  );
  assert(
    progressHtml.includes("escapeHtml(state.config?.min_publish_date||'')"),
    "wizard should escape persisted min_publish_date before injecting HTML",
  );
}

function testTriggerAuthReportsBackendFailure() {
  assert(
    progressHtml.includes("const res = await api.post(`/auth_single?platform=${platformName}`);"),
    "triggerAuth should inspect backend response",
  );
  assert(
    progressHtml.includes("if (res?.ok || res?.accepted) {"),
    "triggerAuth should only show success toast for accepted auth launches",
  );
  assert(
    progressHtml.includes("showToast(res?.message || `无法启动 ${LABELS[platformName]} 授权流程`, \"error\");"),
    "triggerAuth should surface backend auth launch failures",
  );
}

function main() {
  testLoadingScreenAvoidsRemoteRuntimeAssets();
  testProgressHtmlAvoidsInlineStyleBlocks();
  testOnboardingWizardEscapesPersistedConfigValues();
  testTriggerAuthReportsBackendFailure();
}

main();
