const assert = require("assert");
const http = require("http");
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const MONITOR_HTML = fs.readFileSync(path.resolve(__dirname, "..", "frontend", "progress.html"), "utf8");
const MONITOR_THEME_CSS = fs.readFileSync(
  path.resolve(__dirname, "..", "frontend", "assets", "progress-apple-theme.css"),
  "utf8"
);

function buildDefaultProgress(overrides = {}) {
  return {
    platform: overrides.platform || "douyin",
    status: "idle",
    phase: "idle",
    message: "待机中",
    enabled: true,
    needs_auth: false,
    auth_status: "authorized",
    auth_reason: "",
    auth_action: "none",
    ui_status: "idle",
    last_sync_at: null,
    totalWorks: 0,
    processedWorks: 0,
    successWorks: 0,
    skippedWorks: 0,
    failedWorks: 0,
    ...overrides,
  };
}

function buildScenario(overrides = {}) {
  const config = {
    customer_name: "测试客户",
    workspace_name: "本地数据工作台",
    min_publish_date: "2026-01-01",
    browser_channel: "msedge",
    enabled_platforms: ["douyin"],
    onboarding_completed: true,
    feishu_enabled: false,
    feishu_auto_sync: false,
    include_weixin_mp_in_sync: false,
    feishu_app_token: "",
    feishu_app_id: "",
    feishu_app_secret: "",
    ...(overrides.config || {}),
  };
  const configSummary = {
    setup_complete: true,
    feishu_ready: false,
    feishu_enabled: false,
    auto_sync_enabled: false,
    enabled_platform_count: config.enabled_platforms.length,
    enabled_platforms: config.enabled_platforms,
    authorized_platform_count: 1,
    ...(overrides.configSummary || {}),
  };
  const feishuSummary = {
    enabled: !!configSummary.feishu_enabled,
    ready: !!configSummary.feishu_ready,
    auto_sync_enabled: !!configSummary.auto_sync_enabled,
    status: configSummary.feishu_enabled ? (configSummary.feishu_ready ? "idle" : "needs_config") : "disabled",
    message: "",
    current_platforms: [],
    current_platform_labels: [],
    last_sync_at: "",
    last_platforms: [],
    last_platform_labels: [],
    last_summary: "",
    last_error: "",
    ...(overrides.feishuSummary || {}),
  };
  const progress = {
    ok: true,
    running: false,
    douyin: buildDefaultProgress({
      platform: "douyin",
      enabled: true,
      auth_status: "authorized",
      auth_reason: "",
      needs_auth: false,
      ui_status: "idle",
      ...(overrides.douyin || {}),
    }),
    xiaohongshu: buildDefaultProgress({ platform: "xiaohongshu", enabled: false, auth_status: "unauthorized", auth_reason: "not_authorized", needs_auth: true, ui_status: "auth_required" }),
    bilibili: buildDefaultProgress({ platform: "bilibili", enabled: false, auth_status: "unauthorized", auth_reason: "not_authorized", needs_auth: true, ui_status: "auth_required" }),
    kuaishou: buildDefaultProgress({ platform: "kuaishou", enabled: false, auth_status: "unauthorized", auth_reason: "not_authorized", needs_auth: true, ui_status: "auth_required" }),
    weixin_mp: buildDefaultProgress({ platform: "weixin_mp", enabled: false, auth_status: "unauthorized", auth_reason: "not_authorized", needs_auth: true, ui_status: "auth_required" }),
    weixin_channels: buildDefaultProgress({ platform: "weixin_channels", enabled: false, auth_status: "unauthorized", auth_reason: "not_authorized", needs_auth: true, ui_status: "auth_required" }),
    summary: {
      active_platform: "",
      current_stage: "idle",
      has_running_platform: false,
      completed_platforms: [],
      zero_result_platforms: [],
      failed_platforms: [],
      enabled_platform_count: config.enabled_platforms.length,
      authorized_platform_count: configSummary.authorized_platform_count,
      setup_complete: configSummary.setup_complete,
      feishu_enabled: configSummary.feishu_enabled,
      feishu_ready: configSummary.feishu_ready,
      auto_sync_enabled: configSummary.auto_sync_enabled,
      feishu: feishuSummary,
      ...(overrides.progressSummary || {}),
    },
    serverTime: "2026-04-04T18:30:00+0800",
    ...(overrides.progress || {}),
  };

  return {
    config,
    configSummary,
    progress,
    historyRuns: overrides.historyRuns || [],
    allData: overrides.allData || { ok: true, rows: [], row_count: 0 },
    progressMode: "normal",
    runAllRequests: overrides.runAllRequests || [],
    testFeishuResponse: overrides.testFeishuResponse || { ok: true, message: "飞书连接可用。" },
  };
}

async function withServer(scenario, callback) {
  const hangingResponses = new Set();
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    if (url.pathname === "/monitor") {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(MONITOR_HTML);
      return;
    }
    if (url.pathname === "/assets/progress-apple-theme.css") {
      res.writeHead(200, { "Content-Type": "text/css; charset=utf-8" });
      res.end(MONITOR_THEME_CSS);
      return;
    }
    if (url.pathname === "/config") {
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ ok: true, config: scenario.config, summary: scenario.configSummary }));
      return;
    }
    if (url.pathname === "/progress") {
      if (scenario.progressMode === "hang") {
        hangingResponses.add(res);
        return;
      }
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify(scenario.progress));
      return;
    }
    if (url.pathname === "/analytics/history") {
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ ok: true, runs: scenario.historyRuns }));
      return;
    }
    if (url.pathname === "/all-data") {
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify(scenario.allData));
      return;
    }
    if (url.pathname === "/sync_feishu") {
      res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ ok: false, error: "feishu_disabled", message: "飞书同步未启用。" }));
      return;
    }
    if (url.pathname === "/run_all") {
      scenario.runAllRequests.push({
        min_date: url.searchParams.get("min_date") || "",
        max_date: url.searchParams.get("max_date") || "",
        platforms: url.searchParams.get("platforms") || "",
        run_mode: url.searchParams.get("run_mode") || "",
      });
      res.writeHead(202, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({
        ok: true,
        accepted: true,
        min_date: url.searchParams.get("min_date") || "",
        max_date: url.searchParams.get("max_date") || "",
        message: "同步任务已启动。",
      }));
      return;
    }
    if (url.pathname === "/config/test_feishu") {
      const payload = scenario.testFeishuResponse || { ok: true, message: "飞书连接可用。" };
      res.writeHead(payload.ok === false ? (payload.status || 400) : (payload.status || 200), {
        "Content-Type": "application/json; charset=utf-8",
      });
      res.end(JSON.stringify(payload));
      return;
    }
    res.writeHead(404, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ ok: false, error: "not_found" }));
  });

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));

  try {
    await callback({
      baseUrl: `http://127.0.0.1:${server.address().port}`,
      setProgressMode(mode) {
        scenario.progressMode = mode;
      },
    });
  } finally {
    for (const res of hangingResponses) {
      try {
        res.destroy();
      } catch {
        // ignore
      }
    }
    await new Promise((resolve) => server.close(resolve));
  }
}

async function withPage(callback) {
  const chromePath = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
  const edgePath = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
  const launchOptions = { headless: true };
  if (fs.existsSync(chromePath)) {
    launchOptions.executablePath = chromePath;
  } else if (fs.existsSync(edgePath)) {
    launchOptions.executablePath = edgePath;
  }
  const browser = await chromium.launch(launchOptions);
  const page = await browser.newPage();
  try {
    await callback(page);
  } finally {
    await browser.close();
  }
}

async function fillDateGroup(page, groupId, isoDate) {
  const [year, month, day] = String(isoDate).split("-");
  await page.locator(`[data-grp="${groupId}"][data-seg="y"]`).fill(year);
  await page.locator(`[data-grp="${groupId}"][data-seg="m"]`).fill(String(month || ""));
  await page.locator(`[data-grp="${groupId}"][data-seg="d"]`).fill(String(day || ""));
}

async function testRerunModalUsesDateRangeAndSendsBothDates() {
  const scenario = buildScenario({
    config: {
      min_publish_date: "2026-01-01",
      enabled_platforms: ["douyin", "xiaohongshu"],
    },
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });
      await page.click("#rerunBtn");
      await page.waitForTimeout(200);

      assert.strictEqual(await page.locator("#rerunDateInput").inputValue(), "2026-01-01");
      assert.strictEqual(await page.locator("#rerunEndDateInput").count(), 1, "rerun modal should render an end-date input");
      assert(await page.locator("#rerunModal").innerText().then((text) => text.includes("结束日期")), "rerun modal should mention 结束日期");
      assert.notStrictEqual(await page.locator("#rerunEndDateInput").inputValue(), "", "rerun end date should default to a concrete day");
      assert.strictEqual(await page.locator('[data-grp="rerunDateInput"][data-seg="m"]').inputValue(), "01");
      assert.strictEqual(await page.locator('[data-grp="rerunDateInput"][data-seg="d"]').inputValue(), "01");
      assert.strictEqual(await page.locator('[data-grp="rerunDateInput"][data-seg="m"]').getAttribute("type"), "text");
      assert.strictEqual(await page.locator('[data-grp="rerunDateInput"][data-seg="d"]').getAttribute("type"), "text");

      const rerunModalStyles = await page.locator("#rerunModal .modal").evaluate((el) => {
        const style = getComputedStyle(el);
        return { color: style.color, backgroundImage: style.backgroundImage };
      });
      const rerunCloseStyles = await page.locator("#closeRerunBtn").evaluate((el) => {
        const style = getComputedStyle(el);
        return { color: style.color, backgroundColor: style.backgroundColor };
      });

      assert.strictEqual(rerunModalStyles.color, "rgb(29, 29, 31)", "rerun modal should use dark text in the light scene");
      assert.strictEqual(rerunCloseStyles.color, "rgb(29, 29, 31)", "rerun close button should use dark text in the light scene");
      assert.notStrictEqual(rerunCloseStyles.backgroundColor, "rgba(0, 0, 0, 0)", "rerun close button should render a visible light button background");
      assert(!rerunModalStyles.backgroundImage.includes("rgba(14, 21, 34"), "rerun modal should not use the old dark modal background");

      await page.locator('[data-grp="rerunDateInput"][data-seg="y"]').click();
      await page.keyboard.press("End");
      await page.keyboard.type("5");
      await page.waitForTimeout(80);
      assert.strictEqual(await page.evaluate(() => document.activeElement?.dataset?.seg || ""), "y", "editing a prefilled year should not jump before the segment is fully replaced");

      await page.locator('[data-grp="rerunDateInput"][data-seg="y"]').fill("2026");
      await page.waitForTimeout(80);
      assert.strictEqual(await page.evaluate(() => document.activeElement?.dataset?.seg || ""), "m", "start year should advance focus to month");

      await page.locator('[data-grp="rerunDateInput"][data-seg="m"]').fill("");
      await page.keyboard.type("0");
      await page.waitForTimeout(80);
      assert.strictEqual(await page.evaluate(() => document.activeElement?.dataset?.seg || ""), "m", "start month should wait for a complete 2-digit segment");
      assert.strictEqual(await page.locator('[data-grp="rerunDateInput"][data-seg="m"]').inputValue(), "0");

      await page.keyboard.type("2");
      await page.waitForTimeout(80);
      assert.strictEqual(await page.locator('[data-grp="rerunDateInput"][data-seg="m"]').inputValue(), "02");
      assert.strictEqual(await page.evaluate(() => document.activeElement?.dataset?.seg || ""), "d", "start month should advance focus to day");

      await page.locator('[data-grp="rerunEndDateInput"][data-seg="y"]').fill("2026");
      await page.waitForTimeout(80);
      assert.strictEqual(await page.evaluate(() => document.activeElement?.dataset?.seg || ""), "m", "end year should advance focus to month");

      await fillDateGroup(page, "rerunDateInput", "2026-01-10");
      await fillDateGroup(page, "rerunEndDateInput", "2026-01-31");

      await page.click("#confirmRerunBtn");
      await page.waitForTimeout(200);

      assert.strictEqual(scenario.runAllRequests.length, 1, "rerun should submit one run_all request");
      assert.deepStrictEqual(scenario.runAllRequests[0], {
        min_date: "2026-01-10",
        max_date: "2026-01-31",
        platforms: "douyin,xiaohongshu",
        run_mode: "rerun",
      });
    });
  });
}

async function testMonitorHtmlUsesExternalThemeStylesheet() {
  assert(
    MONITOR_HTML.includes('/assets/progress-apple-theme.css'),
    "monitor html should link the external Apple theme stylesheet"
  );
  assert(!/<style[\s>]/i.test(MONITOR_HTML), "monitor html should not keep inline style blocks");
  assert(
    MONITOR_THEME_CSS.includes("--bg-body") && MONITOR_THEME_CSS.includes("--primary"),
    "theme stylesheet should expose the active design tokens"
  );
}

async function testMonitorHtmlUsesSectionShellStructure() {
  assert(
    /<section class="[^"]*shell-section[^"]*shell-section-light[^"]*" id="wizardView">/.test(MONITOR_HTML),
    "wizard view should use a light shell-section wrapper"
  );
  assert(
    /<section class="[^"]*shell-section[^"]*shell-section-light[^"]*" id="dashboardView">/.test(MONITOR_HTML),
    "dashboard view should use a light shell-section wrapper"
  );
  assert(
    /<section class="[^"]*shell-section[^"]*shell-section-light[^"]*" id="resultsView">/.test(MONITOR_HTML),
    "results view should use a light shell-section wrapper"
  );
  assert(
    MONITOR_HTML.includes('class="panel-title panel-title-lg"'),
    "static section headings should use semantic panel-title classes instead of inline font sizing"
  );
  assert(
    !MONITOR_HTML.includes('<h2 id="heroTitle" style="margin:0">'),
    "hero title should no longer rely on inline style for its layout"
  );
  assert(
    MONITOR_THEME_CSS.includes(".shell-section-light") && MONITOR_THEME_CSS.includes(".panel-title"),
    "theme stylesheet should define shell-section and panel-title structures"
  );
}

async function testWizardRequiresAtLeastOneAuthorizedPlatform() {
  const scenario = buildScenario({
    config: {
      onboarding_completed: false,
      enabled_platforms: ["douyin"],
    },
    configSummary: {
      setup_complete: false,
      authorized_platform_count: 0,
    },
    douyin: {
      auth_status: "unauthorized",
      auth_reason: "not_authorized",
      needs_auth: true,
      ui_status: "auth_required",
    },
    progressSummary: {
      authorized_platform_count: 0,
      setup_complete: false,
    },
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });
      await page.click("#wizardNextBtn");
      await page.waitForTimeout(150);
      await page.click("#wizardNextBtn");
      await page.waitForTimeout(300);

      assert.strictEqual(await page.locator("#wizardCustomerName").count(), 0, "step 2 should not advance without any authorized platform");
      const bodyText = await page.locator("#wizardView").innerText();
      assert(bodyText.includes("平台授权"), "wizard should stay on authorization step");
    });
  });
}

async function testWizardRequiresEverySelectedPlatformToBeAuthorized() {
  const scenario = buildScenario({
    config: {
      onboarding_completed: false,
      enabled_platforms: ["douyin", "xiaohongshu"],
    },
    configSummary: {
      setup_complete: false,
      enabled_platform_count: 2,
      authorized_platform_count: 1,
      authorized_platforms: ["douyin"],
    },
    progressSummary: {
      setup_complete: false,
      enabled_platform_count: 2,
      authorized_platform_count: 1,
      authorized_platforms: ["douyin"],
    },
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.route("**/license", (route) => route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, activated: true, valid: true }),
      }));
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });
      await page.waitForSelector("#wiz-chk-douyin");
      await page.click("#wiz-btn-next");
      await page.click("#wiz-btn-next");
      await page.waitForTimeout(200);

      assert.strictEqual(
        await page.locator("#wiz-cust-name").count(),
        0,
        "wizard must stay on authorization until every selected platform is authorized",
      );
      assert(
        (await page.locator("#wizard-body").innerText()).includes("小红书"),
        "authorization step should keep the missing platform visible",
      );
    });
  });
}

async function testPlatformWritesPreserveLatestSnapshotWhenResponsesReverse() {
  const scenario = buildScenario({
    config: {
      onboarding_completed: false,
      enabled_platforms: [],
    },
    configSummary: {
      setup_complete: false,
      enabled_platform_count: 0,
      authorized_platform_count: 0,
    },
  });
  let receivedPosts = 0;
  let serverPlatforms = [];

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.route("**/license", (route) => route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, activated: true, valid: true }),
      }));
      await page.route("**/config", async (route) => {
        const request = route.request();
        if (request.method() !== "POST") {
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              ok: true,
              config: { ...scenario.config, enabled_platforms: serverPlatforms.slice() },
              summary: { ...scenario.configSummary, enabled_platforms: serverPlatforms.slice() },
            }),
          });
          return;
        }
        const payload = request.postDataJSON();
        receivedPosts += 1;
        if (receivedPosts === 1) await new Promise((resolve) => setTimeout(resolve, 100));
        serverPlatforms = (payload.enabled_platforms || []).slice();
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            ok: true,
            config: { ...scenario.config, enabled_platforms: serverPlatforms.slice() },
            summary: { ...scenario.configSummary, enabled_platforms: serverPlatforms.slice() },
          }),
        });
      });

      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });
      await page.waitForSelector("#wiz-chk-douyin");
      await page.evaluate(() => Promise.all([
        window.togglePlatform("douyin", true),
        window.togglePlatform("xiaohongshu", true),
      ]));
      await page.evaluate(() => window.togglePlatform("bilibili", true));
    });
  });

  assert.deepStrictEqual(
    serverPlatforms,
    ["douyin", "xiaohongshu", "bilibili"],
    "the final server write must contain every locally selected platform",
  );
}

async function testSyncFeishuButtonDisabledWhenFeishuIsUnavailable() {
  const scenario = buildScenario({
    config: { feishu_enabled: false },
    configSummary: { feishu_enabled: false, feishu_ready: false, setup_complete: true },
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });
      assert.strictEqual(await page.locator("#syncFeishuBtn").isDisabled(), true, "feishu-only button should be disabled when feishu is unavailable");
    });
  });
}

async function testVisibleCopyAvoidsInternalWording() {
  const scenario = buildScenario({
    config: {
      onboarding_completed: false,
      enabled_platforms: ["douyin"],
    },
    configSummary: {
      setup_complete: false,
      authorized_platform_count: 0,
    },
    douyin: {
      auth_status: "unauthorized",
      auth_reason: "not_authorized",
      needs_auth: true,
      ui_status: "auth_required",
    },
    progressSummary: {
      authorized_platform_count: 0,
      setup_complete: false,
    },
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });
      const text = await page.locator("body").innerText();
      assert(!text.includes("真实接口"), "visible copy should not mention internal real-interface wording");
      assert(!text.includes("真实配置字段"), "visible copy should not mention internal config-field wording");
      assert(!text.includes("交付配置"), "visible copy should not use internal delivery wording on first screen");
    });
  });
}

async function testDashboardAndResultsUseSemanticTemplateShells() {
  const scenario = buildScenario({
    historyRuns: [
      {
        run_id: 1,
        run_at: "2026-04-05 12:20:00",
        started_at: "2026-04-05 12:18:00",
        ended_at: "2026-04-05 12:20:00",
        duration: 120,
        mode: "incremental",
        platforms: ["douyin"],
        platform_count: 1,
        platform_results: [
          {
            platform: "douyin",
            label: "抖音",
            status: "success",
            ui_status: "completed",
            message: "成功同步 6 条",
            success_count: 6,
            skip_count: 0,
            fail_count: 0,
            total_count: 6,
            auth_status: "authorized",
            auth_reason: "",
            auth_action: "none",
            needs_auth: false,
          },
        ],
        successful_platforms: 1,
        empty_platforms: 0,
        failed_platforms: 0,
        skipped_platforms: 0,
        needs_auth_platforms: 0,
        merge_ok: true,
        status: "completed",
        failed_stage: "",
        feishu: {
          attempted: false,
          status: "not_attempted",
          error: "",
          summary: "",
          prepare: {},
          sync: {},
        },
      },
    ],
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });

      assert.strictEqual(await page.locator("#heroSummaryCard .summary-card-shell").count(), 1, "hero summary should render inside a semantic shell");
      assert.strictEqual(await page.locator("#platformCards .platform-card-shell").count(), 1, "platform cards should render semantic card shells");
      assert.strictEqual(await page.locator("#platformCards .platform-card-meta-grid").count(), 1, "platform cards should separate metric grids");
      assert.strictEqual(await page.locator("#platformCards .platform-card-footer").count(), 1, "platform cards should render a footer area");

      await page.click("#openResultsBtn");
      await page.waitForTimeout(200);

      assert.strictEqual(await page.locator("#recentBox .results-overview-shell").count(), 1, "results overview should render inside a semantic shell");
      assert.strictEqual(await page.locator("#latestResultsBox .recent-spotlight-shell").count(), 1, "latest run spotlight should render inside a semantic shell");
    });
  });
}

async function testDashboardUsesLightSceneChrome() {
  const scenario = buildScenario({
    configSummary: {
      setup_complete: true,
      authorized_platform_count: 1,
    },
    historyRuns: [
      {
        run_id: 1,
        run_at: "2026-04-05 12:20:00",
        started_at: "2026-04-05 12:18:00",
        ended_at: "2026-04-05 12:20:00",
        duration: 120,
        mode: "incremental",
        platforms: ["douyin"],
        platform_count: 1,
        platform_results: [
          {
            platform: "douyin",
            label: "抖音",
            status: "success",
            ui_status: "completed",
            message: "成功同步 6 条",
            success_count: 6,
            skip_count: 0,
            fail_count: 0,
            total_count: 6,
            auth_status: "authorized",
            auth_reason: "",
            auth_action: "none",
            needs_auth: false,
          },
        ],
        successful_platforms: 1,
        empty_platforms: 0,
        failed_platforms: 0,
        skipped_platforms: 0,
        needs_auth_platforms: 0,
        merge_ok: true,
        status: "completed",
        failed_stage: "",
        feishu: {
          attempted: false,
          status: "not_attempted",
          error: "",
          summary: "",
          prepare: {},
          sync: {},
        },
      },
    ],
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });

      const headerStyles = await page.locator(".app > .panel:first-child").evaluate((el) => {
        const style = getComputedStyle(el);
        return { color: style.color, backgroundImage: style.backgroundImage };
      });
      const heroStyles = await page.locator(".hero").evaluate((el) => {
        const style = getComputedStyle(el);
        return { color: style.color, backgroundImage: style.backgroundImage };
      });
      const actionStyles = await page.locator(".top-actions .btn").first().evaluate((el) => {
        const style = getComputedStyle(el);
        return { color: style.color, backgroundColor: style.backgroundColor };
      });
      const heroNoteStyles = await page.locator("#dashboardView .hero-note").evaluate((el) => {
        const style = getComputedStyle(el);
        return { backgroundColor: style.backgroundColor };
      });
      const taskCellStyles = await page.locator("#scrapeTask .task-card-cell").first().evaluate((el) => {
        const style = getComputedStyle(el);
        return { backgroundColor: style.backgroundColor };
      });

      assert.strictEqual(headerStyles.color, "rgb(29, 29, 31)", "top header should use dark text in the light scene");
      assert.strictEqual(heroStyles.color, "rgb(29, 29, 31)", "hero should use dark text in the light scene");
      assert.strictEqual(actionStyles.color, "rgb(29, 29, 31)", "top action buttons should use dark text in the light scene");
      assert.notStrictEqual(actionStyles.backgroundColor, "rgba(0, 0, 0, 0)", "top action buttons should render as visible light buttons");
      assert.strictEqual(heroNoteStyles.backgroundColor, "rgba(255, 255, 255, 0.76)", "hero summary cards should become more translucent so the homepage ambient effect can read through");
      assert.strictEqual(taskCellStyles.backgroundColor, "rgba(255, 255, 255, 0.58)", "dashboard metric cells should become lighter glass cards instead of opaque white blocks");
      assert(!headerStyles.backgroundImage.includes("rgba(16, 24, 40"), "top header should no longer use the dark chrome gradient");
      assert(!heroStyles.backgroundImage.includes("rgba(15, 23, 42"), "hero should no longer use the dark dashboard gradient");
    });
  });
}

async function testDashboardProvidesAmbientEffectShell() {
  assert(
    MONITOR_HTML.includes('id="dashboardAmbientShell"'),
    "dashboard view should expose a dedicated ambient shell for the homepage effect"
  );
  assert(
    MONITOR_HTML.includes('id="dashboardAmbientStage" data-us-project="RFooKJ2dsWHI5ArJVBox"'),
    "dashboard view should expose a UnicornStudio mount node with the approved project id"
  );
  assert(
    MONITOR_THEME_CSS.includes(".dashboard-ambient-shell") &&
      MONITOR_THEME_CSS.includes(".dashboard-layer"),
    "theme stylesheet should define dashboard ambient background and content layering"
  );

  const scenario = buildScenario({
    configSummary: {
      setup_complete: true,
      authorized_platform_count: 1,
    },
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });

      assert.strictEqual(await page.locator("#dashboardView #dashboardAmbientShell").count(), 1, "dashboard view should render one ambient shell");
      assert.strictEqual(await page.locator("#wizardView #dashboardAmbientShell").count(), 0, "wizard view should not render the dashboard ambient shell");
      assert.strictEqual(await page.locator("#resultsView #dashboardAmbientShell").count(), 0, "results view should not render the dashboard ambient shell");

      const ambientStyles = await page.locator("#dashboardAmbientShell").evaluate((el) => {
        const style = getComputedStyle(el);
        return {
          pointerEvents: style.pointerEvents,
          position: style.position,
        };
      });
      const layerStyles = await page.locator("#dashboardView .dashboard-layer").first().evaluate((el) => {
        const style = getComputedStyle(el);
        return {
          position: style.position,
          zIndex: style.zIndex,
        };
      });

      assert.strictEqual(ambientStyles.pointerEvents, "none", "ambient shell should never block dashboard interactions");
      assert.strictEqual(ambientStyles.position, "absolute", "ambient shell should render as an absolute background layer");
      assert.strictEqual(layerStyles.position, "relative", "dashboard content should remain on a positioned layer above the effect");
      assert.notStrictEqual(layerStyles.zIndex, "auto", "dashboard content should declare a stacking order above the ambient effect");
    });
  });
}

async function testWizardStepOneUsesLightSceneChrome() {
  const scenario = buildScenario({
    config: {
      enabled_platforms: ["xiaohongshu"],
      onboarding_completed: false,
    },
    configSummary: {
      setup_complete: false,
      authorized_platform_count: 0,
    },
    progressSummary: {
      setup_complete: false,
      authorized_platform_count: 0,
    },
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });

      const wizardHeroStyles = await page.locator("#wizardView .wizard-hero").evaluate((el) => {
        const style = getComputedStyle(el);
        return { color: style.color, backgroundImage: style.backgroundImage, backgroundColor: style.backgroundColor };
      });
      const choiceTileStyles = await page.locator("#wizardView .choice-grid .tile").first().evaluate((el) => {
        const style = getComputedStyle(el);
        return { color: style.color, backgroundColor: style.backgroundColor };
      });

      assert.strictEqual(wizardHeroStyles.color, "rgb(29, 29, 31)", "wizard hero should use dark text in the light scene");
      assert(!wizardHeroStyles.backgroundImage.includes("rgba(15, 23, 42"), "wizard hero should not use the old dark gradient");
      assert.strictEqual(choiceTileStyles.color, "rgb(29, 29, 31)", "wizard platform tiles should use dark text in the light scene");
      assert.notStrictEqual(choiceTileStyles.backgroundColor, "rgba(0, 0, 0, 0)", "wizard platform tiles should render as visible light cards");
    });
  });
}

async function testWizardStepTransitionsUseAnimatedShellsWithoutChangingFlow() {
  assert(
    MONITOR_HTML.includes('id="wizardBody" class="wizard-stage-host"'),
    "wizard view should provide a dedicated animated host for step content"
  );
  assert(
    MONITOR_THEME_CSS.includes(".wizard-stage-host") &&
      MONITOR_THEME_CSS.includes(".wizard-stage.is-entering") &&
      MONITOR_THEME_CSS.includes(".wizard-stage.forward.is-leaving.is-active"),
    "theme stylesheet should define animated wizard step shells for directional transitions"
  );

  const scenario = buildScenario({
    config: {
      enabled_platforms: ["douyin"],
      onboarding_completed: false,
    },
    configSummary: {
      setup_complete: false,
      authorized_platform_count: 1,
    },
    progressSummary: {
      setup_complete: false,
      authorized_platform_count: 1,
    },
    douyin: {
      enabled: true,
      auth_status: "authorized",
      auth_reason: "",
      needs_auth: false,
      ui_status: "idle",
    },
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });

      assert.strictEqual(await page.locator("#wizardBody .wizard-stage").count(), 1, "wizard should render a single current step stage at rest");
      await page.click("#wizardNextBtn");
      await page.waitForTimeout(60);

      assert.strictEqual(await page.locator("#wizardBody.is-transitioning").count(), 1, "moving between wizard steps should enter transition mode");
      assert.strictEqual(await page.locator("#wizardBody .wizard-stage.is-entering").count(), 1, "wizard should render an entering step shell during transitions");
      assert.strictEqual(await page.locator("#wizardBody .wizard-stage.is-leaving").count(), 1, "wizard should keep a leaving shell for the outgoing step");

      await page.waitForTimeout(450);

      const wizardText = await page.locator("#wizardView").innerText();
      assert.strictEqual(await page.locator("#wizardBody .wizard-stage").count(), 1, "wizard should settle back to one stage after the transition");
      assert(wizardText.includes("平台授权"), "wizard should still advance into the real authorization step");
      assert.strictEqual(await page.locator('#wizardBody [data-auth="douyin"]').count(), 1, "wizard step transition should keep the existing authorization action buttons intact");
    });
  });
}

async function testHistoryAndSettingsUseSemanticTemplateShells() {
  const scenario = buildScenario({
    config: {
      enabled_platforms: ["douyin"],
      feishu_enabled: true,
    },
    configSummary: {
      setup_complete: true,
      feishu_enabled: true,
      feishu_ready: true,
      authorized_platform_count: 1,
    },
    historyRuns: [
      {
        run_id: 1,
        run_at: "2026-04-05 12:20:00",
        started_at: "2026-04-05 12:18:00",
        ended_at: "2026-04-05 12:20:00",
        duration: 120,
        mode: "incremental",
        platforms: ["douyin"],
        platform_count: 1,
        platform_results: [
          {
            platform: "douyin",
            label: "抖音",
            status: "success",
            ui_status: "completed",
            message: "成功同步 6 条",
            success_count: 6,
            skip_count: 0,
            fail_count: 0,
            total_count: 6,
            auth_status: "authorized",
            auth_reason: "",
            auth_action: "none",
            needs_auth: false,
          },
        ],
        successful_platforms: 1,
        empty_platforms: 0,
        failed_platforms: 0,
        skipped_platforms: 0,
        needs_auth_platforms: 0,
        merge_ok: true,
        status: "completed",
        failed_stage: "",
        feishu: {
          attempted: true,
          status: "success",
          error: "",
          summary: "明细 6 条",
          prepare: { detail_count: 6, work_count: 6 },
          sync: {},
        },
      },
    ],
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });

      await page.click("#openSettingsBtn");
      await page.waitForTimeout(200);

      assert.strictEqual(await page.locator("#settingPlatformGrid .settings-toggle-shell").count(), 6, "settings should render semantic platform toggle shells");
      assert.strictEqual(await page.locator("#settingsAuthGrid .settings-auth-shell").count(), 1, "settings auth area should render semantic auth card shells");

      await page.click("#closeSettingsBtn");
      await page.waitForTimeout(150);
      await page.click("#openResultsBtn");
      await page.waitForTimeout(200);

      assert.strictEqual(await page.locator("#historyBox .history-record-shell").count(), 1, "history records should render inside semantic shells");
      assert.strictEqual(await page.locator("#historyBox .history-detail-shell").count(), 3, "history record should split base/platform/feishu detail shells");
    });
  });
}

async function testSettingsModalUsesDedicatedChrome() {
  assert(
    MONITOR_HTML.includes('class="modal settings-modal"'),
    "settings dialog should use a dedicated settings-modal shell"
  );
  assert(
    MONITOR_HTML.includes('class="btn modal-close-btn" id="closeSettingsBtn"'),
    "settings dialog should expose a dedicated close button chrome"
  );
  assert(
    MONITOR_HTML.includes('class="modal-foot settings-modal-foot"'),
    "settings dialog should use a dedicated footer shell"
  );
  assert(
    MONITOR_HTML.includes('class="btns settings-footer-actions"'),
    "settings dialog should group footer actions inside a dedicated settings action row"
  );
  assert(
    MONITOR_THEME_CSS.includes(".settings-modal") &&
      MONITOR_THEME_CSS.includes(".settings-inline-toggle") &&
      MONITOR_THEME_CSS.includes(".modal-close-btn") &&
      MONITOR_THEME_CSS.includes(".settings-modal .field label"),
    "theme stylesheet should define dedicated settings modal chrome and light-theme field styling"
  );

  const scenario = buildScenario({
    config: {
      enabled_platforms: ["douyin"],
      feishu_enabled: true,
      feishu_auto_sync: true,
      include_weixin_mp_in_sync: true,
      feishu_app_token: "app-token",
      feishu_app_id: "cli_demo",
      feishu_app_secret: "secret-demo",
    },
    configSummary: {
      setup_complete: true,
      feishu_enabled: true,
      feishu_ready: true,
      authorized_platform_count: 1,
    },
    testFeishuResponse: {
      ok: true,
      message: "飞书连接测试成功，可直接同步。",
    },
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });
      await page.click("#openSettingsBtn");
      await page.waitForTimeout(200);

      assert.strictEqual(await page.locator("#settingsModal .settings-modal").count(), 1, "settings dialog should render a dedicated modal shell");
      assert.strictEqual(await page.locator("#settingsModal .modal-close-btn").count(), 1, "settings dialog should render a dedicated close button");
      assert.strictEqual(await page.locator("#settingsModal .settings-inline-toggle").count(), 3, "settings dialog should render dedicated switch cards");
      assert.strictEqual(await page.locator("#settingsModal .settings-footer-actions").count(), 1, "settings dialog should render a dedicated footer action row");
      assert.strictEqual(await page.locator("#settingsModal .settings-auth-summary").count(), 1, "settings auth card should render a dedicated summary block");
      assert.strictEqual(await page.locator("#settingsModal .settings-auth-metrics").count(), 1, "settings auth card should render a dedicated metrics block");
      assert.strictEqual(await page.locator("#settingsModal .settings-auth-actions").count(), 1, "settings auth card should render a dedicated action row");

      const modalStyles = await page.locator("#settingsModal .settings-modal").evaluate((el) => {
        const style = getComputedStyle(el);
        return { color: style.color };
      });
      const closeStyles = await page.locator("#settingsModal .modal-close-btn").evaluate((el) => {
        const style = getComputedStyle(el);
        return { color: style.color, backgroundColor: style.backgroundColor };
      });
      const toggleStyles = await page.locator("#settingsModal .settings-inline-toggle").first().evaluate((el) => {
        const style = getComputedStyle(el);
        return { color: style.color, backgroundColor: style.backgroundColor };
      });
      const authActionStyles = await page.locator("#settingsModal .settings-auth-actions").evaluate((el) => {
        const style = getComputedStyle(el);
        return { justifyContent: style.justifyContent, flexWrap: style.flexWrap };
      });

      assert.strictEqual(modalStyles.color, "rgb(29, 29, 31)", "settings dialog should use dark text in the light scene");
      assert.strictEqual(closeStyles.color, "rgb(29, 29, 31)", "settings close button should switch to dark text in the light scene");
      assert.notStrictEqual(closeStyles.backgroundColor, "rgba(0, 0, 0, 0)", "settings close button should use a visible light button background");
      assert.strictEqual(toggleStyles.color, "rgb(29, 29, 31)", "settings switch cards should use dark text in the light scene");
      assert.notStrictEqual(toggleStyles.backgroundColor, "rgba(0, 0, 0, 0)", "settings switch cards should render as visible light cards");
      assert.strictEqual(authActionStyles.justifyContent, "flex-start", "settings auth actions should align from the left for clearer hierarchy");
      assert.strictEqual(authActionStyles.flexWrap, "wrap", "settings auth actions should wrap cleanly in the light scene");

      await page.click("#testFeishuBtn");
      await page.waitForFunction(() => {
        const el = document.querySelector("#settingsTestStatus");
        return Boolean(el && !el.classList.contains("hidden") && /飞书连接测试成功/.test(el.textContent || ""));
      });

      assert.strictEqual(await page.locator("#settingsModal #settingsTestStatus").count(), 1, "settings dialog should provide a local status area for feishu checks");
      const localStatusText = await page.locator("#settingsModal #settingsTestStatus").innerText();
      assert(localStatusText.includes("飞书连接测试成功"), "feishu test result should stay visible inside the settings dialog");
      assert.strictEqual(
        await page.locator("#toast").evaluate((el) => el.classList.contains("hidden")),
        true,
        "feishu test feedback should not rely on the global toast outside the settings dialog"
      );
    });
  });
}

async function testOfflineStateAppearsWithinSecondsWhenProgressEndpointHangs() {
  const scenario = buildScenario({
    configSummary: {
      setup_complete: true,
      authorized_platform_count: 1,
    },
    progressSummary: {
      setup_complete: true,
      authorized_platform_count: 1,
    },
  });

  await withServer(scenario, async ({ baseUrl, setProgressMode }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });
      setProgressMode("hang");
      await page.waitForTimeout(8000);

      const topChips = await page.locator("#topChips").innerText();
      assert(topChips.includes("服务离线"), "page should switch to offline state after backend stops responding");
      assert.strictEqual(await page.locator("#startSyncBtn").isDisabled(), true, "start button should be disabled while backend is offline");
    });
  });
}

async function testNoDataCopyUsesWuShuJuInsteadOfZeroCountCopy() {
  const scenario = buildScenario({
    douyin: {
      status: "completed",
      phase: "done",
      ui_status: "completed_empty",
      totalWorks: 6,
      processedWorks: 6,
      successWorks: 0,
      skippedWorks: 6,
      failedWorks: 0,
      message: "本轮没有新增导出，已沿用已有 6 份本地作品结果",
    },
    progressSummary: {
      completed_platforms: [],
      zero_result_platforms: ["douyin"],
      failed_platforms: [],
    },
    historyRuns: [
      {
        run_id: 1,
        run_at: "2026-04-04 21:28:05",
        started_at: "2026-04-04 21:28:05",
        ended_at: "2026-04-04 21:28:05",
        duration: 12,
        mode: "incremental",
        platforms: ["douyin"],
        platform_count: 1,
        platform_results: [
          {
            platform: "douyin",
            label: "抖音",
            status: "completed_empty",
            ui_status: "completed_empty",
            message: "本轮没有新增导出，已沿用已有 6 份本地作品结果",
            success_count: 0,
            skip_count: 6,
            fail_count: 0,
            total_count: 6,
            auth_status: "authorized",
            auth_reason: "",
            auth_action: "none",
            needs_auth: false,
          },
        ],
        successful_platforms: 0,
        empty_platforms: 1,
        failed_platforms: 0,
        skipped_platforms: 0,
        needs_auth_platforms: 0,
        merge_ok: true,
        status: "completed_empty",
        failed_stage: "",
        feishu: {
          attempted: false,
          status: "not_attempted",
          error: "",
          summary: "",
          prepare: {},
          sync: {},
        },
      },
    ],
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });
      const text = await page.locator("#dashboardView").innerText();

      assert(text.includes("无数据"), "dashboard should use 无数据 wording for zero-result states");
      assert(!text.includes("完成（0条）"), "dashboard should not show 完成（0条）");
      assert(!text.includes("成功，0条"), "dashboard should not show 成功，0条");
      assert(!text.includes("成功 / 0条 / 失败平台"), "dashboard should not show 0条 in summary labels");
    });
  });
}

async function testAuthRequiredStateDoesNotPretendToBeCompleted() {
  const scenario = buildScenario({
    config: {
      enabled_platforms: ["douyin"],
      onboarding_completed: true,
    },
    configSummary: {
      setup_complete: true,
      authorized_platform_count: 0,
    },
    douyin: {
      status: "completed",
      phase: "done",
      message: "抖音登录完成（AUTH_ONLY）",
      ui_status: "auth_required",
      auth_status: "needs_auth",
      auth_reason: "manual_reauth_required",
      needs_auth: true,
      totalWorks: 0,
      processedWorks: 0,
      successWorks: 0,
      skippedWorks: 0,
      failedWorks: 0,
    },
    progressSummary: {
      setup_complete: true,
      authorized_platform_count: 0,
    },
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });
      const cardText = await page.locator("#platformCards .platform-card").first().innerText();

      assert(cardText.includes("需重新授权"), "auth card should keep the platform in reauthorize state");
      assert(cardText.includes("需要重新扫码授权"), "auth card should explain why reauthorization is needed");
      assert(!cardText.includes("无数据"), "auth card should not present auth failure as no-data completion");
    });
  });
}

async function testFeishuOnlyRunsFallbackToPlatformsWhenPlatformResultsAreEmpty() {
  const scenario = buildScenario({
    configSummary: {
      setup_complete: true,
      authorized_platform_count: 2,
    },
    historyRuns: [
      {
        run_id: 1,
        run_at: "2026-04-05 15:20:00",
        started_at: "2026-04-05 15:19:30",
        ended_at: "2026-04-05 15:20:00",
        duration: 30,
        mode: "feishu_only",
        platforms: ["douyin", "xiaohongshu"],
        platform_count: 2,
        platform_results: [],
        successful_platforms: 0,
        empty_platforms: 0,
        failed_platforms: 0,
        skipped_platforms: 0,
        needs_auth_platforms: 0,
        merge_ok: true,
        status: "completed",
        failed_stage: "",
        feishu: {
          attempted: false,
          status: "not_attempted",
          error: "",
          summary: "本地没有新数据或指标变化，已跳过飞书同步。",
          prepare: {},
          sync: {},
        },
      },
    ],
  });

  await withServer(scenario, async ({ baseUrl }) => {
    await withPage(async (page) => {
      await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });
      await page.click("#openResultsBtn");
      await page.waitForTimeout(200);

      const latestText = await page.locator("#latestResultsBox").innerText();
      const historyText = await page.locator("#historyBox").innerText();

      assert(latestText.includes("抖音、小红书"), "latest results should fall back to run.platforms labels for feishu-only runs");
      assert(historyText.includes("抖音、小红书"), "history view should fall back to run.platforms labels for feishu-only runs");
      assert(!latestText.includes("平台范围\n-"), "latest results should not render '-' when run.platforms is available");
      assert(!historyText.includes("平台范围\n-"), "history view should not render '-' when run.platforms is available");
    });
  });
}

async function main() {
  await testMonitorHtmlUsesExternalThemeStylesheet();
  await testMonitorHtmlUsesSectionShellStructure();
  await testRerunModalUsesDateRangeAndSendsBothDates();
  await testWizardRequiresAtLeastOneAuthorizedPlatform();
  await testPlatformWritesPreserveLatestSnapshotWhenResponsesReverse();
  await testSyncFeishuButtonDisabledWhenFeishuIsUnavailable();
  await testVisibleCopyAvoidsInternalWording();
  await testDashboardUsesLightSceneChrome();
  await testDashboardProvidesAmbientEffectShell();
  await testWizardStepOneUsesLightSceneChrome();
  await testWizardStepTransitionsUseAnimatedShellsWithoutChangingFlow();
  await testDashboardAndResultsUseSemanticTemplateShells();
  await testHistoryAndSettingsUseSemanticTemplateShells();
  await testSettingsModalUsesDedicatedChrome();
  await testOfflineStateAppearsWithinSecondsWhenProgressEndpointHangs();
  await testNoDataCopyUsesWuShuJuInsteadOfZeroCountCopy();
  await testAuthRequiredStateDoesNotPretendToBeCompleted();
  await testFeishuOnlyRunsFallbackToPlatformsWhenPlatformResultsAreEmpty();
}

const runReliabilityGuards = async () => {
  await testPlatformWritesPreserveLatestSnapshotWhenResponsesReverse();
  await testWizardRequiresEverySelectedPlatformToBeAuthorized();
};

const selectedRun = process.argv.includes("--reliability-only")
  ? runReliabilityGuards
  : process.argv.includes("--platform-write-only")
  ? testPlatformWritesPreserveLatestSnapshotWhenResponsesReverse
  : (process.argv.includes("--onboarding-auth-only")
    ? testWizardRequiresEverySelectedPlatformToBeAuthorized
    : main);

selectedRun().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
