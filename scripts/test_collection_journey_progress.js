const assert = require('assert');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const HTML_PATH = path.join(ROOT, 'frontend', 'progress.html');
const CSS_PATH = path.join(ROOT, 'frontend', 'assets', 'progress-apple-theme.css');
const WALK_ICON_PATH = path.join(ROOT, 'frontend', 'assets', 'vendor', 'tabler', 'walk.svg');
const html = fs.readFileSync(HTML_PATH, 'utf8');
const css = fs.readFileSync(CSS_PATH, 'utf8');
const walkIcon = fs.readFileSync(WALK_ICON_PATH, 'utf8');
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
  assert(html.includes('task-progress-label">PROGRESS'), 'running card should use the approved horizontal progress badge');
  assert(html.includes('/assets/vendor/tabler/walk.svg'), 'walker should use the vendored Tabler walk icon');
  assert(!html.includes('task-walker-figure'), 'walker should not use a handcrafted inline SVG');
  assert(css.includes('@keyframes taskWalkerStep'), 'walker should include a restrained step animation');
  assert(css.includes('@media (prefers-reduced-motion: reduce)'), 'walker should respect reduced-motion preferences');
  assert(html.includes('COLLECTION_JOURNEY_ANIMATION_MS = 2800'), 'journey should interpolate across the backend polling window');
  assert(html.includes('elapsed / COLLECTION_JOURNEY_ANIMATION_MS'), 'journey interpolation should use a linear time ratio');
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
    if (url.pathname === '/assets/vendor/tabler/walk.svg') {
      res.writeHead(200, { 'Content-Type': 'image/svg+xml; charset=utf-8' });
      res.end(walkIcon);
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
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/monitor#session=test-session`, { waitUntil: 'networkidle' });
    await page.locator('#dash-task-pane.active.is-running').waitFor();
    const resetPercent = await page.evaluate((runningProgress) => {
      resetCollectionJourneyVisualProgress();
      renderCollectionJourney(runningProgress, ['douyin', 'xiaohongshu']);
      return document.querySelector('#task-progress-percent')?.textContent;
    }, progress);
    assert.strictEqual(resetPercent, '0', 'the first rendered frame of a new collection should be 0%');
    const firstAnimatedPercent = Number(await page.locator('#task-progress-percent').innerText());
    await page.waitForTimeout(350);
    const secondAnimatedPercent = Number(await page.locator('#task-progress-percent').innerText());
    await page.waitForTimeout(350);
    const thirdAnimatedPercent = Number(await page.locator('#task-progress-percent').innerText());
    const firstStep = secondAnimatedPercent - firstAnimatedPercent;
    const secondStep = thirdAnimatedPercent - secondAnimatedPercent;
    assert(firstStep > 0 && secondStep > 0, 'progress should keep moving between backend polls');
    assert(Math.abs(firstStep - secondStep) <= 2, 'progress should move at an approximately constant linear speed');
    await page.waitForFunction(() => document.querySelector('#task-progress-percent')?.textContent === '71', null, { timeout: 4000 });
    assert.strictEqual(await page.locator('#task-progress-percent').innerText(), '71');
    assert.strictEqual(await page.locator('#task-journey').getAttribute('aria-valuenow'), '71');
    assert((await page.locator('#task-progress-meta').innerText()).includes('1 / 2'));
    assert((await page.locator('#task-progress-stage').innerText()).includes('抖音'));

    const positions = await page.evaluate(() => {
      const pane = document.querySelector('#dash-task-pane').getBoundingClientRect();
      const track = document.querySelector('.task-route-track').getBoundingClientRect();
      const fill = document.querySelector('#task-route-fill').getBoundingClientRect();
      const walker = document.querySelector('#task-walker').getBoundingClientRect();
      const start = document.querySelector('.task-route-start').getBoundingClientRect();
      const finish = document.querySelector('.task-route-finish').getBoundingClientRect();
      const score = document.querySelector('.task-progress-score').getBoundingClientRect();
      const paneStyle = getComputedStyle(document.querySelector('#dash-task-pane'));
      const dividerStyle = getComputedStyle(document.querySelector('#dash-task-pane'), '::before');
      const walkerStyle = getComputedStyle(document.querySelector('#task-walker'));
      const trackStyle = getComputedStyle(document.querySelector('.task-route-track'), '::before');
      const fillStyle = getComputedStyle(document.querySelector('#task-route-fill'));
      const startStyle = getComputedStyle(document.querySelector('.task-route-start'));
      const finishStyle = getComputedStyle(document.querySelector('.task-route-finish'));
      const scoreStyle = getComputedStyle(document.querySelector('.task-progress-score'));
      return {
        paneHeight: pane.height,
        paneBackground: paneStyle.backgroundColor,
        paneBorderColor: paneStyle.borderTopColor,
        paneRadius: paneStyle.borderTopLeftRadius,
        dividerColor: dividerStyle.backgroundColor,
        trackLeft: track.left,
        trackWidth: track.width,
        fillRatio: fill.width / track.width,
        fillRight: fill.right,
        walkerRight: walker.right,
        walkerCenter: walker.left + (walker.width / 2),
        walkerSize: walker.width,
        walkerHeight: walker.height,
        walkerBackground: walkerStyle.backgroundColor,
        trackHeight: track.height,
        trackColor: trackStyle.backgroundColor,
        trackLineHeight: trackStyle.height,
        fillColor: fillStyle.backgroundColor,
        fillOpacity: fillStyle.opacity,
        startColor: startStyle.backgroundColor,
        finishColor: finishStyle.backgroundColor,
        finishBorderColor: finishStyle.borderTopColor,
        scoreBackground: scoreStyle.backgroundColor,
        startSize: start.width,
        finishSize: finish.width,
        scoreWidth: score.width,
        scoreHeight: score.height,
      };
    });
    assert.strictEqual(positions.paneHeight, 140, 'running card should preserve the approved 140px Figma height');
    assert.strictEqual(positions.paneBackground, 'rgb(255, 255, 255)', 'card background should match Figma white');
    assert.strictEqual(positions.paneBorderColor, 'rgb(221, 221, 221)', 'card border should match Figma #ddd');
    assert.strictEqual(positions.paneRadius, '14px', 'card radius should match Figma 14px');
    assert.strictEqual(positions.dividerColor, 'rgb(238, 238, 238)', 'divider should match Figma #eee');
    assert.strictEqual(positions.walkerSize, 18, 'walker should match the latest Figma 18px width');
    assert.strictEqual(positions.walkerHeight, 19, 'walker should match the latest Figma 19px height');
    assert.strictEqual(positions.walkerBackground, 'rgba(0, 0, 0, 0)', 'walker should no longer sit inside a black circle');
    assert.strictEqual(positions.trackHeight, 19, 'route container should hold the latest 17px Figma track and 19px walker');
    assert.strictEqual(positions.trackLineHeight, '17px', 'visible track should match the latest Figma 17px height');
    assert.strictEqual(positions.trackColor, 'rgb(231, 231, 231)', 'track should match Figma #e7e7e7');
    assert.strictEqual(positions.fillColor, 'rgb(17, 17, 17)', 'fill should match Figma #111');
    assert.strictEqual(positions.fillOpacity, '0.8', 'progress fill should use the latest Figma 80% opacity');
    assert.strictEqual(positions.startColor, 'rgba(17, 17, 17, 0.64)', 'start point should preserve Figma layered opacity');
    assert.strictEqual(positions.finishColor, 'rgb(255, 255, 255)', 'finish point should preserve Figma white fill');
    assert.strictEqual(positions.finishBorderColor, 'rgb(221, 221, 221)', 'finish point should preserve Figma #ddd border');
    assert.strictEqual(positions.scoreBackground, 'rgba(17, 17, 17, 0.95)', 'score badge should preserve Figma group opacity');
    assert(Math.abs(positions.fillRatio - 0.705) < 0.001, 'fill geometry should retain the unrounded 70.5% position behind the 71% label');
    assert(Math.abs(positions.walkerRight - positions.fillRight) < 1, 'walker right edge should stay attached to the fill endpoint');
    assert.strictEqual(positions.startSize, 17, 'start marker should match the latest Figma 17px size');
    assert.strictEqual(positions.finishSize, 17, 'finish marker should match the latest Figma 17px size');
    assert.strictEqual(positions.scoreWidth, 121, 'progress badge should preserve the approved width');
    assert.strictEqual(positions.scoreHeight, 31, 'progress badge should preserve the approved height');
    assert(positions.walkerCenter > positions.trackLeft + (positions.trackWidth * 0.65), 'walker should move beyond two thirds of the route');

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
