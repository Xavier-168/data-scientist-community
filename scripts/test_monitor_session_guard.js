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
const MONITOR_FIGMA_CSS = fs.readFileSync(
  path.resolve(__dirname, "..", "frontend", "assets", "progress-figma-dashboard.css"),
  "utf8"
);

function buildProgress({ enabledPlatforms = [], setupComplete = false, authOverrides = {} } = {}) {
  function idleProgress(platform, enabled) {
    const override = authOverrides[platform] || {};
    const authStatus = override.auth_status || (enabled ? "authorized" : "unauthorized");
    const authReason = override.auth_reason || (authStatus === "authorized" ? "" : "not_authorized");
    const needsAuth = override.needs_auth !== undefined ? override.needs_auth : authStatus !== "authorized";
    const uiStatus = override.ui_status || (needsAuth ? "auth_required" : "idle");
    return {
      platform,
      status: "idle",
      phase: "idle",
      message: override.message || "待机中",
      enabled,
      auth_status: authStatus,
      auth_reason: authReason,
      auth_action: override.auth_action || (needsAuth ? "authorize" : "none"),
      needs_auth: needsAuth,
      ui_status: uiStatus,
      successWorks: 0,
      skippedWorks: 0,
      failedWorks: 0,
    };
  }

  return {
    ok: true,
    running: false,
    douyin: idleProgress("douyin", enabledPlatforms.includes("douyin")),
    xiaohongshu: idleProgress("xiaohongshu", enabledPlatforms.includes("xiaohongshu")),
    bilibili: idleProgress("bilibili", enabledPlatforms.includes("bilibili")),
    kuaishou: idleProgress("kuaishou", enabledPlatforms.includes("kuaishou")),
    weixin_mp: idleProgress("weixin_mp", enabledPlatforms.includes("weixin_mp")),
    weixin_channels: idleProgress("weixin_channels", enabledPlatforms.includes("weixin_channels")),
    summary: {
      active_platform: "",
      current_stage: "idle",
      has_running_platform: false,
      completed_platforms: [],
      zero_result_platforms: [],
      failed_platforms: [],
      needs_auth_platforms: [],
      enabled_platform_count: enabledPlatforms.length,
      authorized_platform_count: enabledPlatforms.filter((platform) => (authOverrides[platform]?.auth_status || "authorized") === "authorized").length,
      setup_complete: setupComplete,
      feishu_enabled: false,
      feishu_ready: false,
      auto_sync_enabled: false,
      feishu: {
        enabled: false,
        ready: false,
        auto_sync_enabled: false,
        status: "disabled",
        message: "",
        current_platforms: [],
        current_platform_labels: [],
        last_sync_at: "",
        last_platforms: [],
        last_platform_labels: [],
        last_summary: "",
        last_error: "",
      },
    },
    serverTime: "2026-04-07T21:00:00+0800",
  };
}

async function withServer(handlers, callback) {
  const server = http.createServer(async (req, res) => {
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
    if (url.pathname === "/assets/progress-figma-dashboard.css") {
      res.writeHead(200, { "Content-Type": "text/css; charset=utf-8" });
      res.end(MONITOR_FIGMA_CSS);
      return;
    }

    if (url.pathname === "/progress") {
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify(handlers.progress));
      return;
    }

    if (url.pathname === "/session/recover") {
      const response = handlers.recover
        ? await handlers.recover(req)
        : { status: 403, body: { ok: false, error: "forbidden" } };
      res.writeHead(response.status || 200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify(response.body));
      return;
    }

    if (url.pathname === "/config") {
      const response = await handlers.config(req);
      res.writeHead(response.status || 200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify(response.body));
      return;
    }

    if (url.pathname === "/all-data") {
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ ok: true, rows: [], row_count: 0 }));
      return;
    }

    if (url.pathname === "/analytics/history") {
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ ok: true, runs: [] }));
      return;
    }

    if (url.pathname === "/license") {
      const response = handlers.license
        ? await handlers.license(req)
        : { status: 200, body: { ok: true, activated: true, valid: true, customer_name: "测试客户" } };
      res.writeHead(response.status || 200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify(response.body));
      return;
    }

    if (req.method === "POST") {
      if (handlers.post) {
        const response = await handlers.post(req, url);
        if (response) {
          res.writeHead(response.status || 200, { "Content-Type": "application/json; charset=utf-8" });
          res.end(JSON.stringify(response.body));
          return;
        }
      }
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ ok: true }));
      return;
    }

    res.writeHead(404, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ ok: false, error: "not_found" }));
  });

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));

  try {
    await callback(`http://127.0.0.1:${server.address().port}`);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

async function withPage(callback) {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
    || (process.platform === "darwin" ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" : "");
  const browser = await chromium.launch({
    headless: true,
    ...(executablePath && fs.existsSync(executablePath) ? { executablePath } : {}),
  });
  const page = await browser.newPage();
  try {
    await callback(page);
  } finally {
    await browser.close();
  }
}

async function testMissingSessionShowsRecoveryOverlay() {
  await withServer(
    {
      progress: buildProgress({ enabledPlatforms: [] }),
      async config() {
        return { status: 401, body: { ok: false, error: "session_required" } };
      },
      async license() {
        return { status: 401, body: { ok: false, error: "session_required" } };
      },
    },
    async (baseUrl) => {
      await withPage(async (page) => {
        await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });

        await page.waitForSelector("#session-overlay:not(.hidden)", { timeout: 3000 });
        const message = await page.locator("#session-message").innerText();

        assert(message.includes("启动会话"), "missing-session flow should explain that the launcher session is missing or expired");
        assert.strictEqual(
          await page.locator("#toast-container").innerText(),
          "",
          "missing-session flow should not fall through to an unrelated wizard toast"
        );
        assert.strictEqual(
          await page.locator("#license-overlay").evaluate((node) => node.classList.contains("hidden")),
          true,
          "missing-session flow should suppress the license dialog and surface a single recovery path"
        );
      });
    }
  );
}

async function testStoredSessionTokenRestoresConfigWithoutHash() {
  const restoredToken = "restored-token";
  const configPayload = {
    customer_name: "测试客户",
    workspace_name: "本地数据工作台",
    min_publish_date: "2026-01-01",
    browser_channel: "chrome",
    enabled_platforms: ["douyin"],
    onboarding_completed: false,
    feishu_enabled: false,
    feishu_auto_sync: false,
    include_weixin_mp_in_sync: false,
  };

  await withServer(
    {
      progress: buildProgress({ enabledPlatforms: ["douyin"] }),
      async config(req) {
        if (req.headers["x-yrg-session"] !== restoredToken) {
          return { status: 401, body: { ok: false, error: "session_required" } };
        }
        return {
          status: 200,
          body: {
            ok: true,
            config: configPayload,
            summary: {
              enabled_platform_count: 1,
              enabled_platforms: ["douyin"],
              authorized_platform_count: 1,
              setup_complete: false,
              feishu_enabled: false,
              feishu_ready: false,
              auto_sync_enabled: false,
            },
          },
        };
      },
    },
    async (baseUrl) => {
      await withPage(async (page) => {
        await page.addInitScript((token) => {
          window.localStorage.setItem("yrg_monitor_session_token", token);
        }, restoredToken);

        await page.goto(`${baseUrl}/monitor`, { waitUntil: "networkidle" });

        await page.waitForSelector("#wiz-chk-douyin");
        assert.strictEqual(
          await page.locator("#wiz-chk-douyin").isChecked(),
          true,
          "stored launcher session should restore protected config on a bare /monitor visit"
        );
        assert.strictEqual(
          await page.locator("#session-overlay").evaluate((node) => node.classList.contains("hidden")),
          true,
          "successful session recovery should not show the recovery overlay"
        );
      });
    }
  );
}

async function testStaleSessionRecoversDuringLicenseCheck() {
  const staleToken = "stale-token";
  const liveToken = "live-token";
  let recoverCalls = 0;
  let licenseCalls = 0;
  let configCalls = 0;

  await withServer(
    {
      progress: buildProgress({ enabledPlatforms: ["douyin"] }),
      async recover() {
        recoverCalls += 1;
        return { status: 200, body: { ok: true, token: liveToken } };
      },
      async license(req) {
        licenseCalls += 1;
        if (req.headers["x-yrg-session"] !== liveToken) {
          return { status: 401, body: { ok: false, error: "session_required" } };
        }
        return { status: 200, body: { ok: true, activated: true, valid: true, customer_name: "测试客户" } };
      },
      async config(req) {
        configCalls += 1;
        if (req.headers["x-yrg-session"] !== liveToken) {
          return { status: 401, body: { ok: false, error: "session_required" } };
        }
        return {
          status: 200,
          body: {
            ok: true,
            config: {
              customer_name: "测试客户",
              workspace_name: "本地数据工作台",
              min_publish_date: "2026-01-01",
              browser_channel: "chrome",
              enabled_platforms: ["douyin"],
              onboarding_completed: false,
              feishu_enabled: false,
              feishu_auto_sync: false,
              include_weixin_mp_in_sync: false,
            },
            summary: {
              enabled_platform_count: 1,
              enabled_platforms: ["douyin"],
              authorized_platform_count: 1,
              setup_complete: false,
              feishu_enabled: false,
              feishu_ready: false,
              auto_sync_enabled: false,
            },
          },
        };
      },
    },
    async (baseUrl) => {
      await withPage(async (page) => {
        await page.goto(`${baseUrl}/monitor#session=${staleToken}`, { waitUntil: "networkidle" });

        await page.waitForSelector("#wiz-chk-douyin", { timeout: 3000 });
        await page.waitForFunction(
          (token) => window.location.hash === `#session=${token}`,
          liveToken,
          { timeout: 3000 }
        );

        assert.strictEqual(recoverCalls, 1, "stale session should be recovered exactly once during license bootstrap");
        assert(licenseCalls >= 2, "license check should retry after session recovery");
        assert(configCalls >= 1, "business config should load after the recovered session becomes live");
        assert.strictEqual(
          await page.locator("#session-overlay").evaluate((node) => node.classList.contains("hidden")),
          true,
          "recoverable stale session should not surface the session-expired overlay"
        );
        assert.strictEqual(
          await page.locator("#license-overlay").evaluate((node) => node.classList.contains("hidden")),
          true,
          "successful recovery should not leave the license dialog open"
        );
      });
    }
  );
}

async function testLicenseInvalidConfigLocksUiAndStopsBusinessPolling() {
  let configCalls = 0;
  let licenseCalls = 0;

  await withServer(
    {
      progress: buildProgress({ enabledPlatforms: ["douyin"] }),
      async license() {
        licenseCalls += 1;
        if (licenseCalls === 1) {
          return { status: 200, body: { ok: true, activated: true, valid: true, customer_name: "测试客户" } };
        }
        return {
          status: 200,
          body: {
            ok: true,
            activated: true,
            valid: false,
            customer_name: "测试客户",
            info: {
              error: "machine_mismatch",
              message: "机器指纹不匹配，请重新激活",
            },
          },
        };
      },
      async config() {
        configCalls += 1;
        return {
          status: 403,
          body: {
            ok: false,
            error: "license_invalid",
            license_error: "machine_mismatch",
            message: "机器指纹不匹配，请重新激活",
          },
        };
      },
    },
    async (baseUrl) => {
      await withPage(async (page) => {
        await page.goto(`${baseUrl}/monitor#session=live-token`, { waitUntil: "networkidle" });

        await page.waitForSelector("#license-overlay:not(.hidden)", { timeout: 3000 });
        await page.waitForFunction(
          () => {
            const text = document.getElementById("license-error")?.innerText || "";
            return text.includes("机器指纹不匹配");
          },
          { timeout: 3000 }
        );

        assert.strictEqual(
          await page.locator("#session-overlay").evaluate((node) => node.classList.contains("hidden")),
          true,
          "license blocking should not fall through to the session-expired overlay"
        );
        assert.strictEqual(
          await page.locator("#wizard-overlay").evaluate((node) => node.classList.contains("hidden")),
          true,
          "license blocking should suppress the setup wizard"
        );

        await page.waitForTimeout(3500);

        assert.strictEqual(configCalls, 1, "business config polling should stop after /config returns license_invalid");
        assert(licenseCalls >= 2, "license polling should continue so the page can recover after re-activation");
        assert.strictEqual(
          await page.locator("#license-overlay").evaluate((node) => node.classList.contains("hidden")),
          false,
          "license overlay should remain stable while the license is invalid"
        );
      });
    }
  );
}

async function testUnauthorizedEnabledPlatformDoesNotOfferSingleRun() {
  await withServer(
    {
      progress: buildProgress({
        enabledPlatforms: ["bilibili"],
        setupComplete: true,
        authOverrides: {
          bilibili: {
            auth_status: "unauthorized",
            auth_reason: "not_authorized",
            needs_auth: true,
            ui_status: "auth_required",
            message: "需要先授权 B 站账号",
          },
        },
      }),
      async config() {
        return {
          status: 200,
          body: {
            ok: true,
            config: {
              customer_name: "测试客户",
              workspace_name: "本地数据工作台",
              min_publish_date: "2026-01-01",
              browser_channel: "chrome",
              enabled_platforms: ["bilibili"],
              onboarding_completed: true,
              feishu_enabled: false,
              feishu_auto_sync: false,
              include_weixin_mp_in_sync: false,
            },
            summary: {
              enabled_platform_count: 1,
              enabled_platforms: ["bilibili"],
              authorized_platform_count: 0,
              setup_complete: true,
              feishu_enabled: false,
              feishu_ready: false,
              auto_sync_enabled: false,
            },
          },
        };
      },
    },
    async (baseUrl) => {
      await withPage(async (page) => {
        await page.goto(`${baseUrl}/monitor#session=live-token`, { waitUntil: "networkidle" });

        const bodyText = await page.locator("#platform-grid-container").innerText();
        assert(bodyText.includes("需要先授权 B 站账号"), "dashboard should explain that the platform needs authorization");
        assert(bodyText.includes("去授权"), "dashboard should surface authorization entrypoint for unauthorized platforms");
        assert(!bodyText.includes("单跑此源"), "dashboard should not offer a single-run action before authorization is complete");
      });
    }
  );
}

async function testSingleRunShowsBackendAuthErrorInsteadOfSuccessToast() {
  await withServer(
    {
      progress: buildProgress({
        enabledPlatforms: ["bilibili"],
        setupComplete: true,
        authOverrides: {
          bilibili: {
            auth_status: "unauthorized",
            auth_reason: "not_authorized",
            needs_auth: true,
            ui_status: "auth_required",
            message: "需要先授权 B 站账号",
          },
        },
      }),
      async config() {
        return {
          status: 200,
          body: {
            ok: true,
            config: {
              customer_name: "测试客户",
              workspace_name: "本地数据工作台",
              min_publish_date: "2026-01-01",
              browser_channel: "chrome",
              enabled_platforms: ["bilibili"],
              onboarding_completed: true,
              feishu_enabled: false,
              feishu_auto_sync: false,
              include_weixin_mp_in_sync: false,
            },
            summary: {
              enabled_platform_count: 1,
              enabled_platforms: ["bilibili"],
              authorized_platform_count: 0,
              setup_complete: true,
              feishu_enabled: false,
              feishu_ready: false,
              auto_sync_enabled: false,
            },
          },
        };
      },
      async post(_req, url) {
        if (url.pathname === "/run_bili") {
          return {
            status: 409,
            body: {
              ok: false,
              error: "auth_required",
              message: "B 站未授权，请先完成账号授权。",
            },
          };
        }
        return null;
      },
    },
    async (baseUrl) => {
      await withPage(async (page) => {
        await page.goto(`${baseUrl}/monitor#session=live-token`, { waitUntil: "networkidle" });
        await page.evaluate(() => {
          window.triggerRunSingle("bilibili");
        });
        await page.waitForTimeout(200);
        const toastText = await page.locator("#toast-container").innerText();
        assert(toastText.includes("B 站未授权，请先完成账号授权。"), "single-run should surface the backend auth_required message");
        assert(!toastText.includes("采集任务已提交"), "single-run should not show a success toast when the backend rejected the run");
      });
    }
  );
}

async function testActivationRetriesTransientVerifyLagBeforeShowingFailure() {
  let activationRequested = false;
  let postActivationChecks = 0;

  await withServer(
    {
      progress: buildProgress({ enabledPlatforms: [], setupComplete: false }),
      async license() {
        if (!activationRequested) {
          return {
            status: 200,
            body: {
              ok: true,
              activated: false,
              valid: false,
              customer_name: "",
              info: { error: "not_activated", message: "未激活，请先输入许可证" },
            },
          };
        }
        postActivationChecks += 1;
        if (postActivationChecks < 3) {
          return {
            status: 200,
            body: {
              ok: true,
              activated: true,
              valid: false,
              customer_name: "测试客户",
              info: { error: "not_activated", message: "此设备未激活" },
            },
          };
        }
        return {
          status: 200,
          body: {
            ok: true,
            activated: true,
            valid: true,
            customer_name: "测试客户",
            info: { status: "valid", customer_name: "测试客户" },
          },
        };
      },
      async post(_req, url) {
        if (url.pathname === "/license/activate") {
          activationRequested = true;
          return {
            status: 200,
            body: {
              ok: true,
              message: "激活成功！",
              customer_name: "测试客户",
              license_key: "YRG-TEST-1234",
            },
          };
        }
        return null;
      },
      async config() {
        return { status: 403, body: { ok: false, error: "license_invalid", license_error: "not_activated", message: "未激活，请先输入许可证" } };
      },
    },
    async (baseUrl) => {
      await withPage(async (page) => {
        await page.goto(`${baseUrl}/monitor#session=live-token`, { waitUntil: "networkidle" });
        await page.waitForSelector("#license-overlay:not(.hidden)", { timeout: 3000 });

        await page.fill("#license-key-input", "YRG-TEST-1234");
        await page.click("#btn-activate-license");

        await page.waitForFunction(() => {
          const overlay = document.getElementById("license-overlay");
          const text = document.getElementById("txt-license")?.innerText || "";
          return overlay?.classList.contains("hidden") && text.includes("测试客户");
        }, { timeout: 7000 });

        assert(postActivationChecks >= 3, "activation flow should retry /license verification until the transient lag clears");
        assert.strictEqual(
          await page.locator("#license-overlay").evaluate((node) => node.classList.contains("hidden")),
          true,
          "transient verify lag should not leave the license dialog stuck open"
        );
        assert.strictEqual(
          await page.locator("#license-error").evaluate((node) => getComputedStyle(node).display === "none"),
          true,
          "transient verify lag should not surface a persistent error banner"
        );
        assert.strictEqual(
          await page.locator("#license-success").evaluate((node) => getComputedStyle(node).display === "none"),
          true,
          "license success helper text should be cleared once the overlay is dismissed"
        );
      });
    }
  );
}

async function testTriggerAuthShowsAlreadyRunningMessage() {
  await withServer(
    {
      progress: buildProgress({ enabledPlatforms: ["weixin_channels"], setupComplete: true }),
      async config() {
        return {
          status: 200,
          body: {
            ok: true,
            config: {
              customer_name: "测试客户",
              workspace_name: "本地数据工作台",
              min_publish_date: "2026-01-01",
              browser_channel: "chrome",
              enabled_platforms: ["weixin_channels"],
              onboarding_completed: true,
              feishu_enabled: false,
              feishu_auto_sync: false,
              include_weixin_mp_in_sync: false,
            },
            summary: {
              enabled_platform_count: 1,
              enabled_platforms: ["weixin_channels"],
              authorized_platform_count: 0,
              setup_complete: true,
              feishu_enabled: false,
              feishu_ready: false,
              auto_sync_enabled: false,
            },
          },
        };
      },
      async post(_req, url) {
        if (url.pathname === "/auth_single") {
          return {
            status: 409,
            body: {
              ok: false,
              error: "already_running",
              message: "当前已有授权或采集任务在运行，请等待完成后再试。",
            },
          };
        }
        return null;
      },
    },
    async (baseUrl) => {
      await withPage(async (page) => {
        await page.goto(`${baseUrl}/monitor#session=live-token`, { waitUntil: "networkidle" });
        await page.evaluate(() => {
          window.triggerAuth("weixin_channels");
        });
        await page.waitForTimeout(200);
        const toastText = await page.locator("#toast-container").innerText();
        assert(toastText.includes("当前已有授权或采集任务在运行，请等待完成后再试。"), "auth trigger should surface the backend already_running message");
      });
    }
  );
}

async function testSettingsProvideFeishuSetupEntryWhenSkippedInitially() {
  await withServer(
    {
      progress: buildProgress({ enabledPlatforms: ["douyin"], setupComplete: true }),
      async config() {
        return {
          status: 200,
          body: {
            ok: true,
            config: {
              customer_name: "测试客户",
              workspace_name: "本地数据工作台",
              min_publish_date: "2026-01-01",
              browser_channel: "chrome",
              enabled_platforms: ["douyin"],
              onboarding_completed: true,
              feishu_enabled: false,
              feishu_auto_sync: false,
              include_weixin_mp_in_sync: false,
              feishu_credentials_saved: false,
              feishu_app_id_masked: "",
              feishu_app_token_masked: "",
            },
            summary: {
              enabled_platform_count: 1,
              enabled_platforms: ["douyin"],
              authorized_platform_count: 1,
              setup_complete: true,
              feishu_enabled: false,
              feishu_ready: false,
              auto_sync_enabled: false,
            },
          },
        };
      },
    },
    async (baseUrl) => {
      await withPage(async (page) => {
        await page.goto(`${baseUrl}/monitor#session=live-token`, { waitUntil: "networkidle" });

        await page.click("#btn-settings");
        await page.waitForSelector("#modal-settings:not(.hidden)", { timeout: 3000 });
        assert.strictEqual(
          await page.locator("#btn-open-feishu-setup").count(),
          1,
          "settings should expose a direct Feishu setup entry when Feishu was skipped initially"
        );

        await page.click("#btn-open-feishu-setup");
        await page.waitForSelector("#wizard-overlay:not(.hidden)", { timeout: 3000 });

        const wizardText = await page.locator("#wizard-overlay").innerText();
        assert(wizardText.includes("数据云端同步"), "clicking the settings Feishu entry should open the Feishu setup flow");
        assert(wizardText.includes("创建新表格"), "Feishu setup flow should present setup options from settings");
        assert.strictEqual(
          await page.locator("#modal-settings").evaluate((node) => node.classList.contains("hidden")),
          true,
          "settings modal should close before opening the Feishu setup flow"
        );
      });
    }
  );
}

async function testDashboardShowsFeishuImportingInsteadOfStartingEnvironmentFallback() {
  await withServer(
    {
      progress: {
        ...buildProgress({ enabledPlatforms: ["douyin"], setupComplete: true }),
        running: true,
        summary: {
          active_platform: "",
          current_stage: "importing",
          has_running_platform: false,
          completed_platforms: ["douyin"],
          zero_result_platforms: [],
          failed_platforms: [],
          needs_auth_platforms: [],
          enabled_platform_count: 1,
          authorized_platform_count: 1,
          setup_complete: true,
          feishu_enabled: true,
          feishu_ready: true,
          auto_sync_enabled: true,
          feishu: {
            enabled: true,
            ready: true,
            auto_sync_enabled: true,
            status: "running",
            message: "正在把 抖音 的最新本地结果同步到飞书。",
            current_platforms: ["douyin"],
            current_platform_labels: ["抖音"],
            last_sync_at: "",
            last_platforms: [],
            last_platform_labels: [],
            last_summary: "",
            last_error: "",
          },
        },
      },
      async config() {
        return {
          status: 200,
          body: {
            ok: true,
            config: {
              customer_name: "测试客户",
              workspace_name: "本地数据工作台",
              min_publish_date: "2026-01-01",
              browser_channel: "chrome",
              enabled_platforms: ["douyin"],
              onboarding_completed: true,
              feishu_enabled: true,
              feishu_auto_sync: true,
              include_weixin_mp_in_sync: false,
            },
            summary: {
              enabled_platform_count: 1,
              enabled_platforms: ["douyin"],
              authorized_platform_count: 1,
              setup_complete: true,
              feishu_enabled: true,
              feishu_ready: true,
              auto_sync_enabled: true,
            },
          },
        };
      },
    },
    async (baseUrl) => {
      await withPage(async (page) => {
        await page.goto(`${baseUrl}/monitor#session=live-token`, { waitUntil: "networkidle" });
        const titleText = await page.locator("#task-step-title").innerText();
        const descText = await page.locator("#task-step-desc").innerText();
        assert(titleText.includes("飞书同步"), "dashboard should identify Feishu importing explicitly");
        assert(descText.includes("同步到飞书"), "dashboard should show the runtime Feishu importing message");
        assert(!descText.includes("正在启动环境"), "dashboard should not fall back to the generic startup copy during Feishu importing");
      });
    }
  );
}

async function testSyncFeishuButtonSurfacesBackendAlreadyRunningMessage() {
  await withServer(
    {
      progress: buildProgress({ enabledPlatforms: ["douyin"], setupComplete: true }),
      async config() {
        return {
          status: 200,
          body: {
            ok: true,
            config: {
              customer_name: "测试客户",
              workspace_name: "本地数据工作台",
              min_publish_date: "2026-01-01",
              browser_channel: "chrome",
              enabled_platforms: ["douyin"],
              onboarding_completed: true,
              feishu_enabled: true,
              feishu_auto_sync: true,
              include_weixin_mp_in_sync: false,
            },
            summary: {
              enabled_platform_count: 1,
              enabled_platforms: ["douyin"],
              authorized_platform_count: 1,
              setup_complete: true,
              feishu_enabled: true,
              feishu_ready: true,
              auto_sync_enabled: true,
            },
          },
        };
      },
      async post(_req, url) {
        if (url.pathname === "/sync_feishu") {
          return {
            status: 409,
            body: {
              ok: false,
              error: "already_running",
              message: "飞书同步正在进行中，请稍候。",
            },
          };
        }
        return null;
      },
    },
    async (baseUrl) => {
      await withPage(async (page) => {
        await page.goto(`${baseUrl}/monitor#session=live-token`, { waitUntil: "networkidle" });
        await page.click("#btn-sync-feishu");
        await page.waitForTimeout(200);
        const toastText = await page.locator("#toast-container").innerText();
        assert(toastText.includes("飞书同步正在进行中，请稍候。"), "sync-feishu button should surface backend lock messages");
        assert(!toastText.includes("任务提交失败"), "sync-feishu button should not fall back to the generic failure toast");
      });
    }
  );
}

async function main() {
  await testMissingSessionShowsRecoveryOverlay();
  await testStoredSessionTokenRestoresConfigWithoutHash();
  await testStaleSessionRecoversDuringLicenseCheck();
  await testLicenseInvalidConfigLocksUiAndStopsBusinessPolling();
  await testUnauthorizedEnabledPlatformDoesNotOfferSingleRun();
  await testSingleRunShowsBackendAuthErrorInsteadOfSuccessToast();
  await testActivationRetriesTransientVerifyLagBeforeShowingFailure();
  await testTriggerAuthShowsAlreadyRunningMessage();
  await testSettingsProvideFeishuSetupEntryWhenSkippedInitially();
  await testDashboardShowsFeishuImportingInsteadOfStartingEnvironmentFallback();
  await testSyncFeishuButtonSurfacesBackendAlreadyRunningMessage();
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
