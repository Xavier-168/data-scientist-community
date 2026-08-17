const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const FRONTEND_ROOT = path.join(ROOT, 'frontend');
const HTML_PATH = path.join(FRONTEND_ROOT, 'progress.html');
const THEME_CSS_PATH = path.join(FRONTEND_ROOT, 'assets', 'progress-figma-dashboard.css');
const SCREENSHOT_PATH = process.env.FIGMA_LAYOUT_SCREENSHOT
  || path.join(ROOT, 'tmp', 'figma-local-build', 'figma-fullscreen-layout-1920.png');
const TALL_SCREENSHOT_PATH = process.env.FIGMA_TALL_SCREENSHOT
  || path.join(ROOT, 'tmp', 'figma-local-build', 'figma-fullscreen-layout-tall.png');
const IDLE_SCREENSHOT_PATH = process.env.FIGMA_IDLE_SCREENSHOT
  || path.join(ROOT, 'tmp', 'figma-local-build', 'figma-idle-progress-and-actions.png');
const HISTORY_SCREENSHOT_PATH = process.env.FIGMA_HISTORY_SCREENSHOT
  || path.join(ROOT, 'tmp', 'figma-local-build', 'figma-history-log-scale.png');

function validateSourceContract() {
  const html = fs.readFileSync(HTML_PATH, 'utf8');
  const css = fs.readFileSync(THEME_CSS_PATH, 'utf8');
  const requiredCss = [
    '@font-face',
    'font-family: "Noto Serif SC Local"',
    '--figma-ink: #111111',
    '--figma-muted: #6b6b6b',
    '--figma-line: #e6e6e6',
    '--figma-shadow: 1px 1px 1px rgba(0, 0, 0, 0.08)',
    '--figma-shadow-faint: 1px 1px 1px rgba(160, 160, 160, 0.05)',
    '--figma-shadow-medium: 1px 1px 1px rgba(0, 0, 0, 0.10)',
    '--figma-shadow-strong: 1px 1px 1px rgba(107, 107, 107, 0.12)',
    '--viewport-sidebar-edge: 304px',
    '#viewport-stage::before',
    '#btn-download-excel { width: 156px; min-width: 156px; }',
    '#btn-rerun { width: 177px; min-width: 177px; }',
    '#btn-sync-feishu { width: 151px; min-width: 151px; }',
    '#btn-auth-all { width: 177px; min-width: 177px; }',
    'font-size: 16px',
    'transform: translateY(-50%)',
    'display: block !important',
    '#view-history #history-list-container',
    'min-height: 56px',
    'padding: 14px 18px',
    'min-width: 82px',
    "matrix(-75.8 1.35 -0.38 -21.336 304 516.5)",
    'background-size: 304px 1080px',
    'width: 1920px',
    'height: 1080px',
    '--sidebar-width: 304px',
    'width: 1616px',
    'height: 559px',
    'grid-template-columns: 240px 134px 286px 120px 120px 90px 164px 142px 224px',
    'box-shadow: 2px 2px 2px -6px rgba(209, 209, 209, 0.26)',
    'backdrop-filter: blur(3.5px)',
    'box-shadow: 0 2px 2.1px -6px rgba(136, 136, 136, 0.08)',
    'backdrop-filter: blur(44px)',
    'box-shadow: 0 1px 3px -6px rgba(107, 107, 107, 0.02)',
    'backdrop-filter: blur(5.8px)',
    "x1='0.684146' y1='5.057317' x2='5.701221' y2='1.810976'",
    "stop-color='rgb(18,168,107)'",
    "fill-opacity='0.79'",
    "stop-color='rgb(168,18,20)'",
    "fill-opacity='0.88'",
    'box-shadow: var(--figma-shadow-medium)',
  ];
  for (const token of requiredCss) {
    assert(css.includes(token), `Figma theme source is missing exact token: ${token}`);
  }
  assert(!css.includes('radial-gradient(circle at 100% 48%'), 'approximate sidebar gradient must not replace the exact Figma matrix');
  assert(!css.includes('inset -1px -1px'), 'small component shadows must keep the global x=1, y=1, blur=1 convention');

  const requiredHtml = [
    'const SIDEBAR_DEFAULT_WIDTH = 304',
    'const SIDEBAR_MIN_WIDTH = 304',
    'const SIDEBAR_MAX_WIDTH = 304',
    'const DASHBOARD_DESIGN_WIDTH = 1920',
    'const DASHBOARD_DESIGN_HEIGHT = 1080',
    'Math.min(viewportWidth / DASHBOARD_DESIGN_WIDTH, viewportHeight / DASHBOARD_DESIGN_HEIGHT)',
    'const sidebarEdge = offsetX + (SIDEBAR_DEFAULT_WIDTH * scale)',
    "app.style.setProperty('--dashboard-scale', String(scale))",
    "stage.style.setProperty('--viewport-sidebar-edge', `${sidebarEdge}px`)",
    "stage.dataset.sidebarEdge = sidebarEdge.toFixed(3)",
    "stage.dataset.letterboxX = offsetX.toFixed(3)",
    "stage.dataset.letterboxY = offsetY.toFixed(3)",
  ];
  for (const token of requiredHtml) {
    assert(html.includes(token), `Figma viewport constraint source is missing exact token: ${token}`);
  }
}

const DESIGN = Object.freeze({
  canvas: { x: 0, y: 0, width: 1920, height: 1080 },
  sidebar: { x: 0, y: 0, width: 304, height: 1080 },
  mailbox: { x: 28, y: 965, width: 247, height: 61 },
  title: { x: 352, y: 42, width: 240, height: 57 },
  exportButton: { x: 1127, y: 48, width: 156, height: 51 },
  dateButton: { x: 1311, y: 48, width: 177, height: 51 },
  feishuButton: { x: 1516, y: 48, width: 151, height: 51 },
  runButton: { x: 1695, y: 48, width: 177, height: 51 },
  metrics: { x: 352, y: 132, width: 1520, height: 100 },
  progress: { x: 352, y: 273, width: 1520, height: 149 },
  table: { x: 353, y: 467, width: 1520, height: 559 },
  metricLabel1: { x: 460, y: 164, width: 158, height: 36 },
  metricValue1: { x: 646, y: 151.5, width: 61, height: 61 },
  metricLabel2: { x: 843, y: 164, width: 158, height: 36 },
  metricValue2: { x: 1029, y: 151.5, width: 61, height: 61 },
  metricLabel3: { x: 1214, y: 164, width: 158, height: 36 },
  metricValue3: { x: 1397, y: 161.5, width: 101, height: 41 },
  metricLabel4: { x: 1599, y: 164, width: 158, height: 36 },
  metricValue4: { x: 1777, y: 155.5, width: 77, height: 53 },
  baselineLabel: { x: 405, y: 296, width: 150, height: 36 },
  baselineValue: { x: 405, y: 346, width: 276, height: 40 },
  currentLabel: { x: 804, y: 296, width: 100, height: 36 },
  currentValue: { x: 804, y: 346, width: 236, height: 40 },
  progressLabel: { x: 1099, y: 297, width: 50, height: 36 },
  progressValue: { x: 1097, y: 344, width: 59, height: 39 },
  collectionTitle: { x: 1297, y: 300, width: 208, height: 37 },
  collectionTrack: { x: 1297, y: 353, width: 537, height: 11 },
  firstIcon: { x: 412, y: 564, width: 43, height: 43 },
  firstName: { x: 473, y: 570, width: 45, height: 32 },
  firstStatus: { x: 607, y: 565, width: 106, height: 41 },
  firstTrack: { x: 779, y: 589, width: 185, height: 11 },
  firstRun: { x: 1698, y: 564, width: 140, height: 44 },
  paginationPrev: { x: 1636, y: 976, width: 8, height: 32 },
  paginationPage: { x: 1653, y: 981, width: 28, height: 28 },
  paginationNext: { x: 1690, y: 976, width: 8, height: 32 },
  paginationSize: { x: 1719, y: 976, width: 110, height: 34 },
});

const SELECTORS = Object.freeze({
  stage: '#viewport-stage',
  canvas: '#app',
  sidebar: '.sidebar',
  mailbox: '#update-widget',
  title: '#dash-project-name',
  exportButton: '#btn-download-excel',
  dateButton: '#btn-rerun',
  feishuButton: '#btn-sync-feishu',
  runButton: '#btn-run-all',
  metrics: '#dash-metrics',
  progress: '#dash-task-pane',
  table: '.platform-table-shell',
  tableHead: '.platform-table-head',
  firstRow: '.platform-table-row:nth-child(1)',
  fourthRow: '.platform-table-row:nth-child(4)',
  tableFooter: '.platform-table-footer',
  metricLabel1: '#dash-metrics .metric-card:nth-child(1) .metric-title',
  metricValue1: '#dash-metrics .metric-card:nth-child(1) .metric-value',
  metricLabel2: '#dash-metrics .metric-card:nth-child(2) .metric-title',
  metricValue2: '#dash-metrics .metric-card:nth-child(2) .metric-value',
  metricLabel3: '#dash-metrics .metric-card:nth-child(3) .metric-title',
  metricValue3: '#dash-metrics .metric-card:nth-child(3) .metric-value',
  metricLabel4: '#dash-metrics .metric-card:nth-child(4) .metric-title',
  metricValue4: '#dash-metrics .metric-card:nth-child(4) .metric-value',
  baselineLabel: '.task-baseline-block > span',
  baselineValue: '#task-baseline-range',
  currentLabel: '.task-current-block > span',
  currentValue: '#task-current-platform',
  progressLabel: '.task-progress-label',
  progressValue: '.task-progress-number',
  collectionTitle: '#task-step-title',
  collectionTrack: '#task-journey .task-route-track',
  firstIcon: '.platform-table-row:nth-child(1) .platform-source-icon',
  firstName: '.platform-table-row:nth-child(1) .platform-name',
  firstStatus: '.platform-table-row:nth-child(1) .collection-status',
  firstTrack: '.platform-table-row:nth-child(1) .platform-progress-track',
  firstRun: '.platform-table-row:nth-child(1) .platform-run-action button',
  firstHealth: '.platform-table-row:nth-child(1) .platform-health',
  firstHealthDot: '.platform-table-row:nth-child(1) .platform-health i',
  thirdHealth: '.platform-table-row:nth-child(3) .platform-health',
  thirdHealthDot: '.platform-table-row:nth-child(3) .platform-health i',
  paginationPrev: '.platform-pagination > span:first-child',
  paginationPage: '.platform-pagination > strong',
  paginationNext: '.platform-pagination > span:nth-of-type(2)',
  paginationSize: '.platform-pagination > em',
});

const STYLE_EXPECTATIONS = Object.freeze({
  body: {
    selector: 'body',
    values: {
      color: 'rgb(17, 17, 17)',
      backgroundColor: 'rgb(255, 255, 255)',
      fontFamily: '"Noto Serif SC Local", "Noto Serif SC", "Songti SC", STSong, serif',
    },
  },
  canvas: {
    selector: '#app',
    values: { position: 'absolute', overflow: 'hidden', backgroundColor: 'rgb(255, 255, 255)' },
  },
  sidebar: {
    selector: '.sidebar',
    values: {
      width: '304px',
      minWidth: '304px',
      height: '1080px',
      borderRightColor: 'rgb(230, 230, 230)',
      backgroundRepeat: 'no-repeat',
      backgroundSize: '304px 1080px',
    },
  },
  activeNav: {
    selector: '.nav-item.active',
    values: {
      color: 'rgb(17, 17, 17)',
      backgroundColor: 'rgb(255, 255, 255)',
      borderRadius: '10px',
      boxShadow: 'rgba(0, 0, 0, 0.08) 1px 1px 1px 0px',
      fontSize: '20px',
      fontWeight: '700',
      lineHeight: '29px',
    },
  },
  mailbox: {
    selector: '#update-widget',
    values: {
      backgroundColor: 'rgb(246, 246, 246)',
      borderColor: 'rgb(230, 230, 230)',
      borderRadius: '10px',
      boxShadow: 'rgba(160, 160, 160, 0.05) 1px 1px 1px 0px',
    },
  },
  headerButton: {
    selector: '#btn-download-excel',
    values: {
      color: 'rgb(17, 17, 17)',
      backgroundColor: 'rgb(255, 255, 255)',
      borderColor: 'rgb(230, 230, 230)',
      borderRadius: '10px',
      boxShadow: 'rgba(107, 107, 107, 0.12) 1px 1px 1px 0px',
      fontSize: '16px',
      fontWeight: '500',
      lineHeight: '23px',
    },
  },
  metrics: {
    selector: '#dash-metrics',
    values: {
      backgroundColor: 'rgb(255, 255, 255)',
      borderColor: 'rgb(230, 230, 230)',
      borderRadius: '14px',
      boxShadow: 'rgba(209, 209, 209, 0.26) 2px 2px 2px -6px',
      backdropFilter: 'blur(3.5px)',
    },
  },
  taskPane: {
    selector: '#dash-task-pane',
    values: {
      backgroundColor: 'rgb(255, 255, 255)',
      borderColor: 'rgb(230, 230, 230)',
      borderRadius: '14px',
      boxShadow: 'rgba(136, 136, 136, 0.08) 0px 2px 2.1px -6px',
      backdropFilter: 'blur(44px)',
    },
  },
  table: {
    selector: '.platform-table-shell',
    values: {
      backgroundColor: 'rgba(252, 252, 252, 0.87)',
      borderColor: 'rgba(199, 198, 198, 0.67)',
      borderRadius: '22px',
      boxShadow: 'rgba(107, 107, 107, 0.02) 0px 1px 3px -6px',
      backdropFilter: 'blur(5.8px)',
    },
  },
  title: {
    selector: '#dash-project-name',
    values: {
      color: 'rgb(17, 17, 17)',
      fontFamily: '"Noto Serif SC Local", "Noto Serif SC", "Songti SC", STSong, serif',
      fontSize: '40px',
      fontWeight: '700',
      lineHeight: '57px',
      letterSpacing: 'normal',
    },
  },
  metricLabel: {
    selector: '#dash-metrics .metric-card:nth-child(1) .metric-title',
    values: { color: 'rgb(17, 17, 17)', fontSize: '25px', fontWeight: '400', lineHeight: '36px', letterSpacing: '1.5px' },
  },
  metricValue: {
    selector: '#dash-metrics .metric-card:nth-child(1) .metric-value',
    values: { color: 'rgb(255, 255, 255)', backgroundColor: 'rgb(19, 19, 19)', fontFamily: '"Geist Mono", ui-monospace, SFMono-Regular, monospace', fontSize: '35px', fontWeight: '500' },
  },
  tableHead: {
    selector: '.platform-table-head',
    values: { color: 'rgb(17, 17, 17)', fontSize: '22px', fontWeight: '500', lineHeight: '32px' },
  },
  platformIcon: {
    selector: '.platform-table-row:nth-child(1) .platform-source-icon',
    values: { backgroundColor: 'rgb(255, 255, 255)', borderColor: 'rgb(219, 222, 227)', borderRadius: '12px', boxShadow: 'rgba(0, 0, 0, 0.08) 1px 1px 1px 0px' },
  },
  collectionStatus: {
    selector: '.platform-table-row:nth-child(1) .collection-status',
    values: { color: 'rgb(0, 0, 0)', backgroundColor: 'rgb(255, 255, 255)', borderRadius: '9px', fontSize: '18px', fontWeight: '500', lineHeight: '17px', boxShadow: 'rgba(0, 0, 0, 0.1) 1px 1px 1px 0px' },
  },
  runButton: {
    selector: '.platform-table-row:nth-child(1) .platform-run-action button',
    values: { color: 'rgb(17, 17, 17)', backgroundColor: 'rgb(255, 255, 255)', borderColor: 'rgb(230, 230, 230)', borderRadius: '10px', boxShadow: 'rgba(0, 0, 0, 0.08) 1px 1px 1px 0px', fontSize: '16px', fontWeight: '500' },
  },
  healthyDot: {
    selector: '.platform-table-row:nth-child(1) .platform-health i',
    values: { width: '11px', height: '11px', opacity: '1', backgroundRepeat: 'no-repeat', backgroundSize: '11px 11px' },
  },
});

let chromium;
try {
  ({ chromium } = require('playwright'));
} catch (error) {
  const sharedPlaywright = path.resolve(ROOT, '..', '..', 'node_modules', 'playwright');
  if (!fs.existsSync(sharedPlaywright)) throw error;
  ({ chromium } = require(sharedPlaywright));
}

function resolveChromiumExecutable() {
  const home = os.homedir();
  const candidates = [
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    process.env.BROWSER_EXECUTABLE_PATH,
    path.join(home, 'Library', 'Caches', 'ms-playwright', 'chromium_headless_shell-1234', 'chrome-headless-shell-mac-arm64', 'chrome-headless-shell'),
    path.join(home, 'Library', 'Caches', 'ms-playwright', 'chromium-1234', 'chrome-mac-arm64', 'Google Chrome for Testing.app', 'Contents', 'MacOS', 'Google Chrome for Testing'),
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
  ];
  return candidates.find((candidate) => candidate && fs.existsSync(candidate));
}

function mimeType(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  return {
    '.css': 'text/css; charset=utf-8',
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml; charset=utf-8',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.ttf': 'font/ttf',
    '.otf': 'font/otf',
  }[ext] || 'application/octet-stream';
}

function platformProgress(platform, overrides = {}) {
  return {
    platform,
    enabled: true,
    status: 'completed',
    ui_status: 'completed',
    auth_status: 'authorized',
    auth_health_status: 'healthy',
    auth_checked_at: '2026-08-16T04:30:00+08:00',
    message: '采集完成',
    totalWorks: 0,
    processedWorks: 0,
    successWorks: 0,
    skippedWorks: 0,
    failedWorks: 0,
    ...overrides,
  };
}

function mockBackend({ idle = false } = {}) {
  const enabledPlatforms = ['douyin', 'xiaohongshu', 'bilibili', 'kuaishou'];
  const progress = {
    ok: true,
    running: true,
    douyin: platformProgress('douyin', {
      totalWorks: 120,
      processedWorks: 120,
      successWorks: 120,
    }),
    xiaohongshu: platformProgress('xiaohongshu', {
      totalWorks: 96,
      processedWorks: 96,
      successWorks: 96,
    }),
    bilibili: platformProgress('bilibili', {
      status: 'running',
      ui_status: 'running',
      message: '正在读取 B 站稿件，已处理 87 / 118 条',
      totalWorks: 118,
      processedWorks: 87,
      successWorks: 87,
    }),
    kuaishou: platformProgress('kuaishou', {
      status: 'queued',
      ui_status: 'queued',
      message: '排队等待',
    }),
    summary: {
      active_platform: 'bilibili',
      current_stage: 'scraping',
      completed_platforms: ['douyin', 'xiaohongshu'],
      zero_result_platforms: [],
      failed_platforms: [],
      enabled_platform_count: 4,
      enabled_platforms: enabledPlatforms,
      authorized_platform_count: 4,
      authorized_platforms: enabledPlatforms,
      setup_complete: true,
      onboarding_completed: true,
      has_run_history: true,
      feishu_enabled: true,
      feishu_ready: true,
      auto_sync_enabled: false,
      feishu: { enabled: true, status: 'idle', message: '' },
    },
  };
  const config = {
    customer_name: 'Xavier 本机长期测试',
    workspace_name: '赵逍遥工作台',
    min_publish_date: '2026-04-24',
    enabled_platforms: enabledPlatforms,
    onboarding_completed: true,
    feishu_enabled: true,
    feishu_auto_sync: false,
  };
  if (idle) {
    progress.running = false;
    progress.summary.active_platform = null;
    progress.summary.current_stage = 'idle';
    progress.summary.completed_platforms = [];
    for (const platform of enabledPlatforms) {
      progress[platform] = platformProgress(platform, {
        status: 'idle',
        ui_status: 'idle',
        message: '等待开始',
      });
    }
  }
  return { config, progress };
}

function mockResponses(options = {}) {
  const html = fs.readFileSync(HTML_PATH, 'utf8');
  const { config, progress } = mockBackend(options);
  const history = Array.from({ length: 36 }, (_, index) => ({
    run_id: `run-${index + 1}`,
    run_at: '2026-08-15 00:33:21',
    started_at: '2026-08-16T04:30:00+08:00',
    status: 'completed',
    duration: 12,
    feishu: { attempted: false },
    platform_results: [{
      platform: 'douyin',
      label: '抖音',
      status: 'success',
      success_count: 120,
      skip_count: 0,
      fail_count: 0,
      error: '',
    }],
  }));

  return {
    html,
    json: new Map([
      ['/license', { ok: true, valid: true, activated: true, access_mode: 'community', customer_name: config.customer_name }],
      ['/progress', progress],
      ['/config', { ok: true, config, summary: progress.summary }],
      ['/all-data', { ok: true, rows: [], row_count: 1286 }],
      ['/analytics/history', { ok: true, runs: history }],
      ['/package-info', { ok: true, package_id: 'data-scientist-community-mac-arm64', build_version: '0.1.0' }],
      ['/update/check', { ok: true, update_available: false, reason: 'update_service_not_configured' }],
      ['/update/download-progress', { ok: true, status: 'idle', percent: 0 }],
      ['/feedback/unread', { ok: true, unread_count: 0 }],
      ['/session/recover', { ok: true, token: 'figma-layout-test' }],
    ]),
  };
}

async function installMockRoutes(page, options = {}) {
  const responses = mockResponses(options);
  await page.route('http://figma.local/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/monitor') {
      await route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: responses.html });
      return;
    }
    if (url.pathname.startsWith('/assets/')) {
      const relative = url.pathname.replace(/^\/+/, '');
      const resolved = path.resolve(FRONTEND_ROOT, relative);
      if (resolved.startsWith(`${FRONTEND_ROOT}${path.sep}`) && fs.existsSync(resolved) && fs.statSync(resolved).isFile()) {
        await route.fulfill({ status: 200, contentType: mimeType(resolved), body: fs.readFileSync(resolved) });
      } else {
        await route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ ok: false, error: 'asset_not_found', path: url.pathname }) });
      }
      return;
    }
    const payload = responses.json.get(url.pathname) || { ok: true };
    await route.fulfill({ status: 200, contentType: 'application/json; charset=utf-8', body: JSON.stringify(payload) });
  });
}

function rounded(value) {
  return Math.round(value * 1000) / 1000;
}

function rectSnapshot(rect, scale = 1, origin = { x: 0, y: 0 }) {
  return {
    x: rounded((rect.x - origin.x) / scale),
    y: rounded((rect.y - origin.y) / scale),
    width: rounded(rect.width / scale),
    height: rounded(rect.height / scale),
  };
}

function compareNumber(issues, label, actual, expected, tolerance = 0.51) {
  if (!Number.isFinite(actual) || Math.abs(actual - expected) > tolerance) {
    issues.push(`${label}: expected ${expected}, actual ${actual}`);
  }
}

function compareRect(issues, label, actual, expected, tolerance = 0.51) {
  for (const key of ['x', 'y', 'width', 'height']) {
    compareNumber(issues, `${label}.${key}`, actual[key], expected[key], tolerance);
  }
}

async function collectLayout(page) {
  return page.evaluate(({ selectors, styleExpectations }) => {
    const readRect = (selector) => {
      const element = document.querySelector(selector);
      if (!element) return null;
      const rect = element.getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    };
    const readStyles = (selector, properties, pseudo = null) => {
      const element = document.querySelector(selector);
      if (!element) return null;
      const computed = getComputedStyle(element, pseudo);
      return Object.fromEntries(properties.map((property) => [property, computed[property]]));
    };
    const app = document.querySelector('#app');
    const main = document.querySelector('.main-content');
    const body = document.body;
    const root = document.documentElement;
    const scale = Number(getComputedStyle(app).getPropertyValue('--dashboard-scale')) || 1;
    return {
      scale,
      scaleDataset: app.dataset.viewportScale || '',
      viewport: { width: window.innerWidth, height: window.innerHeight },
      rectangles: Object.fromEntries(Object.entries(selectors).map(([name, selector]) => [name, readRect(selector)])),
      internal: {
        appWidth: app.offsetWidth,
        appHeight: app.offsetHeight,
        appScrollWidth: app.scrollWidth,
        appScrollHeight: app.scrollHeight,
        mainWidth: main.clientWidth,
        mainHeight: main.clientHeight,
        mainScrollWidth: main.scrollWidth,
        mainScrollHeight: main.scrollHeight,
        bodyScrollWidth: body.scrollWidth,
        bodyScrollHeight: body.scrollHeight,
        rootScrollWidth: root.scrollWidth,
        rootScrollHeight: root.scrollHeight,
      },
      forbidden: {
        localCard: Boolean(document.querySelector('.sidebar-local-card')),
        edition: Boolean(document.querySelector('.sidebar-edition')),
      },
      visibleButtons: Array.from(document.querySelectorAll('.header-actions .btn'))
        .filter((button) => getComputedStyle(button).display !== 'none')
        .map((button) => button.id),
      actionAlignment: Array.from(document.querySelectorAll('.header-actions .btn'))
        .filter((button) => getComputedStyle(button).display !== 'none')
        .map((button) => {
          const buttonRect = button.getBoundingClientRect();
          const range = document.createRange();
          range.selectNodeContents(button);
          const textRect = range.getBoundingClientRect();
          return {
            id: button.id,
            buttonCenterX: buttonRect.x + (buttonRect.width / 2),
            buttonCenterY: buttonRect.y + (buttonRect.height / 2),
            textCenterX: textRect.x + (textRect.width / 2),
            textCenterY: textRect.y + (textRect.height / 2),
          };
        }),
      rowCount: document.querySelectorAll('.platform-table-row').length,
      metricAlignment: Array.from(document.querySelectorAll('#dash-metrics .metric-card')).map((card) => {
        const cardRect = card.getBoundingClientRect();
        const labelRect = card.querySelector('.metric-title').getBoundingClientRect();
        const valueRect = card.querySelector('.metric-value').getBoundingClientRect();
        const iconStyle = getComputedStyle(card, '::before');
        return {
          cardCenterY: cardRect.y + (cardRect.height / 2),
          labelCenterY: labelRect.y + (labelRect.height / 2),
          valueCenterY: valueRect.y + (valueRect.height / 2),
          iconTop: iconStyle.top,
          iconTransform: iconStyle.transform,
        };
      }),
      styles: Object.fromEntries(Object.entries(styleExpectations).map(([name, expectation]) => [
        name,
        readStyles(expectation.selector, Object.keys(expectation.values)),
      ])),
      constraints: {
        stage: readStyles('#viewport-stage', ['position', 'width', 'height', 'overflow']),
        stageBackdrop: readStyles('#viewport-stage', ['position', 'zIndex', 'top', 'bottom', 'left', 'width', 'height', 'backgroundColor', 'backgroundRepeat', 'backgroundSize'], '::before'),
        canvas: readStyles('#app', ['position', 'width', 'height', 'overflow', 'transformOrigin', 'transform']),
        sidebar: readStyles('.sidebar', ['position', 'width', 'minWidth', 'height', 'flexBasis']),
        main: readStyles('.main-content', ['position', 'width', 'minWidth', 'height', 'flexBasis', 'overflow']),
        dashboard: readStyles('#view-dashboard', ['position', 'width', 'minWidth', 'height', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'overflow']),
        metrics: readStyles('#dash-metrics', ['position', 'width', 'height', 'overflow']),
        progress: readStyles('#dash-task-pane', ['position', 'width', 'height', 'minHeight', 'overflow']),
        table: readStyles('.platform-table-shell', ['position', 'width', 'height', 'overflow']),
        footer: readStyles('.platform-table-footer', ['position', 'right', 'bottom', 'left', 'height']),
      },
      fonts: {
        serif: document.fonts.check('40px "Noto Serif SC Local"'),
        mono: document.fonts.check('500 35px "Geist Mono"'),
      },
    };
  }, { selectors: SELECTORS, styleExpectations: STYLE_EXPECTATIONS });
}

function validateDesignGeometry(snapshot, label, issues) {
  const scale = snapshot.scale;
  const origin = {
    x: snapshot.rectangles.canvas?.x || 0,
    y: snapshot.rectangles.canvas?.y || 0,
  };
  for (const [name, expected] of Object.entries(DESIGN)) {
    const rawRect = snapshot.rectangles[name];
    if (!rawRect) {
      issues.push(`${label}.${name}: selector ${SELECTORS[name]} not found`);
      continue;
    }
    compareRect(issues, `${label}.${name}`, rectSnapshot(rawRect, scale, origin), expected);
  }
}

function validateInternalStructure(desktop, native, issues) {
  const desktopOrigin = desktop.rectangles.canvas;
  const nativeOrigin = native.rectangles.canvas;
  for (const name of Object.keys(SELECTORS)) {
    if (name === 'stage') continue;
    const desktopRect = desktop.rectangles[name];
    const nativeRect = native.rectangles[name];
    if (!desktopRect || !nativeRect) continue;
    const desktopNormalized = rectSnapshot(desktopRect, desktop.scale, desktopOrigin);
    const nativeNormalized = rectSnapshot(nativeRect, native.scale, nativeOrigin);
    compareRect(issues, `native.structure.${name}`, nativeNormalized, desktopNormalized, 0.02);
  }

  compareNumber(issues, 'desktop.tableHead.height', desktop.rectangles.tableHead?.height, 64, 0.01);
  compareNumber(issues, 'desktop.firstRow.height', desktop.rectangles.firstRow?.height, 108, 0.01);
  compareNumber(issues, 'desktop.fourthRow.height', desktop.rectangles.fourthRow?.height, 108, 0.01);
}

function validateInternalCanvas(snapshot, label, issues) {
  compareNumber(issues, `${label}.internal.appWidth`, snapshot.internal.appWidth, 1920, 0);
  compareNumber(issues, `${label}.internal.appHeight`, snapshot.internal.appHeight, 1080, 0);
  compareNumber(issues, `${label}.internal.appScrollWidth`, snapshot.internal.appScrollWidth, 1920, 0);
  compareNumber(issues, `${label}.internal.appScrollHeight`, snapshot.internal.appScrollHeight, 1080, 0);
  compareNumber(issues, `${label}.internal.mainWidth`, snapshot.internal.mainWidth, 1616, 0);
  compareNumber(issues, `${label}.internal.mainHeight`, snapshot.internal.mainHeight, 1080, 0);
  compareNumber(issues, `${label}.internal.mainScrollWidth`, snapshot.internal.mainScrollWidth, 1616, 0);
  compareNumber(issues, `${label}.internal.mainScrollHeight`, snapshot.internal.mainScrollHeight, 1080, 0);
}

function validateStyles(snapshot, label, issues) {
  for (const [name, expectation] of Object.entries(STYLE_EXPECTATIONS)) {
    const actual = snapshot.styles[name];
    if (!actual) {
      issues.push(`${label}.styles.${name}: selector ${expectation.selector} not found`);
      continue;
    }
    for (const [property, expected] of Object.entries(expectation.values)) {
      if (actual[property] !== expected) {
        issues.push(`${label}.styles.${name}.${property}: expected ${JSON.stringify(expected)}, actual ${JSON.stringify(actual[property])}`);
      }
    }
  }
}

function validateConstraints(snapshot, label, issues) {
  const expected = {
    stage: { position: 'relative', width: '1920px', height: '1080px', overflow: 'hidden' },
    canvas: { position: 'absolute', width: '1920px', height: '1080px', overflow: 'hidden', transformOrigin: '0px 0px' },
    sidebar: { position: 'relative', width: '304px', minWidth: '304px', height: '1080px', flexBasis: 'auto' },
    main: { position: 'relative', width: '1616px', minWidth: '0px', height: '1080px', flexBasis: '1616px', overflow: 'hidden' },
    dashboard: { position: 'static', width: '1616px', minWidth: '1616px', height: '1080px', paddingTop: '48px', paddingRight: '48px', paddingBottom: '48px', paddingLeft: '48px', overflow: 'visible' },
    metrics: { position: 'relative', width: '1520px', height: '100px', overflow: 'hidden' },
    progress: { position: 'relative', width: '1520px', height: '149px', minHeight: '149px', overflow: 'hidden' },
    table: { position: 'relative', width: '1520px', height: '559px', overflow: 'hidden' },
    footer: { position: 'absolute', right: '0px', bottom: '0px', left: '0px', height: '63px' },
  };
  for (const [group, properties] of Object.entries(expected)) {
    const actual = snapshot.constraints[group];
    if (!actual) {
      issues.push(`${label}.constraints.${group}: missing`);
      continue;
    }
    for (const [property, value] of Object.entries(properties)) {
      if (actual[property] !== value) {
        issues.push(`${label}.constraints.${group}.${property}: expected ${JSON.stringify(value)}, actual ${JSON.stringify(actual[property])}`);
      }
    }
  }
  if (!snapshot.fonts.serif) issues.push(`${label}.fonts.serif: bundled Noto Serif SC was not loaded`);
  if (!snapshot.fonts.mono) issues.push(`${label}.fonts.mono: bundled Geist Mono was not loaded`);
}

function validateMetricAlignment(snapshot, label, issues) {
  snapshot.metricAlignment.forEach((metric, index) => {
    const metricLabel = `${label}.metric${index + 1}`;
    compareNumber(issues, `${metricLabel}.labelCenterY`, metric.labelCenterY, metric.cardCenterY, 0.02);
    compareNumber(issues, `${metricLabel}.valueCenterY`, metric.valueCenterY, metric.cardCenterY, 0.02);
    if (metric.iconTop !== '49px') {
      issues.push(`${metricLabel}.iconTop: expected 49px center anchor, actual ${metric.iconTop}`);
    }
    if (metric.iconTransform === 'none') {
      issues.push(`${metricLabel}.iconTransform: expected centered transform, actual none`);
    }
  });
}

function validateActionAlignment(snapshot, label, issues) {
  snapshot.actionAlignment.forEach((action) => {
    compareNumber(issues, `${label}.${action.id}.textCenterX`, action.textCenterX, action.buttonCenterX, 0.02);
    compareNumber(issues, `${label}.${action.id}.textCenterY`, action.textCenterY, action.buttonCenterY, 0.52);
  });
}

async function main() {
  validateSourceContract();
  if (process.env.FIGMA_STATIC_ONLY === '1') {
    process.stdout.write('Figma source color, typography, shadow, geometry and viewport constraints verified.\n');
    return;
  }
  const executablePath = resolveChromiumExecutable();
  const browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  });
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  await installMockRoutes(page);
  const badResponses = [];
  const pageErrors = [];
  page.on('response', (response) => {
    if (response.status() >= 400) badResponses.push(`${response.status()} ${response.url()}`);
  });
  page.on('pageerror', (error) => pageErrors.push(error.message));

  try {
    const url = 'http://figma.local/monitor#session=figma-layout-test';
    await page.goto(url, { waitUntil: 'networkidle' });
    await page.waitForFunction(() => (
      document.querySelectorAll('.platform-table-row').length === 4
      && document.querySelector('#m-total-rows')?.textContent?.trim() === '1286'
      && document.querySelector('#m-history-count')?.textContent?.trim() === '36'
    ));
    await page.evaluate(() => document.fonts.ready);

    const issues = [];
    const desktop = await collectLayout(page);
    compareNumber(issues, 'desktop.scale', desktop.scale, 1, 0.000001);
    compareNumber(issues, 'desktop.viewport.width', desktop.viewport.width, 1920, 0);
    compareNumber(issues, 'desktop.viewport.height', desktop.viewport.height, 1080, 0);
    validateInternalCanvas(desktop, 'desktop', issues);
    validateDesignGeometry(desktop, 'desktop', issues);
    validateStyles(desktop, 'desktop', issues);
    validateConstraints(desktop, 'desktop', issues);
    validateMetricAlignment(desktop, 'desktop', issues);
    validateActionAlignment(desktop, 'desktop', issues);
    compareNumber(issues, 'desktop.stageBackdrop.width', Number.parseFloat(desktop.constraints.stageBackdrop?.width), 304, 0.02);
    if (desktop.forbidden.localCard) issues.push('desktop.sidebar-local-card: expected absent, actual present');
    if (desktop.forbidden.edition) issues.push('desktop.sidebar-edition: expected absent, actual present');
    assert.deepStrictEqual(
      desktop.visibleButtons,
      ['btn-download-excel', 'btn-rerun', 'btn-sync-feishu', 'btn-run-all'],
      'Figma header must expose exactly four action buttons',
    );
    compareNumber(issues, 'desktop.rowCount', desktop.rowCount, 4, 0);
    compareNumber(issues, 'desktop.bodyScrollWidth', desktop.internal.bodyScrollWidth, 1920, 0);
    compareNumber(issues, 'desktop.bodyScrollHeight', desktop.internal.bodyScrollHeight, 1080, 0);
    compareNumber(issues, 'desktop.rootScrollWidth', desktop.internal.rootScrollWidth, 1920, 0);
    compareNumber(issues, 'desktop.rootScrollHeight', desktop.internal.rootScrollHeight, 1080, 0);

    fs.mkdirSync(path.dirname(SCREENSHOT_PATH), { recursive: true });
    await page.screenshot({ path: SCREENSHOT_PATH });

    const idlePage = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
    await installMockRoutes(idlePage, { idle: true });
    await idlePage.goto(url, { waitUntil: 'networkidle' });
    await idlePage.waitForFunction(() => document.querySelector('#task-step-title')?.textContent?.trim() === '等待开始采集');
    await idlePage.evaluate(() => document.fonts.ready);
    const idleState = await idlePage.evaluate(() => {
      const pane = document.querySelector('#dash-task-pane');
      const journey = document.querySelector('#task-journey');
      const track = journey.querySelector('.task-route-track');
      const fill = journey.querySelector('.task-route-fill');
      const labels = journey.querySelector('.task-route-labels');
      const [startLabel, finishLabel] = labels.querySelectorAll('span');
      const journeyStyle = getComputedStyle(journey);
      const trackBefore = getComputedStyle(track, '::before');
      const labelsStyle = getComputedStyle(labels);
      const trackRect = track.getBoundingClientRect();
      const fillRect = fill.getBoundingClientRect();
      const labelsRect = labels.getBoundingClientRect();
      const startRect = startLabel.getBoundingClientRect();
      const finishRect = finishLabel.getBoundingClientRect();
      return {
        paneClass: pane.className,
        title: document.querySelector('#task-step-title')?.textContent?.trim(),
        journeyDisplay: journeyStyle.display,
        journeyVisibility: journeyStyle.visibility,
        track: { x: trackRect.x, y: trackRect.y, width: trackRect.width, height: trackRect.height },
        labels: {
          x: labelsRect.x,
          width: labelsRect.width,
          position: labelsStyle.position,
          display: labelsStyle.display,
          justifyContent: labelsStyle.justifyContent,
        },
        startLabel: { x: startRect.x, right: startRect.right },
        finishLabel: { x: finishRect.x, right: finishRect.right },
        trackBefore: {
          position: trackBefore.position,
          content: trackBefore.content,
          backgroundColor: trackBefore.backgroundColor,
          height: trackBefore.height,
        },
        fillWidth: fillRect.width,
        ariaValueNow: journey.getAttribute('aria-valuenow'),
      };
    });
    if (!idleState.paneClass.includes('is-idle')) issues.push(`idle.paneClass: expected is-idle, actual ${idleState.paneClass}`);
    if (idleState.journeyDisplay !== 'block') issues.push(`idle.journeyDisplay: expected block, actual ${idleState.journeyDisplay}`);
    if (idleState.journeyVisibility !== 'visible') issues.push(`idle.journeyVisibility: expected visible, actual ${idleState.journeyVisibility}`);
    if (idleState.trackBefore.position !== 'absolute') issues.push(`idle.trackBefore.position: expected absolute, actual ${idleState.trackBefore.position}`);
    if (!idleState.trackBefore.content || idleState.trackBefore.content === 'none') issues.push(`idle.trackBefore.content: expected rendered pseudo element, actual ${idleState.trackBefore.content}`);
    if (idleState.trackBefore.backgroundColor !== 'rgb(236, 236, 236)') issues.push(`idle.trackBefore.backgroundColor: expected rgb(236, 236, 236), actual ${idleState.trackBefore.backgroundColor}`);
    compareRect(issues, 'idle.track', idleState.track, DESIGN.collectionTrack, 0.02);
    if (idleState.labels.position !== 'absolute') issues.push(`idle.labels.position: expected absolute, actual ${idleState.labels.position}`);
    if (idleState.labels.display !== 'flex') issues.push(`idle.labels.display: expected flex, actual ${idleState.labels.display}`);
    if (idleState.labels.justifyContent !== 'space-between') issues.push(`idle.labels.justifyContent: expected space-between, actual ${idleState.labels.justifyContent}`);
    compareNumber(issues, 'idle.labels.x', idleState.labels.x, idleState.track.x, 0.02);
    compareNumber(issues, 'idle.labels.width', idleState.labels.width, idleState.track.width, 0.02);
    compareNumber(issues, 'idle.startLabel.x', idleState.startLabel.x, idleState.track.x, 0.02);
    compareNumber(issues, 'idle.finishLabel.right', idleState.finishLabel.right, idleState.track.x + idleState.track.width, 0.02);
    compareNumber(issues, 'idle.fillWidth', idleState.fillWidth, 0, 0.02);
    if (idleState.ariaValueNow !== '0') issues.push(`idle.ariaValueNow: expected 0, actual ${idleState.ariaValueNow}`);
    fs.mkdirSync(path.dirname(IDLE_SCREENSHOT_PATH), { recursive: true });
    await idlePage.screenshot({ path: IDLE_SCREENSHOT_PATH });
    await idlePage.close();

    const historyPage = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
    await installMockRoutes(historyPage);
    await historyPage.goto(url, { waitUntil: 'networkidle' });
    await historyPage.click('[data-view="history"]');
    await historyPage.waitForFunction(() => document.querySelectorAll('.history-item').length > 0);
    await historyPage.evaluate(() => document.fonts.ready);
    const historyState = await historyPage.evaluate(() => {
      const read = (selector) => {
        const element = document.querySelector(selector);
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return {
          rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
          fontSize: style.fontSize,
          lineHeight: style.lineHeight,
          padding: style.padding,
          gap: style.gap,
          borderRadius: style.borderRadius,
          boxShadow: style.boxShadow,
        };
      };
      const header = document.querySelector('.history-header').getBoundingClientRect();
      const time = document.querySelector('.history-time').getBoundingClientRect();
      const status = document.querySelector('.history-status-word').getBoundingClientRect();
      const chevron = document.querySelector('.history-chevron').getBoundingClientRect();
      const centerY = header.y + (header.height / 2);
      return {
        container: read('#history-list-container'),
        item: read('.history-item'),
        header: read('.history-header'),
        time: read('.history-time'),
        status: read('.history-status-word'),
        chevron: read('.history-chevron'),
        centerDeltas: {
          time: (time.y + (time.height / 2)) - centerY,
          status: (status.y + (status.height / 2)) - centerY,
          chevron: (chevron.y + (chevron.height / 2)) - centerY,
        },
      };
    });
    compareRect(issues, 'history.container', historyState.container.rect, { x: 352, y: 115, width: 1520, height: 2592 }, 0.02);
    compareRect(issues, 'history.item', historyState.item.rect, { x: 352, y: 115, width: 1520, height: 58 }, 0.02);
    compareRect(issues, 'history.header', historyState.header.rect, { x: 353, y: 116, width: 1518, height: 56 }, 0.02);
    if (historyState.header.padding !== '14px 18px') issues.push(`history.header.padding: expected 14px 18px, actual ${historyState.header.padding}`);
    if (historyState.time.fontSize !== '16px') issues.push(`history.time.fontSize: expected 16px, actual ${historyState.time.fontSize}`);
    if (historyState.time.lineHeight !== '24px') issues.push(`history.time.lineHeight: expected 24px, actual ${historyState.time.lineHeight}`);
    if (historyState.status.fontSize !== '14px') issues.push(`history.status.fontSize: expected 14px, actual ${historyState.status.fontSize}`);
    if (historyState.status.rect.width < 82) issues.push(`history.status.width: expected >=82px, actual ${historyState.status.rect.width}`);
    if (historyState.chevron.fontSize !== '16px') issues.push(`history.chevron.fontSize: expected 16px, actual ${historyState.chevron.fontSize}`);
    if (historyState.item.boxShadow !== 'rgba(0, 0, 0, 0.08) 1px 1px 1px 0px') issues.push(`history.item.boxShadow: expected global 1/1/1 shadow, actual ${historyState.item.boxShadow}`);
    compareNumber(issues, 'history.time.centerY', historyState.centerDeltas.time, 0, 0.52);
    compareNumber(issues, 'history.status.centerY', historyState.centerDeltas.status, 0, 0.52);
    compareNumber(issues, 'history.chevron.centerY', historyState.centerDeltas.chevron, 0, 0.52);
    fs.mkdirSync(path.dirname(HISTORY_SCREENSHOT_PATH), { recursive: true });
    await historyPage.screenshot({ path: HISTORY_SCREENSHOT_PATH });
    await historyPage.click('.history-item');
    const expandedHistoryState = await historyPage.evaluate(() => {
      const details = document.querySelector('.history-details');
      const paragraphs = details.querySelectorAll(':scope > p');
      const platform = details.querySelector('.grid-2 > div');
      return {
        detailsPadding: getComputedStyle(details).padding,
        summaryFontSize: getComputedStyle(paragraphs[0]).fontSize,
        headingFontSize: getComputedStyle(paragraphs[1]).fontSize,
        platformFontSize: getComputedStyle(platform).fontSize,
        platformPadding: getComputedStyle(platform).padding,
      };
    });
    if (expandedHistoryState.detailsPadding !== '18px') issues.push(`history.details.padding: expected 18px, actual ${expandedHistoryState.detailsPadding}`);
    if (expandedHistoryState.summaryFontSize !== '14px') issues.push(`history.details.summaryFontSize: expected 14px, actual ${expandedHistoryState.summaryFontSize}`);
    if (expandedHistoryState.headingFontSize !== '15px') issues.push(`history.details.headingFontSize: expected 15px, actual ${expandedHistoryState.headingFontSize}`);
    if (expandedHistoryState.platformFontSize !== '14px') issues.push(`history.details.platformFontSize: expected 14px, actual ${expandedHistoryState.platformFontSize}`);
    if (expandedHistoryState.platformPadding !== '9px') issues.push(`history.details.platformPadding: expected 9px, actual ${expandedHistoryState.platformPadding}`);
    await historyPage.close();

    const responsiveViewports = [
      { width: 1720, height: 980, label: 'macbook-large' },
      { width: 1472, height: 914, label: 'native-preview' },
      { width: 1280, height: 800, label: 'macbook-compact' },
      { width: 2560, height: 1440, label: 'retina-desktop' },
      { width: 1496, height: 1000, label: 'tall-window' },
    ];
    for (const viewport of responsiveViewports) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      const expectedScale = Math.min(viewport.width / DESIGN.canvas.width, viewport.height / DESIGN.canvas.height);
      await page.waitForFunction(
        (target) => {
          const app = document.querySelector('#app');
          if (!app) return false;
          const scale = Number(getComputedStyle(app).getPropertyValue('--dashboard-scale'));
          return Math.abs(scale - target) < 0.000001;
        },
        expectedScale,
      );
      const snapshot = await collectLayout(page);
      const label = viewport.label;
      compareNumber(issues, `${label}.scale`, snapshot.scale, expectedScale, 0.000001);
      compareNumber(issues, `${label}.viewport.width`, snapshot.viewport.width, viewport.width, 0);
      compareNumber(issues, `${label}.viewport.height`, snapshot.viewport.height, viewport.height, 0);
      validateInternalCanvas(snapshot, label, issues);
      validateDesignGeometry(snapshot, `${label}.normalized`, issues);
      validateInternalStructure(desktop, snapshot, issues);
      validateMetricAlignment(snapshot, label, issues);
      validateActionAlignment(snapshot, label, issues);

      const visualCanvas = snapshot.rectangles.canvas;
      const expectedCanvasWidth = DESIGN.canvas.width * expectedScale;
      const expectedCanvasHeight = DESIGN.canvas.height * expectedScale;
      const expectedCanvasX = (viewport.width - expectedCanvasWidth) / 2;
      const expectedCanvasY = (viewport.height - expectedCanvasHeight) / 2;
      const expectedSidebarEdge = expectedCanvasX + (DESIGN.sidebar.width * expectedScale);
      compareNumber(issues, `${label}.visualCanvas.x`, visualCanvas.x, expectedCanvasX, 0.02);
      compareNumber(issues, `${label}.visualCanvas.y`, visualCanvas.y, expectedCanvasY, 0.02);
      compareNumber(issues, `${label}.visualCanvas.width`, visualCanvas.width, expectedCanvasWidth, 0.02);
      compareNumber(issues, `${label}.visualCanvas.height`, visualCanvas.height, expectedCanvasHeight, 0.02);
      compareNumber(issues, `${label}.visualSidebar.height`, snapshot.rectangles.sidebar.height, expectedCanvasHeight, 0.02);
      compareNumber(issues, `${label}.stageBackdrop.width`, Number.parseFloat(snapshot.constraints.stageBackdrop?.width), expectedSidebarEdge, 0.02);
      if (snapshot.constraints.stageBackdrop?.backgroundColor !== 'rgb(244, 244, 244)') {
        issues.push(`${label}.stageBackdrop.backgroundColor: expected grey sidebar plane, actual ${snapshot.constraints.stageBackdrop?.backgroundColor}`);
      }
      if (label === 'tall-window') {
        fs.mkdirSync(path.dirname(TALL_SCREENSHOT_PATH), { recursive: true });
        await page.screenshot({ path: TALL_SCREENSHOT_PATH });
      }
      compareNumber(issues, `${label}.bodyScrollWidth`, snapshot.internal.bodyScrollWidth, viewport.width, 0);
      compareNumber(issues, `${label}.rootScrollWidth`, snapshot.internal.rootScrollWidth, viewport.width, 0);
      compareNumber(issues, `${label}.bodyScrollHeight`, snapshot.internal.bodyScrollHeight, viewport.height, 0);
      compareNumber(issues, `${label}.rootScrollHeight`, snapshot.internal.rootScrollHeight, viewport.height, 0);
      if (snapshot.forbidden.localCard) issues.push(`${label}.sidebar-local-card: expected absent, actual present`);
      if (snapshot.forbidden.edition) issues.push(`${label}.sidebar-edition: expected absent, actual present`);
    }

    if (badResponses.length) issues.push(`network responses >= 400: ${badResponses.join(', ')}`);
    if (pageErrors.length) issues.push(`page errors: ${pageErrors.join(' | ')}`);

    if (issues.length) {
      process.stderr.write('Figma fullscreen layout regression failed:\n');
      for (const issue of issues) process.stderr.write(`- ${issue}\n`);
      process.stderr.write(`\nDesktop snapshot:\n${JSON.stringify(desktop, null, 2)}\n`);
      assert.fail(`${issues.length} Figma fullscreen layout assertions failed`);
    }

    process.stdout.write('Figma geometry, colors, typography, shadows and constraints verified at 1920x1080, 1720x980, 1472x914, 1280x800, 2560x1440 and tall-window 1496x1000.\n');
    process.stdout.write(`Screenshot: ${SCREENSHOT_PATH}\n`);
    process.stdout.write(`Tall-window screenshot: ${TALL_SCREENSHOT_PATH}\n`);
    process.stdout.write(`Idle progress screenshot: ${IDLE_SCREENSHOT_PATH}\n`);
    process.stdout.write(`History log screenshot: ${HISTORY_SCREENSHOT_PATH}\n`);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
