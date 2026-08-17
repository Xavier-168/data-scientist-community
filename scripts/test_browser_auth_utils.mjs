import assert from 'node:assert/strict';
import {
  activateBrowserApp,
  isPlaceholderBrowserUrl,
  isTransientNavigationError,
  navigateAuthCandidates,
  prepareAuthPage,
  resolveBrowserAppName,
} from './browser_auth_utils.mjs';
import {
  resolveDownloadsDir,
  resolveProfileDir,
  resolveStateDir,
} from './runtime_paths.mjs';

await (async function testRuntimePathsKeepMacStateOutsideSourceCheckout() {
  const projectRoot = '/tmp/community-source';
  const options = {
    env: {},
    platform: 'darwin',
    homeDir: '/tmp/community-home',
    projectRoot,
    communityEdition: true,
  };
  const stateDir = resolveStateDir(options);
  assert.equal(
    stateDir,
    '/tmp/community-home/Library/Application Support/数据科学家 Community',
  );
  assert.notEqual(stateDir, projectRoot);
  assert.equal(resolveDownloadsDir(options), `${stateDir}/downloads`);
  assert.equal(
    resolveProfileDir('douyin', 'chrome', options),
    `${stateDir}/.auth/profiles/douyin-chrome`,
  );
  assert.equal(
    resolveStateDir({ ...options, env: { YIRENGONGIS_STATE_DIR: '/tmp/custom-state' } }),
    '/tmp/custom-state',
  );
})();

function makePage(url = 'about:blank', behavior = {}) {
  let currentUrl = url;
  let closed = false;
  let bringToFrontCount = 0;
  let gotoCalls = [];
  return {
    url: () => currentUrl,
    isClosed: () => closed,
    async bringToFront() {
      bringToFrontCount += 1;
    },
    async close() {
      closed = true;
    },
    async goto(targetUrl) {
      gotoCalls.push(targetUrl);
      if (behavior.gotoThrowsFor?.includes?.(targetUrl)) {
        throw new Error(`boom:${targetUrl}`);
      }
      currentUrl = behavior.gotoUrlMap?.[targetUrl] ?? targetUrl;
    },
    async waitForTimeout() {},
    get __meta() {
      return { bringToFrontCount, gotoCalls, currentUrl, closed };
    },
  };
}

function makeContext(pages, newPages = []) {
  const allPages = [...pages];
  return {
    pages: () => allPages.filter((item) => !item.isClosed()),
    async newPage() {
      const page = newPages.shift() || makePage();
      allPages.push(page);
      return page;
    },
  };
}

await (async function testPlaceholderDetection() {
  assert.equal(isPlaceholderBrowserUrl('about:blank'), true);
  assert.equal(isPlaceholderBrowserUrl('chrome://new-tab-page/'), true);
  assert.equal(isPlaceholderBrowserUrl('https://example.com'), false);
})();

await (async function testResolveBrowserAppNamePrefersExecutableBundleName() {
  assert.equal(
    resolveBrowserAppName({
      BROWSER_EXECUTABLE_PATH: '/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
      BROWSER_CHANNEL: 'chrome',
    }),
    'Google Chrome for Testing',
  );
  assert.equal(resolveBrowserAppName({ BROWSER_CHANNEL: 'chromium' }), 'Chromium');
})();

await (async function testPrepareAuthPageCreatesFreshPageFromBlankTab() {
  const blank = makePage('about:blank');
  const fresh = makePage('about:blank');
  const context = makeContext([blank], [fresh]);
  const page = await prepareAuthPage(context, blank);
  assert.equal(page, fresh);
  assert.equal(blank.__meta.closed, true);
})();

await (async function testNavigateCandidatesRetriesWithFreshPage() {
  const blank = makePage('about:blank');
  const firstAttempt = makePage('about:blank', {
    gotoUrlMap: {
      'https://bad.example': 'about:blank',
    },
  });
  const retry = makePage('about:blank', {
    gotoUrlMap: {
      'https://good.example': 'https://good.example/login',
    },
  });
  const context = makeContext([blank], [firstAttempt, retry]);
  const page = await navigateAuthCandidates(context, blank, ['https://bad.example', 'https://good.example'], { settleMs: 0 });
  assert.equal(page, retry);
  assert.deepEqual(blank.__meta.gotoCalls, []);
  assert.deepEqual(firstAttempt.__meta.gotoCalls, ['https://bad.example']);
  assert.deepEqual(retry.__meta.gotoCalls, ['https://good.example']);
  assert.equal(retry.url(), 'https://good.example/login');
})();

await (async function testNavigateSameUrlRetriesOnlyTransientFailures() {
  const blank = makePage('about:blank');
  const failedAttempt = makePage('about:blank', {
    gotoThrowsFor: ['https://creator.example/manage'],
  });
  const recoveredAttempt = makePage('about:blank');
  const context = makeContext([blank], [failedAttempt, recoveredAttempt]);
  const slept = [];
  const page = await navigateAuthCandidates(
    context,
    blank,
    ['https://creator.example/manage'],
    {
      settleMs: 0,
      sameUrlAttempts: 3,
      retryDelaysMs: [3000, 8000],
      sleep: async (delayMs) => slept.push(delayMs),
      shouldRetry: () => true,
    },
  );
  assert.equal(page, recoveredAttempt);
  assert.deepEqual(failedAttempt.__meta.gotoCalls, ['https://creator.example/manage']);
  assert.deepEqual(recoveredAttempt.__meta.gotoCalls, ['https://creator.example/manage']);
  assert.equal(failedAttempt.__meta.closed, true);
  assert.deepEqual(slept, [3000]);
})();

await (async function testTransientNavigationClassifierIsNarrow() {
  assert.equal(
    isTransientNavigationError(new Error('page.goto: net::ERR_CONNECTION_CLOSED')),
    true,
  );
  assert.equal(
    isTransientNavigationError(new Error('page.goto: Timeout 30000ms exceeded.')),
    true,
  );
  assert.equal(isTransientNavigationError(new Error('登录状态失效')), false);
})();

await (async function testPrepareAuthPageActivatesVisibleBrowserOnMac() {
  const blank = makePage('about:blank');
  const fresh = makePage('about:blank');
  const context = makeContext([blank], [fresh]);
  const activationCalls = [];
  const page = await prepareAuthPage(context, blank, {
    browserActivation: {
      platform: 'darwin',
      env: {
        HEADLESS: 'false',
        BROWSER_EXECUTABLE_PATH: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      },
      execFile: async (command, args, options) => {
        activationCalls.push({ command, args, options });
        return { stdout: '', stderr: '' };
      },
    },
  });
  assert.equal(page, fresh);
  assert.equal(activationCalls.length, 1);
  assert.equal(activationCalls[0].command, 'osascript');
  assert.match(activationCalls[0].args[1], /Google Chrome/);
})();

await (async function testActivateBrowserAppSkipsHeadlessRuns() {
  let called = false;
  const activated = await activateBrowserApp({
    platform: 'darwin',
    env: {
      HEADLESS: 'true',
      BROWSER_CHANNEL: 'chrome',
    },
    execFile: async () => {
      called = true;
      return { stdout: '', stderr: '' };
    },
  });
  assert.equal(activated, false);
  assert.equal(called, false);
})();
