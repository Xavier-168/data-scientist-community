const assert = require('assert');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const HTML = fs.readFileSync(path.join(ROOT, 'frontend', 'progress.html'), 'utf8');
const exportRequests = [];

function platformProgress(platform, enabled) {
  return {
    platform,
    status: 'idle',
    phase: 'idle',
    message: '待机中',
    enabled,
    needs_auth: false,
    auth_status: 'authorized',
    auth_reason: '',
    auth_action: 'none',
    ui_status: 'idle',
    last_sync_at: null,
    totalWorks: 0,
    processedWorks: 0,
    successWorks: 0,
    skippedWorks: 0,
    failedWorks: 0,
  };
}

function json(res, payload, status = 200) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload));
}

async function main() {
  assert(!HTML.includes('const sel = prompt('), 'Excel export must not use JavaScript prompt');
  assert(!HTML.includes("window.open(withSessionQuery(withEnabledPlatformsQuery('/download-excel"), 'Excel export must not open an external browser');

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (url.pathname === '/monitor') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(HTML);
      return;
    }
    if (url.pathname.startsWith('/assets/')) {
      const target = path.join(ROOT, 'frontend', url.pathname);
      if (fs.existsSync(target)) {
        res.writeHead(200);
        res.end(fs.readFileSync(target));
      } else {
        res.writeHead(404);
        res.end();
      }
      return;
    }
    if (url.pathname === '/config') {
      json(res, {
        ok: true,
        config: {
          customer_name: '测试客户',
          workspace_name: '测试工作台',
          min_publish_date: '2026-01-01',
          enabled_platforms: ['douyin', 'xiaohongshu', 'bilibili'],
          onboarding_completed: true,
          feishu_enabled: false,
          feishu_auto_sync: false,
        },
        summary: {
          setup_complete: true,
          enabled_platform_count: 3,
          authorized_platform_count: 3,
          feishu_enabled: false,
          feishu_ready: false,
          auto_sync_enabled: false,
        },
      });
      return;
    }
    if (url.pathname === '/progress') {
      json(res, {
        ok: true,
        running: false,
        douyin: platformProgress('douyin', true),
        xiaohongshu: platformProgress('xiaohongshu', true),
        bilibili: platformProgress('bilibili', true),
        kuaishou: platformProgress('kuaishou', false),
        summary: {
          active_platform: '',
          current_stage: 'idle',
          has_running_platform: false,
          completed_platforms: [],
          zero_result_platforms: [],
          failed_platforms: [],
          enabled_platform_count: 3,
          authorized_platform_count: 3,
          setup_complete: true,
          feishu_enabled: false,
          feishu_ready: false,
          auto_sync_enabled: false,
        },
        serverTime: '2026-08-17T15:30:00+0800',
      });
      return;
    }
    if (url.pathname === '/analytics/history') {
      json(res, { ok: true, runs: [], total_count: 0 });
      return;
    }
    if (url.pathname === '/all-data') {
      json(res, { ok: true, rows: [], row_count: 0 });
      return;
    }
    if (url.pathname === '/license') {
      json(res, { ok: true, activated: true, valid: true, access_mode: 'license' });
      return;
    }
    if (url.pathname === '/package-info') {
      json(res, { ok: true, package_id: 'test', build_version: 'test' });
      return;
    }
    if (url.pathname === '/update/check' || url.pathname === '/update/download-progress') {
      json(res, { ok: true, update_available: false, status: 'idle' });
      return;
    }
    if (url.pathname === '/export-excel' && req.method === 'POST') {
      let body = '';
      req.on('data', (chunk) => { body += chunk; });
      req.on('end', () => {
        exportRequests.push({
          session: req.headers['x-yrg-session'] || '',
          body: JSON.parse(body || '{}'),
        });
        json(res, { ok: true, cancelled: true, message: '已取消保存' });
      });
      return;
    }
    json(res, { ok: true });
  });

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const browserExecutable = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
    || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  assert(fs.existsSync(browserExecutable), 'verified local Chromium executable is missing');
  const browser = await chromium.launch({ headless: true, executablePath: browserExecutable });
  const page = await browser.newPage();
  await page.addInitScript(() => {
    window.prompt = () => {
      throw new Error('JavaScript prompt must not be used');
    };
    window.__nativeExcelExports = [];
    Object.defineProperty(window, 'webkit', {
      configurable: true,
      value: {
        messageHandlers: {
          excelExport: {
            postMessage(payload) {
              window.__nativeExcelExports.push(payload);
            },
          },
        },
      },
    });
  });

  try {
    const baseURL = 'http://127.0.0.1:' + server.address().port;
    await page.goto(baseURL + '/monitor#session=live-token');
    await page.waitForFunction(() => {
      const button = document.getElementById('btn-download-excel');
      return button && !button.disabled;
    });

    await page.evaluate(() => document.getElementById('btn-download-excel').click());
    await page.waitForSelector('#modal-excel-export:not(.hidden)');
    if (process.env.EXCEL_EXPORT_SCREENSHOT) {
      await page.screenshot({ path: process.env.EXCEL_EXPORT_SCREENSHOT, fullPage: true });
    }
    const choiceLabels = await page.locator('[data-excel-export-key]').allTextContents();
    assert.deepStrictEqual(
      choiceLabels.map((text) => text.replace(/\s+/g, ' ').trim()),
      [
        '全平台汇总汇总当前启用的 3 个平台',
        '抖音仅导出该平台数据',
        '小红书仅导出该平台数据',
        'B站仅导出该平台数据',
      ],
    );

    await page.locator('[data-excel-export-key="bilibili"]').click();
    await page.locator('#modal-excel-export-confirm').click();
    await page.waitForFunction(() => window.__nativeExcelExports.length === 1);
    const nativePayload = await page.evaluate(() => window.__nativeExcelExports[0]);
    assert.deepStrictEqual(nativePayload, {
      file: 'bilibili',
      platforms: [],
      label: 'B站',
      suggestedFilename: 'B站数据.xlsx',
    });

    await page.evaluate(() => {
      window.handleNativeExcelExportResult({
        ok: true,
        path: '/tmp/用户选择/B站数据.xlsx',
        file_name: 'B站数据.xlsx',
      });
    });
    assert.strictEqual(
      await page.locator('#excel-export-status').textContent(),
      'Excel 已保存完成。',
    );
    assert.strictEqual(
      await page.locator('#excel-export-path').textContent(),
      '/tmp/用户选择/B站数据.xlsx',
    );

    await page.evaluate(() => {
      delete window.webkit;
      document.getElementById('modal-excel-export-close').click();
      document.getElementById('btn-download-excel').click();
    });
    await page.locator('#modal-excel-export-confirm').click();
    await page.waitForFunction(() => {
      return document.getElementById('excel-export-status').textContent.includes('已取消保存');
    });
    assert.strictEqual(exportRequests.length, 1);
    assert.strictEqual(exportRequests[0].session, 'live-token');
    assert.deepStrictEqual(exportRequests[0].body, {
      file: 'all',
      platforms: ['douyin', 'xiaohongshu', 'bilibili'],
    });

    console.log('excel export flow: PASS');
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
