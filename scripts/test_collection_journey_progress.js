const assert = require('assert');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const HTML_PATH = path.join(ROOT, 'frontend', 'progress.html');
const CSS_PATH = path.join(ROOT, 'frontend', 'assets', 'progress-apple-theme.css');
const FIGMA_CSS_PATH = path.join(ROOT, 'frontend', 'assets', 'progress-figma-dashboard.css');
const WALK_ICON_PATH = path.join(ROOT, 'frontend', 'assets', 'vendor', 'tabler', 'walk.svg');
const MAILBOX_ICON_PATH = path.join(ROOT, 'frontend', 'assets', 'figma', 'mailbox.svg');
const FIGMA_FONT_PATH = path.join(ROOT, 'frontend', 'assets', 'fonts', 'NotoSerifSC-Variable.ttf');
const html = fs.readFileSync(HTML_PATH, 'utf8');
const css = fs.readFileSync(CSS_PATH, 'utf8');
const figmaCss = fs.readFileSync(FIGMA_CSS_PATH, 'utf8');
const walkIcon = fs.readFileSync(WALK_ICON_PATH, 'utf8');
const mailboxIcon = fs.readFileSync(MAILBOX_ICON_PATH, 'utf8');
const figmaFont = fs.readFileSync(FIGMA_FONT_PATH);
const platformIcons = Object.fromEntries(
  ['douyin', 'bilibili', 'kuaishou'].map((name) => [
    `/assets/platforms/${name}.svg`,
    fs.readFileSync(path.join(ROOT, 'frontend', 'assets', 'platforms', `${name}.svg`), 'utf8'),
  ]),
);
let chromium;
try {
  ({ chromium } = require('playwright'));
} catch (error) {
  const sharedPlaywright = path.resolve(ROOT, '..', '..', 'node_modules', 'playwright');
  if (!fs.existsSync(sharedPlaywright)) throw error;
  ({ chromium } = require(sharedPlaywright));
}

function extractJourneyCalculator() {
  const start = html.indexOf('function clampNumber(');
  const end = html.indexOf('function renderCollectionJourney(', start);
  assert(start >= 0 && end > start, 'collection journey calculator should remain available');
  const source = html.slice(start, end);
  return vm.runInNewContext(`${source}\ncalculateCollectionJourneyProgress`, {
    Array,
    Math,
    Number,
    PLATFORMS: ['douyin', 'xiaohongshu', 'bilibili', 'kuaishou'],
    LABELS: {
      douyin: '抖音',
      xiaohongshu: '小红书',
      bilibili: 'B站',
      kuaishou: '快手',
    },
    Set,
    String,
  });
}

function buildRunningProgress() {
  const idle = (platform, enabled = false) => ({
    platform,
    enabled,
    status: 'idle',
    ui_status: 'idle',
    auth_status: enabled ? 'authorized' : 'unauthorized',
    message: '待机中',
    totalWorks: 0,
    processedWorks: 0,
    successWorks: 0,
    skippedWorks: 0,
    failedWorks: 0,
  });

  return {
    ok: true,
    running: true,
    douyin: {
      ...idle('douyin', true),
      status: 'running',
      ui_status: 'running',
      message: '正在读取作品数据，已处理 15 / 30 条',
      totalWorks: 30,
      processedWorks: 15,
      successWorks: 13,
      skippedWorks: 2,
    },
    xiaohongshu: {
      ...idle('xiaohongshu', true),
      status: 'completed',
      ui_status: 'completed',
      message: '小红书任务完成，共 27 条',
      totalWorks: 27,
      processedWorks: 27,
      successWorks: 27,
    },
    bilibili: idle('bilibili'),
    kuaishou: idle('kuaishou'),
    summary: {
      active_platform: 'douyin',
      current_stage: 'scraping',
      completed_platforms: ['xiaohongshu'],
      zero_result_platforms: [],
      failed_platforms: [],
      enabled_platform_count: 2,
      enabled_platforms: ['douyin', 'xiaohongshu'],
      authorized_platform_count: 2,
      authorized_platforms: ['douyin', 'xiaohongshu'],
      setup_complete: true,
      onboarding_completed: true,
      has_run_history: true,
      feishu_enabled: false,
      feishu_ready: false,
      auto_sync_enabled: false,
      feishu: { enabled: false, status: 'disabled', message: '' },
    },
  };
}

function resolveChromiumExecutable() {
  const home = os.homedir();
  const candidates = [
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    path.join(home, 'Library', 'Caches', 'ms-playwright', 'chromium_headless_shell-1234', 'chrome-headless-shell-mac-arm64', 'chrome-headless-shell'),
    path.join(home, 'Library', 'Caches', 'ms-playwright', 'chromium-1234', 'chrome-mac-arm64', 'Google Chrome for Testing.app', 'Contents', 'MacOS', 'Google Chrome for Testing'),
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ];
  return candidates.find((candidate) => candidate && fs.existsSync(candidate));
}

function verifyCalculation() {
  const calculate = extractJourneyCalculator();
  const progress = buildRunningProgress();
  const journey = calculate(progress, ['douyin', 'xiaohongshu']);

  assert.strictEqual(journey.percent, 70.5, 'internal progress should preserve the Figma-aligned fractional position');
  assert.strictEqual(journey.completedCount, 1);
  assert.strictEqual(journey.totalCount, 2);
  assert.strictEqual(journey.stageLabel, '正在采集 抖音');

  const starting = buildRunningProgress();
  starting.douyin.totalWorks = 0;
  starting.douyin.processedWorks = 0;
  starting.douyin.successWorks = 0;
  starting.douyin.skippedWorks = 0;
  starting.xiaohongshu.status = 'idle';
  starting.xiaohongshu.ui_status = 'idle';
  starting.xiaohongshu.totalWorks = 0;
  starting.xiaohongshu.processedWorks = 0;
  starting.xiaohongshu.successWorks = 0;
  starting.summary.completed_platforms = [];
  assert.strictEqual(
    calculate(starting, ['douyin', 'xiaohongshu']).percent,
    0,
    'a new collection should start at a factual 0%',
  );

  const authorizing = calculate({
    ...starting,
    summary: {
      ...starting.summary,
      current_stage: 'authorizing',
    },
  }, ['douyin', 'xiaohongshu']);
  assert.strictEqual(authorizing.percent, 0, 'authorization setup should not fabricate collection progress');

  const importing = calculate({
    ...progress,
    summary: {
      ...progress.summary,
      active_platform: '',
      current_stage: 'importing',
      feishu: { status: 'idle' },
    },
  }, ['douyin', 'xiaohongshu']);
  assert.strictEqual(importing.percent, 96, 'data consolidation should reserve the final visible segment');
  assert.strictEqual(importing.stageLabel, '正在整理数据并生成 Excel');
}

function verifyStaticContract() {
  for (const id of [
    'dash-task-pane',
    'task-journey',
    'task-walker',
    'task-route-fill',
    'task-progress-percent',
    'task-progress-meta',
    'task-progress-stage',
  ]) {
    assert(html.includes(`id="${id}"`), `progress page should expose #${id}`);
  }
  assert(html.includes('role="progressbar"'), 'journey should expose an accessible progressbar role');
  assert(html.includes('LIVE COLLECTION'), 'running card should use the approved Figma kicker');
  assert(html.includes('正在采集平台数据'), 'running card should use the approved Figma title');
  assert(html.includes('task-progress-label">进度'), 'running card should use the approved Chinese progress label');
  assert(html.includes('id="task-baseline-range"'), 'running card should expose the synchronization baseline');
  assert(html.includes('id="task-current-platform"'), 'running card should expose the current platform');
  assert(html.includes('class="platform-table-shell"'), 'dashboard should use the approved Figma table projection');
  assert(figmaCss.includes('--figma-shadow: 1px 1px 1px'), 'small dashboard components should use the global 1/1/1 shadow rule');
  assert(figmaCss.includes('@keyframes collectionStatusFlow'), 'running collection status should keep its internal motion');
  assert(html.includes('/assets/vendor/tabler/walk.svg'), 'walker should use the vendored Tabler walk icon');
  assert(!html.includes('task-walker-figure'), 'walker should not use a handcrafted inline SVG');
  assert(css.includes('@keyframes taskWalkerStep'), 'walker should include a restrained step animation');
  assert(css.includes('@media (prefers-reduced-motion: reduce)'), 'walker should respect reduced-motion preferences');
  assert(html.includes('COLLECTION_JOURNEY_ANIMATION_MS = 2800'), 'journey should interpolate across the backend polling window');
  assert(html.includes('elapsed / COLLECTION_JOURNEY_ANIMATION_MS'), 'journey interpolation should use a linear time ratio');
  assert(html.includes('DASHBOARD_DESIGN_WIDTH = 1920'), 'dashboard should retain the approved Figma desktop width as its scale baseline');
  assert(html.includes('DASHBOARD_DESIGN_HEIGHT = 1080'), 'dashboard should retain the approved Figma desktop height as its scale baseline');
  assert(html.includes('id="viewport-stage"'), 'dashboard should use a dedicated letterbox stage');
  assert(html.includes('/assets/figma/mailbox.svg'), 'sidebar should use the exact editable Figma mailbox asset');
  assert(figmaCss.includes('fonts/NotoSerifSC-Variable.ttf'), 'Figma projection should use the vendored OFL Noto Serif SC file');
  assert(html.includes('applyDashboardViewportScale();'), 'dashboard should scale the full design canvas before rendering');
  assert(!figmaCss.includes('@media (max-width: 1500px)'), 'narrow native windows must not trigger a hybrid partial redesign');
  assert(!html.includes('progressbar.js'), 'the production page should not add an external progress dependency');

  const componentCss = css.slice(css.indexOf('/* Task Current Status Pane */'), css.indexOf('/* Product Mailbox */'));
  assert(!componentCss.includes('var(--green)'), 'progress component should not use the green theme token');
  assert(!componentCss.includes('16, 185, 129'), 'progress component should not retain the old green tint');
  assert(!componentCss.includes('cubic-bezier'), 'progress position should not use eased movement');
}

async function verifyBrowserRender() {
  const progress = buildRunningProgress();
  const config = {
    customer_name: 'Xavier 本机长期测试',
    workspace_name: '本地数据工作台',
    min_publish_date: '2026-04-01',
    browser_channel: 'chromium',
    enabled_platforms: ['douyin', 'xiaohongshu'],
    onboarding_completed: true,
    feishu_enabled: false,
    feishu_auto_sync: false,
  };

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const sendJson = (payload, status = 200) => {
      res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify(payload));
    };

    if (url.pathname === '/monitor') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(html);
      return;
    }
    if (url.pathname === '/assets/progress-apple-theme.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
      res.end(css);
      return;
    }
    if (url.pathname === '/assets/progress-figma-dashboard.css') {
      res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' });
      res.end(figmaCss);
      return;
    }
    if (url.pathname === '/assets/vendor/tabler/walk.svg') {
      res.writeHead(200, { 'Content-Type': 'image/svg+xml; charset=utf-8' });
      res.end(walkIcon);
      return;
    }
    if (url.pathname === '/assets/figma/mailbox.svg') {
      res.writeHead(200, { 'Content-Type': 'image/svg+xml; charset=utf-8' });
      res.end(mailboxIcon);
      return;
    }
    if (url.pathname === '/assets/fonts/NotoSerifSC-Variable.ttf') {
      res.writeHead(200, { 'Content-Type': 'font/ttf' });
      res.end(figmaFont);
      return;
    }
    if (platformIcons[url.pathname]) {
      res.writeHead(200, { 'Content-Type': 'image/svg+xml; charset=utf-8' });
      res.end(platformIcons[url.pathname]);
      return;
    }
    if (url.pathname === '/license') return sendJson({ valid: true, activated: true, customer_name: config.customer_name });
    if (url.pathname === '/progress') return sendJson(progress);
    if (url.pathname === '/config') return sendJson({ ok: true, config, summary: progress.summary });
    if (url.pathname === '/all-data') return sendJson({ ok: true, rows: [], row_count: 54 });
    if (url.pathname === '/analytics/history') return sendJson({ ok: true, runs: [{ run_id: 1 }, { run_id: 2 }] });
    if (url.pathname === '/package-info') return sendJson({ ok: true, build_version: '20260802' });
    if (url.pathname === '/update/check') return sendJson({ ok: true, update_available: false });
    return sendJson({ ok: false, error: 'not_found' }, 404);
  });

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const executablePath = resolveChromiumExecutable();
  const browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  });
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/monitor#session=test-session`, { waitUntil: 'networkidle' });
    await page.locator('#dash-task-pane.active.is-running').waitFor();
    const resetPercent = await page.evaluate((runningProgress) => {
      resetCollectionJourneyVisualProgress();
      renderCollectionJourney(runningProgress, ['douyin', 'xiaohongshu']);
      return document.querySelector('#task-progress-percent')?.textContent;
    }, progress);
    assert.strictEqual(resetPercent, '0', 'the first rendered frame of a new collection should be 0%');
    await page.waitForFunction(() => Number(document.querySelector('#task-progress-percent')?.textContent) >= 3, null, { timeout: 2000 });
    const firstAnimatedPercent = Number(await page.locator('#task-progress-percent').innerText());
    await page.waitForFunction(
      (previous) => Number(document.querySelector('#task-progress-percent')?.textContent) >= previous + 3,
      firstAnimatedPercent,
      { timeout: 2000 },
    );
    const secondAnimatedPercent = Number(await page.locator('#task-progress-percent').innerText());
    assert(secondAnimatedPercent > firstAnimatedPercent, 'progress should keep moving between backend polls');
    await page.waitForFunction(() => document.querySelector('#task-progress-percent')?.textContent === '71', null, { timeout: 4000 });
    assert.strictEqual(await page.locator('#task-progress-percent').innerText(), '71');
    assert.strictEqual(await page.locator('#task-journey').getAttribute('aria-valuenow'), '71');
    assert((await page.locator('#task-progress-meta').innerText()).includes('1 / 2'));
    assert((await page.locator('#task-progress-stage').innerText()).includes('抖音'));

    const positions = await page.evaluate(() => {
      const pane = document.querySelector('#dash-task-pane').getBoundingClientRect();
      const track = document.querySelector('.task-route-track').getBoundingClientRect();
      const fill = document.querySelector('#task-route-fill').getBoundingClientRect();
      const baseline = document.querySelector('.task-baseline-block').getBoundingClientRect();
      const current = document.querySelector('.task-current-block').getBoundingClientRect();
      const score = document.querySelector('.task-progress-score').getBoundingClientRect();
      const table = document.querySelector('.platform-table-shell').getBoundingClientRect();
      const tableHead = document.querySelector('.platform-table-head').getBoundingClientRect();
      const firstRow = document.querySelector('.platform-table-row').getBoundingClientRect();
      const appStyle = getComputedStyle(document.querySelector('#app'));
      const paneStyle = getComputedStyle(document.querySelector('#dash-task-pane'));
      const walkerStyle = getComputedStyle(document.querySelector('#task-walker'));
      const trackStyle = getComputedStyle(document.querySelector('.task-route-track'), '::before');
      const fillStyle = getComputedStyle(document.querySelector('#task-route-fill'));
      const scoreStyle = getComputedStyle(document.querySelector('.task-progress-score'));
      return {
        innerWidth: window.innerWidth,
        sidebarWidth: appStyle.getPropertyValue('--sidebar-width').trim(),
        paneHeight: pane.height,
        paneLeft: pane.left,
        paneWidth: pane.width,
        paneBackground: paneStyle.backgroundColor,
        paneBorderColor: paneStyle.borderTopColor,
        paneRadius: paneStyle.borderTopLeftRadius,
        baselineWidth: baseline.width,
        currentWidth: current.width,
        trackLeft: track.left,
        trackWidth: track.width,
        fillRatio: fill.width / track.width,
        walkerDisplay: walkerStyle.display,
        trackHeight: track.height,
        trackColor: trackStyle.backgroundColor,
        trackLineHeight: trackStyle.height,
        fillColor: fillStyle.backgroundColor,
        fillOpacity: fillStyle.opacity,
        scoreBackground: scoreStyle.backgroundColor,
        scoreWidth: score.width,
        scoreHeight: score.height,
        tableLeft: table.left,
        tableWidth: table.width,
        tableHeight: table.height,
        tableHeadHeight: tableHead.height,
        firstRowHeight: firstRow.height,
      };
    });
    if (process.env.DEBUG_LAYOUT) process.stdout.write(`${JSON.stringify(positions, null, 2)}\n`);
    assert.strictEqual(positions.paneHeight, 149, 'running card should preserve the approved 149px Figma height');
    assert.strictEqual(positions.innerWidth, 1920, 'visual regression should run at the Figma desktop width');
    assert.strictEqual(positions.sidebarWidth, '304px', 'desktop layout should preserve the 304px Figma sidebar');
    assert.strictEqual(positions.paneLeft, 352, 'running card should align to the 48px main-content gutter');
    assert.strictEqual(positions.paneWidth, 1520, 'running card should span the approved 1520px content width');
    assert.strictEqual(positions.paneBackground, 'rgb(255, 255, 255)', 'card background should match Figma white');
    assert.strictEqual(positions.paneBorderColor, 'rgb(230, 230, 230)', 'card border should match the Figma divider color');
    assert.strictEqual(positions.paneRadius, '14px', 'card radius should match Figma 14px');
    assert.strictEqual(positions.baselineWidth, 390, 'baseline segment should preserve its Figma width');
    assert.strictEqual(positions.currentWidth, 355, 'current-platform segment should preserve its Figma width');
    assert.strictEqual(positions.walkerDisplay, 'none', 'the final Figma projection should use a simple monochrome bar without the old walker');
    assert.strictEqual(positions.trackHeight, 11, 'route should use the approved 11px bar');
    assert.strictEqual(positions.trackLineHeight, '11px', 'visible track should use the approved 11px height');
    assert.strictEqual(positions.trackColor, 'rgb(236, 236, 236)', 'track should match Figma #ececec');
    assert.strictEqual(positions.fillColor, 'rgb(17, 17, 17)', 'fill should match Figma #111');
    assert.strictEqual(positions.fillOpacity, '1', 'progress fill should use solid black');
    assert.strictEqual(positions.scoreBackground, 'rgba(0, 0, 0, 0)', 'progress score should be plain text in the final Figma layout');
    assert(Math.abs(positions.fillRatio - 0.705) < 0.001, 'fill geometry should retain the unrounded 70.5% position behind the 71% label');
    assert.strictEqual(positions.scoreWidth, 137, 'progress score should preserve the approved segment width');
    assert.strictEqual(positions.scoreHeight, 149, 'progress score container should share the full Figma task-pane height');
    assert.strictEqual(positions.tableLeft, 353, 'platform table should align to the Figma x=353 coordinate');
    assert.strictEqual(positions.tableWidth, 1520, 'platform table should preserve the approved width');
    assert.strictEqual(positions.tableHeight, 559, 'platform table should preserve the approved height');
    assert.strictEqual(positions.tableHeadHeight, 64, 'platform table header should preserve the approved height');
    assert.strictEqual(positions.firstRowHeight, 108, 'platform rows should preserve the approved height');

    await page.setViewportSize({ width: 1472, height: 914 });
    await page.waitForFunction(() => document.querySelector('#app')?.dataset.viewportScale === '0.766667');
    const compact = await page.evaluate(() => {
      const app = document.querySelector('#app');
      const sidebar = document.querySelector('.sidebar');
      const main = document.querySelector('.main-content');
      const table = document.querySelector('.platform-table-shell');
      const appRect = app.getBoundingClientRect();
      const sidebarRect = sidebar.getBoundingClientRect();
      const tableRect = table.getBoundingClientRect();
      return {
        scale: Number(app.dataset.viewportScale),
        appLayoutWidth: app.offsetWidth,
        appVisualWidth: appRect.width,
        appVisualHeight: appRect.height,
        appVisualY: appRect.y,
        sidebarVisualWidth: sidebarRect.width,
        mainClientWidth: main.clientWidth,
        mainScrollWidth: main.scrollWidth,
        tableRight: tableRect.right,
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
        bodyScrollWidth: document.body.scrollWidth,
        rootScrollWidth: document.documentElement.scrollWidth,
      };
    });
    assert(Math.abs(compact.scale - (1472 / 1920)) < 0.000001, 'native window should use one uniform 1920-to-viewport scale');
    assert.strictEqual(compact.appLayoutWidth, 1920, 'scaled native window should preserve the internal 1920px design canvas');
    assert(Math.abs(compact.appVisualWidth - 1472) < 0.01, 'scaled design canvas should exactly fit the native viewport width');
    assert(Math.abs(compact.appVisualHeight - (1080 * 1472 / 1920)) < 0.01, 'scaled design canvas should preserve the 16:9 Figma aspect ratio');
    assert(Math.abs(compact.appVisualY - 43) < 0.01, 'non-16:9 native windows should center the fixed canvas with vertical letterboxing');
    assert(Math.abs(compact.sidebarVisualWidth - (304 * 1472 / 1920)) < 0.01, 'sidebar should scale proportionally with the full canvas');
    assert.strictEqual(compact.mainClientWidth, 1616, 'main content should retain the 1920px Figma coordinate system');
    assert.strictEqual(compact.mainScrollWidth, compact.mainClientWidth, 'main content should not keep a horizontal overflow strip');
    assert(compact.tableRight <= compact.viewportWidth + 0.01, 'platform table should remain inside the visible native window');
    assert.strictEqual(compact.bodyScrollWidth, compact.viewportWidth, 'body should not expose horizontal overflow after scaling');
    assert.strictEqual(compact.rootScrollWidth, compact.viewportWidth, 'root should not expose horizontal overflow after scaling');

    if (process.argv.includes('--screenshot')) {
      const compactOutput = path.join(ROOT, 'tmp', 'figma-local-build', 'repro-1472-after.png');
      fs.mkdirSync(path.dirname(compactOutput), { recursive: true });
      await page.screenshot({ path: compactOutput });
      process.stdout.write(`${compactOutput}\n`);
    }

    const idleFillWidth = await page.evaluate(() => {
      const pane = document.querySelector('#dash-task-pane');
      pane.classList.remove('is-running');
      pane.classList.add('is-idle');
      resetCollectionJourneyVisualProgress();
      return document.querySelector('#task-route-fill').getBoundingClientRect().width;
    });
    assert.strictEqual(idleFillWidth, 0, 'idle dashboard must not show a full black progress bar at 0%');

    await page.emulateMedia({ reducedMotion: 'reduce' });
    const reducedMotionStyles = await page.locator('#task-walker .task-walker-icon').evaluate((element) => {
      const style = getComputedStyle(element);
      return { animationName: style.animationName, transitionDuration: style.transitionDuration };
    });
    assert.strictEqual(reducedMotionStyles.animationName, 'none', 'reduced-motion mode should stop the walk cycle');

    if (process.argv.includes('--screenshot')) {
      const output = path.join(ROOT, 'tmp', 'collection-journey-progress-bw.png');
      fs.mkdirSync(path.dirname(output), { recursive: true });
      await page.screenshot({ path: output, fullPage: true });
      process.stdout.write(`${output}\n`);
    }
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}

async function main() {
  verifyStaticContract();
  verifyCalculation();
  await verifyBrowserRender();
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
