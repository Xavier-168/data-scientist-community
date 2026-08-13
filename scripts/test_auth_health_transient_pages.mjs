import assert from 'node:assert/strict';
import http from 'node:http';

import { chromium } from 'playwright';
import { classifyBilibiliAuthPage } from './bilibili_export.mjs';
import { classifyDouyinAuthPage } from './douyin_export.mjs';
import { classifyXiaohongshuAuthPage } from './xiaohongshu_export.mjs';
import { classifyKuaishouAuthPage } from './kuaishou_export.mjs';

const PLATFORM_FIXTURES = [
  {
    id: 'douyin',
    dashboardMarker: '内容管理',
    classify: classifyDouyinAuthPage,
  },
  {
    id: 'xiaohongshu',
    dashboardMarker: '笔记管理',
    classify: classifyXiaohongshuAuthPage,
  },
  {
    id: 'kuaishou',
    dashboardMarker: '数据中心',
    classify: classifyKuaishouAuthPage,
  },
  {
    id: 'bilibili',
    dashboardMarker: '数据概览',
    classify: classifyBilibiliAuthPage,
  },
];

function html(body) {
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>fixture</title></head><body>${body}</body></html>`;
}

function startFixtureServer() {
  const server = http.createServer((request, response) => {
    const url = new URL(request.url || '/', 'http://127.0.0.1');
    response.setHeader('Content-Type', 'text/html; charset=utf-8');

    if (url.pathname === '/503') {
      response.statusCode = 503;
      response.end(html('<main><h1>Service temporarily unavailable</h1></main>'));
      return;
    }
    if (url.pathname === '/blank') {
      response.statusCode = 200;
      response.end(html(''));
      return;
    }
    if (url.pathname === '/hidden-auth-screen') {
      response.statusCode = 200;
      response.end(html('<button style="display:none">扫码登录</button>'));
      return;
    }
    if (url.pathname === '/auth-screen') {
      response.statusCode = 200;
      response.end(html('<main><h1>账号验证</h1><button id="login-marker" type="button">扫码登录</button></main>'));
      return;
    }
    if (url.pathname === '/login') {
      response.statusCode = 200;
      response.end(html('<main><h1>账号验证</h1><button type="button">扫码登录</button></main>'));
      return;
    }
    if (url.pathname === '/bilibili-logout-only') {
      response.statusCode = 200;
      response.end(html('<nav><button type="button">退出登录</button></nav>'));
      return;
    }
    if (url.pathname.startsWith('/authorized/')) {
      const platform = PLATFORM_FIXTURES.find((item) => url.pathname === `/authorized/${item.id}`);
      if (platform) {
        response.statusCode = 200;
        response.end(platform.id === 'bilibili'
          ? html('<iframe title="B 站数据中心" src="/york/data-center-web"></iframe>')
          : html(`<nav><span id="dashboard-marker">${platform.dashboardMarker}</span></nav>`));
        return;
      }
    }
    if (url.pathname === '/york/data-center-web') {
      response.statusCode = 200;
      response.end(html('<main><h1>数据概览</h1><section>稿件分析</section></main>'));
      return;
    }

    response.statusCode = 404;
    response.end(html('<main><h1>Not found</h1></main>'));
  });

  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      assert(address && typeof address === 'object');
      resolve({ server, baseUrl: `http://127.0.0.1:${address.port}` });
    });
  });
}

async function closeServer(server) {
  await new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

async function launchProductionChrome() {
  const executablePath = String(process.env.BROWSER_EXECUTABLE_PATH || '').trim();
  if (executablePath) {
    return chromium.launch({ headless: true, executablePath });
  }
  const browserChannel = String(process.env.BROWSER_CHANNEL || '').trim();
  if (browserChannel && browserChannel !== 'chromium') {
    return chromium.launch({ headless: true, channel: browserChannel });
  }
  return chromium.launch({ headless: true });
}

function removeMarkerOnFirstStabilityWait(page, selector) {
  let removed = false;
  return new Proxy(page, {
    get(target, property) {
      if (property === 'waitForTimeout') {
        return async (timeoutMs) => {
          if (!removed) {
            removed = true;
            await target.locator(selector).evaluate((element) => element.remove());
          }
          await target.waitForTimeout(timeoutMs);
        };
      }
      const value = Reflect.get(target, property, target);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
}

const { server, baseUrl } = await startFixtureServer();
let browser;

try {
  browser = await launchProductionChrome();
  const page = await browser.newPage();

  for (const fixture of PLATFORM_FIXTURES) {
    const cases = [
      { path: '/503', status: 503, expected: 'transient' },
      { path: '/blank', status: 200, expected: 'transient' },
      { path: '/hidden-auth-screen', status: 200, expected: 'transient' },
      { path: '/login', status: 200, expected: 'login_required' },
      { path: '/auth-screen', status: 200, expected: 'login_required' },
      { path: `/authorized/${fixture.id}`, status: 200, expected: 'authorized' },
    ];

    for (const testCase of cases) {
      const response = await page.goto(`${baseUrl}${testCase.path}`, {
        waitUntil: 'domcontentloaded',
        timeout: 10000,
      });
      assert(response, `${fixture.id} ${testCase.path} should return an HTTP response`);
      assert.equal(response.status(), testCase.status, `${fixture.id} ${testCase.path} HTTP status`);

      const actual = await fixture.classify(page, { stableChecks: 2, settleMs: 50 });
      assert.equal(actual, testCase.expected, `${fixture.id} ${testCase.path} classification`);
    }

    await page.goto(`${baseUrl}/auth-screen`, { waitUntil: 'domcontentloaded', timeout: 10000 });
    const unstableLoginPage = removeMarkerOnFirstStabilityWait(page, '#login-marker');
    assert.equal(
      await fixture.classify(unstableLoginPage, { stableChecks: 2, settleMs: 10 }),
      'transient',
      `${fixture.id} must not expire a cookie from one unstable login-marker observation`,
    );

    if (fixture.id === 'douyin') {
      await page.goto(`${baseUrl}/authorized/douyin`, { waitUntil: 'domcontentloaded', timeout: 10000 });
      const unstableDashboardPage = removeMarkerOnFirstStabilityWait(page, '#dashboard-marker');
      assert.equal(
        await fixture.classify(unstableDashboardPage, { stableChecks: 2, settleMs: 10 }),
        'transient',
        'douyin must keep dashboard evidence visible for two checks',
      );
      console.log('[auth-health-browser] douyin: unstable-dashboard=transient');
    }
    if (fixture.id === 'bilibili') {
      await page.goto(`${baseUrl}/bilibili-logout-only`, { waitUntil: 'domcontentloaded', timeout: 10000 });
      assert.equal(
        await fixture.classify(page, { stableChecks: 2, settleMs: 10 }),
        'transient',
        'bilibili must not treat 退出登录 as a login prompt',
      );
    }
    console.log(`[auth-health-browser] ${fixture.id}: 503/blank/hidden/unstable-login=transient, visible-login=login_required, dashboard=authorized`);
  }

  console.log('AUTH_HEALTH_TRANSIENT_PAGES_OK platforms=4 browser=playwright');
} finally {
  if (browser) await browser.close();
  await closeServer(server);
}
