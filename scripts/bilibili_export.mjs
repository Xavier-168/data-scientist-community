#!/usr/bin/env node
import { chromium } from 'playwright';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { pathToFileURL } from 'node:url';
import { navigateAuthCandidates, prepareAuthPage } from './browser_auth_utils.mjs';
import { resolveDownloadsDir, resolveProfileDir } from './runtime_paths.mjs';

const execFileAsync = promisify(execFile);
const DEFAULT_PYTHON_BIN = process.platform === 'win32' ? 'python' : 'python3';
const DEFAULT_BROWSER_CHANNEL = process.env.BROWSER_CHANNEL ?? 'chrome';
const DEFAULT_DOWNLOAD_DIR = resolveDownloadsDir();

const CONFIG = {
  homeUrl: process.env.BILI_HOME_URL ?? 'https://www.bilibili.com/',
  dashboardUrl: process.env.BILI_DASHBOARD_URL ?? 'https://member.bilibili.com/platform/data-up/video/',
  navApiUrl: process.env.BILI_NAV_API_URL ?? 'https://api.bilibili.com/x/web-interface/nav',
  wbiArcSearchBase: process.env.BILI_WBI_ARC_SEARCH_BASE ?? 'https://api.bilibili.com/x/space/wbi/arc/search',
  dataMode: process.env.BILI_DATA_MODE ?? '历史累计数据',
  browserChannel: DEFAULT_BROWSER_CHANNEL,
  browserExecutablePath: String(process.env.BROWSER_EXECUTABLE_PATH ?? '').trim(),
  userDataDir: process.env.USER_DATA_DIR
    ? path.resolve(process.env.USER_DATA_DIR)
    : resolveProfileDir('bilibili', DEFAULT_BROWSER_CHANNEL),
  headless: (process.env.HEADLESS ?? 'true') === 'true',
  authOnly: (process.env.AUTH_ONLY ?? 'false') === 'true',
  cleanProfileLocks: (process.env.CLEAN_PROFILE_LOCKS ?? 'true') === 'true',
  videoLimit: Number.parseInt(process.env.VIDEO_LIMIT ?? '200', 10),
  minPublishDate: process.env.MIN_PUBLISH_DATE ?? '',
  maxPublishDate: process.env.MAX_PUBLISH_DATE ?? '',
  scanWaitMs: Number.parseInt(process.env.SCAN_WAIT_MS ?? '300000', 10),
  scanPollMs: Number.parseInt(process.env.SCAN_POLL_MS ?? '2000', 10),
  pageSize: Number.parseInt(process.env.BILI_PAGE_SIZE ?? '50', 10),
  requestGapMs: Number.parseInt(process.env.BILI_REQUEST_GAP_MS ?? '250', 10),
  retryMax: Number.parseInt(process.env.BILI_RETRY_MAX ?? '6', 10),
  retryBaseDelayMs: Number.parseInt(process.env.BILI_RETRY_BASE_DELAY_MS ?? '2000', 10),
  retryMaxDelayMs: Number.parseInt(process.env.BILI_RETRY_MAX_DELAY_MS ?? '60000', 10),
  retryJitterMs: Number.parseInt(process.env.BILI_RETRY_JITTER_MS ?? '800', 10),
  progressPath: process.env.PROGRESS_PATH
    ? path.resolve(process.env.PROGRESS_PATH)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'bilibili_progress.json'),
  outputPath: process.env.BILI_OUTPUT_PATH
    ? path.resolve(process.env.BILI_OUTPUT_PATH)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'bilibili_all_videos.xlsx'),
  tempRowsPath: process.env.BILI_TEMP_ROWS_PATH
    ? path.resolve(process.env.BILI_TEMP_ROWS_PATH)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'bilibili_rows.json'),
  officialDownloadDir: process.env.BILI_OFFICIAL_DOWNLOAD_DIR
    ? path.resolve(process.env.BILI_OFFICIAL_DOWNLOAD_DIR)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'bilibili_official_exports'),
  normalizerScriptPath: process.env.BILI_NORMALIZER_SCRIPT
    ? path.resolve(process.env.BILI_NORMALIZER_SCRIPT)
    : path.resolve('scripts', 'normalize_bilibili_official_export.py'),
  // B 站官方 CSV 只包含「自选指标」里已勾选的列；这两个列默认可能缺失。
  requiredMetrics: (process.env.BILI_REQUIRED_METRICS ?? '封标点击率,3秒跳出率')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean),
  pythonBin: process.env.PYTHON_BIN ?? DEFAULT_PYTHON_BIN,
};

const REQUIRED_METRIC_ALIASES = {
  封标点击率: ['封标点击率', '封面点击率'],
  '3秒跳出率': ['3秒跳出率', '3s跳出率'],
};

// 真实页面验证：B 站稿件选择弹窗最多 10 个稿件，超过会 toast「最多选择10个稿件」。
const MAX_WORKS_PER_OFFICIAL_EXPORT = Math.min(
  10,
  Math.max(1, Number.parseInt(process.env.BILI_MAX_WORKS_PER_EXPORT ?? '10', 10) || 10),
);

const MIXIN_KEY_ENC_TAB = [
  46, 47, 18, 2, 53, 8, 23, 32,
  15, 50, 10, 31, 58, 3, 45, 35,
  27, 43, 5, 49, 33, 9, 42, 19,
  29, 28, 14, 39, 12, 38, 41, 13,
  37, 48, 7, 16, 24, 55, 40, 61,
  26, 17, 0, 1, 60, 51, 30, 4,
  22, 25, 54, 21, 56, 59, 6, 63,
  57, 62, 11, 36, 20, 34, 44, 52,
];

function parseDateValue(dateStr) {
  if (!dateStr) return null;
  const match = String(dateStr).trim().match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})/);
  if (!match) return null;
  const [, year, month, day] = match;
  const date = new Date(Number.parseInt(year, 10), Number.parseInt(month, 10) - 1, Number.parseInt(day, 10));
  if (Number.isNaN(date.getTime())) return null;
  return date.getTime();
}

function parseChineseDateTime(value) {
  const text = String(value || '').trim();
  const match = text.match(/(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})[日]?\s*(\d{1,2}:\d{2}(?::\d{2})?)?/);
  if (!match) return null;
  const [, year, month, day] = match;
  const date = new Date(Number.parseInt(year, 10), Number.parseInt(month, 10) - 1, Number.parseInt(day, 10));
  if (Number.isNaN(date.getTime())) return null;
  return date.getTime();
}

function startOfLocalDay(ts) {
  const date = new Date(ts);
  date.setHours(0, 0, 0, 0);
  return date.getTime();
}

function meetsDateRangeFromTs(ts) {
  if (!ts) return false;
  const minTs = parseDateValue(CONFIG.minPublishDate);
  const maxTs = parseDateValue(CONFIG.maxPublishDate);
  const dayTs = startOfLocalDay(ts);
  if (minTs && dayTs < startOfLocalDay(minTs)) return false;
  if (maxTs && dayTs > startOfLocalDay(maxTs)) return false;
  return true;
}

function md5(text) {
  return crypto.createHash('md5').update(text).digest('hex');
}

function getMixinKey(orig) {
  return MIXIN_KEY_ENC_TAB.map((index) => orig[index]).join('').slice(0, 32);
}

function extractKeyFromUrl(url) {
  const seg = String(url || '').split('/').pop() || '';
  return seg.includes('.') ? seg.split('.')[0] : seg;
}

function buildWbiQuery(params, imgKey, subKey) {
  const mixinKey = getMixinKey(`${imgKey}${subKey}`);
  const currTime = Math.round(Date.now() / 1000);
  const payload = { ...params, wts: currTime };
  const query = Object.keys(payload)
    .sort()
    .map((key) => {
      const value = String(payload[key]).replace(/[!'()*]/g, '');
      return `${encodeURIComponent(key)}=${encodeURIComponent(value)}`;
    })
    .join('&');
  const wRid = md5(query + mixinKey);
  return `${query}&w_rid=${wRid}`;
}

function normalizeBilibiliTitle(value) {
  return String(value ?? '')
    .replace(/\n+/g, ' ')
    .replace(/[\u200B-\u200D\uFEFF\u00A0]+/gu, ' ')
    .replace(/＃/g, '#')
    .replace(/\s*#.*$/u, '')
    .replace(/\s+/g, ' ')
    .trim();
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
  const match = text.match(/(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})/);
  if (!match) return '';
  const yyyy = match[1];
  const mm = String(Number.parseInt(match[2], 10)).padStart(2, '0');
  const dd = String(Number.parseInt(match[3], 10)).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

function normalizePublishKey(value) {
  return formatDate(value) || String(value || '').trim();
}

function workCardKey(card) {
  return `${normalizeBilibiliTitle(card.title)}|${normalizePublishKey(card.publishText)}`;
}

export function classifyBilibiliTargetDiscovery({
  responseShapeOk = true,
  rawWorks = 0,
  acceptedWorks = 0,
  invalidWorks = 0,
  outsideDateWorks = 0,
} = {}) {
  if (acceptedWorks > 0) return 'ok';
  if (!responseShapeOk) return 'response_shape_changed';
  if (rawWorks <= 0) return 'api_empty';
  if (invalidWorks >= rawWorks) return 'unparseable_items';
  if (outsideDateWorks >= Math.max(1, rawWorks - invalidWorks)) return 'outside_date_range';
  return 'no_eligible_items';
}

export function buildBilibiliOfficialFallbackTargets(cards, {
  minDate = '',
  maxDate = '',
  videoLimit = 0,
} = {}) {
  const minTs = parseDateValue(minDate);
  const maxTs = parseDateValue(maxDate);
  const worksByKey = new Map();

  for (const card of Array.isArray(cards) ? cards : []) {
    const title = normalizeBilibiliTitle(card?.title || '');
    const publishText = formatDate(card?.publishText) || '';
    const publishTs = Number(card?.publishTs) || parseChineseDateTime(card?.publishText);
    if (!title || !publishText || !publishTs) continue;

    const dayTs = startOfLocalDay(publishTs);
    if (minTs && dayTs < startOfLocalDay(minTs)) continue;
    if (maxTs && dayTs > startOfLocalDay(maxTs)) continue;

    const normalized = {
      ...card,
      targetId: String(card?.targetId || `official:${title}|${publishText}`),
      title,
      publishText,
      publishTs,
      bvid: String(card?.bvid || '').trim(),
      aid: String(card?.aid || '').trim(),
    };
    worksByKey.set(workCardKey(normalized), normalized);
  }

  const works = Array.from(worksByKey.values())
    .sort((left, right) => (right.publishTs || 0) - (left.publishTs || 0));
  return videoLimit > 0 ? works.slice(0, videoLimit) : works;
}

export function shouldStopBilibiliOfficialFallbackScroll({
  scrollChanged = true,
  atBottom = false,
  reachedDateBoundary = false,
  stableRounds = 0,
} = {}) {
  if (reachedDateBoundary && stableRounds >= 2) return true;
  return (!scrollChanged || atBottom) && stableRounds >= 2;
}

function coverageKey(title, publishText) {
  return `${normalizeBilibiliTitle(title)}|${normalizePublishKey(publishText)}`;
}

export function validateTargetCoverage(targetWorks, normalizedRows) {
  const rows = Array.isArray(normalizedRows) ? normalizedRows : [];
  const missing = (Array.isArray(targetWorks) ? targetWorks : [])
    .filter((target) => !rows.some((row) => (
      normalizePublishKey(target?.publishText) === normalizePublishKey(row?.发布日期 || row?.发布时间)
      && titlesLooselyMatch(target?.title, row?.标题)
    )))
    .map((target) => coverageKey(target?.title, target?.publishText));
  return { ok: missing.length === 0, missing };
}

export function shouldStopBilibiliTargetScroll({
  found = 0,
  target = 0,
  scrollChanged = true,
  atBottom = false,
  reachedDateBoundary = false,
  stableRounds = 0,
} = {}) {
  if (target > 0 && found >= target) return true;
  return (!scrollChanged || atBottom) && reachedDateBoundary && stableRounds >= 2;
}

export function buildOfficialBatchError(batchIndex, error) {
  const message = error instanceof Error ? error.message : String(error);
  return new Error(`official_batch_failed:${batchIndex + 1}:${message}`);
}

function titlesLooselyMatch(left, right) {
  const normalizedLeft = normalizeBilibiliTitle(left);
  const normalizedRight = normalizeBilibiliTitle(right);
  if (!normalizedLeft || !normalizedRight) return false;
  if (normalizedLeft === normalizedRight) return true;
  const shorter = normalizedLeft.length <= normalizedRight.length ? normalizedLeft : normalizedRight;
  const longer = normalizedLeft.length <= normalizedRight.length ? normalizedRight : normalizedLeft;
  const prefixLength = Math.min(18, shorter.length);
  if (prefixLength < 6) return false;
  return longer.includes(shorter.slice(0, prefixLength));
}

function workCardsLooselyMatch(targetWork, card) {
  return normalizePublishKey(targetWork.publishText) === normalizePublishKey(card.publishText)
    && titlesLooselyMatch(targetWork.title, card.title);
}

function findMatchingTargetWork(targetWorks, card, matchedTargetIds = null) {
  return targetWorks.find((targetWork) => {
    if (matchedTargetIds && matchedTargetIds.has(targetWork.targetId)) return false;
    return workCardsLooselyMatch(targetWork, card);
  }) || null;
}

function chunkArray(items, size) {
  const chunks = [];
  for (let index = 0; index < items.length; index += size) {
    chunks.push(items.slice(index, index + size));
  }
  return chunks;
}

function newProgressState() {
  return {
    platform: 'bilibili',
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
  };
}

async function ensureDir(dirPath) {
  await fs.mkdir(dirPath, { recursive: true });
}

async function updateProgress(patch = {}) {
  const current = newProgressState();
  try {
    const text = await fs.readFile(CONFIG.progressPath, 'utf-8');
    Object.assign(current, JSON.parse(text));
  } catch {
    // ignore
  }
  Object.assign(current, patch, { updatedAt: new Date().toISOString() });
  await ensureDir(path.dirname(CONFIG.progressPath));
  const tmpPath = `${CONFIG.progressPath}.${process.pid}.${Math.random().toString(36).slice(2)}.tmp`;
  await fs.writeFile(tmpPath, JSON.stringify(current, null, 2), 'utf-8');
  await fs.rename(tmpPath, CONFIG.progressPath);
}

async function cleanProfileLocks(profileDir) {
  if (!CONFIG.cleanProfileLocks) return;
  for (const name of ['SingletonLock', 'SingletonCookie', 'SingletonSocket']) {
    try {
      await fs.rm(path.join(profileDir, name), { force: true, recursive: true });
    } catch {
      // ignore
    }
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJson(request, url) {
  for (let attempt = 0; attempt <= CONFIG.retryMax; attempt += 1) {
    const response = await request.get(url, {
      timeout: 60_000,
      headers: {
        Referer: CONFIG.homeUrl,
      },
    });
    const text = await response.text();

    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      throw new Error(`B 站接口返回了非 JSON 响应(${response.status()}): ${text.slice(0, 200)}`);
    }

    const code = payload?.code;
    if (code === undefined || code === 0) {
      return payload;
    }

    const message = String(payload?.message ?? payload?.msg ?? '').trim();
    if (code === -799 && attempt < CONFIG.retryMax) {
      const base = Math.max(500, CONFIG.retryBaseDelayMs);
      const delay = Math.min(CONFIG.retryMaxDelayMs, base * Math.pow(2, attempt));
      const jitter = Math.floor(Math.random() * Math.max(0, CONFIG.retryJitterMs));
      await sleep(delay + jitter);
      continue;
    }

    throw new Error(`B 站接口错误（code=${code}${message ? ` ${message}` : ''}）`);
  }
  throw new Error('B 站接口频控（code=-799）重试失败');
}

function isLoginUrl(url) {
  return /passport\.bilibili\.com|\/login/i.test(String(url || ''));
}

function isCreatorPlatformUrl(url) {
  return /member\.bilibili\.com\/platform/i.test(String(url || ''));
}

async function pageHasDashboardMarkers(page) {
  try {
    for (const frame of page.frames()) {
      if (!frame.url().includes('/york/data-center-web')) continue;
      const bodyText = await frame.locator('body').innerText({ timeout: 1500 }).catch(() => '');
      if (/数据概览|稿件分析|近期稿件对比/.test(bodyText)) return true;
    }
    const bodyText = await page.locator('body').innerText({ timeout: 1500 }).catch(() => '');
    if (isCreatorPlatformUrl(page.url()) && /数据中心|稿件管理|内容管理|数据概览|稿件分析/.test(bodyText)) {
      return true;
    }
  } catch {
    // ignore
  }
  return false;
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

export async function classifyBilibiliAuthPageOnce(page) {
  try {
    if (isLoginUrl(page.url())) return 'login_required';

    const loginMarkers = [
      'text=扫码登录',
      'text=请登录',
      'text=登录 B 站',
      'text=登录B站',
      'text=账号登录',
      'text=密码登录',
      'text=验证码登录',
      'text=短信登录',
      'button:has-text("立即登录")',
      'button:has-text("扫码登录")',
      'iframe[src*="passport"]',
      'iframe[src*="login"]',
    ];
    for (const marker of loginMarkers) {
      if (await visibleMarkerExists(page, marker)) return 'login_required';
    }

    if (await pageHasDashboardMarkers(page)) return 'authorized';
  } catch {
    // Navigation and renderer failures are transient, not proof that cookies expired.
  }
  return 'transient';
}

export async function classifyBilibiliAuthPage(page, options = {}) {
  const stableChecks = Math.max(2, Number.parseInt(options.stableChecks ?? '2', 10) || 2);
  const settleMs = Number.isFinite(options.settleMs)
    ? Math.max(0, options.settleMs)
    : Math.min(Math.max(CONFIG.scanPollMs, 500), 1500);
  let stableClassification = '';

  for (let index = 0; index < stableChecks; index += 1) {
    const current = await classifyBilibiliAuthPageOnce(page);
    if (current === 'transient') return 'transient';
    if (stableClassification && current !== stableClassification) return 'transient';
    stableClassification = current;
    if (index + 1 < stableChecks) await page.waitForTimeout(settleMs);
  }
  return stableClassification || 'transient';
}

async function findDataFrame(page, { requireRecent = false, timeoutMs = 45_000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const frame of page.frames()) {
      const frameUrl = frame.url();
      if (!frameUrl.includes('/york/data-center-web')) continue;
      const text = await frame.locator('body').innerText({ timeout: 1500 }).catch(() => '');
      const basicReady = text.includes('数据概览') || text.includes('稿件分析') || text.includes('近期稿件对比');
      const recentReady = text.includes('近期稿件对比') && text.includes('稿件选择');
      if (basicReady && (!requireRecent || recentReady)) return frame;
    }
    await sleep(700);
  }
  throw new Error(requireRecent ? '未找到 B 站数据中心「近期稿件对比」iframe' : '未找到 B 站数据中心 iframe');
}

async function isDashboardReady(page) {
  return (await classifyBilibiliAuthPageOnce(page)) === 'authorized';
}

async function isDashboardStable(page) {
  return (await classifyBilibiliAuthPage(page)) === 'authorized';
}

async function findReadyDashboardPage(context, preferredPage = null) {
  const pages = [];
  if (preferredPage && !preferredPage.isClosed?.()) {
    pages.push(preferredPage);
  }
  for (const candidate of context.pages()) {
    if (!candidate || candidate === preferredPage || candidate.isClosed?.()) continue;
    pages.push(candidate);
  }
  for (const candidate of pages) {
    if (await isDashboardStable(candidate)) return candidate;
  }
  return null;
}

async function waitForDashboard(context, page) {
  const deadline = Date.now() + CONFIG.scanWaitMs;
  let lastDashboardJumpAt = 0;
  while (Date.now() < deadline) {
    const readyPage = await findReadyDashboardPage(context, page);
    if (readyPage) return readyPage;
    const currentUrl = page.url();
    const shouldRetryDashboard =
      !isLoginUrl(currentUrl)
      && !currentUrl.includes('/platform/data-up/video')
      && Date.now() - lastDashboardJumpAt > 12000;
    if (shouldRetryDashboard) {
      lastDashboardJumpAt = Date.now();
      await page.goto(CONFIG.dashboardUrl, { waitUntil: 'domcontentloaded', timeout: 60_000 }).catch(() => {});
      await page.waitForTimeout(1200);
      const retriedReadyPage = await findReadyDashboardPage(context, page);
      if (retriedReadyPage) return retriedReadyPage;
    }
    await page.waitForTimeout(CONFIG.scanPollMs);
  }
  return null;
}

async function ensureDashboard(context, page) {
  page = await navigateAuthCandidates(context, page, [CONFIG.dashboardUrl], { timeout: 60_000, settleMs: 2000 });
  const initialReadyPage = await findReadyDashboardPage(context, page);
  if (initialReadyPage) return initialReadyPage;

  if (CONFIG.headless) {
    const authClassification = await classifyBilibiliAuthPage(page);
    if (authClassification === 'login_required') {
      throw new Error('B 站未登录（headless=true 无法扫码登录）');
    }
    throw new Error('B 站创作中心数据页暂不可访问（登录态未作失效处理）');
  }

  await updateProgress({
    phase: 'login',
    message: `请在浏览器中登录 B 站创作中心（最多 ${Math.round(CONFIG.scanWaitMs / 1000)} 秒）`,
  });

  const readyPage = await waitForDashboard(context, page);
  if (!readyPage) {
    const authClassification = await classifyBilibiliAuthPage(page);
    if (authClassification !== 'login_required') {
      throw new Error('B 站创作中心数据页暂不可访问（登录态未作失效处理）');
    }
    const currentUrl = page.url();
    const title = await page.title().catch(() => '');
    throw new Error(`B 站登录超时或未进入数据页（当前页：${currentUrl} ${title}）`);
  }
  return readyPage;
}

async function scrollRecentComparisonIntoView(frame) {
  for (let round = 0; round < 28; round += 1) {
    const found = await frame.evaluate(() => {
      const visible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
      };
      const target = Array.from(document.querySelectorAll('body *')).find((el) => {
        const text = (el.textContent || '').replace(/\s+/g, '');
        return visible(el) && (text.includes('近期稿件对比') || text.includes('稿件选择'));
      });
      if (!target) return false;
      target.scrollIntoView({ block: 'center', inline: 'nearest' });
      return true;
    });
    if (found) {
      await sleep(800);
      return;
    }

    const scrollState = await frame.evaluate(() => {
      const visible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 300 && rect.height > 300 && style.display !== 'none' && style.visibility !== 'hidden';
      };
      const candidates = [document.scrollingElement || document.documentElement, ...Array.from(document.querySelectorAll('*'))]
        .filter((el) => el && el.scrollHeight > el.clientHeight + 80 && visible(el))
        .map((el) => ({ el, area: el.clientWidth * el.clientHeight, before: el.scrollTop }))
        .sort((a, b) => b.area - a.area);
      const picked = candidates[0];
      if (!picked) {
        window.scrollBy(0, 900);
        return { changed: true, source: 'window' };
      }
      picked.el.scrollBy(0, Math.max(700, Math.floor(picked.el.clientHeight * 0.85)));
      return { changed: picked.el.scrollTop > picked.before, source: picked.el === document.scrollingElement ? 'document' : 'element' };
    });
    if (!scrollState.changed) {
      await frame.evaluate(() => window.scrollBy(0, 1100)).catch(() => {});
    }
    await sleep(900);
  }
  throw new Error('未找到 B 站「近期稿件对比」模块，请确认数据概览页已加载并可向下滚动');
}

async function selectDataMode(frame) {
  if (!CONFIG.dataMode) return;
  const modeText = frame.getByText(CONFIG.dataMode, { exact: false }).first();
  if ((await modeText.count()) > 0) return;

  const selectedMode = frame.locator('text=/历史累计数据|近期数据|单稿件/').first();
  if ((await selectedMode.count()) === 0) return;
  await selectedMode.click({ timeout: 5000 }).catch(() => {});
  await sleep(500);
  const option = frame.getByText(CONFIG.dataMode, { exact: false }).last();
  if ((await option.count()) > 0) {
    await option.click({ timeout: 5000 }).catch(() => {});
    await sleep(800);
  }
}

// 导出前必须先补齐指标：否则官方导出的 CSV 表头会直接缺列，后续归一化拿不到值。
async function ensureComparisonMetrics(frame) {
  if (!CONFIG.requiredMetrics.length) return;
  await updateProgress({
    phase: 'selecting',
    message: `确认 B 站自选指标：${CONFIG.requiredMetrics.join('、')}`,
  });

  await scrollRecentComparisonIntoView(frame);

  const opened = await frame.evaluate(() => {
    const normalizeText = (value) => String(value || '').replace(/\s+/g, '');
    const isVisible = (el) => {
      if (!el || typeof el.getBoundingClientRect !== 'function') return false;
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return rect.width > 8
        && rect.height > 8
        && rect.bottom > 0
        && rect.top < window.innerHeight
        && style.visibility !== 'hidden'
        && style.display !== 'none'
        && style.opacity !== '0';
    };
    const candidates = Array.from(document.querySelectorAll('button, [role="button"], div, span'))
      .filter((el) => normalizeText(el.innerText || el.textContent) === '自选指标')
      .filter(isVisible);
    const target = candidates[candidates.length - 1];
    if (!target) return false;
    (target.closest('button, [role="button"], [tabindex]') || target).click();
    return true;
  });
  if (!opened) throw new Error('未定位到 B 站「自选指标」按钮');

  const modalReady = await frame.waitForFunction(() => {
    const text = document.body.innerText || '';
    return text.includes('自选指标') && text.includes('勾选选择数据');
  }, null, { timeout: 15_000 }).then(() => true).catch(() => false);
  if (!modalReady) throw new Error('B 站「自选指标」弹窗未打开');

  const selectedMetrics = await frame.evaluate(({ requiredMetrics, metricAliases }) => {
    const normalizeText = (value) => String(value || '').replace(/\s+/g, '');
    const isVisible = (el) => {
      if (!el || typeof el.getBoundingClientRect !== 'function') return false;
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return rect.width > 8
        && rect.height > 8
        && rect.bottom > 0
        && rect.top < window.innerHeight
        && style.visibility !== 'hidden'
        && style.display !== 'none'
        && style.opacity !== '0';
    };
    const modal = Array.from(document.querySelectorAll('[role="dialog"], .bili-modal, .bcc-dialog, .modal'))
      .filter((el) => (el.innerText || '').includes('勾选选择数据'))
      .filter(isVisible)
      .sort((a, b) => (b.getBoundingClientRect().width * b.getBoundingClientRect().height)
        - (a.getBoundingClientRect().width * a.getBoundingClientRect().height))[0] || document.body;
    const result = {};

    const findCheckboxTarget = (alias) => {
      const aliasText = normalizeText(alias);
      const labels = Array.from(modal.querySelectorAll('label.bcc-checkbox'))
        .filter(isVisible)
        .filter((el) => normalizeText(el.innerText || el.textContent).includes(aliasText));
      if (labels.length > 0) {
        const exact = labels.find((el) => normalizeText(el.innerText || el.textContent) === aliasText);
        const label = exact || labels[0];
        return {
          label,
          input: label.querySelector('input[type="checkbox"]'),
        };
      }

      const inputs = Array.from(modal.querySelectorAll('input[type="checkbox"]'))
        .map((input) => ({
          input,
          label: input.closest('label.bcc-checkbox') || input.closest('label'),
          text: normalizeText(input.value || input.getAttribute('value') || ''),
        }))
        .filter(({ input, label, text }) => text.includes(aliasText) && isVisible(label || input));
      if (inputs.length > 0) {
        const exact = inputs.find(({ text }) => text === aliasText);
        const { input, label } = exact || inputs[0];
        return { label, input };
      }

      return null;
    };

    for (const metric of requiredMetrics) {
      const aliases = (metricAliases?.[metric] || [metric]).map((item) => normalizeText(item));
      let found = false;
      let checked = false;

      for (const alias of aliases) {
        const target = findCheckboxTarget(alias);
        if (!target?.label || !target.input) continue;
        found = true;

        if (!target.input.checked) {
          target.label.click();
        }
        if (!target.input.checked) {
          target.input.click();
        }
        checked = Boolean(target.input.checked)
          || /bcc-checkbox-checked/.test(String(target.label.className || ''));
        if (checked) break;
      }

      result[metric] = { found, checked };
    }
    return result;
  }, { requiredMetrics: CONFIG.requiredMetrics, metricAliases: REQUIRED_METRIC_ALIASES });

  const failedMetrics = CONFIG.requiredMetrics.filter((metric) => !selectedMetrics[metric]?.found || !selectedMetrics[metric]?.checked);
  if (failedMetrics.length) {
    throw new Error(`B 站自选指标未找到：${failedMetrics.join('、')}`);
  }

  const confirmed = await frame.evaluate(() => {
    const isVisible = (el) => {
      if (!el || typeof el.getBoundingClientRect !== 'function') return false;
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return rect.width > 8
        && rect.height > 8
        && rect.bottom > 0
        && rect.top < window.innerHeight
        && style.visibility !== 'hidden'
        && style.display !== 'none'
        && style.opacity !== '0';
    };
    const buttons = Array.from(document.querySelectorAll('button, [role="button"], div'))
      .filter((el) => (el.textContent || '').trim() === '确定')
      .filter(isVisible);
    const target = buttons[buttons.length - 1];
    if (!target) return false;
    target.click();
    return true;
  });
  if (!confirmed) throw new Error('未定位到 B 站「自选指标」确定按钮');

  await sleep(1500);
  const reopened = await frame.evaluate(() => {
    const normalizeText = (value) => String(value || '').replace(/\s+/g, '');
    const isVisible = (el) => {
      if (!el || typeof el.getBoundingClientRect !== 'function') return false;
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return rect.width > 8
        && rect.height > 8
        && rect.bottom > 0
        && rect.top < window.innerHeight
        && style.visibility !== 'hidden'
        && style.display !== 'none'
        && style.opacity !== '0';
    };
    const reopenButton = Array.from(document.querySelectorAll('button, [role="button"], div, span'))
      .filter((el) => normalizeText(el.innerText || el.textContent) === '自选指标')
      .filter(isVisible)
      .pop();
    (reopenButton?.closest('button, [role="button"], [tabindex]') || reopenButton)?.click();
    return Boolean(reopenButton);
  });

  const reopenedReady = reopened
    ? await frame.waitForFunction(() => {
      const isVisible = (el) => {
        if (!el || typeof el.getBoundingClientRect !== 'function') return false;
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return rect.width > 8
          && rect.height > 8
          && rect.bottom > 0
          && rect.top < window.innerHeight
          && style.visibility !== 'hidden'
          && style.display !== 'none'
          && style.opacity !== '0';
      };
      return Array.from(document.querySelectorAll('[role="dialog"], .bili-modal, .bcc-dialog, .modal'))
        .some((el) => isVisible(el) && (el.innerText || '').includes('勾选选择数据'));
    }, null, { timeout: 8_000 }).then(() => true).catch(() => false)
    : false;

  const persistedMetrics = await frame.evaluate(({ requiredMetrics, metricAliases }) => {
    const normalizeText = (value) => String(value || '').replace(/\s+/g, '');
    const isVisible = (el) => {
      if (!el || typeof el.getBoundingClientRect !== 'function') return false;
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return rect.width > 8
        && rect.height > 8
        && rect.bottom > 0
        && rect.top < window.innerHeight
        && style.visibility !== 'hidden'
        && style.display !== 'none'
        && style.opacity !== '0';
    };

    const modal = Array.from(document.querySelectorAll('[role="dialog"], .bili-modal, .bcc-dialog, .modal'))
      .filter((el) => (el.innerText || '').includes('勾选选择数据'))
      .filter(isVisible)
      .sort((a, b) => (b.getBoundingClientRect().width * b.getBoundingClientRect().height)
        - (a.getBoundingClientRect().width * a.getBoundingClientRect().height))[0] || document.body;
    const modalReady = modal !== document.body;

    const result = { __modalReady: modalReady };
    for (const metric of requiredMetrics) {
      const aliases = (metricAliases?.[metric] || [metric]).map((item) => normalizeText(item));
      const labels = Array.from(modal.querySelectorAll('label.bcc-checkbox'))
        .filter(isVisible)
        .map((label) => ({
          label,
          input: label.querySelector('input[type="checkbox"]'),
          text: normalizeText(label.innerText || label.textContent),
        }))
        .filter(({ input, text }) => input && aliases.some((alias) => text.includes(alias)));
      const target = labels.find(({ text }) => aliases.includes(text)) || labels[0];
      const label = target?.label;
      const input = target?.input;
      result[metric] = Boolean(input?.checked) || /bcc-checkbox-checked/.test(String(label?.className || ''));
    }

    const cancelBtn = Array.from(document.querySelectorAll('button'))
      .find((button) => normalizeText(button.innerText || button.textContent) === '取消');
    cancelBtn?.click();
    return result;
  }, { requiredMetrics: CONFIG.requiredMetrics, metricAliases: REQUIRED_METRIC_ALIASES });

  if (reopenedReady && persistedMetrics.__modalReady) {
    const missingAfter = CONFIG.requiredMetrics.filter((metric) => !persistedMetrics[metric]);
    if (missingAfter.length) {
      throw new Error(`B 站自选指标保存后仍未勾选：${missingAfter.join('、')}`);
    }
  } else {
    console.warn('[bili-warning] B 站自选指标保存后未能稳定复查弹窗，将以官方导出 CSV 字段校验为准');
  }
}

async function openWorkSelection(frame) {
  await scrollRecentComparisonIntoView(frame);
  await frame.getByText('稿件选择', { exact: false }).first().click({ timeout: 15_000 });
  await frame.getByText('稿件列表', { exact: false }).first().waitFor({ timeout: 15_000 });
  await sleep(1000);
}

async function listTargetWorksByApi(page) {
  await updateProgress({
    phase: 'selecting',
    message: `通过 B 站稿件接口锁定 ${CONFIG.minPublishDate || '不限起始日期'} 之后的目标稿件`,
  });

  const nav = await fetchJson(page.request, CONFIG.navApiUrl);
  const mid = String(nav?.data?.mid || '').trim();
  if (!mid) throw new Error('无法获取 B 站 mid（可能未登录）');

  const imgKey = extractKeyFromUrl(nav?.data?.wbi_img?.img_url);
  const subKey = extractKeyFromUrl(nav?.data?.wbi_img?.sub_url);
  if (!imgKey || !subKey) {
    throw new Error('无法获取 B 站 wbi_img keys（可能接口变更或登录失效）');
  }

  const worksByKey = new Map();
  const minTs = parseDateValue(CONFIG.minPublishDate);
  const diagnostics = {
    responseShapeOk: true,
    pagesRequested: 0,
    rawWorks: 0,
    invalidWorks: 0,
    outsideDateWorks: 0,
  };

  for (let pageNo = 1; pageNo <= 200; pageNo += 1) {
    diagnostics.pagesRequested += 1;
    await sleep(CONFIG.requestGapMs + Math.floor(Math.random() * 120));
    const query = buildWbiQuery(
      {
        mid,
        pn: pageNo,
        ps: CONFIG.pageSize,
        order: 'pubdate',
      },
      imgKey,
      subKey,
    );
    const payload = await fetchJson(page.request, `${CONFIG.wbiArcSearchBase}?${query}`);
    const list = payload?.data?.list?.vlist ?? payload?.data?.list?.vList;
    if (!Array.isArray(list)) {
      diagnostics.responseShapeOk = false;
      break;
    }
    if (list.length === 0) break;
    diagnostics.rawWorks += list.length;

    let oldestPageTs = null;
    for (const item of list) {
      const publishText = formatDate(item?.created);
      const publishTs = parseDateValue(publishText);
      if (publishTs) {
        oldestPageTs = oldestPageTs === null ? publishTs : Math.min(oldestPageTs, publishTs);
      }

      const card = {
        targetId: String(item?.bvid || item?.bvid_str || item?.aid || `${pageNo}-${worksByKey.size}`),
        title: normalizeBilibiliTitle(item?.title || ''),
        publishText,
        publishTs,
        bvid: String(item?.bvid || item?.bvid_str || '').trim(),
        aid: String(item?.aid || '').trim(),
      };
      if (!card.title || !card.publishText || !card.publishTs) {
        diagnostics.invalidWorks += 1;
        continue;
      }
      if (!meetsDateRangeFromTs(card.publishTs)) {
        diagnostics.outsideDateWorks += 1;
        continue;
      }
      worksByKey.set(workCardKey(card), card);
    }

    await updateProgress({
      phase: 'selecting',
      message: `B 站稿件接口已锁定 ${worksByKey.size} 条目标稿件`,
      totalWorks: CONFIG.videoLimit > 0 ? Math.min(CONFIG.videoLimit, worksByKey.size) : worksByKey.size,
      processedWorks: worksByKey.size,
      successWorks: worksByKey.size,
      queuedWorks: 0,
      currentIndex: worksByKey.size,
    });

    if (CONFIG.videoLimit > 0 && worksByKey.size >= CONFIG.videoLimit) break;
    if (minTs && oldestPageTs && startOfLocalDay(oldestPageTs) < startOfLocalDay(minTs)) break;
  }

  const works = Array.from(worksByKey.values()).sort((left, right) => (right.publishTs || 0) - (left.publishTs || 0));
  const limitedWorks = CONFIG.videoLimit > 0 ? works.slice(0, CONFIG.videoLimit) : works;
  return {
    works: limitedWorks,
    diagnostics: {
      ...diagnostics,
      acceptedWorks: limitedWorks.length,
      reason: classifyBilibiliTargetDiscovery({
        ...diagnostics,
        acceptedWorks: limitedWorks.length,
      }),
    },
  };
}

async function readSelectedWorkCount(frame) {
  return frame.evaluate(() => {
    const isVisible = (el) => {
      if (!el || typeof el.getBoundingClientRect !== 'function') return false;
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      return rect.width > 20
        && rect.height > 20
        && rect.bottom > 0
        && rect.top < window.innerHeight
        && style.visibility !== 'hidden'
        && style.display !== 'none'
        && style.opacity !== '0';
    };
    const dialog = Array.from(document.querySelectorAll('[role="dialog"], .bili-modal, .bcc-dialog, .modal'))
      .filter(isVisible)
      .find((el) => (el.innerText || '').includes('稿件列表'));
    const text = (dialog?.innerText || '').trim();
    const matches = Array.from(text.matchAll(/已选\s*(\d+)\s*个稿件/g));
    const latest = matches.at(-1);
    return latest ? Number.parseInt(latest[1], 10) : 0;
  });
}

async function getVisibleWorkCards(frame) {
  const cards = await frame.evaluate(() => {
    const datePattern = /(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})[日]?\s*(\d{1,2}:\d{2}(?::\d{2})?)?/;
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      return rect.width > 20 && rect.height > 20 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const dialog = Array.from(document.querySelectorAll('[role="dialog"], .bili-modal, .bcc-dialog, .modal, body'))
      .find((el) => (el.innerText || '').includes('稿件列表')) || document.body;
    const candidateCards = (() => {
      const explicitCards = Array.from(dialog.querySelectorAll('*'))
        .filter((el) => el.classList?.contains('arcp-archive'))
        .filter(visible);
      if (explicitCards.length > 0) {
        return explicitCards;
      }
      return Array.from(dialog.querySelectorAll('*'))
        .filter((node) => {
          if (!visible(node)) return false;
          const text = node.innerText || '';
          if (!text.match(datePattern)) return false;
          if (!node.querySelector('img') && !node.querySelector('video') && !node.querySelector('picture')) return false;
          const rect = node.getBoundingClientRect();
          return rect.width >= 120 && rect.width <= 320 && rect.height >= 90 && rect.height <= 260;
        });
    })();

    const unique = new Map();
    for (const card of candidateCards) {
      const rect = card.getBoundingClientRect();
      if (rect.width < 120 || rect.height < 90 || rect.top < 0 || rect.bottom > window.innerHeight + 120) continue;
      const cardText = card.innerText || '';
      const publishText = (cardText.match(datePattern) || [])[0] || '';
      if (!publishText) continue;
      const titleNode = card.querySelector(
        '.arcp-archive-title, [class*="archive-title"], [class*="archive-name"]',
      );
      const title = String(titleNode?.innerText || titleNode?.textContent || '')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 120) || cardText
        .replace(publishText, '')
        .replace(/\d+:\d{2}/g, '')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 120);
      const key = `${title}|${publishText}`;
      if (unique.has(key)) continue;

      const chainClass = [];
      let cursor = card;
      for (let i = 0; i < 4 && cursor; i += 1) {
        chainClass.push(String(cursor.className || ''));
        cursor = cursor.parentElement;
      }
      const classText = chainClass.join(' ').toLowerCase();
      const style = window.getComputedStyle(card);
      const selectedByInput = Boolean(card.querySelector('input[type="checkbox"]:checked'));
      const selectedByClass = /(selected|active|checked|is-checked)/i.test(classText);
      const selectedByStyle = /0,\s*161,\s*214|0,\s*174,\s*236|0,\s*170,\s*238/.test(`${style.borderColor} ${style.backgroundColor}`);
      const selected = selectedByInput || selectedByClass || selectedByStyle;

      unique.set(key, {
        title,
        publishText,
        selected,
        top: rect.top,
      });
    }

    return Array.from(unique.values()).sort((a, b) => a.top - b.top);
  });

  return cards.map((card) => ({
    ...card,
    title: normalizeBilibiliTitle(card.title),
    publishText: formatDate(card.publishText) || card.publishText,
    publishTs: parseChineseDateTime(card.publishText),
  }));
}

async function clickWorkCard(frame, targetCard) {
  return frame.evaluate(({ title, publishText }) => {
    const datePattern = /(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})[日]?\s*(\d{1,2}:\d{2}(?::\d{2})?)?/;
    const normalizeDate = (value) => {
      const match = String(value || '').match(/(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})/);
      if (!match) return '';
      const yyyy = match[1];
      const mm = String(Number.parseInt(match[2], 10)).padStart(2, '0');
      const dd = String(Number.parseInt(match[3], 10)).padStart(2, '0');
      return `${yyyy}-${mm}-${dd}`;
    };
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      return rect.width > 20 && rect.height > 20 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const normalizedTargetDate = normalizeDate(publishText);
    const dialog = Array.from(document.querySelectorAll('[role="dialog"], .bili-modal, .bcc-dialog, .modal, body'))
      .find((el) => (el.innerText || '').includes('稿件列表')) || document.body;
    const candidateCards = (() => {
      const explicitCards = Array.from(dialog.querySelectorAll('*'))
        .filter((el) => el.classList?.contains('arcp-archive'))
        .filter(visible);
      if (explicitCards.length > 0) {
        return explicitCards;
      }
      return Array.from(dialog.querySelectorAll('*'))
        .filter((node) => {
          if (!visible(node)) return false;
          const text = node.innerText || '';
          if (!text.match(datePattern)) return false;
          if (!node.querySelector('img') && !node.querySelector('video') && !node.querySelector('picture')) return false;
          const rect = node.getBoundingClientRect();
          return rect.width >= 120 && rect.width <= 320 && rect.height >= 90 && rect.height <= 260;
        });
    })();

    for (const card of candidateCards) {
      const text = (card.innerText || '').replace(/\s+/g, ' ');
      const cardPublishText = (text.match(datePattern) || [])[0] || '';
      if (normalizeDate(cardPublishText) !== normalizedTargetDate) continue;
      const titleNode = card.querySelector(
        '.arcp-archive-title, [class*="archive-title"], [class*="archive-name"]',
      );
      const cardTitle = String(titleNode?.innerText || titleNode?.textContent || '')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 120) || text
        .replace(cardPublishText, '')
        .replace(/\d+:\d{2}/g, '')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 120);
      if (title && cardTitle !== title && !cardTitle.includes(title.slice(0, Math.min(18, title.length)))) continue;
      if (typeof card.click === 'function') {
        card.click();
      } else {
        card.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, composed: true }));
      }
      return true;
    }
    return false;
  }, { title: targetCard.title, publishText: targetCard.publishText });
}

async function scrollWorkList(frame) {
  return frame.evaluate(() => {
    const datePattern = /(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})[日]?\s*(\d{1,2}:\d{2}(?::\d{2})?)?/;
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      return rect.width > 20 && rect.height > 20 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const dialog = Array.from(document.querySelectorAll('[role="dialog"], .bili-modal, .bcc-dialog, .modal, body'))
      .find((el) => (el.innerText || '').includes('稿件列表')) || document.body;
    const cardCandidates = (() => {
      const explicitCards = Array.from(dialog.querySelectorAll('.arcp-archive'));
      if (explicitCards.length > 0) return explicitCards;
      return Array.from(dialog.querySelectorAll('*'))
        .filter((node) => {
          const text = node.innerText || '';
          if (!text.match(datePattern)) return false;
          if (!node.querySelector('img') && !node.querySelector('video') && !node.querySelector('picture')) return false;
          return visible(node);
        });
    })();

    const scored = new Map();
    for (const card of cardCandidates) {
      let cursor = card.parentElement;
      let distance = 1;
      while (cursor && cursor !== dialog && dialog.contains(cursor)) {
        if (cursor.scrollHeight > cursor.clientHeight + 20 && cursor.clientHeight > 120) {
          const prev = scored.get(cursor) || { el: cursor, votes: 0, minDistance: Number.POSITIVE_INFINITY };
          prev.votes += 1;
          prev.minDistance = Math.min(prev.minDistance, distance);
          scored.set(cursor, prev);
        }
        cursor = cursor.parentElement;
        distance += 1;
      }
    }

    const candidates = Array.from(scored.values())
      .map((item) => {
        const rect = item.el.getBoundingClientRect();
        return {
          ...item,
          rect,
          area: rect.width * rect.height,
          scrollRange: item.el.scrollHeight - item.el.clientHeight,
        };
      })
      .filter((item) => item.rect.width > 160 && item.rect.height > 160)
      .sort((left, right) => right.votes - left.votes
        || left.minDistance - right.minDistance
        || right.scrollRange - left.scrollRange
        || left.area - right.area);
    const target = candidates[0]?.el || document.scrollingElement || document.documentElement;
    const before = target.scrollTop;
    const delta = Math.max(420, Math.floor(target.clientHeight * 0.78));
    target.scrollBy(0, delta);
    const after = target.scrollTop;
    return {
      changed: after > before,
      atBottom: after + target.clientHeight >= target.scrollHeight - 10,
    };
  });
}

async function scrollWorkListToTop(frame) {
  await frame.evaluate(() => {
    const datePattern = /(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})[日]?\s*(\d{1,2}:\d{2}(?::\d{2})?)?/;
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      return rect.width > 20 && rect.height > 20 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const dialog = Array.from(document.querySelectorAll('[role="dialog"], .bili-modal, .bcc-dialog, .modal, body'))
      .find((el) => (el.innerText || '').includes('稿件列表')) || document.body;
    const cardCandidates = (() => {
      const explicitCards = Array.from(dialog.querySelectorAll('.arcp-archive'));
      if (explicitCards.length > 0) return explicitCards;
      return Array.from(dialog.querySelectorAll('*'))
        .filter((node) => {
          const text = node.innerText || '';
          if (!text.match(datePattern)) return false;
          if (!node.querySelector('img') && !node.querySelector('video') && !node.querySelector('picture')) return false;
          return visible(node);
        });
    })();

    const scored = new Map();
    for (const card of cardCandidates) {
      let cursor = card.parentElement;
      let distance = 1;
      while (cursor && cursor !== dialog && dialog.contains(cursor)) {
        if (cursor.scrollHeight > cursor.clientHeight + 20 && cursor.clientHeight > 120) {
          const prev = scored.get(cursor) || { el: cursor, votes: 0, minDistance: Number.POSITIVE_INFINITY };
          prev.votes += 1;
          prev.minDistance = Math.min(prev.minDistance, distance);
          scored.set(cursor, prev);
        }
        cursor = cursor.parentElement;
        distance += 1;
      }
    }

    const candidates = Array.from(scored.values())
      .map((item) => {
        const rect = item.el.getBoundingClientRect();
        return {
          ...item,
          rect,
          area: rect.width * rect.height,
          scrollRange: item.el.scrollHeight - item.el.clientHeight,
        };
      })
      .filter((item) => item.rect.width > 160 && item.rect.height > 160)
      .sort((left, right) => right.votes - left.votes
        || left.minDistance - right.minDistance
        || right.scrollRange - left.scrollRange
        || left.area - right.area);
    const target = candidates[0]?.el || document.scrollingElement || document.documentElement;
    target.scrollTo(0, 0);
  });
  await sleep(700);
}

async function confirmWorkSelection(frame) {
  const clicked = await frame.evaluate(() => {
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      return rect.width > 20
        && rect.height > 20
        && rect.bottom > 0
        && rect.top < window.innerHeight
        && style.visibility !== 'hidden'
        && style.display !== 'none'
        && style.opacity !== '0';
    };
    const candidates = Array.from(document.querySelectorAll('.arcp-queue-confirm, button, [role="button"], div'))
      .filter((el) => (el.textContent || '').trim() === '确认')
      .filter(visible);
    const active = candidates.find((el) => String(el.className || '').includes('active')) || candidates[candidates.length - 1];
    if (!active) return false;
    active.click();
    return true;
  });
  if (!clicked) throw new Error('未定位到可见的 B 站稿件选择确认按钮');
  await sleep(2000);
}

// 严格以 B 站接口返回的目标稿件集为准，再在官方导出弹窗里逐个定位，避免 UI 懒加载导致早停漏稿。
async function collectWorksByTargetSet(frame, targetWorks) {
  await updateProgress({
    phase: 'selecting',
    message: `在 B 站稿件弹窗里定位 ${targetWorks.length} 条目标稿件`,
  });
  await scrollWorkListToTop(frame);

  const worksByTargetId = new Map();
  const oldestTargetTs = targetWorks.reduce((min, work) => {
    if (!work.publishTs) return min;
    if (min === null) return work.publishTs;
    return Math.min(min, work.publishTs);
  }, null);
  let stableRounds = 0;

  for (let round = 1; round <= 160; round += 1) {
    const cards = await getVisibleWorkCards(frame);
    let newFound = 0;
    let oldestVisibleTs = null;
    const matchedTargetIds = new Set(worksByTargetId.keys());

    for (const card of cards) {
      if (!card.publishTs) continue;
      oldestVisibleTs = oldestVisibleTs === null ? card.publishTs : Math.min(oldestVisibleTs, card.publishTs);
      const matchedWork = findMatchingTargetWork(targetWorks, card, matchedTargetIds);
      if (matchedWork && !worksByTargetId.has(matchedWork.targetId)) {
        worksByTargetId.set(matchedWork.targetId, { ...matchedWork, ...card });
        matchedTargetIds.add(matchedWork.targetId);
        newFound += 1;
      }
    }

    await updateProgress({
      phase: 'selecting',
      message: `B 站稿件弹窗已定位 ${worksByTargetId.size}/${targetWorks.length} 条目标稿件`,
      totalWorks: targetWorks.length,
      processedWorks: worksByTargetId.size,
      successWorks: worksByTargetId.size,
      queuedWorks: Math.max(0, targetWorks.length - worksByTargetId.size),
      currentIndex: worksByTargetId.size,
    });

    if (shouldStopBilibiliTargetScroll({
      found: worksByTargetId.size,
      target: targetWorks.length,
    })) break;
    if (newFound === 0) stableRounds += 1;
    else stableRounds = 0;

    const scrollState = await scrollWorkList(frame);
    await sleep(700);
    const reachedBoundary = oldestVisibleTs && oldestTargetTs
      ? startOfLocalDay(oldestVisibleTs) <= startOfLocalDay(oldestTargetTs)
      : false;
    if (shouldStopBilibiliTargetScroll({
      found: worksByTargetId.size,
      target: targetWorks.length,
      scrollChanged: scrollState.changed,
      atBottom: scrollState.atBottom,
      reachedDateBoundary: reachedBoundary,
      stableRounds,
    })) break;
  }

  const missingWorks = targetWorks.filter((work) => !worksByTargetId.has(work.targetId));
  if (missingWorks.length > 0) {
    const preview = missingWorks
      .slice(0, 5)
      .map((work) => `${work.publishText} ${work.title}`)
      .join('；');
    throw new Error(`B 站稿件弹窗未完整加载目标稿件：缺少 ${missingWorks.length} 条（例如：${preview}）`);
  }

  return targetWorks.map((work) => worksByTargetId.get(work.targetId) || work);
}

async function collectWorksByOfficialDateRange(frame, diagnostics = {}) {
  const reasonLabels = {
    response_shape_changed: '返回结构变化',
    api_empty: '返回空列表',
    unparseable_items: '稿件字段不可解析',
    outside_date_range: '公开空间稿件均在日期范围外',
    no_eligible_items: '没有可用目标稿件',
  };
  const reasonLabel = reasonLabels[diagnostics.reason] || '未锁定目标稿件';
  await updateProgress({
    phase: 'selecting',
    message: `B 站空间接口${reasonLabel}，改用创作中心稿件列表按日期扫描`,
  });
  await scrollWorkListToTop(frame);

  const cardsByKey = new Map();
  const unparseableCards = new Set();
  const minTs = parseDateValue(CONFIG.minPublishDate);
  let stableRounds = 0;

  for (let round = 1; round <= 160; round += 1) {
    const cards = await getVisibleWorkCards(frame);
    let newFound = 0;
    let oldestVisibleTs = null;

    for (const card of cards) {
      const key = `${card.title || ''}|${card.publishText || ''}`;
      if (!card.publishTs) {
        if (key !== '|') unparseableCards.add(key);
        continue;
      }
      oldestVisibleTs = oldestVisibleTs === null
        ? card.publishTs
        : Math.min(oldestVisibleTs, card.publishTs);
      const normalizedKey = workCardKey(card);
      if (!cardsByKey.has(normalizedKey)) {
        cardsByKey.set(normalizedKey, card);
        newFound += 1;
      }
    }

    if (newFound === 0) stableRounds += 1;
    else stableRounds = 0;

    const eligibleWorks = buildBilibiliOfficialFallbackTargets(Array.from(cardsByKey.values()), {
      minDate: CONFIG.minPublishDate,
      maxDate: CONFIG.maxPublishDate,
      videoLimit: CONFIG.videoLimit,
    });
    await updateProgress({
      phase: 'selecting',
      message: `B 站创作中心稿件列表已扫描 ${cardsByKey.size} 条，日期范围内 ${eligibleWorks.length} 条`,
      totalWorks: eligibleWorks.length,
      processedWorks: eligibleWorks.length,
      successWorks: eligibleWorks.length,
      queuedWorks: 0,
      currentIndex: eligibleWorks.length,
    });

    const scrollState = await scrollWorkList(frame);
    await sleep(700);
    const reachedBoundary = Boolean(
      minTs
      && oldestVisibleTs
      && startOfLocalDay(oldestVisibleTs) < startOfLocalDay(minTs)
    );
    if (shouldStopBilibiliOfficialFallbackScroll({
      scrollChanged: scrollState.changed,
      atBottom: scrollState.atBottom,
      reachedDateBoundary: reachedBoundary,
      stableRounds,
    })) break;
  }

  const works = buildBilibiliOfficialFallbackTargets(Array.from(cardsByKey.values()), {
    minDate: CONFIG.minPublishDate,
    maxDate: CONFIG.maxPublishDate,
    videoLimit: CONFIG.videoLimit,
  });
  if (works.length <= 0) {
    throw new Error(
      `B 站空间接口${reasonLabel}，创作中心稿件列表也未发现符合日期范围的稿件`
      + `（日期范围：${CONFIG.minPublishDate || '不限'} - ${CONFIG.maxPublishDate || '不限'}；`
      + `已扫描 ${cardsByKey.size} 条，可解析失败 ${unparseableCards.size} 条）`,
    );
  }
  return works;
}

async function confirmTargetsWithOfficialScroll(page, targetWorks) {
  const confirmFrame = await findDataFrame(page, { requireRecent: true });
  await openWorkSelection(confirmFrame);
  const confirmed = await collectWorksByTargetSet(confirmFrame, targetWorks);
  await updateProgress({
    phase: 'selecting',
    message: `B 站已完成接口目标与官方弹窗滚动双重确认：${confirmed.length} 条`,
    totalWorks: confirmed.length,
    processedWorks: confirmed.length,
    successWorks: confirmed.length,
    queuedWorks: 0,
    currentIndex: confirmed.length,
  });
  return { confirmed, confirmFrame };
}

async function closeWorkSelection(frame) {
  await frame.page().keyboard.press('Escape').catch(() => {});
  await sleep(300);
  const closeAttempt = await frame.evaluate(() => {
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      return rect.width > 10 && rect.height > 10 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const dialog = Array.from(document.querySelectorAll('[role="dialog"], .bili-modal, .bcc-dialog, .modal'))
      .filter(visible)
      .find((el) => (el.innerText || '').includes('稿件列表'));
    // B 站会把关闭后的弹窗 DOM 留在页面里；只有仍可见的稿件弹窗才需要继续关闭。
    if (!dialog) return 'already_closed';
    const explicitClose = Array.from(dialog.querySelectorAll('button, [role="button"], span, div, i, svg'))
      .filter(visible)
      .find((el) => {
        const text = (el.textContent || '').replace(/\s+/g, '').trim();
        const hints = [el.className, el.getAttribute?.('aria-label'), el.getAttribute?.('title')].join(' ').toLowerCase();
        return text === '取消' || text === '关闭' || /\b(close|modal-close|dialog-close)\b/.test(hints);
      });
    const confirm = Array.from(dialog.querySelectorAll('.arcp-queue-confirm'))
      .filter(visible)
      .find((el) => String(el.className || '').includes('active'));
    const target = explicitClose || confirm;
    if (target) {
      (target.closest('button, [role="button"], [tabindex]') || target).click();
      return explicitClose ? 'explicit_close' : 'active_confirm';
    }

    // 无已选稿件时确认按钮不可用；再次点击原“稿件选择”入口可收起选择层。
    const trigger = Array.from(document.querySelectorAll('button, [role="button"], span, div'))
      .filter(visible)
      .filter((el) => !dialog.contains(el))
      .find((el) => (el.textContent || '').replace(/\s+/g, '').trim() === '稿件选择');
    if (!trigger) return 'not_found';
    (trigger.closest('button, [role="button"], [tabindex]') || trigger).click();
    return 'toggle_trigger';
  });
  await sleep(700);
  const stillOpen = await frame.evaluate(() => {
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      return rect.width > 10 && rect.height > 10 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    return Array.from(document.querySelectorAll('[role="dialog"], .bili-modal, .bcc-dialog, .modal'))
      .filter(visible)
      .some((el) => (el.innerText || '').includes('稿件列表'));
  });
  if (stillOpen) {
    throw new Error(`B 站稿件滚动确认完成后未能关闭选择弹窗（关闭动作：${closeAttempt}）`);
  }
}

async function clearSelectedWorks(frame) {
  let selectedCount = await readSelectedWorkCount(frame);
  if (selectedCount <= 0) return;

  await updateProgress({
    phase: 'selecting',
    message: `清空 B 站当前已选稿件（剩余 ${selectedCount} 个）`,
  });

  await scrollWorkListToTop(frame);
  let stableRounds = 0;

  for (let round = 1; round <= 60 && selectedCount > 0; round += 1) {
    const toggled = await frame.evaluate((limit) => {
      const visible = (el) => {
        if (!el || typeof el.getBoundingClientRect !== 'function') return false;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 20
          && rect.height > 20
          && rect.bottom > 0
          && rect.top < window.innerHeight
          && style.visibility !== 'hidden'
          && style.display !== 'none'
          && style.opacity !== '0';
      };
      const dialog = Array.from(document.querySelectorAll('[role="dialog"], .bili-modal, .bcc-dialog, .modal'))
        .filter(visible)
        .find((el) => (el.innerText || '').includes('稿件列表'));
      if (!dialog) return 0;
      const targets = Array.from(dialog.querySelectorAll('.arcp-archive.active'))
        .filter(visible)
        .slice(0, Math.max(1, limit));
      for (const card of targets) {
        card.click();
      }
      return targets.length;
    }, Math.min(selectedCount, MAX_WORKS_PER_OFFICIAL_EXPORT));
    await sleep(300);

    const nextSelectedCount = await readSelectedWorkCount(frame);
    await updateProgress({
      phase: 'selecting',
      message: `清空 B 站当前已选稿件（剩余 ${nextSelectedCount} 个）`,
    });
    if (nextSelectedCount <= 0) return;

    if (nextSelectedCount < selectedCount) {
      selectedCount = nextSelectedCount;
      stableRounds = 0;
    } else {
      stableRounds += 1;
    }

    const scrollState = await scrollWorkList(frame);
    await sleep(500);
    if (toggled === 0 && (!scrollState.changed || scrollState.atBottom) && stableRounds >= 2) break;
  }

  const finalSelectedCount = await readSelectedWorkCount(frame);
  if (finalSelectedCount > 0) {
    throw new Error(`B 站稿件选择弹窗清空失败，仍有 ${finalSelectedCount} 个已选稿件`);
  }
}

// 每批只选择最多 10 条，确认后立刻导出；多批文件最后交给 normalizer 合并去重。
async function selectWorkBatch(frame, batch, batchIndex, totalBatches, totalWorks) {
  const seenTargetIds = new Set();
  let bottomPassConsumed = false;
  await updateProgress({
    phase: 'selecting',
    message: `选择 B 站稿件第 ${batchIndex + 1}/${totalBatches} 批（每批最多 ${MAX_WORKS_PER_OFFICIAL_EXPORT} 个）`,
    totalWorks,
    processedWorks: batchIndex * MAX_WORKS_PER_OFFICIAL_EXPORT,
    successWorks: batchIndex * MAX_WORKS_PER_OFFICIAL_EXPORT,
    queuedWorks: Math.max(0, totalWorks - batchIndex * MAX_WORKS_PER_OFFICIAL_EXPORT),
  });

  await clearSelectedWorks(frame);
  await scrollWorkListToTop(frame);

  for (let round = 1; round <= 140; round += 1) {
    const cards = await getVisibleWorkCards(frame);

    for (const card of cards) {
      const matchedWork = findMatchingTargetWork(batch, card);
      if (matchedWork) seenTargetIds.add(matchedWork.targetId);
      if (card.selected && !matchedWork) {
        await clickWorkCard(frame, card);
        await sleep(180);
      }
    }

    let selectedCount = await readSelectedWorkCount(frame);
    for (const card of cards) {
      const matchedWork = findMatchingTargetWork(batch, card);
      if (!matchedWork || card.selected) continue;
      if (selectedCount >= batch.length) break;
      if (await clickWorkCard(frame, matchedWork)) {
        await sleep(220);
        selectedCount += 1;
      }
    }

    selectedCount = await readSelectedWorkCount(frame);
    await updateProgress({
      phase: 'selecting',
      message: `B 站第 ${batchIndex + 1}/${totalBatches} 批：已选 ${selectedCount}/${batch.length}，已定位 ${seenTargetIds.size}/${batch.length}`,
      currentIndex: batchIndex * MAX_WORKS_PER_OFFICIAL_EXPORT + Math.min(selectedCount, batch.length),
      processedWorks: Math.min(totalWorks, batchIndex * MAX_WORKS_PER_OFFICIAL_EXPORT + seenTargetIds.size),
      successWorks: batchIndex * MAX_WORKS_PER_OFFICIAL_EXPORT,
      queuedWorks: Math.max(0, totalWorks - batchIndex * MAX_WORKS_PER_OFFICIAL_EXPORT - Math.min(selectedCount, batch.length)),
    });

    if (seenTargetIds.size >= batch.length && selectedCount === batch.length) {
      break;
    }

    const scrollState = await scrollWorkList(frame);
    await sleep(650);
    if (!scrollState.changed || scrollState.atBottom) {
      if (scrollState.atBottom && scrollState.changed && !bottomPassConsumed) {
        bottomPassConsumed = true;
        continue;
      }
      if (seenTargetIds.size < batch.length) {
        throw new Error(`B 站第 ${batchIndex + 1} 批有稿件未在选择弹窗中定位到`);
      }
      if (selectedCount !== batch.length) {
        throw new Error(`B 站第 ${batchIndex + 1} 批选择数量异常：期望 ${batch.length}，实际 ${selectedCount}`);
      }
      break;
    }
    bottomPassConsumed = false;
  }

  const finalSelectedCount = await readSelectedWorkCount(frame);
  if (seenTargetIds.size < batch.length || finalSelectedCount !== batch.length) {
    throw new Error(`B 站第 ${batchIndex + 1} 批选择未完成：定位 ${seenTargetIds.size}/${batch.length}，已选 ${finalSelectedCount}/${batch.length}`);
  }

  await confirmWorkSelection(frame);
  return batch.length;
}

async function clickExportButton(frame) {
  const clicked = await frame.evaluate(() => {
    const normalizeText = (value) => String(value || '').replace(/\s+/g, '');
    const isVisible = (el) => {
      if (!el || typeof el.getBoundingClientRect !== 'function') return false;
      const style = window.getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
      const rect = el.getBoundingClientRect();
      if (rect.width < 8 || rect.height < 8) return false;
      if (rect.right <= 0 || rect.bottom <= 0 || rect.left >= window.innerWidth || rect.top >= window.innerHeight) return false;
      return true;
    };
    const dispatchClick = (target, rect) => {
      const x = Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2));
      const y = Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2));
      const topElement = document.elementFromPoint(x, y) || target;
      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        topElement.dispatchEvent(new MouseEvent(type, {
          bubbles: true,
          cancelable: true,
          view: window,
          clientX: x,
          clientY: y,
        }));
      }
    };

    const heading = Array.from(document.querySelectorAll('*')).find((el) => normalizeText(el.innerText) === '近期稿件对比' && isVisible(el));
    if (!heading) return false;

    const ancestorCandidates = [];
    let section = heading;
    while (section && section !== document.documentElement && section !== document.body) {
      const rect = section.getBoundingClientRect();
      const text = normalizeText(section.innerText);
      if (
        rect.width > 700
        && text.includes('近期稿件对比')
        && text.includes('稿件选择')
        && text.includes('自选指标')
      ) {
        ancestorCandidates.push({ section, rect, area: rect.width * rect.height, text });
      }
      section = section.parentElement;
    }
    ancestorCandidates.sort((a, b) => {
      const aHasMode = a.text.includes('历史累计数据') ? 0 : 1;
      const bHasMode = b.text.includes('历史累计数据') ? 0 : 1;
      if (aHasMode !== bHasMode) return aHasMode - bHasMode;
      return a.area - b.area;
    });
    section = ancestorCandidates[0]?.section || heading.parentElement || heading;

    const headingRect = heading.getBoundingClientRect();
    const sectionRect = section.getBoundingClientRect();
    const headerTop = headingRect.top - 50;
    const headerBottom = headingRect.bottom + 100;
    const rightControlsStart = Math.max(headingRect.left + 520, sectionRect.right - 320);
    const candidates = Array.from(section.querySelectorAll('[aria-label], [title], button, [role="button"], svg, i, span, div'))
      .map((el) => {
        const target = el.closest('button, [role="button"], [tabindex]') || el;
        const rect = target.getBoundingClientRect();
        const text = normalizeText(target.innerText);
        const attrs = normalizeText(`${target.getAttribute('aria-label') || ''}${target.getAttribute('title') || ''}${target.className || ''}`);
        return { target, rect, text, attrs };
      })
      .filter((item, index, arr) => arr.findIndex((other) => other.target === item.target) === index)
      .filter((item) => isVisible(item.target))
      .filter((item) => item.rect.width >= 12 && item.rect.width <= 90 && item.rect.height >= 12 && item.rect.height <= 70)
      .filter((item) => item.rect.left >= rightControlsStart && item.rect.right <= sectionRect.right + 8)
      .filter((item) => item.rect.top >= headerTop && item.rect.top <= headerBottom)
      .filter((item) => !/历史累计数据|稿件选择|自选指标|近期稿件对比/.test(item.text));
    candidates.sort((a, b) => {
      const aNamed = /导出|下载|export|download/i.test(`${a.text}${a.attrs}`) ? 1 : 0;
      const bNamed = /导出|下载|export|download/i.test(`${b.text}${b.attrs}`) ? 1 : 0;
      if (aNamed !== bNamed) return bNamed - aNamed;
      return b.rect.left - a.rect.left;
    });
    const picked = candidates[0];
    if (!picked) return false;
    dispatchClick(picked.target, picked.rect);
    return true;
  });

  if (!clicked) throw new Error('未定位到 B 站「近期稿件对比」导出按钮');
}

async function clickVisibleExportMenuItem(frame) {
  await sleep(800);
  return frame.evaluate(() => {
    const normalizeText = (value) => String(value || '').replace(/\s+/g, '');
    const isVisible = (el) => {
      if (!el || typeof el.getBoundingClientRect !== 'function') return false;
      const style = window.getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
      const rect = el.getBoundingClientRect();
      if (rect.width < 8 || rect.height < 8) return false;
      if (rect.right <= 0 || rect.bottom <= 0 || rect.left >= window.innerWidth || rect.top >= window.innerHeight) return false;
      const x = Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2));
      const y = Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2));
      const topElement = document.elementFromPoint(x, y);
      return Boolean(topElement && (el === topElement || el.contains(topElement) || topElement.contains(el)));
    };
    const dispatchClick = (target, rect) => {
      const x = Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2));
      const y = Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2));
      const topElement = document.elementFromPoint(x, y) || target;
      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        topElement.dispatchEvent(new MouseEvent(type, {
          bubbles: true,
          cancelable: true,
          view: window,
          clientX: x,
          clientY: y,
        }));
      }
    };

    const candidates = Array.from(document.querySelectorAll('button, [role="button"], a, li, div, span'))
      .map((el) => {
        const text = normalizeText(el.innerText);
        const rect = el.getBoundingClientRect();
        return { el, text, rect };
      })
      .filter((item) => /导出|下载/.test(item.text))
      .filter((item) => item.text.length <= 20)
      .filter((item) => isVisible(item.el));
    candidates.sort((a, b) => {
      const aExact = /^(导出|导出数据|下载|下载数据)$/.test(a.text) ? 1 : 0;
      const bExact = /^(导出|导出数据|下载|下载数据)$/.test(b.text) ? 1 : 0;
      if (aExact !== bExact) return bExact - aExact;
      return a.rect.top - b.rect.top;
    });

    const picked = candidates[0];
    if (!picked) return false;
    dispatchClick(picked.el, picked.rect);
    return true;
  });
}

async function downloadOfficialExport(page, frame) {
  await updateProgress({ phase: 'exporting', message: '点击 B 站官方导出按钮' });
  await ensureDir(CONFIG.officialDownloadDir);

  let download = null;
  const firstDownload = page.waitForEvent('download', { timeout: 15_000 }).catch((error) => ({ error }));
  await clickExportButton(frame);
  const firstResult = await firstDownload;
  if (firstResult && !firstResult.error) {
    download = firstResult;
  } else {
    const secondDownload = page.waitForEvent('download', { timeout: 60_000 }).catch((error) => ({ error }));
    const menuClicked = await clickVisibleExportMenuItem(frame);
    if (!menuClicked) {
      const cause = firstResult?.error?.message ? `：${firstResult.error.message}` : '';
      throw new Error(`点击导出后未触发下载，也未找到可见的二次确认菜单${cause}`);
    }
    const secondResult = await secondDownload;
    if (!secondResult || secondResult.error) {
      const cause = secondResult?.error?.message ? `：${secondResult.error.message}` : '';
      throw new Error(`B 站官方导出未开始下载${cause}`);
    }
    download = secondResult;
  }

  const suggested = download.suggestedFilename() || `bilibili-official-${Date.now()}.csv`;
  const safeName = suggested.replace(/[^\w\u4e00-\u9fa5().-]+/g, '_');
  const savedPath = path.join(CONFIG.officialDownloadDir, `${Date.now()}-${safeName}`);
  await download.saveAs(savedPath);
  return savedPath;
}

async function normalizeOfficialExport(officialPaths) {
  await updateProgress({ phase: 'merging', message: '规范化 B 站官方导出文件' });
  const inputPaths = Array.isArray(officialPaths) ? officialPaths : [officialPaths];
  const args = [
    CONFIG.normalizerScriptPath,
    '--input',
    ...inputPaths,
    '--rows-output',
    CONFIG.tempRowsPath,
    '--excel-output',
    CONFIG.outputPath,
  ];
  if (CONFIG.minPublishDate) args.push('--min-date', CONFIG.minPublishDate);
  if (CONFIG.maxPublishDate) args.push('--max-date', CONFIG.maxPublishDate);

  const { stdout } = await execFileAsync(CONFIG.pythonBin, args, { timeout: 120_000 });
  const payload = JSON.parse(stdout.trim().split('\n').pop() || '{}');
  return Number.parseInt(payload.rows ?? '0', 10) || 0;
}

async function assertNormalizedMetricCoverage() {
  const text = await fs.readFile(CONFIG.tempRowsPath, 'utf-8');
  const rows = JSON.parse(text || '[]');
  if (!Array.isArray(rows) || rows.length <= 0) {
    throw new Error('B 站官方导出归一化后没有稿件数据');
  }

  const metricAliases = {
    封标点击率: ['封标点击率', '封面点击率'],
    '3秒跳出率': ['3秒跳出率', '3s跳出率'],
  };

  const missing = [];
  for (const metric of CONFIG.requiredMetrics) {
    const aliases = metricAliases[metric] || [metric];
    const hasValue = rows.some((row) => aliases.some((key) => String(row?.[key] ?? '').trim()));
    if (!hasValue) missing.push(metric);
  }
  if (missing.length) {
    throw new Error(`B 站官方导出缺少有效指标值：${missing.join('、')}`);
  }
  return rows;
}

// ── XHR 直采模式（提速 ~25 倍，替代官方 CSV 导出链路）──────────────────────
// 探测验证：member.bilibili.com/x/web/data/archive_diagnose/compare 接口
// 在创作中心页面加载时返回稿件的 stat（完播率/跳出率/封标点击率等深度指标）。
// 我们在浏览器内监听这个响应直接取 JSON，省掉「勾自选指标 + 点导出 + 等下载 +
// 解析 CSV」整条慢链路。从平台角度看和正常浏览无区别，封号风险极低。
//
// 字段口径已实测对照（见提交说明）：
//   - play/like/comment/fav/share/coin 等计数字段：与 CSV 完全一致（整数）
//   - full_play_ratio/crash_rate/play_viewer_rate 等百分比字段：
//     XHR 返回 ×100 整数（5783 = 57.83%），映射时 ÷100 加 %
//   - interact_rate（互动率）：XHR 直接给，CSV 没有（bonus）
//   - 平均播放时长/进度：接口不返回，留空（unified_avg_watch_seconds 对
//     bilibili 本来就硬编码返回 0，不影响下游）

const ARCHIVE_DIAGNOSE_API = '/x/web/data/archive_diagnose/compare';

function biliStatToRow(item) {
  const stat = item?.stat ?? {};
  // 百分比字段：XHR 返回 ×100 整数（5783），转成带 % 的文本（57.83%），
  // 与 normalize_bilibili_official_export.py 输出口径保持一致。
  const ratioText = (raw) => {
    if (typeof raw !== 'number' || !Number.isFinite(raw) || raw === 0) return '';
    return `${(raw / 100).toFixed(2)}%`;
  };
  // 稿件时长（秒）→ "N分M秒" 或 "M秒"，与 B 站展示一致
  const durationText = (seconds) => {
    if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) return '';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return m > 0 ? `${m}分${s}秒` : `${s}秒`;
  };
  const pubTs = Number.parseInt(item?.pubtime ?? item?.pubdate, 10) || 0;
  const publishDate = pubTs > 0 ? formatDate(pubTs * 1000) : '';
  const publishTime = pubTs > 0 ? formatDate(pubTs * 1000) : '';
  const bvid = item?.bvid || '';
  // CSV 导出没有 bvid，用 title+publish 生成稳定 ID；XHR 有 bvid 更好，用它。
  const workId = bvid || normalizePublishKey(`${item?.title ?? ''}-${publishDate}`);

  return {
    平台: 'bilibili',
    作品ID: bvid,
    标题: normalizeBilibiliTitle(item?.title ?? ''),
    发布日期: publishDate,
    发布时间: publishTime,
    播放量: String(stat.play ?? ''),
    游客播放占比: ratioText(stat.play_passer_rate),
    粉丝观看率: ratioText(stat.play_fan_rate),
    粉丝播放占比: ratioText(stat.play_fan_rate),
    封标点击率: ratioText(stat.play_viewer_rate),
    封面点击率: ratioText(stat.play_viewer_rate),
    '3秒跳出率': ratioText(stat.crash_rate),
    '3s跳出率': ratioText(stat.crash_rate),
    涨粉量: String(stat.total_new_attention_cnt ?? ''),
    点赞量: String(stat.like ?? ''),
    评论量: String(stat.comment ?? ''),
    弹幕量: String(stat.danmaku ?? ''),
    收藏量: String(stat.fav ?? ''),
    投币量: String(stat.coin ?? ''),
    转发量: String(stat.share ?? ''),
    分享量: String(stat.share ?? ''),
    平均播放进度: '',
    平均播放占比: '',
    完播率: ratioText(stat.full_play_ratio),
    内容类型: '视频',
  };
}

function meetsDateRangeByPubdate(item, { minDate, maxDate }) {
  // archive_diagnose 接口稿件发布日期字段是 pubtime（实测确认），不是 pubdate
  const pubTs = Number.parseInt(item?.pubtime ?? item?.pubdate, 10) || 0;
  if (pubTs <= 0) return minDate || maxDate ? false : true;
  return meetsDateRangeFromTs(pubTs * 1000);
}

async function collectWorksByXhr(page, apiTargets) {
  // 主动 fetch archive_diagnose/compare?size=50 拿全稿件，再按时间范围本地过滤。
  // 探测验证：size=10 是页面默认值，size 参数有效（size=50 返回 50 条，字段完整），
  // 接口不支持时间筛选（time/st/et 参数无效），所以用 size 拿全量再本地过滤。
  // 这正是「先确定时间范围，再选范围内所有作品」的思路。
  const targetKeys = new Set(apiTargets.map((work) => work.key).filter(Boolean));
  const targetBvids = new Set(apiTargets.map((work) => work.bvid).filter(Boolean));
  const FETCH_SIZE = Math.min(50, Math.max(10, CONFIG.videoLimit || 50));

  await updateProgress({ phase: 'xhr_collecting', message: 'XHR 直采 B 站稿件数据（提速模式）' });

  // 在浏览器上下文里主动调接口（带登录 cookie，和正常浏览无区别）
  const diagnoseApiBase = 'https://member.bilibili.com/x/web/data/archive_diagnose/compare';
  const rawItems = await page.evaluate(async (url) => {
    const resp = await fetch(`${url}?size=50`, { credentials: 'include' });
    const parsed = await resp.json();
    return parsed?.data?.list || parsed?.data?.archives || [];
  }, diagnoseApiBase).catch(() => []);

  const collected = new Map();
  for (const item of rawItems) {
    const bvid = item?.bvid;
    if (!bvid || collected.has(bvid)) continue;
    collected.set(bvid, item);
  }

  // 按发布日期过滤 + 映射成业务行
  const allItems = Array.from(collected.values());
  const items = allItems
    .filter((item) => meetsDateRangeByPubdate(item, { minDate: CONFIG.minPublishDate, maxDate: CONFIG.maxPublishDate }));
  const rows = items.map(biliStatToRow);
  return { rows, collectedBvids: new Set(items.map((item) => item?.bvid).filter(Boolean)) };
}

async function writeXhrRows(rows) {
  // 直接产出 tempRowsPath（JSON）+ outputPath（xlsx），格式与 normalizer 输出一致，
  // 下游 merge_channels / platform_source_rows / build_excel_export 无需改动。
  const dedupMap = new Map();
  for (const row of rows) {
    const key = row['作品ID'] || row['标题'] || '';
    if (!key || dedupMap.has(key)) continue;
    dedupMap.set(key, row);
  }
  const dedupedRows = Array.from(dedupMap.values());
  await fs.mkdir(path.dirname(CONFIG.tempRowsPath), { recursive: true });
  await fs.writeFile(CONFIG.tempRowsPath, JSON.stringify(dedupedRows, null, 2), 'utf-8');

  const args = [
    path.resolve('scripts', 'write_rows_excel.py'),
    '--input', CONFIG.tempRowsPath,
    '--output', CONFIG.outputPath,
    '--columns',
    '平台,作品ID,标题,发布日期,发布时间,播放量,封标点击率,封面点击率,3秒跳出率,3s跳出率,涨粉量,点赞量,评论量,弹幕量,收藏量,投币量,转发量,分享量,完播率,内容类型',
    '--dedupe-key', '作品ID',
  ];
  await execFileAsync(CONFIG.pythonBin, args, { timeout: 120_000 });
  return dedupedRows.length;
}

async function tryXhrCollection(page, apiTargets) {
  // XHR 直采：尝试用 archive_diagnose/compare 接口直采。
  // 接口硬上限 50 条（size>50 仍返回 50 条，无分页参数有效）。
  // 成功返回 { ok: true, rowsCount, collectedBvids }；失败返回 { ok: false, error }。
  try {
    const { rows, collectedBvids } = await collectWorksByXhr(page, apiTargets);
    if (rows.length === 0) {
      return { ok: false, error: 'XHR 直采未捕获到稿件数据（接口可能未加载或返回空）' };
    }
    const rowsCount = await writeXhrRows(rows);
    if (rowsCount === 0) {
      return { ok: false, error: 'XHR 直采数据写入后为空' };
    }
    return { ok: true, rowsCount, collectedBvids };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { ok: false, error: message };
  }
}

async function main() {
  await updateProgress({
    ...newProgressState(),
    status: 'running',
    phase: 'boot',
    message: 'B 站任务启动',
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
    args: ['--disable-blink-features=AutomationControlled', '--disable-session-crashed-bubble'],
  });

  let page = await prepareAuthPage(context, context.pages()[0]);

  try {
    page = await ensureDashboard(context, page);

    if (CONFIG.authOnly) {
      await updateProgress({
        status: 'completed',
        phase: 'done',
        message: 'B 站登录完成（AUTH_ONLY）',
        auth_status: 'authorized',
        auth_reason: '',
        needs_auth: false,
        finishedAt: new Date().toISOString(),
      });
      return;
    }

    const targetDiscovery = await listTargetWorksByApi(page);
    let confirmedTargets;
    let confirmFrame;
    let forceOfficialCsvFallback = false;

    if (targetDiscovery.works.length > 0) {
      // Fast XHR collection must still be checked against the real official scroll list. Otherwise a
      // complete API response could hide a broken/changed UI scroll path until the slower CSV fallback.
      ({ confirmed: confirmedTargets, confirmFrame } = await confirmTargetsWithOfficialScroll(page, targetDiscovery.works));
    } else {
      confirmFrame = await findDataFrame(page, { requireRecent: true });
      await openWorkSelection(confirmFrame);
      confirmedTargets = await collectWorksByOfficialDateRange(confirmFrame, targetDiscovery.diagnostics);
      forceOfficialCsvFallback = true;
      await updateProgress({
        phase: 'selecting',
        message: `B 站已从创作中心稿件列表锁定 ${confirmedTargets.length} 条，使用官方 CSV 保证完整性`,
        totalWorks: confirmedTargets.length,
        processedWorks: confirmedTargets.length,
        successWorks: confirmedTargets.length,
        queuedWorks: 0,
        currentIndex: confirmedTargets.length,
      });
    }

    // ── 提速模式：优先 XHR 直采（~25 倍提速）────────────────────────────
    // 探测验证：archive_diagnose/compare 接口能直接返回完播率/跳出率/封标点击率
    // 等深度指标的 JSON。成功则跳过整条慢链路（勾自选指标+点导出+等下载+解析CSV）。
    // 失败则自动 fallback 到原 CSV 导出链路，保证稳定性。
    let rowsCount = 0;
    const xhrResult = forceOfficialCsvFallback
      ? { ok: false, error: '空间接口目标为空，使用创作中心官方 CSV 链路' }
      : await tryXhrCollection(page, confirmedTargets);
    const xhrCoveredAll = (() => {
      // 检查 XHR 是否覆盖了 WBI 锁定的全部目标稿件
      // archive_diagnose 接口硬上限 50 条，超出部分 XHR 拿不到，需降级 CSV 补全
      if (!xhrResult.ok || !xhrResult.collectedBvids) return false;
      const targetBvids = new Set(confirmedTargets.map((w) => w.bvid).filter(Boolean));
      if (targetBvids.size === 0) return true; // WBI 没锁定目标，XHR 采到什么算什么
      const missed = Array.from(targetBvids).filter((bvid) => !xhrResult.collectedBvids.has(bvid));
      if (missed.length > 0) {
        console.warn(`[bili-warning] XHR 直采覆盖 ${xhrResult.collectedBvids.size}/${targetBvids.size} 条，遗漏 ${missed.length} 条（超出接口 50 条上限），降级 CSV 补全`);
        return false;
      }
      return true;
    })();

    if (xhrResult.ok && xhrCoveredAll) {
      rowsCount = xhrResult.rowsCount;
    } else {
      // ── Fallback：官方 CSV 导出链路（XHR 失败 或 超出50条覆盖不全时降级）──
      const reason = !xhrResult.ok
        ? `XHR 直采失败，降级到官方 CSV 导出：${xhrResult.error}`
        : `XHR 直采未覆盖全部目标稿件（超出接口 50 条上限），降级 CSV 补全`;
      console.warn(`[bili-warning] ${reason}`);
      await updateProgress({ phase: 'csv_fallback', message: reason });
      await closeWorkSelection(confirmFrame);

      let dataFrame = await findDataFrame(page, { requireRecent: true });
      await selectDataMode(dataFrame);
      await ensureComparisonMetrics(dataFrame);
      await openWorkSelection(dataFrame);
      const works = await collectWorksByTargetSet(dataFrame, confirmedTargets);
      const batches = chunkArray(works, MAX_WORKS_PER_OFFICIAL_EXPORT);
      const officialPaths = [];
      let selectedCount = 0;

      for (let batchIndex = 0; batchIndex < batches.length; batchIndex += 1) {
        if (batchIndex > 0) {
          await page.close().catch(() => {});
          page = await prepareAuthPage(context, await context.newPage());
          page = await ensureDashboard(context, page);
          dataFrame = await findDataFrame(page, { requireRecent: true });
          await selectDataMode(dataFrame);
          await ensureComparisonMetrics(dataFrame);
          await openWorkSelection(dataFrame);
        }
        const batch = batches[batchIndex];
        try {
          selectedCount += await selectWorkBatch(dataFrame, batch, batchIndex, batches.length, works.length);
          const officialPath = await downloadOfficialExport(page, dataFrame);
          officialPaths.push(officialPath);
        } catch (error) {
          throw buildOfficialBatchError(batchIndex, error);
        }
      }

      rowsCount = await normalizeOfficialExport(officialPaths);
      const normalizedRows = await assertNormalizedMetricCoverage();
      const coverage = validateTargetCoverage(works, normalizedRows);
      if (!coverage.ok) {
        throw new Error(
          `B 站官方导出未覆盖全部目标稿件：缺少 ${coverage.missing.length} 条（${coverage.missing.slice(0, 5).join('；')}）`
        );
      }
      if (selectedCount > 0 && rowsCount <= 0) {
        throw new Error(`B 站官方导出文件未解析出稿件数据：${officialPaths.map((item) => path.basename(item)).join(', ')}`);
      }
    }

    await updateProgress({
      status: 'completed',
      phase: 'done',
      message: `B 站任务完成，共 ${rowsCount} 条`,
      warning: '',
      finishedAt: new Date().toISOString(),
      totalWorks: rowsCount,
      processedWorks: rowsCount,
      successWorks: rowsCount,
      queuedWorks: 0,
      failedWorks: 0,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await updateProgress({
      status: 'failed',
      phase: 'failed',
      message: `B 站链路失败：${message}`,
      finishedAt: new Date().toISOString(),
    });
    throw error;
  } finally {
    await context.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.error(`[bili-error] ${error instanceof Error ? error.message : String(error)}`);
    process.exit(1);
  });
}
