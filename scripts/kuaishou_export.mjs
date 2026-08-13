import fs from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { execFile } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { chromium } from 'playwright';
import { navigateAuthCandidates, prepareAuthPage } from './browser_auth_utils.mjs';
import { resolveDownloadsDir, resolveProfileDir } from './runtime_paths.mjs';

const DEFAULT_PYTHON_BIN = process.platform === 'win32' ? 'python' : 'python3';
const DEFAULT_BROWSER_CHANNEL = process.env.BROWSER_CHANNEL ?? 'chrome';
const DEFAULT_DOWNLOAD_DIR = resolveDownloadsDir();

const CONFIG = {
  homeUrl: process.env.KS_HOME_URL ?? 'https://cp.kuaishou.com/',
  articleUrl: process.env.KS_ARTICLE_URL ?? 'https://cp.kuaishou.com/statistics/article',
  articleListApiUrl: process.env.KS_ARTICLE_LIST_API_URL
    ?? 'https://cp.kuaishou.com/rest/cp/creator/analysis/pc/photo/list',
  exportTaskListApiUrl: process.env.KS_EXPORT_TASK_LIST_API_URL
    ?? 'https://cp.kuaishou.com/rest/cp/creator/analysis/export/task/list',
  exportTaskDownloadApiUrl: process.env.KS_EXPORT_TASK_DOWNLOAD_API_URL
    ?? 'https://cp.kuaishou.com/rest/cp/creator/analysis/export/download',
  // Use Data Center / Works Analysis as the primary source; Content Management misses analytics fields.
  worksUrl: process.env.KS_WORKS_URL ?? 'https://cp.kuaishou.com/article/manage/video',
  detailUrlBase: process.env.KS_DETAIL_URL_BASE ?? 'https://cp.kuaishou.com/statistics/article/detail',
  browserChannel: DEFAULT_BROWSER_CHANNEL,
  browserExecutablePath: String(process.env.BROWSER_EXECUTABLE_PATH ?? '').trim(),
  userDataDir: process.env.USER_DATA_DIR
    ? path.resolve(process.env.USER_DATA_DIR)
    : resolveProfileDir('kuaishou', DEFAULT_BROWSER_CHANNEL),
  headless: (process.env.HEADLESS ?? 'true') === 'true',
  authOnly: (process.env.AUTH_ONLY ?? 'false') === 'true',
  cleanProfileLocks: (process.env.CLEAN_PROFILE_LOCKS ?? 'true') === 'true',
  videoLimit: Number.parseInt(process.env.VIDEO_LIMIT ?? '200', 10),
  minPublishDate: process.env.MIN_PUBLISH_DATE ?? '',
  maxPublishDate: process.env.MAX_PUBLISH_DATE ?? '',
  refreshDays: Number.parseInt(process.env.REFRESH_DAYS ?? '0', 10),
  refreshLatestCount: Number.parseInt(process.env.REFRESH_LATEST_COUNT ?? '0', 10),
  forceFullExport: (process.env.FORCE_FULL_EXPORT ?? 'false') === 'true',
  scanWaitMs: Number.parseInt(process.env.SCAN_WAIT_MS ?? '300000', 10),
  scanPollMs: Number.parseInt(process.env.SCAN_POLL_MS ?? '2000', 10),
  detailWaitMs: Number.parseInt(process.env.KS_DETAIL_WAIT_MS ?? '45000', 10),
  detailExportEnabled: (process.env.KS_DETAIL_EXPORT_ENABLED ?? 'true') === 'true',
  detailExportTimeoutMs: Number.parseInt(process.env.KS_DETAIL_EXPORT_TIMEOUT_MS ?? '90000', 10),
  detailExportDir: process.env.KS_DETAIL_EXPORT_DIR
    ? path.resolve(process.env.KS_DETAIL_EXPORT_DIR)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'kuaishou_detail_exports'),
  staleRoundsLimit: Number.parseInt(process.env.STALE_ROUNDS_LIMIT ?? '6', 10),
  progressPath: process.env.PROGRESS_PATH
    ? path.resolve(process.env.PROGRESS_PATH)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'kuaishou_progress.json'),
  outputPath: process.env.KS_OUTPUT_PATH
    ? path.resolve(process.env.KS_OUTPUT_PATH)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'kuaishou_all_videos.xlsx'),
  tempRowsPath: process.env.KS_TEMP_ROWS_PATH
    ? path.resolve(process.env.KS_TEMP_ROWS_PATH)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'kuaishou_rows.json'),
  writerScriptPath: process.env.KS_WRITER_SCRIPT
    ? path.resolve(process.env.KS_WRITER_SCRIPT)
    : path.resolve('scripts', 'write_rows_excel.py'),
  detailNormalizerScriptPath: process.env.KS_DETAIL_NORMALIZER_SCRIPT
    ? path.resolve(process.env.KS_DETAIL_NORMALIZER_SCRIPT)
    : path.resolve('scripts', 'normalize_kuaishou_detail_export.py'),
  pythonBin: process.env.PYTHON_BIN ?? DEFAULT_PYTHON_BIN,
};

const execFileAsync = promisify(execFile);

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const KUAISHOU_TITLE_KEYS = ['title', 'photoTitle', 'caption', 'desc', 'description'];
const KUAISHOU_TITLE_CONTAINER_KEYS = ['photo', 'work', 'item', 'content'];
const KUAISHOU_EXPORT_CREATE_API_PATH = '/rest/cp/creator/pc/analysis/photo/data/export';

function normalizeKuaishouTitleSource(value) {
  if (typeof value !== 'string') return '';
  return value
    .replace(/\n+/g, ' ')
    .replace(/[\u200B-\u200D\uFEFF\u00A0]+/gu, ' ')
    .replace(/＃/g, '#')
    .replace(/\s+/g, ' ')
    .trim();
}

function kuaishouTitleScopes(value) {
  const scopes = [];
  const queue = [value];
  const seen = new Set();
  while (queue.length > 0 && scopes.length < 17) {
    const node = queue.shift();
    if (!node || typeof node !== 'object' || Array.isArray(node) || seen.has(node)) continue;
    seen.add(node);
    scopes.push(node);
    for (const key of KUAISHOU_TITLE_CONTAINER_KEYS) queue.push(node[key]);
  }
  return scopes;
}

function cleanKuaishouTitleCandidate(rawValue) {
  const raw = normalizeKuaishouTitleSource(rawValue);
  if (!raw) return '';
  const stripTrailingTopics = (text) => text.replace(
    /(?:\s+|(?<=[。！？!?】）》」』]))#[^#\s]+(?:\s+#[^#\s]+)*\s*$/u,
    '',
  ).trim();

  if (raw.startsWith('#')) {
    const nextHash = raw.indexOf('#', 1);
    const firstSegment = raw.slice(1, nextHash >= 0 ? nextHash : undefined).trim();
    // 快手真实列表偶尔把长句正文开头也写成 #。只有足够长且
    // 存在完整句终标点时才恢复；其他前导 # 内容按源话题保留。
    if (firstSegment.length < 12 || !/[。！？!?]/u.test(firstSegment)) return '';
    return stripTrailingTopics(raw.slice(1));
  }

  // 只移除空白或句末标点之后的话题，避免把 C# / F# 当作话题起点。
  return stripTrailingTopics(raw);
}

export function extractKuaishouTitle(value) {
  const scopes = kuaishouTitleScopes(value);
  const extractFromKeys = (keys) => {
    let sourceFallback = '';
    for (const scope of scopes) {
      for (const key of keys) {
        const raw = normalizeKuaishouTitleSource(scope[key]);
        if (!raw) continue;
        sourceFallback ||= raw;
        const cleaned = cleanKuaishouTitleCandidate(raw);
        if (cleaned) return cleaned;
      }
    }
    return sourceFallback;
  };

  // name 语义较弱：只有强标题字段全部没有源值时才使用，
  // 避免作者名覆盖纯话题作品标题。
  return extractFromKeys(KUAISHOU_TITLE_KEYS) || extractFromKeys(['name']);
}

function sanitizeFilename(value, maxLength = 160) {
  const text = String(value || 'kuaishou-export')
    .replace(/[\\/:*?"<>|\n\r\t]+/g, '-')
    .replace(/\s+/g, ' ')
    .trim();
  return (text || 'kuaishou-export').slice(0, maxLength);
}

async function ensureDir(dirPath) {
  await fs.mkdir(dirPath, { recursive: true });
}

function newProgressState() {
  return {
    platform: 'kuaishou',
    status: 'idle',
    phase: 'idle',
    message: '待机中',
    startedAt: null,
    finishedAt: null,
    updatedAt: new Date().toISOString(),
    totalWorks: 0,
    queuedWorks: 0,
    processedWorks: 0,
    successWorks: 0,
    skippedWorks: 0,
    failedWorks: 0,
    currentIndex: 0,
    currentWorkId: '',
    currentTitle: '',
    headless: CONFIG.headless,
  };
}

let progressState = newProgressState();

async function updateProgress(patch = {}) {
  progressState = {
    ...progressState,
    ...patch,
    updatedAt: new Date().toISOString(),
    headless: CONFIG.headless,
  };
  await ensureDir(path.dirname(CONFIG.progressPath));
  const tmpPath = CONFIG.progressPath + '.tmp';
  await fs.writeFile(tmpPath, JSON.stringify(progressState, null, 2), 'utf-8');
  await fs.rename(tmpPath, CONFIG.progressPath);
}

async function cleanProfileLocks(profileDir) {
  if (!CONFIG.cleanProfileLocks) return;
  const lockFiles = ['SingletonLock', 'SingletonCookie', 'SingletonSocket', 'RunningChromeVersion'];
  for (const file of lockFiles) {
    const target = path.join(profileDir, file);
    try {
      await fs.unlink(target);
    } catch {
      // ignore
    }
  }
}

async function visibleMarkerExists(page, selector) {
  const locator = page.locator(selector);
  const count = await locator.count();
  for (let index = 0; index < count; index += 1) {
    const candidate = typeof locator.nth === 'function' ? locator.nth(index) : locator.first();
    if (await candidate.isVisible().catch(() => false)) return true;
  }
  return false;
}

export async function classifyKuaishouAuthPageOnce(page) {
  try {
    const currentUrl = page.url();
    const loginUrlMarkers = ['/login', '/passport/', 'passport.kuaishou.com'];
    if (loginUrlMarkers.some((marker) => currentUrl.includes(marker))) return 'login_required';

    // Hidden SPA templates are not proof of expiry; only visible login controls count.
    const loginMarkers = [
      'text=立即登录',
      'text=扫码登录',
      'text=手机号登录',
      'text=验证码登录',
      'button:has-text("立即登录")',
      'button:has-text("扫码登录")',
      'iframe[src*="login"]',
      'iframe[src*="passport"]',
    ];
    for (const marker of loginMarkers) {
      if (await visibleMarkerExists(page, marker)) return 'login_required';
    }

    const dashboardMarkers = ['text=数据中心', 'text=内容管理', 'text=作品管理', 'text=发布作品', 'text=退出登录'];
    for (const marker of dashboardMarkers) {
      if (await visibleMarkerExists(page, marker)) return 'authorized';
    }
  } catch {
    // Navigation and renderer failures are transient, not proof that cookies expired.
  }
  return 'transient';
}

export async function classifyKuaishouAuthPage(page, options = {}) {
  const stableChecks = Math.max(2, Number.parseInt(options.stableChecks ?? '2', 10) || 2);
  const settleMs = Number.isFinite(options.settleMs)
    ? Math.max(0, options.settleMs)
    : Math.min(Math.max(CONFIG.scanPollMs, 500), 1500);
  let stableClassification = '';

  for (let index = 0; index < stableChecks; index += 1) {
    const current = await classifyKuaishouAuthPageOnce(page);
    if (current === 'transient') return 'transient';
    if (stableClassification && current !== stableClassification) return 'transient';
    stableClassification = current;
    if (index + 1 < stableChecks) await page.waitForTimeout(settleMs);
  }
  return stableClassification || 'transient';
}

async function hasStableLogin(page) {
  return (await classifyKuaishouAuthPage(page)) === 'authorized';
}

async function waitForLogin(page) {
  const deadline = Date.now() + CONFIG.scanWaitMs;
  while (Date.now() < deadline) {
    if (await hasStableLogin(page)) return true;
    await page.waitForTimeout(CONFIG.scanPollMs);
  }
  return false;
}

function kuaishouAuthError(classification) {
  if (classification === 'login_required') {
    return new Error('快手未登录（headless=true 无法扫码登录）');
  }
  return new Error('快手创作者数据页暂不可访问（登录态未作失效处理）');
}

async function gotoWorksPage(context, page) {
  const candidates = [CONFIG.articleUrl, CONFIG.worksUrl, CONFIG.homeUrl];
  return navigateAuthCandidates(context, page, candidates, { settleMs: 1500 });
}

async function ensureOnWorksPage(context, page) {
  page = await gotoWorksPage(context, page);

  // If we're on the marketing page, click login.
  const loginButton = page.locator('text=立即登录').first();
  if ((await loginButton.count()) > 0 && await loginButton.isVisible().catch(() => false)) {
    await loginButton.click({ timeout: 5000 }).catch(() => {});
    // Some flows open a login modal or a new route; allow time for the QR to appear.
    await page.waitForTimeout(1500);
  }

  let authClassification = await classifyKuaishouAuthPage(page);
  if (authClassification !== 'authorized') {
    await updateProgress({
      phase: 'login',
      message: authClassification === 'login_required'
        ? (CONFIG.headless
          ? '需要登录：请使用 headless=false 运行并扫码'
          : `等待扫码登录（最多 ${Math.round(CONFIG.scanWaitMs / 1000)} 秒）`)
        : '快手创作者数据页暂不可访问（登录态未作失效处理）',
    });

    if (CONFIG.headless) {
      throw kuaishouAuthError(authClassification);
    }

    const ok = await waitForLogin(page);
    if (!ok) {
      authClassification = await classifyKuaishouAuthPage(page);
      if (authClassification !== 'authorized') {
        if (authClassification === 'transient') throw kuaishouAuthError(authClassification);
        const currentUrl = page.url();
        const title = await page.title().catch(() => '');
        throw new Error(`快手登录超时（当前页：${currentUrl} ${title}）`);
      }
    }
  }

  if (!page.url().includes('/statistics/article')) {
    await page.goto(CONFIG.articleUrl, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(1500);
  }

  authClassification = await classifyKuaishouAuthPage(page);
  if (authClassification !== 'authorized') {
    if (authClassification === 'login_required' && !CONFIG.headless) {
      const currentUrl = page.url();
      const title = await page.title().catch(() => '');
      throw new Error(`快手登录超时（当前页：${currentUrl} ${title}）`);
    }
    throw kuaishouAuthError(authClassification);
  }
  return page;
}

function formatDate(value) {
  if (value === null || value === undefined || value === '') return '';
  if (typeof value === 'number') {
    const date = value > 1_000_000_000_000 ? new Date(value) : new Date(value * 1000);
    if (Number.isNaN(date.getTime())) return '';
    const yyyy = date.getFullYear();
    const mm = String(date.getMonth() + 1).padStart(2, '0');
    const dd = String(date.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
  }
  const text = String(value).trim();
  const m = text.match(/(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})/);
  if (!m) return '';
  const yyyy = m[1];
  const mm = String(Number.parseInt(m[2], 10)).padStart(2, '0');
  const dd = String(Number.parseInt(m[3], 10)).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

function parseDateValue(dateStr) {
  if (!dateStr) return null;
  const match = String(dateStr).trim().match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/);
  if (!match) return null;
  const year = Number.parseInt(match[1], 10);
  const month = Number.parseInt(match[2], 10);
  const day = Number.parseInt(match[3], 10);
  const date = new Date(year, month - 1, day);
  if (Number.isNaN(date.getTime())) return null;
  return date.getTime();
}

function meetsDateRange(dateStr) {
  const minTs = parseDateValue(CONFIG.minPublishDate);
  const maxTs = parseDateValue(CONFIG.maxPublishDate);
  const ts = parseDateValue(dateStr);
  if (!ts) return true;
  if (minTs && ts < minTs) return false;
  if (maxTs && ts > maxTs) return false;
  return true;
}

function toNumber(value) {
  if (value === null || value === undefined || value === '') return 0;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const text = String(value).replace(/[,_\s]/g, '').trim();
  if (!text) return 0;
  if (text.endsWith('万')) return Math.round(Number.parseFloat(text.slice(0, -1)) * 10000);
  if (text.endsWith('亿')) return Math.round(Number.parseFloat(text.slice(0, -1)) * 100000000);
  const parsed = Number.parseFloat(text.replace(/[^\d.\-]/g, ''));
  return Number.isFinite(parsed) ? parsed : 0;
}

function pick(obj, keys) {
  if (!obj || typeof obj !== 'object') return undefined;
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(obj, key)) return obj[key];
  }
  return undefined;
}

function findLargestArray(obj) {
  if (!obj || typeof obj !== 'object') return null;
  let best = null;
  let bestLen = 0;
  for (const value of Object.values(obj)) {
    if (Array.isArray(value) && value.length > bestLen) {
      best = value;
      bestLen = value.length;
    }
  }
  return best;
}

function findLargestArrayDeep(value, depth = 0) {
  if (depth > 6) return null;
  if (!value) return null;

  if (Array.isArray(value)) {
    let best = value;
    for (const item of value) {
      const found = findLargestArrayDeep(item, depth + 1);
      if (found && found.length > best.length) best = found;
    }
    return best;
  }

  if (typeof value !== 'object') return null;

  let best = null;
  for (const v of Object.values(value)) {
    const found = findLargestArrayDeep(v, depth + 1);
    if (found && (!best || found.length > best.length)) best = found;
  }
  return best;
}

function extractItems(payload) {
  if (!payload) return { items: [], total: 0 };
  if (Array.isArray(payload)) return { items: payload, total: payload.length };
  const data = payload.data ?? payload;
  const items = Array.isArray(data?.photoList?.photoItems)
    ? data.photoList.photoItems
    : Array.isArray(data?.photoList?.list)
      ? data.photoList.list
      : Array.isArray(data?.list)
        ? data.list
        : Array.isArray(data?.items)
          ? data.items
          : Array.isArray(data?.works)
            ? data.works
            : findLargestArray(data) ?? findLargestArrayDeep(data) ?? [];
  const total = toNumber(
    data?.photoList?.totalCount
    ?? data?.photoList?.total_count
    ?? data?.totalCount
    ?? data?.total_count
    ?? data?.total
    ?? data?.count
    ?? payload?.total
    ?? payload?.totalCount,
  ) || items.length;
  return { items, total };
}

function normalizeItem(item) {
  if (!item || typeof item !== 'object') return null;

  // /rest/cp/works/v2/video/pc/photo/list returns the list items with metrics on the root object.
  const stat = item.stat ?? item.stats ?? item.metric ?? item.metrics ?? item;

  const workId = pick(item, ['workId', 'publishId', 'photoId', 'id']) ?? '';
  const publishDate = formatDate(pick(item, ['uploadTime', 'createTime', 'createdAt', 'publishTime', 'timestamp']));

  const row = {
    平台: 'kuaishou',
    作品ID: String(workId ?? '').replace(/\.0$/, ''),
    标题: extractKuaishouTitle(item),
    发布日期: publishDate,
    曝光量: toNumber(pick(stat, ['impression', 'showCount', 'exposure', 'exposureCnt', 'exposureCount', 'pv'])),
    播放量: toNumber(pick(stat, ['playCount', 'viewCount', '播放量', 'play', 'views', 'vv'])),
    点赞量: toNumber(pick(stat, ['likeCount', '点赞量', 'like', 'likes'])),
    收藏量: toNumber(pick(stat, ['collectCount', 'favoriteCount', '收藏量', 'collect', 'favorites'])),
    评论量: toNumber(pick(stat, ['commentCount', '评论量', 'comment', 'comments', 'reply'])),
    分享量: toNumber(pick(stat, ['shareCount', '分享量', 'share', 'shares', 'forward'])),
    涨粉量: toNumber(pick(stat, ['followCount', 'newFollowCount', 'follow', 'followDelta', 'fansInc', 'fansIncrease'])),
  };

  if (!row.作品ID) return null;
  return row;
}

function formatPercentFromDecimal(value) {
  const num = typeof value === 'number' ? value : Number.parseFloat(String(value ?? '').replace(/[^\d.\-]/g, ''));
  if (!Number.isFinite(num)) return '';
  const percent = Math.abs(num) <= 1 ? num * 100 : num;
  const rounded = Math.round(percent * 100) / 100;
  return `${Number.isInteger(rounded) ? String(rounded) : String(rounded)}%`;
}

function normalizeArticleItem(item) {
  if (!item || typeof item !== 'object') return null;

  const workId = pick(item, ['photoId', 'workId', 'publishId', 'id']) ?? '';
  const publishDate = formatDate(pick(item, ['publishTime', 'uploadTime', 'createTime', 'createdAt', 'timestamp']));

  const row = {
    平台: 'kuaishou',
    作品ID: String(workId ?? '').replace(/\.0$/, ''),
    标题: extractKuaishouTitle(item),
    发布日期: publishDate,
    曝光量: 0,
    播放量: toNumber(pick(item, ['playCount', 'viewCount', '播放量', 'play', 'views', 'vv'])),
    点赞量: toNumber(pick(item, ['likeCount', '点赞量', 'like', 'likes'])),
    收藏量: toNumber(pick(item, ['collectCount', 'favoriteCount', '收藏量', 'collect', 'favorites'])),
    评论量: toNumber(pick(item, ['commentCount', '评论量', 'comment', 'comments', 'reply'])),
    分享量: toNumber(pick(item, ['shareCount', '分享量', 'share', 'shares', 'forward'])),
    涨粉量: toNumber(pick(item, ['followCount', 'newFollowCount', 'follow', 'followDelta', 'fansInc', 'fansIncrease'])),
    完播率: formatPercentFromDecimal(pick(item, ['fpr', 'finishPlayRate', 'completionRate', '完播率'])),
  };

  if (!row.作品ID) return null;
  return row;
}

function parseApiPhFromText(value) {
  const text = String(value || '');
  try {
    const parsed = JSON.parse(text);
    const apiPh = parsed?.['kuaishou.web.cp.api_ph'];
    if (apiPh) return String(apiPh);
  } catch {
    // fall through to regex parsing
  }
  const match = text.match(/"kuaishou\.web\.cp\.api_ph"\s*:\s*"([^"]+)"/);
  return match?.[1] || '';
}

async function fetchArticleListPage(page, apiPh, pageIndex, pageSize) {
  const response = await page.request.post(CONFIG.articleListApiUrl, {
    data: {
      orderType: 2,
      sortType: 1,
      type: 0,
      count: pageSize,
      page: pageIndex,
      'kuaishou.web.cp.api_ph': apiPh,
    },
  });
  if (!response.ok()) {
    throw new Error(`快手作品分析列表接口失败：HTTP ${response.status()}`);
  }
  return response.json();
}

function roundNumber(value, digits = 2) {
  const num = typeof value === 'number' ? value : Number.parseFloat(String(value ?? '').replace(/[^\d.\-]/g, ''));
  if (!Number.isFinite(num)) return '';
  const factor = 10 ** digits;
  return Math.round(num * factor) / factor;
}

function formatPercentValue(value) {
  const rounded = roundNumber(value, 2);
  return rounded === '' ? '' : `${rounded}%`;
}

function formatSecondsFromMillis(value) {
  const rounded = roundNumber((Number(value) || 0) / 1000, 1);
  return rounded === '' ? '' : `${rounded}s`;
}

function overviewMetrics(payload, tabType) {
  const list = payload?.data?.trendList;
  if (!Array.isArray(list)) return {};

  const metrics = {};
  for (const item of list) {
    if (!item || typeof item !== 'object') continue;
    const name = String(item.name ?? '').trim();
    const enName = String(item.enName ?? '').trim();
    const value = item.sumCount;
    if (!name && !enName) continue;

    if (tabType === 1) {
      if (enName === 'PLAY_CNT' || name === '播放量') metrics.播放量 = toNumber(value);
      else if (enName === 'AVG_PLAY_DURATION' || name === '平均播放时长') {
        metrics.平均播放时长 = formatSecondsFromMillis(value);
        metrics.平均观看时长 = metrics.平均播放时长;
      } else if (enName === 'OUTSIDE_CTR' || name === '封面点击率') metrics.封面点击率 = formatPercentValue(value);
      else if (enName === 'TWO_SECONDS_EXIT' || name === '2秒跳出率') {
        metrics['2s跳出率'] = formatPercentValue(value);
        metrics.跳出率口径 = '2s';
      } else if (enName === 'FIVE_SECONDS_FPR' || name === '5秒完播率') metrics['5s完播率'] = formatPercentValue(value);
      else if (enName === 'FPR' || name === '完播率') metrics.完播率 = formatPercentValue(value);
    } else if (tabType === 2) {
      if (enName === 'LIKE_CNT' || name === '点赞量') metrics.点赞量 = toNumber(value);
      else if (enName === 'COMMENT_CNT' || name === '评论量') metrics.评论量 = toNumber(value);
      else if (enName === 'SHARE_CNT' || name === '分享量') metrics.分享量 = toNumber(value);
      else if (enName === 'COLLECT_CNT' || name === '收藏量') metrics.收藏量 = toNumber(value);
      else if (enName === 'FOLLOW_CNT' || name === '涨粉量') metrics.涨粉量 = toNumber(value);
    }
  }

  return metrics;
}

async function waitForOverview(page, workId, tabType) {
  const response = await page.waitForResponse((candidate) => {
    if (candidate.status() !== 200) return false;
    if (!candidate.url().includes('/rest/cp/creator/analysis/pc/photo/single/overview')) return false;
    const body = candidate.request().postData() || '';
    return body.includes(`"photoId":"${workId}"`) && body.includes(`"tabType":${tabType}`);
  }, { timeout: CONFIG.detailWaitMs });
  return response.json();
}

async function parseDetailExport(filePath) {
  const { stdout } = await execFileAsync(CONFIG.pythonBin, [
    CONFIG.detailNormalizerScriptPath,
    '--input',
    filePath,
  ], { encoding: 'utf-8', maxBuffer: 1024 * 1024 });
  return JSON.parse(stdout || '{}');
}

async function fetchExportTasks(page, apiPh) {
  if (!apiPh) throw new Error('快手导出任务列表缺少 api_ph');
  const response = await page.request.post(CONFIG.exportTaskListApiUrl, {
    data: {
      page: 1,
      count: 1000,
      'kuaishou.web.cp.api_ph': apiPh,
    },
  });
  if (!response.ok()) {
    throw new Error(`快手导出任务列表请求失败：HTTP ${response.status()}`);
  }
  const payload = await response.json().catch(() => null);
  const list = payload?.data?.list;
  if (!Array.isArray(list)) throw new Error('快手导出任务列表结构异常');
  return list;
}

function exportTaskId(task) {
  return String(task?.taskId ?? '').trim();
}

function exportCreateRequestBody(response) {
  const request = response?.request?.();
  if (!request) return null;
  try {
    return request.postDataJSON();
  } catch {
    try {
      return JSON.parse(request.postData() || 'null');
    } catch {
      return null;
    }
  }
}

export function isExpectedKuaishouExportCreateResponse(response, workId) {
  const request = response?.request?.();
  if (!request || request.method() !== 'POST') return false;
  if (!response.url().includes(KUAISHOU_EXPORT_CREATE_API_PATH)) return false;
  const body = exportCreateRequestBody(response);
  return String(body?.workId ?? '') === String(workId ?? '') && Number(body?.exportType) === 2;
}

export function extractKuaishouExportTaskId(payload) {
  const candidates = [
    payload?.data?.taskId,
    payload?.data?.exportTaskId,
    payload?.taskId,
    payload?.exportTaskId,
  ];
  for (const value of candidates) {
    const taskId = String(value ?? '').trim();
    if (taskId) return taskId;
  }
  return '';
}

export async function waitForUniqueNewCompletedExportTask(
  page,
  apiPh,
  beforeTaskIds,
  expectedFilenamePart,
  {
    fetchTasks = fetchExportTasks,
    timeoutMs = CONFIG.detailExportTimeoutMs,
    pollMs = 1500,
    expectedTaskId = '',
    confirmationPolls = 2,
  } = {},
) {
  const baselineIds = beforeTaskIds instanceof Set ? beforeTaskIds : new Set(beforeTaskIds || []);
  const deadline = Date.now() + timeoutMs;
  let boundTaskId = String(expectedTaskId || '').trim();
  let completedObservations = 0;
  const observedNewTaskIds = new Set();
  while (Date.now() < deadline) {
    const tasks = await fetchTasks(page, apiPh);
    if (!Array.isArray(tasks)) throw new Error('快手导出任务列表结构异常');

    if (!expectedTaskId) {
      for (const task of tasks) {
        const taskId = exportTaskId(task);
        if (taskId && !baselineIds.has(taskId)) observedNewTaskIds.add(taskId);
      }
      if (observedNewTaskIds.size > 1) {
        throw new Error(`快手新增任务不唯一：${[...observedNewTaskIds].join(', ')}`);
      }
      if (!boundTaskId && observedNewTaskIds.size === 1) boundTaskId = [...observedNewTaskIds][0];
    }

    if (boundTaskId) {
      const task = tasks.find((candidate) => exportTaskId(candidate) === boundTaskId);
      if (task && Number(task.status) === 3) {
        const filename = String(task.filename || '');
        if (!filename.includes(expectedFilenamePart)) {
          throw new Error(`快手新增任务文件名异常：${filename || '(empty)'}`);
        }
        // 创建响应含 taskId 时可精确返回；无 taskId 时多轮确认，
        // 给同时创建的其他任务留出现窗口，一旦歧义就显式失败。
        completedObservations += 1;
        if (expectedTaskId || completedObservations > confirmationPolls) return task;
      } else {
        completedObservations = 0;
      }
    }
    await page.waitForTimeout(pollMs);
  }
  throw new Error(`等待快手新增导出任务完成超时：${expectedFilenamePart}`);
}

export async function saveCompletedExportTask(page, task, row, kind, apiPh) {
  const taskId = String(task?.taskId ?? '').trim();
  if (!taskId) throw new Error('快手导出任务缺少 taskId');

  const response = await page.request.get(CONFIG.exportTaskDownloadApiUrl, {
    params: {
      taskId,
      'kuaishou.web.cp.api_ph': apiPh,
    },
    timeout: CONFIG.detailExportTimeoutMs,
  });
  if (!response.ok()) {
    throw new Error(`快手导出文件下载失败：HTTP ${response.status()}`);
  }

  const body = await response.body();
  if (!body?.length) throw new Error('快手导出文件下载结果为空');

  const suggested = sanitizeFilename(task?.filename || `${row.作品ID}-作品数据分析明细表.xlsx`, 100);
  const targetPath = path.join(
    CONFIG.detailExportDir,
    sanitizeFilename(`ks-detail-${row.作品ID}-${kind}-${Date.now()}-${suggested}`, 180),
  );
  await fs.writeFile(targetPath, body);
  return targetPath;
}

async function exportDetailFile(detailPage, row, kind, apiPh) {
  await ensureDir(CONFIG.detailExportDir);
  const beforeTasks = await fetchExportTasks(detailPage, apiPh);
  const beforeTaskIds = new Set(beforeTasks.map(exportTaskId).filter(Boolean));
  const exportButton = detailPage.getByText('导出数据', { exact: true }).first();
  if ((await exportButton.count().catch(() => 0)) <= 0) {
    throw new Error('详情页未找到“导出数据”按钮');
  }

  const [createResponse] = await Promise.all([
    detailPage.waitForResponse(
      (response) => isExpectedKuaishouExportCreateResponse(response, row.作品ID),
      { timeout: CONFIG.detailExportTimeoutMs },
    ),
    exportButton.click({ timeout: 10000 }),
  ]);
  if (!createResponse.ok()) {
    throw new Error(`快手详情导出创建失败：HTTP ${createResponse.status()}`);
  }
  const createPayload = await createResponse.json().catch(() => null);
  const expectedTaskId = extractKuaishouExportTaskId(createPayload);
  const task = await waitForUniqueNewCompletedExportTask(
    detailPage,
    apiPh,
    beforeTaskIds,
    '作品数据分析明细表',
    { expectedTaskId },
  );
  return saveCompletedExportTask(detailPage, task, row, kind, apiPh);
}

async function collectDetailMetrics(detailPage, row, index, total, apiPh) {
  const workId = String(row.作品ID ?? '').trim();
  if (!workId) return { 详情采集状态: 'skipped_no_work_id', 详情采集错误: '作品ID为空' };

  await updateProgress({
    phase: 'details',
    message: `采集快手详情数据：${index}/${total} ${row.标题 || workId}`,
    totalWorks: total,
    processedWorks: index - 1,
    queuedWorks: Math.max(0, total - index + 1),
    currentIndex: index,
    currentWorkId: workId,
    currentTitle: row.标题 || '',
  });

  const detailUrl = `${CONFIG.detailUrlBase}/${encodeURIComponent(workId)}`;
  const corePromise = waitForOverview(detailPage, workId, 1).catch(e => e);
  await detailPage.goto(detailUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  const coreResult = await corePromise;
  if (coreResult instanceof Error) throw coreResult;
  const coreMetrics = overviewMetrics(coreResult, 1);
  const files = [];
  const errors = [];
  let exportMetrics = {};

  if (CONFIG.detailExportEnabled) {
    try {
      const playTab = detailPage.getByText('播放数据', { exact: true }).first();
      await playTab.click({ timeout: 10000 }).catch(() => {});
      const playFile = await exportDetailFile(detailPage, row, 'play', apiPh);
      files.push({ kind: 'play', path: playFile });
    } catch (error) {
      errors.push(`play: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  const interactionPromise = waitForOverview(detailPage, workId, 2).catch(e => e);
  const interactionTab = detailPage.getByText('互动效果', { exact: true }).last();
  await interactionTab.click({ timeout: 10000 });
  const interactionResult = await interactionPromise;
  if (interactionResult instanceof Error) throw interactionResult;
  const interactionMetrics = overviewMetrics(interactionResult, 2);

  if (CONFIG.detailExportEnabled) {
    try {
      const interactionFile = await exportDetailFile(detailPage, row, 'interaction', apiPh);
      files.push({ kind: 'interaction', path: interactionFile });
    } catch (error) {
      errors.push(`interaction: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  for (const file of files) {
    try {
      const metrics = await parseDetailExport(file.path);
      exportMetrics = { ...exportMetrics, ...metrics };
    } catch (error) {
      errors.push(`${file.kind}: 解析失败 ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  const exportedKinds = [...new Set(files.map((file) => file.kind))];
  let detailStatus = CONFIG.detailExportEnabled ? 'export_failed_page_fallback' : 'overview_core_interaction';
  if (exportedKinds.includes('play') && exportedKinds.includes('interaction')) {
    detailStatus = 'exported_play_interaction';
  } else if (exportedKinds.length > 0) {
    detailStatus = `partial_exported_${exportedKinds.join('_')}`;
  }

  return {
    ...exportMetrics,
    ...coreMetrics,
    ...interactionMetrics,
    详情采集状态: detailStatus,
    详情采集错误: errors.join('；'),
  };
}

async function loadCachedRowsById() {
  try {
    const raw = await fs.readFile(CONFIG.tempRowsPath, 'utf-8');
    const rows = JSON.parse(raw);
    if (!Array.isArray(rows)) return new Map();
    const out = new Map();
    for (const row of rows) {
      const workId = String(row?.作品ID || '').trim();
      if (workId) out.set(workId, row);
    }
    return out;
  } catch {
    return new Map();
  }
}

function shouldRefreshWork(row, index) {
  if (CONFIG.forceFullExport) return true;
  if (CONFIG.refreshLatestCount > 0 && index < CONFIG.refreshLatestCount) return true;
  if (CONFIG.refreshDays > 0) {
    const publishTs = parseDateValue(row?.发布日期);
    const refreshAfter = Date.now() - CONFIG.refreshDays * 24 * 3600 * 1000;
    if (publishTs && publishTs >= refreshAfter) return true;
  }
  return false;
}

async function enrichRowsWithDetailMetrics(context, rows, apiPh) {
  if (rows.length <= 0) {
    return {
      rows: [],
      metrics: { totalWorks: 0, processedWorks: 0, successWorks: 0, skippedWorks: 0, failedWorks: 0 },
    };
  }

  const cachedRows = await loadCachedRowsById();
  const enrichedRows = [];
  let detailPage = null;
  let successCount = 0;
  let failedCount = 0;
  let skippedCount = 0;

  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    const workId = String(row?.作品ID || '').trim();
    const cached = workId ? cachedRows.get(workId) : null;
    if (cached && !shouldRefreshWork(row, index)) {
      skippedCount += 1;
      enrichedRows.push(cached);
      await updateProgress({
        phase: 'details',
        message: `跳过已存在的快手作品：${row.标题 || workId}`,
        totalWorks: rows.length,
        processedWorks: index + 1,
        successWorks: successCount,
        skippedWorks: skippedCount,
        failedWorks: failedCount,
        queuedWorks: Math.max(0, rows.length - index - 1),
        currentIndex: index + 1,
        currentWorkId: workId,
        currentTitle: row.标题 || '',
      });
      continue;
    }

    if (!detailPage) {
      detailPage = await prepareAuthPage(context, await context.newPage());
    }

    try {
      const detailMetrics = await collectDetailMetrics(detailPage, row, index + 1, rows.length, apiPh);
      successCount += detailMetrics.详情采集状态 === 'failed' ? 0 : 1;
      enrichedRows.push({ ...row, ...detailMetrics });
    } catch (error) {
      failedCount += 1;
      const message = error instanceof Error ? error.message : String(error);
      enrichedRows.push({
        ...row,
        详情采集状态: 'failed',
        详情采集错误: message,
      });
    }

    await updateProgress({
      phase: 'details',
      message: `快手详情数据采集中：${index + 1}/${rows.length}`,
      totalWorks: rows.length,
      processedWorks: index + 1,
      successWorks: successCount,
      skippedWorks: skippedCount,
      failedWorks: failedCount,
      queuedWorks: Math.max(0, rows.length - index - 1),
    });

    if (index < rows.length - 1) {
      await detailPage.waitForTimeout(500 + Math.floor(Math.random() * 500));
    }
  }

  if (detailPage) await detailPage.close().catch(() => {});
  return {
    rows: enrichedRows,
    metrics: {
      totalWorks: rows.length,
      processedWorks: rows.length,
      successWorks: successCount,
      skippedWorks: skippedCount,
      failedWorks: failedCount,
    },
  };
}

function sanitizeHeaders(headers) {
  const out = {};
  if (!headers || typeof headers !== 'object') return out;

  const allow = new Set([
    'accept',
    'accept-language',
    'content-type',
    'origin',
    'referer',
    'user-agent',
    'x-requested-with',
    'sec-ch-ua',
    'sec-ch-ua-platform',
    'sec-ch-ua-mobile',
  ]);

  for (const [k, v] of Object.entries(headers)) {
    const key = String(k || '').toLowerCase();
    if (!key) continue;
    if (key === 'cookie' || key === 'host' || key === 'content-length') continue;
    if (allow.has(key) || key.startsWith('x-') || key.startsWith('ks-') || key.startsWith('k-')) {
      out[key] = v;
    }
  }
  return out;
}

function deepClone(value) {
  try {
    return JSON.parse(JSON.stringify(value));
  } catch {
    return value;
  }
}

function updatePagination(body, pageIndex, pageSize, { zeroBasedPage = true } = {}) {
  const obj = deepClone(body || {});
  const pageKeys = ['page', 'pageNum', 'pn', 'currentPage', 'pageNo', 'current', 'page_index', 'pageIndex'];
  const sizeKeys = ['pageSize', 'size', 'ps', 'limit', 'count', 'page_size', 'pageCount'];

  let foundPage = false;
  let foundSize = false;

  const visit = (node, depth = 0) => {
    if (!node || typeof node !== 'object' || depth > 4) return;
    for (const [k, v] of Object.entries(node)) {
      if (pageKeys.includes(k) && (typeof v === 'number' || /^\d+$/.test(String(v)))) {
        node[k] = zeroBasedPage ? pageIndex : pageIndex + 1;
        foundPage = true;
      }
      if (sizeKeys.includes(k) && (typeof v === 'number' || /^\d+$/.test(String(v)))) {
        node[k] = pageSize;
        foundSize = true;
      }
      if (v && typeof v === 'object') visit(v, depth + 1);
    }
  };

  visit(obj);

  if (!foundPage && obj && typeof obj === 'object' && Object.prototype.hasOwnProperty.call(obj, 'offset')) {
    obj.offset = Math.max(0, pageIndex * pageSize);
    foundPage = true;
    if (!Object.prototype.hasOwnProperty.call(obj, 'limit')) {
      obj.limit = pageSize;
      foundSize = true;
    }
  }

  return { body: obj, foundPage, foundSize };
}

async function writeExcel(rows) {
  await ensureDir(path.dirname(CONFIG.tempRowsPath));
  await fs.writeFile(CONFIG.tempRowsPath, JSON.stringify(rows, null, 2), 'utf-8');

  await execFileAsync(CONFIG.pythonBin, [
    CONFIG.writerScriptPath,
    '--input',
    CONFIG.tempRowsPath,
    '--output',
    CONFIG.outputPath,
    '--columns',
    [
      '平台',
      '作品ID',
      '标题',
      '发布日期',
      '曝光量',
      '播放量',
      '点赞量',
      '收藏量',
      '评论量',
      '分享量',
      '涨粉量',
      '平均观看时长',
      '平均播放时长',
      '封面点击率',
      '2s跳出率',
      '跳出率口径',
      '5s完播率',
      '完播率',
      '详情采集状态',
      '详情采集错误',
    ].join(','),
    '--dedupe-key',
    '作品ID',
  ]);
}

async function scrapeWorks(context, page) {
  await updateProgress({ phase: 'collecting', message: '进入快手作品分析页' });

  const rowsById = new Map();
  let totalCount = 0;
  let apiPh = '';
  let initialPayload = null;

  const handleRequest = (request) => {
    const url = request.url();
    if (!url.includes('/rest/cp/creator/analysis/pc/photo/list')) return;
    apiPh ||= parseApiPhFromText(request.postData() || '');
  };
  const handleResponse = async (response) => {
    const url = response.url();
    if (!url.includes('/rest/cp/creator/analysis/pc/photo/list')) return;
    if (response.status() !== 200) return;

    let payload = null;
    try {
      payload = await response.json();
    } catch {
      return;
    }

    const req = response.request();
    apiPh ||= parseApiPhFromText(req.postData() || '');
    initialPayload ||= payload;

    const { items, total } = extractItems(payload);
    totalCount = total || totalCount;
    for (const item of items) {
      const row = normalizeArticleItem(item);
      if (!row) continue;
      if (!meetsDateRange(row.发布日期)) continue;
      rowsById.set(row.作品ID, row);
    }
  };

  page.on('request', handleRequest);
  page.on('response', handleResponse);

  page = await ensureOnWorksPage(context, page);
  await page.waitForTimeout(4500);

  const waitDeadline = Date.now() + 25000;
  while ((!initialPayload || !apiPh) && Date.now() < waitDeadline) {
    await page.waitForTimeout(500);
  }

  page.off('request', handleRequest);
  page.off('response', handleResponse);

  if (!initialPayload) {
    throw new Error('未捕获快手作品分析列表接口（请确认已进入「数据中心 > 作品分析」页面）');
  }
  if (!apiPh) {
    throw new Error('未捕获快手作品分析接口 api_ph，无法稳定分页拉取');
  }

  const pageSize = 10;
  const targetTotal = CONFIG.videoLimit > 0 ? Math.min(totalCount || CONFIG.videoLimit, CONFIG.videoLimit) : (totalCount || 0);

  await updateProgress({
    phase: 'collecting',
    message: `已捕获作品分析接口，开始分页拉取（预计 ${targetTotal || totalCount || '未知'} 条）`,
    totalWorks: targetTotal || totalCount || 0,
    processedWorks: rowsById.size,
    successWorks: rowsById.size,
    queuedWorks: Math.max(0, (targetTotal || totalCount || 0) - rowsById.size),
  });

  const maxPages = Math.max(
    1,
    Math.ceil((targetTotal || totalCount || CONFIG.videoLimit || pageSize) / pageSize) + 2,
  );
  for (let pageIndex = 0; pageIndex < maxPages; pageIndex += 1) {
    if (CONFIG.videoLimit > 0 && rowsById.size >= CONFIG.videoLimit) break;
    if (targetTotal && rowsById.size >= targetTotal) break;

    const before = rowsById.size;
    const payload = pageIndex === 0 ? initialPayload : await fetchArticleListPage(page, apiPh, pageIndex, pageSize);
    const { items, total } = extractItems(payload);
    totalCount = total || totalCount;
    for (const item of items) {
      const row = normalizeArticleItem(item);
      if (!row) continue;
      if (!meetsDateRange(row.发布日期)) continue;
      rowsById.set(row.作品ID, row);
    }
    const after = rowsById.size;
    const newCount = Math.max(0, after - before);

    await updateProgress({
      phase: 'collecting',
      message: `作品分析分页拉取中：第 ${pageIndex + 1} 页，新增 ${newCount} 条，累计 ${rowsById.size}`,
      totalWorks: targetTotal || totalCount || rowsById.size,
      processedWorks: rowsById.size,
      successWorks: rowsById.size,
      queuedWorks: Math.max(0, (targetTotal || totalCount || rowsById.size) - rowsById.size),
      currentIndex: rowsById.size,
    });

    if (items.length <= 0) break;
    if (pageIndex > 0) {
      await page.waitForTimeout(500 + Math.floor(Math.random() * 500));
    }
  }

  return { rows: Array.from(rowsById.values()), apiPh };
}

async function main() {
  await updateProgress({
    ...newProgressState(),
    status: 'running',
    phase: 'boot',
    message: '快手任务启动',
    startedAt: new Date().toISOString(),
    finishedAt: null,
  });

  await ensureDir(CONFIG.userDataDir);
  await cleanProfileLocks(CONFIG.userDataDir);

  const context = await chromium.launchPersistentContext(CONFIG.userDataDir, {
    ...(CONFIG.browserExecutablePath
      ? { executablePath: CONFIG.browserExecutablePath }
      : (CONFIG.browserChannel === 'chromium' ? {} : { channel: CONFIG.browserChannel })),
    headless: CONFIG.headless,
    acceptDownloads: true,
    viewport: { width: 1480, height: 960 },
    args: ['--disable-blink-features=AutomationControlled'],
  });

  let page = await prepareAuthPage(context, context.pages()[0]);

  try {
    await updateProgress({ phase: 'login', message: '检查登录状态' });
    page = await ensureOnWorksPage(context, page);

    if (CONFIG.authOnly) {
      await updateProgress({
        status: 'completed',
        phase: 'done',
        message: '快手登录完成（AUTH_ONLY）',
        auth_status: 'authorized',
        auth_reason: '',
        needs_auth: false,
        finishedAt: new Date().toISOString(),
        totalWorks: 0,
        queuedWorks: 0,
        processedWorks: 0,
        successWorks: 0,
        skippedWorks: 0,
        failedWorks: 0,
        currentIndex: 0,
        currentWorkId: '',
        currentTitle: '',
      });
      return;
    }

    const { rows, apiPh } = await scrapeWorks(context, page);

    const finalRows = CONFIG.videoLimit > 0 ? rows.slice(0, CONFIG.videoLimit) : rows;

    const detailResult = await enrichRowsWithDetailMetrics(context, finalRows, apiPh);
    const enrichedRows = detailResult.rows;
    const metrics = detailResult.metrics;

    await updateProgress({
      phase: 'merging',
      message: `写入快手总表（${enrichedRows.length} 条）`,
      totalWorks: enrichedRows.length,
      processedWorks: enrichedRows.length,
      queuedWorks: 0,
      successWorks: metrics.successWorks,
      failedWorks: metrics.failedWorks,
      skippedWorks: metrics.skippedWorks,
    });

    await writeExcel(enrichedRows);

    await updateProgress({
      status: 'completed',
      phase: 'done',
      message: metrics.skippedWorks > 0 && metrics.successWorks === 0 && metrics.failedWorks === 0
        ? `本轮没有新增采集，已沿用已有 ${metrics.skippedWorks} 条快手本地结果`
        : metrics.failedWorks > 0
        ? `快手任务完成，共 ${enrichedRows.length} 条，${metrics.failedWorks} 条详情失败`
        : `快手任务完成，共 ${enrichedRows.length} 条`,
      finishedAt: new Date().toISOString(),
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await updateProgress({
      status: 'failed',
      phase: 'failed',
      message: `快手链路失败：${message}`,
      finishedAt: new Date().toISOString(),
    });
    throw error;
  } finally {
    await context.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.error(`[ks-error] ${error instanceof Error ? error.message : String(error)}`);
    process.exit(1);
  });
}
