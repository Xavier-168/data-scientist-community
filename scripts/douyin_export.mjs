import fs from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { execFile } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { chromium } from 'playwright';
import {
  isTransientNavigationError,
  navigateAuthCandidates,
  prepareAuthPage,
} from './browser_auth_utils.mjs';
import { resolveDownloadsDir, resolveProfileDir } from './runtime_paths.mjs';
import { cleanCollectedTitle } from './title_cleanup_utils.mjs';

const DEFAULT_PYTHON_BIN = process.platform === 'win32' ? 'python' : 'python3';
const DEFAULT_BROWSER_CHANNEL = process.env.BROWSER_CHANNEL ?? 'chrome';
const DEFAULT_DOWNLOAD_DIR = resolveDownloadsDir();
const DEFAULT_NAVIGATION_RETRY_DELAYS_MS = [3000, 8000];

function parseRetryDelays(value, fallback = DEFAULT_NAVIGATION_RETRY_DELAYS_MS) {
  const parsed = String(value ?? '')
    .split(',')
    .map((item) => Number.parseInt(item.trim(), 10))
    .filter((item) => Number.isFinite(item) && item >= 0);
  return parsed.length > 0 ? parsed : [...fallback];
}

const CONFIG = {
  contentManageUrl: 'https://creator.douyin.com/creator-micro/content/manage',
  downloadDir: process.env.DOWNLOAD_DIR
    ? path.resolve(process.env.DOWNLOAD_DIR)
    : DEFAULT_DOWNLOAD_DIR,
  browserChannel: DEFAULT_BROWSER_CHANNEL,
  browserExecutablePath: String(process.env.BROWSER_EXECUTABLE_PATH ?? '').trim(),
  userDataDir: process.env.USER_DATA_DIR
    ? path.resolve(process.env.USER_DATA_DIR)
    : resolveProfileDir('douyin', DEFAULT_BROWSER_CHANNEL),
  headless: (process.env.HEADLESS ?? 'false') === 'true',
  videoLimit: Number.parseInt(process.env.VIDEO_LIMIT ?? '10', 10),
  scanWaitMs: Number.parseInt(process.env.SCAN_WAIT_MS ?? '300000', 10),
  scanPollMs: Number.parseInt(process.env.SCAN_POLL_MS ?? '2000', 10),
  downloadTimeoutMs: Number.parseInt(process.env.DOWNLOAD_TIMEOUT_MS ?? '120000', 10),
  slowMoMs: Number.parseInt(process.env.SLOW_MO_MS ?? '0', 10),
  listLoadTimeoutMs: Number.parseInt(process.env.LIST_LOAD_TIMEOUT_MS ?? '60000', 10),
  navigationRetryAttempts: Math.max(
    1,
    Number.parseInt(process.env.NAVIGATION_RETRY_ATTEMPTS ?? '3', 10) || 3,
  ),
  navigationRetryDelaysMs: parseRetryDelays(process.env.NAVIGATION_RETRY_DELAYS_MS),
  detailNavigationTimeoutMs: Math.max(
    5000,
    Number.parseInt(process.env.DETAIL_NAVIGATION_TIMEOUT_MS ?? '30000', 10) || 30000,
  ),
  navigationFailureCooldownMs: Math.max(
    0,
    Number.parseInt(process.env.NAVIGATION_FAILURE_COOLDOWN_MS ?? '15000', 10) || 0,
  ),
  maxConsecutiveNavigationFailures: Math.max(
    1,
    Number.parseInt(process.env.MAX_CONSECUTIVE_NAVIGATION_FAILURES ?? '2', 10) || 2,
  ),
  workIntervalMs: Math.max(
    0,
    Number.parseInt(process.env.WORK_INTERVAL_MS ?? '2000', 10) || 0,
  ),
  startIndex: Number.parseInt(process.env.START_INDEX ?? '0', 10),
  minPublishDate: process.env.MIN_PUBLISH_DATE ?? '',
  maxPublishDate: process.env.MAX_PUBLISH_DATE ?? '',
  refreshDays: Number.parseInt(process.env.REFRESH_DAYS ?? '0', 10),
  refreshLatestCount: Number.parseInt(process.env.REFRESH_LATEST_COUNT ?? '0', 10),
  forceFullExport: (process.env.FORCE_FULL_EXPORT ?? 'false') === 'true',
  staleRoundsLimit: Number.parseInt(process.env.STALE_ROUNDS_LIMIT ?? '3', 10),
  paginationStallRoundsLimit: Math.max(
    4,
    Number.parseInt(process.env.PAGINATION_STALL_ROUNDS_LIMIT ?? '8', 10) || 8,
  ),
  // Controls how many merged files are included in the master excel (`all_videos.xlsx`).
  // Default is 0 (= no limit, include all merged files).
  masterLimit: Number.parseInt(process.env.MASTER_LIMIT ?? '0', 10),
  summaryPath: process.env.SUMMARY_PATH
    ? path.resolve(process.env.SUMMARY_PATH)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'summary.csv'),
  mergeScriptPath: process.env.MERGE_SCRIPT_PATH
    ? path.resolve(process.env.MERGE_SCRIPT_PATH)
    : path.resolve('scripts', 'merge_exports.py'),
  mergeAllScriptPath: process.env.MERGE_ALL_SCRIPT_PATH
    ? path.resolve(process.env.MERGE_ALL_SCRIPT_PATH)
    : path.resolve('scripts', 'merge_all_videos.py'),
  masterPath: process.env.MASTER_PATH
    ? path.resolve(process.env.MASTER_PATH)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'all_videos.xlsx'),
  progressPath: process.env.PROGRESS_PATH
    ? path.resolve(process.env.PROGRESS_PATH)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'douyin_progress.json'),
  statePath: process.env.STATE_PATH
    ? path.resolve(process.env.STATE_PATH)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'processed_ids.json'),
  cleanProfileLocks: (process.env.CLEAN_PROFILE_LOCKS ?? 'true') === 'true',
  authOnly: (process.env.AUTH_ONLY ?? 'false') === 'true',
  pythonBin: process.env.PYTHON_BIN ?? DEFAULT_PYTHON_BIN,
};

const SELECTORS = {
  exportButtonRole: 'button',
  exportButtonName: '导出',
  tabTraffic: '流量分析',
  tabOverview: '总览',
  videoCard: 'div[class*="video-card-"][class*="video-card-new"]',
};

const UNAVAILABLE_MARKERS = [
  '不可查看数据',
  '暂不可查看',
  '暂无数据',
  '暂无权限',
  '无法查看数据',
];

const EMPTY_WORK_LIST_MARKERS = [
  '暂无作品',
  '暂无内容',
  '还没有作品',
  '还没有发布作品',
  '去发布作品',
];

function sanitizeFilename(name, maxLength = 80) {
  return name
    .replace(/[\\/:*?"<>|]+/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, maxLength);
}

async function defaultSleep(milliseconds) {
  if (milliseconds <= 0) return;
  await new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export async function retryTransientOperation(operation, options = {}) {
  const maxAttempts = Math.max(1, Number.parseInt(options.maxAttempts ?? '1', 10) || 1);
  const retryDelaysMs = Array.isArray(options.retryDelaysMs)
    ? options.retryDelaysMs.map((item) => Math.max(0, Number(item) || 0))
    : [];
  const sleep = typeof options.sleep === 'function' ? options.sleep : defaultSleep;
  const shouldRetry = typeof options.shouldRetry === 'function'
    ? options.shouldRetry
    : isTransientNavigationError;
  const onRetry = typeof options.onRetry === 'function' ? options.onRetry : async () => {};

  let lastError = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      return await operation(attempt);
    } catch (error) {
      lastError = error instanceof Error ? error : new Error(String(error));
      if (attempt >= maxAttempts || !shouldRetry(lastError)) {
        lastError.attempts = attempt;
        throw lastError;
      }
      const delayMs = retryDelaysMs[Math.min(attempt - 1, Math.max(retryDelaysMs.length - 1, 0))] || 0;
      await onRetry({ attempt, nextAttempt: attempt + 1, delayMs, error: lastError });
      await sleep(delayMs);
    }
  }
  throw lastError || new Error('transient_operation_failed');
}

function sanitizeBaseName(name, maxLength = 100) {
  return sanitizeFilename(name, maxLength);
}

function csvEscape(value) {
  const text = String(value ?? '');
  if (text.includes(',') || text.includes('"') || text.includes('\n')) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

function cleanTitle(value) {
  return cleanCollectedTitle(value);
}

function formatDateFromUnix(ts) {
  if (!ts || Number.isNaN(ts)) return 'unknown-date';
  const date = new Date(Number(ts) * 1000);
  if (Number.isNaN(date.getTime())) return 'unknown-date';
  const yyyy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const dd = String(date.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

function parseDateValue(dateStr) {
  if (!dateStr) return null;
  const cleaned = String(dateStr).trim();
  const match = cleaned.match(/^(\d{4})[./-](\d{1,2})[./-](\d{1,2})$/);
  if (!match) return null;
  const year = Number.parseInt(match[1], 10);
  const month = Number.parseInt(match[2], 10);
  const day = Number.parseInt(match[3], 10);
  if (!year || !month || !day) return null;
  const date = new Date(year, month - 1, day);
  if (Number.isNaN(date.getTime())) return null;
  return date.getTime();
}

function normalizeDate(dateStr) {
  const ts = parseDateValue(dateStr);
  if (!ts) return '';
  const date = new Date(ts);
  const yyyy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const dd = String(date.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
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

function extractWorkId(url) {
  const match = url.match(/work-detail\/(\d+)/);
  return match ? match[1] : 'unknown-work';
}

function buildDetailUrl(id) {
  return `https://creator.douyin.com/creator-micro/work-management/work-detail/${id}?enter_from=content`;
}

async function ensureDir(dirPath) {
  await fs.mkdir(dirPath, { recursive: true });
}

function newProgressState() {
  const now = new Date().toISOString();
  return {
    platform: 'douyin',
    status: 'idle',
    phase: 'idle',
    message: '待机中',
    startedAt: null,
    finishedAt: null,
    updatedAt: now,
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

async function renameWithBusyRetry(tmpPath, targetPath) {
  // Windows 上目标正被 Python supervisor 轮询读取时 rename 抛 EPERM，短暂重试
  for (let attempt = 0; ; attempt += 1) {
    try {
      await fs.rename(tmpPath, targetPath);
      return;
    } catch (error) {
      const code = error && error.code;
      const retryable = code === 'EPERM' || code === 'EACCES' || code === 'EBUSY';
      if (!retryable || attempt >= 3) throw error;
      await new Promise((resolve) => setTimeout(resolve, 120));
    }
  }
}

async function updateProgress(patch = {}) {
  progressState = {
    ...progressState,
    ...patch,
    updatedAt: new Date().toISOString(),
    headless: CONFIG.headless,
  };
  await ensureDir(path.dirname(CONFIG.progressPath));
  const tmpPath = `${CONFIG.progressPath}.${process.pid}.${Math.random().toString(36).slice(2)}.tmp`;
  await fs.writeFile(tmpPath, JSON.stringify(progressState, null, 2), 'utf-8');
  await renameWithBusyRetry(tmpPath, CONFIG.progressPath);
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
async function appendSummaryRow(row) {
  const header = ['work_id', 'title', 'publish_date', 'merged_file', 'raw_files'];
  try {
    await fs.access(CONFIG.summaryPath);
  } catch {
    await ensureDir(path.dirname(CONFIG.summaryPath));
    await fs.writeFile(CONFIG.summaryPath, `${header.join(',')}\n`, 'utf-8');
  }
  const firstLine = (await fs.readFile(CONFIG.summaryPath, 'utf-8')).split('\n')[0];
  const targetPath =
    firstLine.trim() === header.join(',')
      ? CONFIG.summaryPath
      : path.resolve(path.dirname(CONFIG.summaryPath), 'summary_v2.csv');

  if (targetPath !== CONFIG.summaryPath) {
    try {
      await fs.access(targetPath);
    } catch {
      await fs.writeFile(targetPath, `${header.join(',')}\n`, 'utf-8');
    }
  }

  const line = [
    csvEscape(row.workId),
    csvEscape(row.title),
    csvEscape(row.publishDate),
    csvEscape(row.mergedFile || ''),
    csvEscape(row.rawFiles.join('|')),
  ].join(',');
  await fs.appendFile(targetPath, `${line}\n`, 'utf-8');
}

const execFileAsync = promisify(execFile);

async function loadState() {
  try {
    const raw = await fs.readFile(CONFIG.statePath, 'utf-8');
    const parsed = JSON.parse(raw);
    const ids = new Set(Array.isArray(parsed?.processed_ids) ? parsed.processed_ids : []);
    return { ids };
  } catch {
    return { ids: new Set() };
  }
}

async function saveState(ids) {
  const payload = {
    processed_ids: Array.from(ids),
    updated_at: new Date().toISOString(),
  };
  await ensureDir(path.dirname(CONFIG.statePath));
  await fs.writeFile(CONFIG.statePath, JSON.stringify(payload, null, 2), 'utf-8');
}

async function mergeExcels({ workId, title, publishDate, files }) {
  if (!files || files.length === 0) return null;
  const outputBase = sanitizeBaseName(`merged-${publishDate}-${workId}`, 60);
  const outputName = `${outputBase}.xlsx`;
  const outputPath = path.join(CONFIG.downloadDir, outputName);
  await execFileAsync(CONFIG.pythonBin, [
    CONFIG.mergeScriptPath,
    '--output',
    outputPath,
    '--title',
    title,
    '--date',
    publishDate,
    '--work-id',
    workId,
    '--files',
    ...files,
  ]);
  console.log(`[merge] 已生成合并文件：${outputPath}`);
  return outputPath;
}

async function listAllMergedFiles() {
  const entries = await fs.readdir(CONFIG.downloadDir);
  return entries
    .filter((name) => name.startsWith('merged-') && /\.(xlsx|xls)$/i.test(name))
    .map((name) => path.join(CONFIG.downloadDir, name));
}

async function mergeAllVideos() {
  const files = await listAllMergedFiles();
  if (!files || files.length === 0) return null;
  const args = [CONFIG.mergeAllScriptPath, '--output', CONFIG.masterPath, '--files', ...files];
  if (CONFIG.masterLimit && CONFIG.masterLimit > 0) {
    args.push('--limit', String(CONFIG.masterLimit));
  }
  await execFileAsync(CONFIG.pythonBin, args);
  console.log(`[merge] 已生成总表：${CONFIG.masterPath}`);
  return CONFIG.masterPath;
}

export function normalizeFinalMetrics(metrics = {}) {
  const totalWorks = Number.parseInt(metrics.totalWorks ?? 0, 10) || 0;
  const processedWorks = Number.parseInt(metrics.processedWorks ?? 0, 10) || 0;
  const successWorks = Number.parseInt(metrics.successWorks ?? 0, 10) || 0;
  const skippedWorks = Number.parseInt(metrics.skippedWorks ?? 0, 10) || 0;
  const failedWorks = Number.parseInt(metrics.failedWorks ?? 0, 10) || 0;
  return {
    totalWorks,
    queuedWorks: Math.max(totalWorks - processedWorks, 0),
    processedWorks,
    successWorks,
    skippedWorks,
    failedWorks,
  };
}

export function classifyExportOutcome(metrics = {}) {
  const normalized = normalizeFinalMetrics(metrics);
  if (normalized.totalWorks > 0 && normalized.successWorks === 0 && normalized.failedWorks > 0) {
    return 'failed';
  }
  if (normalized.successWorks > 0 && normalized.failedWorks > 0) {
    return 'partial_failure';
  }
  return 'success';
}

export function assertExportOutcome(metrics = {}) {
  const outcome = classifyExportOutcome(metrics);
  if (outcome === 'success') return outcome;
  const normalized = normalizeFinalMetrics(metrics);
  const error = new Error(
    outcome === 'partial_failure'
      ? `partial_failure: 成功 ${normalized.successWorks} 条，失败 ${normalized.failedWorks} 条，待重试 ${normalized.queuedWorks} 条`
      : 'all_candidates_failed',
  );
  error.code = outcome;
  throw error;
}

export function buildFinalCompletionPatch(metrics = {}, availableMergedCount = 0) {
  const normalized = normalizeFinalMetrics(metrics);
  const hasAvailableResults = availableMergedCount > 0;
  const hasProcessedWork =
    normalized.totalWorks > 0 ||
    normalized.processedWorks > 0 ||
    normalized.successWorks > 0 ||
    normalized.skippedWorks > 0 ||
    normalized.failedWorks > 0;

  if (!hasProcessedWork && !hasAvailableResults) {
    return {
      ...normalized,
      message: '抖音任务完成，当前账号暂无可采集作品',
    };
  }

  if (hasAvailableResults && normalized.successWorks === 0 && normalized.failedWorks === 0) {
    return {
      ...normalized,
      message:
        normalized.skippedWorks > 0
          ? `本轮没有新增导出，已沿用已有 ${availableMergedCount} 份本地作品结果`
          : '本轮没有新增导出，已沿用已有本地结果',
    };
  }

  return {
    ...normalized,
    message:
      normalized.successWorks > 0
        ? `抖音任务完成，共 ${normalized.successWorks} 条`
        : '抖音任务完成',
  };
}

export function extractDouyinWorkListPage(json = {}) {
  const data = json?.data && typeof json.data === 'object' ? json.data : json;
  const items = data?.items || data?.aweme_list || data?.list || [];
  const nextCursor = data?.max_cursor ?? data?.next_cursor ?? null;
  const total = Number.parseInt(data?.total ?? data?.total_count ?? '0', 10) || 0;
  return {
    items: Array.isArray(items) ? items : [],
    hasMore: Boolean(data?.has_more ?? data?.hasMore),
    nextCursor,
    total,
  };
}

export function shouldStopDouyinListScan({
  eligibleCount = 0,
  limit = 0,
  staleRounds = 0,
  staleRoundsLimit = 3,
  apiHasMore = null,
  apiReachedDateBoundary = false,
} = {}) {
  if (limit > 0 && eligibleCount >= limit && staleRounds >= 1) return true;
  if (apiReachedDateBoundary) return true;
  if (apiHasMore === true) return false;
  return staleRounds >= staleRoundsLimit;
}

export function isDouyinPaginationStalled({
  apiHasMore = null,
  staleRounds = 0,
  stallRoundsLimit = 8,
} = {}) {
  return apiHasMore === true && staleRounds >= Math.max(1, Number(stallRoundsLimit) || 8);
}

async function scrollDouyinWorkList(page, cardLocator) {
  const lastCard = cardLocator.last();
  if ((await lastCard.count().catch(() => 0)) > 0) {
    await lastCard.scrollIntoViewIfNeeded().catch(() => {});
  }
  const scrolled = await page.evaluate(() => {
    const candidates = Array.from(document.querySelectorAll('*')).filter((element) => {
      const style = window.getComputedStyle(element);
      return /(auto|scroll)/u.test(style.overflowY) && element.scrollHeight > element.clientHeight + 50;
    });
    const target = candidates.sort((left, right) => right.scrollHeight - left.scrollHeight)[0];
    if (!target) return false;
    target.scrollTop = target.scrollHeight;
    target.dispatchEvent(new Event('scroll', { bubbles: true }));
    return true;
  }).catch(() => false);
  if (!scrolled) {
    await page.mouse.wheel(0, 4000);
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight)).catch(() => {});
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

async function hasLoginPrompt(page, loginPromptMarkers) {
  const currentUrl = page.url();
  const loginUrlMarkers = ['/passport/', '/login', 'auth.douyin.com'];
  if (loginUrlMarkers.some((marker) => currentUrl.includes(marker))) {
    return true;
  }

  for (const selector of loginPromptMarkers) {
    try {
      if (await visibleMarkerExists(page, selector)) return true;
    } catch {
      // ignore a single unavailable marker and continue checking
    }
  }
  return false;
}

async function hasCreatorShell(page, shellMarkers) {
  for (const selector of shellMarkers) {
    try {
      if (await visibleMarkerExists(page, selector)) return true;
    } catch {
      // ignore a single unavailable marker and continue checking
    }
  }
  return false;
}

export async function classifyDouyinAuthPageOnce(page) {
  const loginPromptMarkers = [
    'text=扫码登录',
    'text=请扫码登录',
    'text=登录抖音创作者中心',
    'text=登录后即可查看',
    'text=账号登录',
    'text=验证码登录',
    'button:has-text("立即登录")',
    'button:has-text("扫码登录")',
    'iframe[src*="login"]',
    'iframe[src*="passport"]',
  ];
  const shellMarkers = [
    'text=内容管理',
    'text=作品管理',
    'text=数据中心',
    'a[href*="content/manage"]',
    'a[href*="work-management"]',
    '[class*="semi-navigation"]',
    '[class*="semi-layout-sider"]',
    '[class*="creator-sidebar"]',
  ];

  try {
    if (await hasLoginPrompt(page, loginPromptMarkers)) return 'login_required';
    if (await hasCreatorShell(page, shellMarkers)) return 'authorized';
  } catch {
    // Navigation and renderer failures are transient, not proof that cookies expired.
  }
  return 'transient';
}

export async function classifyDouyinAuthPage(page, options = {}) {
  const stableChecks = Math.max(2, Number.parseInt(options.stableChecks ?? '2', 10) || 2);
  const settleMs = Number.isFinite(options.settleMs)
    ? Math.max(0, options.settleMs)
    : 1200;
  let stableClassification = '';

  for (let index = 0; index < stableChecks; index += 1) {
    const current = await classifyDouyinAuthPageOnce(page);
    if (current === 'transient') return 'transient';
    if (stableClassification && current !== stableClassification) return 'transient';
    stableClassification = current;
    if (index + 1 < stableChecks) await page.waitForTimeout(settleMs);
  }
  return stableClassification || 'transient';
}

async function isLoggedIn(page) {
  return (await classifyDouyinAuthPage(page)) === 'authorized';
}

function douyinAuthError(classification) {
  if (classification === 'login_required') {
    return new Error('抖音未登录（headless=true 无法扫码登录）');
  }
  return new Error('抖音创作者数据页暂不可访问（登录态未作失效处理）');
}

async function isEmptyWorkList(page) {
  for (const marker of EMPTY_WORK_LIST_MARKERS) {
    try {
      if ((await page.locator(`text=${marker}`).count()) > 0) return true;
    } catch {
      // ignore
    }
  }
  return false;
}

async function waitForWorkListReady(page) {
  const deadline = Date.now() + CONFIG.listLoadTimeoutMs;
  while (Date.now() < deadline) {
    const cardCount = await page.locator(SELECTORS.videoCard).count().catch(() => 0);
    if (cardCount > 0) {
      return { empty: false };
    }
    if (await isEmptyWorkList(page)) {
      return { empty: true };
    }
    await page.waitForTimeout(800);
  }
  return { empty: false, timedOut: true };
}

async function suppressOverlays(page) {
  try {
    await page.addStyleTag({
      content: `
        .douyin-creator-vmock-portal,
        .douyin-creator-vmock-modal-wrap,
        iframe[src*="creatorvideo"],
        .shepherd-element,
        .shepherd-modal-overlay-container,
        .shepherd-modal-overlay,
        .shepherd-enabled,
        .douyin-creator-pc-master__wrap {
          pointer-events: none !important;
        }
        .shepherd-element,
        .shepherd-modal-overlay-container,
        .shepherd-modal-overlay,
        .douyin-creator-pc-master__wrap {
          opacity: 0 !important;
        }
      `,
    });
  } catch {
    // ignore
  }

  try {
    await page.keyboard.press('Escape');
  } catch {
    // ignore
  }
}

async function waitForLogin(page) {
  const deadline = Date.now() + CONFIG.scanWaitMs;
  console.log(`[login] 请在 ${Math.round(CONFIG.scanWaitMs / 1000)} 秒内扫码登录。登录成功后会自动继续。`);
  while (Date.now() < deadline) {
    if (await isLoggedIn(page)) {
      console.log('[login] 已检测到登录状态。');
      return true;
    }
    await page.waitForTimeout(CONFIG.scanPollMs);
  }
  return false;
}

async function waitForDetailReady(page, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const marker of UNAVAILABLE_MARKERS) {
      try {
        if ((await page.locator(`text=${marker}`).count()) > 0) return true;
      } catch {
        // ignore
      }
    }

    try {
      if ((await page.locator(`text=${SELECTORS.tabOverview}`).count()) > 0) return true;
    } catch {
      // ignore
    }

    await page.waitForTimeout(500);
  }
  return false;
}

function isRetryableDetailNavigationError(error) {
  return (
    isTransientNavigationError(error)
    || /作品详情页加载超时|未找到可导出数据入口/u.test(
      String(error instanceof Error ? error.message : error || ''),
    )
  );
}

export function isDouyinDetailNavigationExhausted(error) {
  return String(error?.code || '').trim() === 'douyin_detail_navigation_exhausted';
}

async function openDetailPageWithRetry(context, targetUrl) {
  try {
    return await retryTransientOperation(
      async () => {
        let candidatePage = null;
        try {
          candidatePage = await context.newPage();
          await candidatePage.goto(targetUrl, {
            waitUntil: 'domcontentloaded',
            timeout: CONFIG.detailNavigationTimeoutMs,
          });
          const ready = await waitForDetailReady(candidatePage, 15000);
          if (!ready) {
            throw new Error('作品详情页加载超时，未找到可导出数据入口');
          }
          return candidatePage;
        } catch (error) {
          if (candidatePage && !candidatePage.isClosed()) {
            await candidatePage.close().catch(() => {});
          }
          throw error;
        }
      },
      {
        maxAttempts: CONFIG.navigationRetryAttempts,
        retryDelaysMs: CONFIG.navigationRetryDelaysMs,
        shouldRetry: isRetryableDetailNavigationError,
        onRetry: async ({ attempt, nextAttempt, delayMs, error }) => {
          console.warn(
            `[network] 作品详情导航第 ${attempt} 次失败，将在 ${delayMs}ms 后进行第 ${nextAttempt} 次尝试：${error.message}`,
          );
        },
      },
    );
  } catch (error) {
    if (isRetryableDetailNavigationError(error)) {
      error.code = 'douyin_detail_navigation_exhausted';
    }
    throw error;
  }
}

async function openDetailFromCard(listPage, card) {
  await card.scrollIntoViewIfNeeded();

  const clickTargets = [
    card.locator('div[class*="video-card-cover"]'),
    card.locator('div[class*="video-card-info"]'),
    card.locator('div[class*="video-card-content"]'),
    card,
  ];

  for (const target of clickTargets) {
    await suppressOverlays(listPage);

    const popupPromise = listPage.waitForEvent('popup', { timeout: 10000 }).catch(() => null);
    const urlPromise = listPage
      .waitForURL(/work-detail/, { timeout: 10000 })
      .then(() => listPage)
      .catch(() => null);
    const markerPromise = listPage
      .waitForSelector(`text=${SELECTORS.tabOverview}`, { timeout: 10000 })
      .then(() => listPage)
      .catch(() => null);

    await target.click({ timeout: 10000, force: true });

    const result = await Promise.race([popupPromise, urlPromise, markerPromise]);

    if (result && result.url && typeof result.url === 'function') {
      if (result !== listPage) {
        await result.waitForLoadState('domcontentloaded').catch(() => {});
        await waitForDetailReady(result, 15000).catch(() => {});
        if (result.url().includes('/work-detail/')) return result;
      } else if (listPage.url().includes('/work-detail/')) {
        await listPage.waitForLoadState('domcontentloaded').catch(() => {});
        await waitForDetailReady(listPage, 15000).catch(() => {});
        return listPage;
      }
    }
  }

  throw new Error('无法打开作品详情页（点击作品卡片无响应）');
}

async function maybeConfirmExport(page) {
  const dialog = page.locator('div[role="dialog"], .semi-modal, .modal');
  if ((await dialog.count()) === 0) return;

  const candidates = [
    dialog.getByRole('button', { name: '导出' }),
    dialog.getByRole('button', { name: '确定' }),
    dialog.getByRole('button', { name: '确认' }),
    dialog.getByRole('button', { name: '开始导出' }),
  ];

  for (const locator of candidates) {
    if ((await locator.count()) > 0) {
      await locator.first().click();
      return;
    }
  }
}

async function findExportButtons(page) {
  const byRole = page.getByRole(SELECTORS.exportButtonRole, {
    name: SELECTORS.exportButtonName,
  });
  if ((await byRole.count()) > 0) return byRole;
  return page.locator('text=导出');
}

async function triggerDownload(page, buttonLocator, prefix) {
  const downloadPromise = page
    .waitForEvent('download', { timeout: CONFIG.downloadTimeoutMs })
    .catch(() => null);

  await buttonLocator.scrollIntoViewIfNeeded();
  await buttonLocator.click({ timeout: 10000 });
  await maybeConfirmExport(page);

  const download = await downloadPromise;
  if (!download) {
    console.log(`[export] 未捕获到下载事件：${prefix}`);
    return null;
  }

  const suggested = sanitizeFilename(download.suggestedFilename());
  const finalName = sanitizeFilename(`${prefix}-${suggested}`);
  const targetPath = path.join(CONFIG.downloadDir, finalName);
  await download.saveAs(targetPath);
  console.log(`[export] 已保存：${targetPath}`);
  return targetPath;
}

async function exportFromTab(page, tabName, expectedCount, prefix) {
  const savedFiles = [];
  const tabByRole = page.getByRole('tab', { name: tabName });
  if ((await tabByRole.count()) > 0) {
    await tabByRole.first().click({ force: true });
  } else {
    const tabByText = page.locator(`text=${tabName}`).first();
    await tabByText.click({ force: true });
  }

  await page.waitForTimeout(1500);

  const exportButtons = await findExportButtons(page);
  const total = await exportButtons.count();
  const target = Math.min(expectedCount, total);

  console.log(`[export] ${tabName} 导出按钮数量：${total}，准备点击前 ${target} 个。`);

  for (let i = 0; i < target; i += 1) {
    const label = `${prefix}-${tabName}-${i + 1}`;
    const saved = await triggerDownload(page, exportButtons.nth(i), label);
    if (saved) savedFiles.push(saved);
    await page.waitForTimeout(1000);
  }
  return savedFiles;
}

async function exportWorkFromDetailPage(detailPage, index, meta) {
  const urlWorkId = extractWorkId(detailPage.url());
  const workId = urlWorkId !== 'unknown-work' ? urlWorkId : meta?.id || urlWorkId;
  const title = meta?.title || 'untitled';
  const publishDate = normalizeDate(meta?.publishDate) || 'unknown-date';

  console.log(`\n[work] (${index}) 打开作品：${workId}`);

  const ready = await waitForDetailReady(detailPage, 15000);
  if (!ready) {
    throw new Error('作品详情页加载超时，未找到可导出数据入口');
  }
  await detailPage.waitForTimeout(1200);
  await suppressOverlays(detailPage);
  for (const marker of UNAVAILABLE_MARKERS) {
    try {
      if ((await detailPage.locator(`text=${marker}`).count()) > 0) {
        console.log(`[work] ${workId} 标记为不可查看数据（${marker}），跳过。`);
        return { workId, title, publishDate, files: [], skip: true };
      }
    } catch {
      // ignore
    }
  }
  const hasOverview = (await detailPage.locator(`text=${SELECTORS.tabOverview}`).count()) > 0;
  if (!hasOverview) {
    console.log(`[work] ${workId} 未找到“总览/流量分析”入口，跳过。`);
    return { workId, title, publishDate, files: [], folder: null };
  }

  const prefix = sanitizeFilename(`work-${workId}-${publishDate}`, 60);
  const expectedTotal = 5;

  const attemptExport = async () => {
    const collected = [];
    collected.push(...(await exportFromTab(detailPage, SELECTORS.tabOverview, 2, prefix)));
    collected.push(...(await exportFromTab(detailPage, SELECTORS.tabTraffic, 3, prefix)));
    return collected;
  };

  let files = await attemptExport();
  if (files.length < expectedTotal) {
    for (const filePath of files) {
      try {
        await fs.unlink(filePath);
      } catch {
        // ignore
      }
    }
    console.log(`[work] ${workId} 导出不足，尝试刷新后重试。`);
    await detailPage.reload({ waitUntil: 'domcontentloaded' });
    await waitForDetailReady(detailPage, 15000);
    await detailPage.waitForTimeout(1200);
    await suppressOverlays(detailPage);
    files = await attemptExport();
  }

  if (files.length < expectedTotal) {
    console.log(`[work] ${workId} 导出文件数量不足（${files.length}/${expectedTotal}），本次不合并。`);
    return { workId, title, publishDate, files, incomplete: true };
  }

  return { workId, title, publishDate, files };
}

async function exportRecentWorksFromList(context, page, limit, onProgress = async () => {}) {
  const workItems = new Map();
  const mergedFiles = [];
  const state = await loadState();
  const processedIds = state.ids;
  let apiHasMore = null;
  let apiNextCursor = null;
  let apiTotal = 0;
  let apiReachedDateBoundary = false;
  let responseCount = 0;
  let workListResponseSequence = 0;
  let latestAppliedWorkListResponseSequence = 0;
  const pendingWorkListResponses = new Set();

  const parseWorkListResponse = async (response, sequence) => {
    const raw = await response.text();
    const safe = raw.replace(
      /"(id|aweme_id|item_id|group_id)"\s*:\s*(\d{16,})/g,
      '"$1":"$2"'
    );
    let json;
    try {
      json = JSON.parse(safe);
    } catch {
      return;
    }
    const parsedPage = extractDouyinWorkListPage(json);
    const items = parsedPage.items;
    apiTotal = Math.max(apiTotal, parsedPage.total);
    if (sequence >= latestAppliedWorkListResponseSequence) {
      latestAppliedWorkListResponseSequence = sequence;
      apiHasMore = parsedPage.hasMore;
      apiNextCursor = parsedPage.nextCursor;
      const minPublishTs = parseDateValue(CONFIG.minPublishDate);
      const cursorTs = Number.parseInt(String(apiNextCursor ?? ''), 10);
      apiReachedDateBoundary = Boolean(
        minPublishTs && Number.isFinite(cursorTs) && cursorTs > 100000000000 && cursorTs < minPublishTs
      );
    }
    responseCount += 1;
    for (const item of items) {
      const id = String(item?.id || item?.aweme_id || item?.item_id || item?.group_id || '');
      if (!id) continue;
      if (workItems.has(id)) continue;
      workItems.set(id, {
        id,
        title: cleanTitle(item?.title || item?.description || item?.desc || item?.name || ''),
        publishDate: formatDateFromUnix(item?.create_time),
      });
    }
  };

  const workListResponseListener = (response) => {
    const url = String(response?.url?.() || '');
    if (!url.includes('/janus/douyin/creator/pc/work_list')) return;
    workListResponseSequence += 1;
    const sequence = workListResponseSequence;
    const pending = parseWorkListResponse(response, sequence)
      .catch(() => {})
      .finally(() => pendingWorkListResponses.delete(pending));
    pendingWorkListResponses.add(pending);
  };
  const settlePendingWorkListResponses = async (timeoutMs = 15000) => {
    if (pendingWorkListResponses.size === 0) return true;
    let timeoutId = null;
    const timedOut = Symbol('work_list_response_timeout');
    const timeoutPromise = new Promise((resolve) => {
      timeoutId = setTimeout(() => resolve(timedOut), Math.max(1, timeoutMs));
    });
    try {
      const result = await Promise.race([
        Promise.allSettled(Array.from(pendingWorkListResponses)),
        timeoutPromise,
      ]);
      return result !== timedOut;
    } finally {
      if (timeoutId) clearTimeout(timeoutId);
    }
  };
  const responseEventTarget = typeof context?.on === 'function' ? context : page;
  responseEventTarget.on('response', workListResponseListener);

  try {
  page = await navigateAuthCandidates(context, page, [CONFIG.contentManageUrl], {
    waitUntil: 'domcontentloaded',
    timeout: 60000,
    settleMs: 1500,
    sameUrlAttempts: CONFIG.navigationRetryAttempts,
    retryDelaysMs: CONFIG.navigationRetryDelaysMs,
  });
  const listReady = await waitForWorkListReady(page);
  if (listReady.timedOut) {
    const authClassification = await classifyDouyinAuthPage(page);
    if (authClassification !== 'authorized') throw douyinAuthError(authClassification);
    throw new Error('作品列表加载超时，请确认已进入作品管理页。');
  }
  await suppressOverlays(page);
  await onProgress({
    phase: 'collecting',
    message: '正在扫描作品列表',
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

  const scanState = { stableRounds: 0 };
  let lastCount = 0;
  let lastWorkItemCount = 0;
  let lastResponseCount = 0;
  let lastCursor = null;

  while (true) {
    const cards = page.locator(SELECTORS.videoCard);
    const count = await cards.count();
    if (count === 0) {
      if (await isEmptyWorkList(page)) {
        await onProgress({
          phase: 'collecting',
          message: '当前账号暂无可采集作品',
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
        return {
          mergedFiles: [],
          metrics: normalizeFinalMetrics({
            totalWorks: 0,
            processedWorks: 0,
            successWorks: 0,
            skippedWorks: 0,
            failedWorks: 0,
          }),
        };
      }
      throw new Error('未找到作品卡片，请确认已进入作品管理页。');
    }

    const eligibleCount = Array.from(workItems.values()).filter((item) =>
      meetsDateRange(item.publishDate)
    ).length;

    if (pendingWorkListResponses.size > 0) {
      const settled = await settlePendingWorkListResponses();
      if (!settled) {
        const error = new Error('作品列表接口响应解析超时');
        error.code = 'work_list_response_timeout';
        throw error;
      }
      continue;
    }

    if (shouldStopDouyinListScan({
      eligibleCount,
      limit,
      staleRounds: scanState.staleRounds,
      staleRoundsLimit: CONFIG.staleRoundsLimit,
      apiHasMore,
      apiReachedDateBoundary,
    })) {
      break;
    }

    const hasNewListData =
      workItems.size > lastWorkItemCount ||
      responseCount > lastResponseCount ||
      (apiNextCursor != null && apiNextCursor !== lastCursor);
    if (count === lastCount && !hasNewListData) {
      scanState.staleRounds += 1;
    } else {
      scanState.staleRounds = 0;
    }

    if (isDouyinPaginationStalled({
      apiHasMore,
      staleRounds: scanState.staleRounds,
      stallRoundsLimit: CONFIG.paginationStallRoundsLimit,
    })) {
      const error = new Error(
        `作品列表分页停滞：平台仍标记有下一页，但连续 ${scanState.staleRounds} 轮未收到新作品或游标`,
      );
      error.code = 'work_list_pagination_stalled';
      throw error;
    }

    if (scanState.staleRounds >= CONFIG.staleRoundsLimit && apiHasMore !== true) {
      console.log('[list] 平台已无更多作品，停止扫描。');
      break;
    }

    lastCount = count;
    lastWorkItemCount = workItems.size;
    lastResponseCount = responseCount;
    lastCursor = apiNextCursor;
    await scrollDouyinWorkList(page, cards);
    await page.waitForTimeout(2500);
    await onProgress({
      phase: 'collecting',
      message: `扫描中，已发现 ${workItems.size}${apiTotal > 0 ? ` / 平台共 ${apiTotal}` : ''} 条作品`,
      currentIndex: 0,
      currentWorkId: '',
      currentTitle: '',
    });
  }

  } finally {
    if (typeof responseEventTarget.off === 'function') {
      responseEventTarget.off('response', workListResponseListener);
    }
    if (pendingWorkListResponses.size > 0) {
      await settlePendingWorkListResponses();
    }
  }

  let candidates = Array.from(workItems.values()).filter((item) => meetsDateRange(item.publishDate));
  candidates.sort((a, b) => {
    const aTs = parseDateValue(a.publishDate) ?? 0;
    const bTs = parseDateValue(b.publishDate) ?? 0;
    return bTs - aTs;
  });
  if (limit > 0 && candidates.length > limit) {
    candidates = candidates.slice(0, limit);
  }

  const metrics = {
    totalWorks: candidates.length,
    queuedWorks: candidates.length,
    processedWorks: 0,
    successWorks: 0,
    skippedWorks: 0,
    failedWorks: 0,
  };

  const progressPatch = (extra = {}) => ({
    ...metrics,
    queuedWorks: Math.max(metrics.totalWorks - metrics.processedWorks, 0),
    ...extra,
  });

  await onProgress(
    progressPatch({
      phase: 'exporting',
      message: `待处理 ${candidates.length} 条作品`,
      currentIndex: 0,
      currentWorkId: '',
      currentTitle: '',
    })
  );

  let consecutiveNavigationFailures = 0;
  for (let index = 0; index < candidates.length; index += 1) {
    const meta = candidates[index];
    const metaId = meta?.id ? String(meta.id) : '';
    if (!metaId) continue;

    await onProgress(
      progressPatch({
        phase: 'exporting',
        message: '正在导出作品数据',
        currentIndex: index + 1,
        currentWorkId: metaId,
        currentTitle: meta?.title || '',
      })
    );

    const publishTs = parseDateValue(meta.publishDate);
    const refreshAfter =
      CONFIG.refreshDays > 0 ? Date.now() - CONFIG.refreshDays * 24 * 60 * 60 * 1000 : null;
    const shouldRefresh =
      CONFIG.forceFullExport ||
      (CONFIG.refreshLatestCount > 0 && index < CONFIG.refreshLatestCount) ||
      (refreshAfter && publishTs && publishTs >= refreshAfter);

    if (processedIds.has(metaId) && !shouldRefresh) {
      console.log(`[skip] 已处理过：${metaId}`);
      metrics.processedWorks += 1;
      metrics.skippedWorks += 1;
      await onProgress(
        progressPatch({
          phase: 'exporting',
          message: '跳过已处理作品',
          currentIndex: index + 1,
          currentWorkId: metaId,
          currentTitle: meta?.title || '',
        })
      );
      consecutiveNavigationFailures = 0;
      if (CONFIG.workIntervalMs > 0) await page.waitForTimeout(CONFIG.workIntervalMs);
      continue;
    }

    let detailPage = null;
    let stopAfterCurrent = false;
    let failureCooldownMs = 0;
    try {
      detailPage = await openDetailPageWithRetry(context, buildDetailUrl(metaId));
      const result = await exportWorkFromDetailPage(detailPage, index + 1, meta);

      if (result.skip) {
        if (metaId && metaId !== 'unknown-work') {
          processedIds.add(String(metaId));
          await saveState(processedIds);
        }
        metrics.processedWorks += 1;
        metrics.skippedWorks += 1;
        await onProgress(
          progressPatch({
            phase: 'exporting',
            message: '作品不可查看，已跳过',
            currentIndex: index + 1,
            currentWorkId: metaId,
            currentTitle: meta?.title || '',
          })
        );
        consecutiveNavigationFailures = 0;
        continue;
      }

      if (result.incomplete) {
        for (const filePath of result.files || []) {
          try {
            await fs.unlink(filePath);
          } catch {
            // ignore
          }
        }
        metrics.processedWorks += 1;
        metrics.skippedWorks += 1;
        await onProgress(
          progressPatch({
            phase: 'exporting',
            message: '导出不完整，已跳过',
            currentIndex: index + 1,
            currentWorkId: metaId,
            currentTitle: meta?.title || '',
          })
        );
        consecutiveNavigationFailures = 0;
        continue;
      }

      if (result.files.length > 0) {
        const mergedFile = await mergeExcels({
          workId: result.workId,
          title: result.title,
          publishDate: result.publishDate,
          files: result.files,
        });

        await appendSummaryRow({
          workId: result.workId,
          title: result.title,
          publishDate: result.publishDate,
          mergedFile: mergedFile ? path.basename(mergedFile) : '',
          rawFiles: result.files.map((file) => path.basename(file)),
        });

        if (mergedFile) {
          mergedFiles.push(mergedFile);
        }

        for (const filePath of result.files) {
          try {
            await fs.unlink(filePath);
          } catch {
            // ignore
          }
        }

        if (result.workId && result.workId !== 'unknown-work') {
          processedIds.add(String(result.workId));
          await saveState(processedIds);
        }
        metrics.processedWorks += 1;
        metrics.successWorks += 1;
        await onProgress(
          progressPatch({
            phase: 'exporting',
            message: '导出并合并成功',
            currentIndex: index + 1,
            currentWorkId: metaId,
            currentTitle: meta?.title || '',
          })
        );
        consecutiveNavigationFailures = 0;
      } else {
        metrics.processedWorks += 1;
        metrics.skippedWorks += 1;
        await onProgress(
          progressPatch({
            phase: 'exporting',
            message: '无可用导出数据，已跳过',
            currentIndex: index + 1,
            currentWorkId: metaId,
            currentTitle: meta?.title || '',
          })
        );
        consecutiveNavigationFailures = 0;
      }
    } catch (error) {
      console.error(`[work] 第 ${index + 1} 条打开失败：${error.message}`);
      const screenshotPath = path.join(CONFIG.downloadDir, `error-open-${Date.now()}.png`);
      const screenshotPage = detailPage || page;
      try {
        await screenshotPage.screenshot({ path: screenshotPath, fullPage: true });
        console.error(`[work] 已截图：${screenshotPath}`);
      } catch {
        // ignore
      }
      metrics.processedWorks += 1;
      metrics.failedWorks += 1;
      const transientNavigationFailure = isDouyinDetailNavigationExhausted(error);
      consecutiveNavigationFailures = transientNavigationFailure
        ? consecutiveNavigationFailures + 1
        : 0;
      await onProgress(
        progressPatch({
          phase: 'exporting',
          message: `导出失败：${error.message}`,
          currentIndex: index + 1,
          currentWorkId: metaId,
          currentTitle: meta?.title || '',
        })
      );
      if (
        transientNavigationFailure
        && consecutiveNavigationFailures >= CONFIG.maxConsecutiveNavigationFailures
      ) {
        const remainingWorks = Math.max(candidates.length - (index + 1), 0);
        stopAfterCurrent = true;
        await onProgress(
          progressPatch({
            phase: 'exporting',
            message: `连续网络异常，已暂停剩余 ${remainingWorks} 条并保留 ${metrics.successWorks} 条成功结果`,
            currentIndex: index + 1,
            currentWorkId: '',
            currentTitle: '',
          })
        );
      } else if (transientNavigationFailure) {
        failureCooldownMs = CONFIG.navigationFailureCooldownMs;
      }
    } finally {
      if (detailPage) {
        try {
          await detailPage.close();
        } catch {
          // ignore
        }
      }
      const intervalMs = failureCooldownMs > 0 ? failureCooldownMs : CONFIG.workIntervalMs;
      if (!stopAfterCurrent && intervalMs > 0) {
        console.log(`[network] 下一条作品前等待 ${intervalMs}ms。`);
        await page.waitForTimeout(intervalMs);
      }
    }
    if (stopAfterCurrent) break;
  }

  await onProgress(
    progressPatch({
      phase: 'merging',
      message: '作品处理结束，准备整理结果',
      currentIndex: metrics.processedWorks,
      currentWorkId: '',
      currentTitle: '',
    })
  );

  await saveState(processedIds);
  return { mergedFiles, metrics: { ...metrics } };
}

async function main() {
  await ensureDir(CONFIG.downloadDir);
  await ensureDir(CONFIG.userDataDir);
  await cleanProfileLocks(CONFIG.userDataDir);
  progressState = newProgressState();
  await updateProgress({
    status: 'running',
    phase: 'starting',
    message: '初始化导出任务',
    startedAt: new Date().toISOString(),
    finishedAt: null,
  });

  let context = null;
  let page = null;

  try {
    context = await chromium.launchPersistentContext(CONFIG.userDataDir, {
      headless: CONFIG.headless,
      acceptDownloads: true,
      slowMo: CONFIG.slowMoMs,
      viewport: { width: 1440, height: 900 },
      ...(CONFIG.browserExecutablePath
        ? { executablePath: CONFIG.browserExecutablePath }
        : (CONFIG.browserChannel === 'chromium' ? {} : { channel: CONFIG.browserChannel })),
    });

    page = await prepareAuthPage(context);

    await updateProgress({
      phase: 'login',
      message: '正在检查登录状态',
    });

    page = await navigateAuthCandidates(context, page, [CONFIG.contentManageUrl], {
      settleMs: 1500,
      sameUrlAttempts: CONFIG.navigationRetryAttempts,
      retryDelaysMs: CONFIG.navigationRetryDelaysMs,
    });
    let authClassification = await classifyDouyinAuthPage(page);
    if (authClassification !== 'authorized') {
      await updateProgress({
        phase: 'login',
        message: authClassification === 'login_required'
          ? (CONFIG.headless
            ? '需要登录：请使用 headless=false 运行并扫码'
            : `等待扫码登录（最多 ${Math.round(CONFIG.scanWaitMs / 1000)} 秒）`)
          : '抖音创作者数据页暂不可访问（登录态未作失效处理）',
      });

      if (CONFIG.headless) {
        throw douyinAuthError(authClassification);
      }

      const loggedIn = await waitForLogin(page);
      if (!loggedIn) {
        authClassification = await classifyDouyinAuthPage(page);
        if (authClassification === 'login_required') {
          throw new Error('登录超时，未检测到登录状态。');
        }
        if (authClassification !== 'authorized') throw douyinAuthError('transient');
      }
    }

    if (CONFIG.authOnly) {
      await updateProgress({
        status: 'completed',
        phase: 'done',
        message: '抖音登录完成（AUTH_ONLY）',
        auth_status: 'authorized',
        auth_reason: '',
        needs_auth: false,
        finishedAt: new Date().toISOString(),
        currentWorkId: '',
        currentTitle: '',
      });
      if (context) {
        await context.close();
        context = null;
      }
      process.exit(0);
      return;
    }

    await updateProgress({
      phase: 'collecting',
      message: '已登录，开始收集作品列表',
    });
    const exportResult = await exportRecentWorksFromList(context, page, CONFIG.videoLimit, updateProgress);
    const mergedFiles = Array.isArray(exportResult?.mergedFiles) ? exportResult.mergedFiles : [];
    const metrics = exportResult?.metrics || {};
    const outcome = classifyExportOutcome(metrics);
    const allMergedFiles = await listAllMergedFiles();
    if (allMergedFiles.length > 0 && (outcome === 'success' || normalizeFinalMetrics(metrics).successWorks > 0)) {
      await updateProgress({
        phase: 'merging',
        message: outcome === 'partial_failure' ? '正在保留已成功作品结果' : '正在合并总表',
        ...normalizeFinalMetrics(metrics),
        currentWorkId: '',
        currentTitle: '',
      });
      await mergeAllVideos();
    }
    assertExportOutcome(metrics);

    console.log('\n[done] 抖音任务完成。');
    const completionPatch = buildFinalCompletionPatch(metrics, allMergedFiles.length || mergedFiles.length || 0);
    await updateProgress({
      status: 'completed',
      phase: 'done',
      message: completionPatch.message,
      finishedAt: new Date().toISOString(),
      ...completionPatch,
      currentWorkId: '',
      currentTitle: '',
    });
  } catch (error) {
    console.error(`[error] ${error.message}`);
    if (error?.stack) console.error(error.stack);
    if (page) {
      const screenshotPath = path.join(CONFIG.downloadDir, `error-${Date.now()}.png`);
      try {
        await page.screenshot({ path: screenshotPath, fullPage: true });
        console.error(`[error] 已截图：${screenshotPath}`);
      } catch {
        // ignore screenshot errors
      }
    }
    await updateProgress({
      status: 'failed',
      phase: 'failed',
      message: `导出失败：${error.message}`,
      error: error.code || error.message,
      finishedAt: new Date().toISOString(),
    });
    process.exitCode = 1;
  } finally {
    if (context) {
      await context.close();
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  await main();
}
