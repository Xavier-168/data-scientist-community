import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
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
  loginUrl: process.env.XHS_LOGIN_URL ?? 'https://creator.xiaohongshu.com/login',
  dataAnalysisUrl: process.env.XHS_DATA_ANALYSIS_URL ?? 'https://creator.xiaohongshu.com/statistics/data-analysis',
  noteDetailBaseUrl: process.env.XHS_NOTE_DETAIL_BASE_URL ?? 'https://creator.xiaohongshu.com/statistics/note-detail',
  noteManagerUrl: process.env.XHS_NOTE_MANAGER_URL ?? 'https://creator.xiaohongshu.com/new/note-manager',
  homeUrl: process.env.XHS_HOME_URL ?? 'https://creator.xiaohongshu.com/new/home',
  browserChannel: DEFAULT_BROWSER_CHANNEL,
  browserExecutablePath: String(process.env.BROWSER_EXECUTABLE_PATH ?? '').trim(),
  userDataDir: process.env.USER_DATA_DIR
    ? path.resolve(process.env.USER_DATA_DIR)
    : resolveProfileDir('xiaohongshu', DEFAULT_BROWSER_CHANNEL),
  headless: (process.env.HEADLESS ?? 'true') === 'true',
  authOnly: (process.env.AUTH_ONLY ?? 'false') === 'true',
  cleanProfileLocks: (process.env.CLEAN_PROFILE_LOCKS ?? 'true') === 'true',
  videoLimit: Number.parseInt(process.env.VIDEO_LIMIT ?? '50', 10),
  scanWaitMs: Number.parseInt(process.env.SCAN_WAIT_MS ?? '300000', 10),
  scanPollMs: Number.parseInt(process.env.SCAN_POLL_MS ?? '2000', 10),
  staleRoundsLimit: Number.parseInt(process.env.STALE_ROUNDS_LIMIT ?? '6', 10),
  maxScrollRounds: Number.parseInt(process.env.MAX_SCROLL_ROUNDS ?? '120', 10),
  minPublishDate: process.env.MIN_PUBLISH_DATE ?? '',
  maxPublishDate: process.env.MAX_PUBLISH_DATE ?? '',
  refreshDays: Number.parseInt(process.env.REFRESH_DAYS ?? '0', 10),
  refreshLatestCount: Number.parseInt(process.env.REFRESH_LATEST_COUNT ?? '0', 10),
  forceFullExport: (process.env.FORCE_FULL_EXPORT ?? 'false') === 'true',
  onlyVideo: (process.env.XHS_ONLY_VIDEO ?? 'false') === 'true',
  progressPath: process.env.PROGRESS_PATH
    ? path.resolve(process.env.PROGRESS_PATH)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'xiaohongshu_progress.json'),
  outputPath: process.env.XHS_OUTPUT_PATH
    ? path.resolve(process.env.XHS_OUTPUT_PATH)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'xiaohongshu_all_videos.xlsx'),
  tempRowsPath: process.env.XHS_TEMP_ROWS_PATH
    ? path.resolve(process.env.XHS_TEMP_ROWS_PATH)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'xiaohongshu_rows.json'),
  detailExportDir: process.env.XHS_DETAIL_EXPORT_DIR
    ? path.resolve(process.env.XHS_DETAIL_EXPORT_DIR)
    : path.join(DEFAULT_DOWNLOAD_DIR, 'xiaohongshu_detail_exports'),
  detailExportTimeoutMs: Number.parseInt(process.env.XHS_DETAIL_EXPORT_TIMEOUT_MS ?? '90000', 10),
  detailExportEnabled: (process.env.XHS_DETAIL_EXPORT_ENABLED ?? 'true') === 'true',
  writerScriptPath: process.env.XHS_WRITER_SCRIPT
    ? path.resolve(process.env.XHS_WRITER_SCRIPT)
    : path.resolve('scripts', 'write_xhs_excel.py'),
  detailNormalizerScriptPath: process.env.XHS_DETAIL_NORMALIZER_SCRIPT
    ? path.resolve(process.env.XHS_DETAIL_NORMALIZER_SCRIPT)
    : path.resolve('scripts', 'normalize_xhs_detail_export.py'),
  pythonBin: process.env.PYTHON_BIN ?? DEFAULT_PYTHON_BIN,
};

const execFileAsync = promisify(execFile);

const DATA_ANALYSIS_ACTIVE_PAGE_SELECTOR = [
  '.el-pagination .number.active',
  '.ant-pagination-item-active',
  '.arco-pagination-item-active',
  '[aria-current="page"]',
  '.d-pagination .d-pagination-page[class*="--color-bg-primary-light"]',
  '.d-pagination .d-pagination-page[class*="--color-primary"]',
].join(', ');

const DATA_ANALYSIS_NEXT_PAGE_SELECTORS = [
  '.el-pagination .btn-next:not(.is-disabled)',
  '.ant-pagination-next:not(.ant-pagination-disabled)',
  '.arco-pagination-next:not(.arco-pagination-item-disabled)',
  'li[title="下一页"]:not(.ant-pagination-disabled)',
  'button[aria-label*="Next"]:not([disabled])',
  'button[aria-label*="下一"]:not([disabled])',
  '.d-pagination .d-pagination-page:last-child:not(.disabled)',
];

function parseDateValue(dateStr) {
  if (!dateStr) return null;
  const match = String(dateStr).trim().match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return null;
  const year = Number.parseInt(match[1], 10);
  const month = Number.parseInt(match[2], 10);
  const day = Number.parseInt(match[3], 10);
  const date = new Date(year, month - 1, day);
  if (Number.isNaN(date.getTime())) return null;
  return date.getTime();
}

function formatDateInput(text) {
  if (!text) return '';
  const value = String(text).trim();
  const m = value.match(/(\d{4})年(\d{1,2})月(\d{1,2})日/)
    || value.match(/(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})/);
  if (!m) return '';
  const y = m[1];
  const mo = String(Number.parseInt(m[2], 10)).padStart(2, '0');
  const d = String(Number.parseInt(m[3], 10)).padStart(2, '0');
  return `${y}-${mo}-${d}`;
}

function formatDateTimeFromTimestamp(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return '';
  const date = new Date(number);
  if (Number.isNaN(date.getTime())) return '';
  const y = date.getFullYear();
  const mo = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  const h = String(date.getHours()).padStart(2, '0');
  const mi = String(date.getMinutes()).padStart(2, '0');
  return `${y}-${mo}-${d} ${h}:${mi}`;
}

function formatDateFromTimestamp(value) {
  const text = formatDateTimeFromTimestamp(value);
  return text ? text.slice(0, 10) : '';
}

function parseCount(value) {
  if (value === null || value === undefined) return 0;
  const text = String(value).replace(/,/g, '').trim();
  if (!text) return 0;
  if (text === '-') return 0;

  let multiplier = 1;
  let cleaned = text;
  if (cleaned.endsWith('万')) {
    multiplier = 10000;
    cleaned = cleaned.slice(0, -1);
  } else if (cleaned.endsWith('亿')) {
    multiplier = 100000000;
    cleaned = cleaned.slice(0, -1);
  }

  cleaned = cleaned.replace(/[^\d.\-]/g, '');
  const num = Number.parseFloat(cleaned);
  if (!Number.isFinite(num)) return 0;
  return Math.round(num * multiplier);
}

function cleanTitle(value) {
  return String(value ?? '')
    .replace(/\n+/g, ' ')
    .replace(/[\u200B-\u200D\uFEFF\u00A0]+/gu, ' ')
    .replace(/＃/g, '#')
    .replace(/\s*#.*$/u, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function sanitizeFilename(value, maxLength = 120) {
  const cleaned = String(value ?? '')
    .replace(/[\\/:*?"<>|]+/g, '-')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, maxLength)
    .replace(/[.\s]+$/g, '');
  return cleaned || 'xiaohongshu-detail';
}

function stableRowId(title, publishText) {
  const seed = `${cleanTitle(title)}|${formatDateInput(publishText) || String(publishText || '').trim()}`;
  return `xhs-${crypto.createHash('sha1').update(seed).digest('hex').slice(0, 16)}`;
}

function extractNoteIdFromUrl(url) {
  const match = String(url || '').match(/[?&]noteId=([^&#]+)/i);
  return match ? decodeURIComponent(match[1]) : '';
}

function buildDetailUrl(noteId) {
  const id = String(noteId || '').trim();
  if (!id) return '';
  return `${CONFIG.noteDetailBaseUrl}?noteId=${encodeURIComponent(id)}`;
}

function rowPublishHint(row) {
  const rowKey = String(row?.__rowKey || '');
  const parts = rowKey.split('|');
  if (parts.length > 1 && parts.slice(1).join('|').trim()) {
    return parts.slice(1).join('|').trim();
  }
  return String(row?.['发布日期'] || '').trim();
}

function normalizePercentText(value) {
  const text = String(value ?? '').replace(/,/g, '').trim();
  if (!text || text === '-') return '';
  const match = text.match(/-?\d+(?:\.\d+)?/);
  return match ? match[0] : '';
}

function parseSecondsText(value) {
  const text = String(value ?? '').replace(/,/g, '').trim();
  if (!text || text === '-') return '';
  const match = text.match(/-?\d+(?:\.\d+)?/);
  if (!match) return '';
  const number = Number.parseFloat(match[0]);
  if (!Number.isFinite(number)) return '';
  return Number.isInteger(number) ? String(number) : String(number);
}

function normalizeRatioPercent(value) {
  if (value === null || value === undefined || value === '') return '';
  const number = Number(value);
  if (Number.isFinite(number)) {
    const percent = Math.abs(number) <= 1 ? number * 100 : number;
    const rounded = Math.round(percent * 100) / 100;
    return Number.isInteger(rounded) ? String(rounded) : String(rounded);
  }
  return normalizePercentText(value);
}

function meetsDateRange(dateStr) {
  const minTs = parseDateValue(CONFIG.minPublishDate);
  const maxTs = parseDateValue(CONFIG.maxPublishDate);
  const ts = parseDateValue(dateStr);
  if (!ts) return !(minTs || maxTs);
  if (minTs && ts < minTs) return false;
  if (maxTs && ts > maxTs) return false;
  return true;
}

async function ensureDir(dirPath) {
  await fs.mkdir(dirPath, { recursive: true });
}

function newProgressState() {
  return {
    platform: 'xiaohongshu',
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

export async function classifyXiaohongshuAuthPageOnce(page) {
  try {
    const currentUrl = page.url();
    const loginUrlMarkers = ['/login', '/passport/', 'passport.xiaohongshu.com'];
    if (loginUrlMarkers.some((marker) => currentUrl.includes(marker))) return 'login_required';

    const loginMarkers = [
      'text=扫码登录',
      'text=手机号登录',
      'text=验证码登录',
      'text=登录小红书创作服务平台',
      'button:has-text("立即登录")',
      'button:has-text("扫码登录")',
      'iframe[src*="login"]',
      'iframe[src*="passport"]',
    ];
    for (const marker of loginMarkers) {
      if (await visibleMarkerExists(page, marker)) return 'login_required';
    }

    const dashboardMarkers = ['text=笔记管理', 'text=发布笔记', 'text=数据看板', 'text=笔记数据', 'text=直播场次数据'];
    for (const marker of dashboardMarkers) {
      if (await visibleMarkerExists(page, marker)) return 'authorized';
    }
  } catch {
    // Navigation and renderer failures are transient, not proof that cookies expired.
  }
  return 'transient';
}

export async function classifyXiaohongshuAuthPage(page, options = {}) {
  const stableChecks = Math.max(2, Number.parseInt(options.stableChecks ?? '2', 10) || 2);
  const settleMs = Number.isFinite(options.settleMs)
    ? Math.max(0, options.settleMs)
    : Math.min(Math.max(CONFIG.scanPollMs, 500), 1500);
  let stableClassification = '';

  for (let index = 0; index < stableChecks; index += 1) {
    const current = await classifyXiaohongshuAuthPageOnce(page);
    if (current === 'transient') return 'transient';
    if (stableClassification && current !== stableClassification) return 'transient';
    stableClassification = current;
    if (index + 1 < stableChecks) await page.waitForTimeout(settleMs);
  }
  return stableClassification || 'transient';
}

export async function isLoggedIn(page) {
  return (await classifyXiaohongshuAuthPageOnce(page)) === 'authorized';
}

export async function waitForLogin(page, options = {}) {
  const timeoutMs = Number.isFinite(options.timeoutMs) ? options.timeoutMs : CONFIG.scanWaitMs;
  const stableChecks = Math.max(1, Number.parseInt(options.stableChecks ?? '2', 10));
  const deadline = Date.now() + timeoutMs;
  let consecutiveReady = 0;
  if (options.announce !== false) {
    console.log(`[xhs-login] 请在 ${Math.round(timeoutMs / 1000)} 秒内完成登录`);
  }
  while (Date.now() < deadline) {
    if (await isLoggedIn(page)) {
      consecutiveReady += 1;
      if (consecutiveReady >= stableChecks) return true;
    } else {
      consecutiveReady = 0;
    }
    await page.waitForTimeout(CONFIG.scanPollMs);
  }
  return false;
}

function xiaohongshuAuthError(classification) {
  if (classification === 'login_required') {
    return new Error('小红书未登录（headless=true 无法扫码登录）');
  }
  return new Error('小红书创作者数据页暂不可访问（登录态未作失效处理）');
}

async function gotoNoteManager(context, page) {
  const candidates = [CONFIG.dataAnalysisUrl, CONFIG.noteManagerUrl, CONFIG.homeUrl, CONFIG.loginUrl];
  page = await navigateAuthCandidates(context, page, candidates, { settleMs: 1200 });
  return page;
}

async function ensureOnNoteManager(context, page) {
  page = await gotoNoteManager(context, page);

  let authClassification = await classifyXiaohongshuAuthPage(page);
  if (authClassification !== 'authorized') {
    if (CONFIG.headless && authClassification === 'login_required') {
      throw xiaohongshuAuthError(authClassification);
    }
    const waitMs = CONFIG.headless
      ? Math.min(Math.max(CONFIG.scanWaitMs, 5000), 15000)
      : CONFIG.scanWaitMs;
    const ok = await waitForLogin(page, {
      timeoutMs: waitMs,
      announce: !CONFIG.headless,
      stableChecks: 2,
    });
    if (!ok) {
      authClassification = await classifyXiaohongshuAuthPage(page);
      if (authClassification !== 'authorized') {
        if (CONFIG.headless || authClassification === 'transient') {
          throw xiaohongshuAuthError(authClassification);
        }
        const currentUrl = page.url();
        const title = await page.title().catch(() => '');
        throw new Error(`小红书登录超时（当前页：${currentUrl} ${title}）`);
      }
    }
  }

  if (!page.url().includes('/statistics/data-analysis')) {
    await page.goto(CONFIG.dataAnalysisUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  }

  const dataBoardTab = page.locator('text=数据看板').first();
  if ((await dataBoardTab.count()) > 0 && !page.url().includes('/statistics/data-analysis')) {
    await dataBoardTab.click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(1500);
  }

  await page.waitForTimeout(2000);
  const stable = await waitForLogin(page, {
    timeoutMs: CONFIG.headless ? 15000 : 20000,
    announce: false,
    stableChecks: 2,
  });
  if (!stable) {
    authClassification = await classifyXiaohongshuAuthPage(page);
    if (authClassification === 'authorized') return page;
    if (authClassification === 'login_required' && !CONFIG.headless) {
      const currentUrl = page.url();
      const title = await page.title().catch(() => '');
      throw new Error(`小红书登录超时（当前页：${currentUrl} ${title}）`);
    }
    throw xiaohongshuAuthError(authClassification);
  }
  return page;
}

async function collectVisibleNotes(page) {
  return page.evaluate(() => {
    const getText = (el) => (el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '');

    const parseNoteId = (el) => {
      const values = [];
      const nodes = [el, ...Array.from(el.querySelectorAll('*'))];
      for (const node of nodes) {
        for (const attr of Array.from(node.attributes || [])) {
          if (!attr?.value) continue;
          values.push(`${attr.name}=${attr.value}`);
        }
      }
      const joined = values.join(' ');
      const patterns = [
        /[?&]noteId=([^&#"'\s]+)/i,
        /noteId["'\s:=]+([0-9a-zA-Z_-]{12,})/i,
        /note_id["'\s:=]+([0-9a-zA-Z_-]{12,})/i,
        /noteId\\?":\\?"?([0-9a-zA-Z_-]{12,})/i,
      ];
      for (const pattern of patterns) {
        const match = joined.match(pattern);
        if (match?.[1]) return decodeURIComponent(match[1]);
      }

      const dataImpression = el.getAttribute('data-impression') || '';
      if (dataImpression) {
        try {
          const parsed = JSON.parse(dataImpression);
          const id = parsed?.noteTarget?.value?.noteId;
          if (id) return String(id);
        } catch {
          const match = dataImpression.match(/noteId\":\"([^\"]+)/);
          if (match) return match[1];
        }
      }
      const dataNoteId = el.getAttribute('data-note-id') || '';
      if (dataNoteId) return String(dataNoteId);
      return '';
    };

    const parseDetailHref = (el) => {
      const link = el.querySelector('a[href*="note-detail"], [href*="note-detail"]');
      const href = link?.getAttribute('href') || '';
      if (!href) return '';
      try {
        return new URL(href, window.location.href).toString();
      } catch {
        return href;
      }
    };

    const allTabText = getText(Array.from(document.querySelectorAll('*')).find((n) => /全部笔记(?:\(\d+\)|\s*\d+)/.test(getText(n))));
    const allMatch = allTabText.match(/全部笔记(?:\(|\s*)(\d+)/);
    const totalFromTab = allMatch ? Number.parseInt(allMatch[1], 10) : 0;

    const endHintText = getText(Array.from(document.querySelectorAll('*')).find((n) => /共\d+篇笔记/.test(getText(n))));
    const endMatch = endHintText.match(/共\s*(\d+)\s*篇笔记/);
    const endTotal = endMatch ? Number.parseInt(endMatch[1], 10) : 0;

    const notes = [];
    const dataRows = Array.from(document.querySelectorAll('tbody tr, .el-table__body tr, .ant-table-tbody tr, [role="row"]'))
      .filter((row) => /详情数据/.test(getText(row)) && /发布于\s*\d{4}/.test(getText(row)));

    for (const row of dataRows) {
      const cells = Array.from(row.querySelectorAll('td, [role="cell"]'));
      if (cells.length < 10) continue;
      const baseText = getText(cells[0]);
      const publishMatch = baseText.match(/发布于\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)/);
      const publishText = publishMatch ? publishMatch[1] : '';
      const title = publishMatch ? baseText.slice(0, publishMatch.index).trim() : baseText.trim();
      const noteId = parseNoteId(row);
      const detailHref = parseDetailHref(row);
      notes.push({
        noteId,
        detailHref,
        rowKey: `${title}|${publishText}`,
        title,
        publishText,
        isVideo: true,
        exposureText: getText(cells[1]),
        readText: getText(cells[2]),
        coverClickText: getText(cells[3]),
        likeText: getText(cells[4]),
        commentText: getText(cells[5]),
        collectText: getText(cells[6]),
        fansText: getText(cells[7]),
        shareText: getText(cells[8]),
        avgWatchText: getText(cells[9]),
        danmakuText: getText(cells[10]),
      });
    }

    if (notes.length > 0) {
      return {
        notes,
        totalFromTab: endTotal || totalFromTab || 0,
        endTotal,
        url: window.location.href,
      };
    }

    const cards = Array.from(document.querySelectorAll('.note'));
    for (const card of cards) {
      const noteId = parseNoteId(card);
      const title = getText(card.querySelector('.title'));
      const publishText = getText(card.querySelector('.time'));
      const icons = Array.from(card.querySelectorAll('.icon_list .icon span')).map((n) => getText(n));

      notes.push({
        noteId,
        title,
        publishText,
        isVideo: Boolean(card.querySelector('.play_time')),
        readText: icons[0] || '',
        commentText: icons[1] || '',
        likeText: icons[2] || '',
        collectText: icons[3] || '',
        shareText: icons[4] || '',
      });
    }

    return {
      notes,
      totalFromTab,
      endTotal,
      url: window.location.href,
    };
  });
}

async function scrollNoteList(page) {
  return page.evaluate(() => {
    const containerSelectors = ['.d-table__content', '.d-table__body-wrapper', '.content'];
    for (const selector of containerSelectors) {
      const container = document.querySelector(selector);
      if (!container || container.scrollHeight <= container.clientHeight + 20) continue;
      const before = container.scrollTop;
      const delta = Math.max(620, Math.floor(container.clientHeight * 0.95));
      container.scrollBy(0, delta);
      const after = container.scrollTop;
      const maxTop = container.scrollHeight - container.clientHeight;
      return {
        changed: after > before,
        atBottom: after >= maxTop - 5,
        channel: selector,
      };
    }

    const before = window.scrollY;
    window.scrollBy(0, window.innerHeight * 0.95);
    const after = window.scrollY;
    const maxTop = document.documentElement.scrollHeight - window.innerHeight;
    return {
      changed: after > before,
      atBottom: after >= maxTop - 5,
      channel: 'window',
    };
  });
}

async function readDataAnalysisPageSnapshot(page) {
  return page.evaluate((selector) => {
    const getText = (el) => (el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '');
    const rows = Array.from(document.querySelectorAll('tbody tr, .el-table__body tr, .ant-table-tbody tr, [role="row"]'))
      .filter((row) => /详情数据/.test(getText(row)) && /发布于\s*\d{4}/.test(getText(row)))
      .slice(0, 5)
      .map((row) => {
        const cells = Array.from(row.querySelectorAll('td, [role="cell"]'));
        return getText(cells[0] || row).slice(0, 160);
      });
    const activePage = getText(document.querySelector(selector));
    const pageHint = getText(Array.from(document.querySelectorAll('*')).find((el) => /共\s*\d+\s*页/.test(getText(el))));
    return {
      href: window.location.href,
      activePage,
      pageHint,
      rowSignature: rows.join('||'),
    };
  }, DATA_ANALYSIS_ACTIVE_PAGE_SELECTOR);
}

async function waitForDataAnalysisPageAdvance(page, previousSnapshot) {
  const changed = await page.waitForFunction(({ prev, selector }) => {
    const getText = (el) => (el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '');
    const rows = Array.from(document.querySelectorAll('tbody tr, .el-table__body tr, .ant-table-tbody tr, [role="row"]'))
      .filter((row) => /详情数据/.test(getText(row)) && /发布于\s*\d{4}/.test(getText(row)))
      .slice(0, 5)
      .map((row) => {
        const cells = Array.from(row.querySelectorAll('td, [role="cell"]'));
        return getText(cells[0] || row).slice(0, 160);
      });
    const activePage = getText(document.querySelector(selector));
    const pageHint = getText(Array.from(document.querySelectorAll('*')).find((el) => /共\s*\d+\s*页/.test(getText(el))));
    const current = {
      href: window.location.href,
      activePage,
      pageHint,
      rowSignature: rows.join('||'),
    };
    return current.href !== prev.href
      || current.activePage !== prev.activePage
      || current.rowSignature !== prev.rowSignature
      || current.pageHint !== prev.pageHint;
  }, { prev: previousSnapshot, selector: DATA_ANALYSIS_ACTIVE_PAGE_SELECTOR }, { timeout: 8000 }).then(() => true).catch(() => false);

  if (changed) await page.waitForTimeout(1200);
  return changed;
}

async function goToNextDataAnalysisPage(page) {
  const previousSnapshot = await readDataAnalysisPageSnapshot(page);
  const selectors = DATA_ANALYSIS_NEXT_PAGE_SELECTORS;

  for (const selector of selectors) {
    const next = page.locator(selector).last();
    if ((await next.count()) === 0) continue;
    const disabled = await next.evaluate((node) => Boolean(node.disabled)
      || /\b(disabled|is-disabled|pagination-disabled)\b/.test(String(node.className || ''))).catch(() => true);
    if (disabled) continue;
    await next.scrollIntoViewIfNeeded().catch(() => {});
    await next.click({ timeout: 5000 }).catch(() => {});
    if (await waitForDataAnalysisPageAdvance(page, previousSnapshot)) {
      return true;
    }
  }

  const clicked = await page.evaluate((activeSelector) => {
    const isVisible = (el) => {
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const isDisabled = (el) => {
      const text = [
        el?.textContent,
        el?.getAttribute?.('aria-label'),
        el?.getAttribute?.('title'),
        el?.className,
        el?.getAttribute?.('aria-disabled'),
      ].join(' ').toLowerCase();
      return Boolean(el?.disabled)
        || el?.getAttribute?.('aria-disabled') === 'true'
        || /\b(disabled|is-disabled|pagination-disabled)\b/.test(text);
    };
    const clickCandidate = (el) => {
      const target = el?.closest?.('button, a, li, [role="button"], [tabindex], .btn-next') || el;
      if (!target || !isVisible(target) || isDisabled(target)) return false;
      target.click();
      return true;
    };

    const activePage = document.querySelector(activeSelector);
    let sibling = activePage?.nextElementSibling || null;
    while (sibling) {
      if (clickCandidate(sibling)) return true;
      sibling = sibling.nextElementSibling;
    }

    const candidates = Array.from(document.querySelectorAll('button, li, a, span, div, [role="button"], .btn-next'))
      .filter(isVisible)
      .filter((el) => {
        const text = [
          el.textContent,
          el.getAttribute('aria-label'),
          el.getAttribute('title'),
          el.className,
        ].join(' ').toLowerCase();
        if (isDisabled(el)) return false;
        const inPagination = Boolean(
          el.closest?.('.el-pagination, .ant-pagination, .arco-pagination, [class*="pagination"], [class*="pager"]'),
        );
        return inPagination && /下一|next|right|›|>/.test(text);
      });
    const target = candidates[candidates.length - 1];
    if (!target) return false;
    return clickCandidate(target);
  }, DATA_ANALYSIS_ACTIVE_PAGE_SELECTOR);
  if (!clicked) return false;
  return waitForDataAnalysisPageAdvance(page, previousSnapshot);
}

function normalizeRow(raw) {
  const title = cleanTitle(raw.title || '');
  if (!title) return null;
  const sourceNoteId = String(raw.noteId || extractNoteIdFromUrl(raw.detailHref || '') || '').trim();
  const workId = sourceNoteId || stableRowId(title, raw.publishText || '');
  const avgWatchSeconds = parseSecondsText(raw.avgWatchText || '');

  return {
    '作品ID': workId,
    '标题': title,
    '发布日期': formatDateInput(raw.publishText),
    '曝光量': parseCount(raw.exposureText),
    '阅读量': parseCount(raw.readText),
    '点赞量': parseCount(raw.likeText),
    '收藏量': parseCount(raw.collectText),
    '评论量': parseCount(raw.commentText),
    '分享量': parseCount(raw.shareText),
    '涨粉量': parseCount(raw.fansText),
    '弹幕量': parseCount(raw.danmakuText),
    '封面点击率': normalizePercentText(raw.coverClickText),
    '平均观看时长': avgWatchSeconds,
    '平均播放时长': avgWatchSeconds,
    '链接': raw.detailHref || (sourceNoteId ? buildDetailUrl(sourceNoteId) : ''),
    '平台': 'xiaohongshu',
    '内容类型': raw.isVideo ? 'video' : 'image_text',
    '__isVideo': raw.isVideo ? '1' : '0',
    '__detailHref': raw.detailHref || '',
    '__rowKey': raw.rowKey || `${title}|${raw.publishText || ''}`,
    '__hasRealNoteId': sourceNoteId ? '1' : '0',
  };
}

function normalizeApiRow(note) {
  const workId = String(note?.id || '').trim();
  const title = cleanTitle(note?.display_title || note?.title || '');
  if (!workId || !title) return null;

  const isVideo = String(note?.type || '').toLowerCase() === 'video'
    || Number(note?.type) === 2
    || Number.parseInt(note?.video_info?.duration || 0, 10) > 0;
  const publishText = formatDateTimeFromTimestamp(note?.post_time || note?.time || note?.publish_time);
  const avgWatchSeconds = parseSecondsText(note?.view_time_avg ?? note?.avg_view_time ?? '');

  return {
    '作品ID': workId,
    '标题': title,
    '发布日期': formatDateInput(note?.time || '') || formatDateFromTimestamp(note?.post_time || note?.publish_time),
    '曝光量': parseCount(note?.imp_count ?? note?.impression_count ?? note?.expose_count ?? 0),
    '阅读量': parseCount(note?.read_count ?? note?.view_count ?? 0),
    '点赞量': parseCount(note?.likes ?? note?.like_count ?? 0),
    '收藏量': parseCount(note?.fav_count ?? note?.collected_count ?? note?.collect_count ?? 0),
    '评论量': parseCount(note?.comments_count ?? note?.comment_count ?? 0),
    '分享量': parseCount(note?.shared_count ?? note?.share_count ?? 0),
    '涨粉量': parseCount(
      note?.new_fans_count
      ?? note?.new_follow_count
      ?? note?.gain_fans_count
      ?? note?.fans_increment
      ?? note?.follow_count_delta
      ?? note?.increase_fans_count
      ?? 0,
    ),
    '弹幕量': parseCount(note?.danmaku_count ?? 0),
    '封面点击率': normalizeRatioPercent(note?.coverClickRate ?? note?.cover_click_rate ?? ''),
    '平均观看时长': avgWatchSeconds,
    '平均播放时长': avgWatchSeconds,
    '平台': 'xiaohongshu',
    '内容类型': isVideo ? 'video' : 'image_text',
    '链接': buildDetailUrl(workId),
    '__isVideo': isVideo ? '1' : '0',
    '__detailHref': buildDetailUrl(workId),
    '__rowKey': `${title}|${publishText || note?.time || ''}`,
    '__hasRealNoteId': '1',
  };
}

function mergeFoundRows(domFound, apiFound) {
  const merged = new Map(domFound);
  for (const [workId, apiRow] of apiFound.entries()) {
    let targetKey = workId;
    if (!merged.has(targetKey)) {
      for (const [key, row] of merged.entries()) {
        const sameRowKey = row.__rowKey && apiRow.__rowKey && row.__rowKey === apiRow.__rowKey;
        const sameTitleDate = row['标题'] === apiRow['标题'] && row['发布日期'] === apiRow['发布日期'];
        if (sameRowKey || sameTitleDate) {
          targetKey = key;
          break;
        }
      }
    }
    const existing = merged.get(targetKey) || {};
    if (targetKey !== workId) merged.delete(targetKey);
    merged.set(workId, { ...existing, ...apiRow });
  }
  return merged;
}

function stripInternalFields(row) {
  const copy = { ...row };
  for (const key of Object.keys(copy)) {
    if (key.startsWith('__')) delete copy[key];
  }
  return copy;
}

export function applySelectionRules(rows, options = {}) {
  const stripInternal = options.stripInternal !== false;
  const filteredRows = CONFIG.onlyVideo
    ? rows.filter((row) => row.__isVideo === '1')
    : rows;

  const selected = [];
  for (let i = 0; i < filteredRows.length; i += 1) {
    const row = filteredRows[i];
    const inRequestedDateWindow = meetsDateRange(row['发布日期']);

    if (inRequestedDateWindow) {
      selected.push(row);
    }
  }

  const clean = stripInternal ? selected.map(stripInternalFields) : selected;

  if (CONFIG.forceFullExport) return clean;
  if (CONFIG.videoLimit > 0) return clean.slice(0, CONFIG.videoLimit);
  return clean;
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
  ]);

  console.log(`[xhs-merge] 已生成总表：${CONFIG.outputPath}`);
}

async function loadCachedRowsById() {
  try {
    const raw = await fs.readFile(CONFIG.tempRowsPath, 'utf-8');
    const rows = JSON.parse(raw);
    if (!Array.isArray(rows)) return new Map();
    const out = new Map();
    for (const row of rows) {
      const workId = String(row?.['作品ID'] || '').trim();
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
    const publishTs = parseDateValue(row?.['发布日期']);
    const refreshAfter = Date.now() - CONFIG.refreshDays * 24 * 3600 * 1000;
    if (publishTs && publishTs >= refreshAfter) return true;
  }
  return false;
}

async function ensureCoreDataTab(page) {
  await page.waitForLoadState('domcontentloaded', { timeout: 60000 }).catch(() => {});
  const coreTabByRole = page.getByRole('tab', { name: /核心数据/ }).first();
  if ((await coreTabByRole.count()) > 0) {
    await coreTabByRole.click({ timeout: 8000 }).catch(() => {});
  } else {
    const coreTabByText = page.getByText('核心数据', { exact: true }).first();
    if ((await coreTabByText.count()) > 0) {
      await coreTabByText.click({ timeout: 8000 }).catch(() => {});
    }
  }
  await page.waitForTimeout(1200);
  await page.waitForSelector('text=基础数据', { timeout: 15000 }).catch(() => {});
}

async function extractDetailMetricsFromPage(page) {
  return page.evaluate(() => {
    const getText = (el) => (el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '');
    const normalizeValue = (value) => {
      const text = String(value || '').replace(/,/g, '').trim();
      const match = text.match(/-?\d+(?:\.\d+)?/);
      return match ? match[0] : '';
    };
    const metricNames = [
      ['曝光数', '曝光量'],
      ['观看数', '阅读量'],
      ['封面点击率', '封面点击率'],
      ['平均观看时长', '平均观看时长'],
      ['完播率', '完播率'],
      ['2秒退出率', '2s跳出率'],
      ['涨粉数', '涨粉量'],
    ];
    const text = getText(document.body);
    const metrics = {};
    for (const [label, field] of metricNames) {
      const pattern = new RegExp(`${label}\\s*([0-9,.]+\\s*%?|[0-9,.]+\\s*秒?)`);
      const match = text.match(pattern);
      if (match?.[1]) metrics[field] = normalizeValue(match[1]);
    }
    if (metrics['平均观看时长']) metrics['平均播放时长'] = metrics['平均观看时长'];
    if (metrics['2s跳出率']) metrics['跳出率口径'] = '2s';
    return metrics;
  }).catch(() => ({}));
}

async function clickVisibleDetailForRow(page, row) {
  const title = row['标题'] || '';
  const publishDate = rowPublishHint(row).replace(/[/.]/g, '-').slice(0, 10);
  let rowLocator = page.locator('tbody tr, .el-table__body tr, .ant-table-tbody tr, [role="row"]');
  if (title) rowLocator = rowLocator.filter({ hasText: title });
  if (publishDate) rowLocator = rowLocator.filter({ hasText: publishDate });

  const rowCount = await rowLocator.count().catch(() => 0);
  for (let index = 0; index < rowCount; index += 1) {
    const rowItem = rowLocator.nth(index);
    const detail = rowItem.locator('.note-detail, [class*="note-detail"]').filter({ hasText: /^\s*详情数据\s*$/ }).first();
    if ((await detail.count().catch(() => 0)) <= 0) continue;
    await detail.scrollIntoViewIfNeeded().catch(() => {});
    await detail.click({ timeout: 10000 });
    const rowText = await rowItem.innerText({ timeout: 3000 }).catch(() => '');
    return { clicked: true, rowText: rowText.slice(0, 200) };
  }

  return { clicked: false, reason: 'row_not_found' };
}

async function restoreDataAnalysisPage(page) {
  if (page.url().includes('/statistics/data-analysis')) return;
  await page.goBack({ waitUntil: 'domcontentloaded', timeout: 30000 }).catch(async () => {
    await page.goto(CONFIG.dataAnalysisUrl, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
  });
  if (!page.url().includes('/statistics/data-analysis')) {
    await page.goto(CONFIG.dataAnalysisUrl, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
  }
  await page.waitForTimeout(1500);
}

async function resolveOneDetailUrlFromList(page, row, index, total) {
  if (row.__detailHref || row['链接']) return row;
  await updateProgress({
    phase: 'resolving_detail_url',
    message: `解析小红书详情链接：${index}/${total} ${row['标题'] || row['作品ID']}`,
    currentIndex: index,
    currentWorkId: row['作品ID'] || '',
    currentTitle: row['标题'] || '',
    totalWorks: total,
    processedWorks: Math.max(0, index - 1),
    queuedWorks: Math.max(0, total - index + 1),
  });

  await page.goto(CONFIG.dataAnalysisUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(1800);

  const maxPagesToSearch = 5;
  for (let pageIndex = 0; pageIndex < maxPagesToSearch; pageIndex += 1) {
    const clicked = await clickVisibleDetailForRow(page, row);
    if (clicked.clicked) {
      await page.waitForURL(/\/statistics\/note-detail\?noteId=/, { waitUntil: 'domcontentloaded', timeout: 20000 }).catch(() => {});
      await page.waitForTimeout(1200);
      const detailHref = page.url();
      const noteId = extractNoteIdFromUrl(detailHref);
      await restoreDataAnalysisPage(page);
      if (noteId) {
        return {
          ...row,
          '作品ID': noteId,
          '链接': buildDetailUrl(noteId),
          '__detailHref': buildDetailUrl(noteId),
          '__hasRealNoteId': '1',
        };
      }
      return { ...row, '详情采集错误': `详情跳转未解析到 noteId：${detailHref}` };
    }

    const moved = await goToNextDataAnalysisPage(page);
    if (!moved) break;
    await page.waitForTimeout(1200);
  }
  return { ...row, '详情采集错误': '列表页未定位到对应“详情数据”入口' };
}

async function resolveMissingDetailUrls(page, rows) {
  if (!CONFIG.detailExportEnabled || rows.length === 0) return rows;
  const resolved = [];
  for (let i = 0; i < rows.length; i += 1) {
    const row = rows[i];
    const result = await resolveOneDetailUrlFromList(page, row, i + 1, rows.length);
    resolved.push(result);
  }
  return resolved;
}

async function parseDetailExport(filePath) {
  const { stdout } = await execFileAsync(CONFIG.pythonBin, [
    CONFIG.detailNormalizerScriptPath,
    '--input',
    filePath,
  ], { encoding: 'utf-8', maxBuffer: 1024 * 1024 });
  return JSON.parse(stdout || '{}');
}

async function downloadDetailExports(page, row) {
  await ensureDir(CONFIG.detailExportDir);
  const exportButtons = page.getByRole('button', { name: /导出数据/ });
  const total = await exportButtons.count();
  if (total <= 0) {
    return { files: [], errors: ['详情页未找到“导出数据”按钮'] };
  }

  const files = [];
  const errors = [];
  const maxExports = Math.min(total, 2);
  for (let index = 0; index < maxExports; index += 1) {
    const kind = index === 0 ? 'core' : 'interaction';
    try {
      const button = exportButtons.nth(index);
      const downloadPromise = page.waitForEvent('download', { timeout: CONFIG.detailExportTimeoutMs }).catch(() => null);
      await button.scrollIntoViewIfNeeded().catch(() => {});
      await button.click({ timeout: 10000 });
      const download = await downloadPromise;
      if (!download) {
        errors.push(`${kind}: 点击导出后未捕获到下载文件`);
        continue;
      }

      const suggested = sanitizeFilename(download.suggestedFilename() || `${row['作品ID']}-数据明细表.xlsx`, 100);
      const targetPath = path.join(
        CONFIG.detailExportDir,
        sanitizeFilename(`xhs-detail-${row['作品ID']}-${kind}-${Date.now()}-${suggested}`, 180),
      );
      await download.saveAs(targetPath);
      files.push({ kind, path: targetPath });
      await page.waitForTimeout(500).catch(() => {});
    } catch (error) {
      errors.push(`${kind}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  return { files, errors };
}

async function enrichOneRowWithDetail(context, row, index, total) {
  if (!CONFIG.detailExportEnabled || row.__isVideo !== '1') return row;

  const currentId = String(row['作品ID'] || '').trim();
  const detailHref = row.__detailHref || row['链接'] || (row.__hasRealNoteId === '1' ? buildDetailUrl(currentId) : '');
  if (!detailHref) {
    return { ...row, '详情采集状态': 'skipped_no_detail_url' };
  }

  const detailPage = await context.newPage();
  try {
    await updateProgress({
      phase: 'detail_exporting',
      message: `补采小红书详情指标：${index}/${total} ${row['标题'] || currentId}`,
      currentIndex: index,
      currentWorkId: currentId,
      currentTitle: row['标题'] || '',
      totalWorks: total,
      processedWorks: Math.max(0, index - 1),
      queuedWorks: Math.max(0, total - index + 1),
    });

    await detailPage.goto(detailHref, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await ensureCoreDataTab(detailPage);
    const noteId = extractNoteIdFromUrl(detailPage.url()) || (row.__hasRealNoteId === '1' ? currentId : '');
    const pageMetrics = await extractDetailMetricsFromPage(detailPage);
    let exportMetrics = {};
    let exportedKinds = [];
    let detailErrors = [];
    try {
      const { files, errors } = await downloadDetailExports(detailPage, { ...row, '作品ID': noteId || currentId });
      detailErrors = errors;
      for (const file of files) {
        try {
          const metrics = await parseDetailExport(file.path);
          exportMetrics = { ...exportMetrics, ...metrics };
          exportedKinds.push(file.kind);
        } catch (error) {
          detailErrors.push(`${file.kind}: 解析失败 ${error instanceof Error ? error.message : String(error)}`);
        }
      }
    } catch (error) {
      detailErrors.push(error instanceof Error ? error.message : String(error));
    }
    if (detailErrors.length > 0) {
      console.log(`[xhs-detail] ${currentId} 详情导出/解析部分失败：${detailErrors.join('；')}`);
    }

    exportedKinds = [...new Set(exportedKinds)];
    let detailStatus = 'page_fallback';
    if (exportedKinds.includes('core') && exportedKinds.includes('interaction')) {
      detailStatus = 'exported_core_interaction';
    } else if (exportedKinds.length > 0) {
      detailStatus = `partial_exported_${exportedKinds.join('_')}`;
    }

    const merged = {
      ...row,
      ...pageMetrics,
      ...exportMetrics,
      '作品ID': noteId || currentId,
      '链接': noteId ? buildDetailUrl(noteId) : detailHref,
      '详情采集状态': detailStatus,
      '详情采集错误': detailErrors.join('；'),
    };
    return merged;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.log(`[xhs-detail] ${currentId} 详情补采失败：${message}`);
    return { ...row, '详情采集状态': `failed: ${message}` };
  } finally {
    await detailPage.close().catch(() => {});
  }
}

async function enrichRowsWithDetails(context, rows) {
  if (!CONFIG.detailExportEnabled || rows.length === 0) return rows;
  const enriched = [];
  for (let i = 0; i < rows.length; i += 1) {
    const row = rows[i];
    const result = await enrichOneRowWithDetail(context, row, i + 1, rows.length);
    enriched.push(result);
    await pageSafeDelay(700);
  }
  return enriched;
}

async function reuseCachedRowsOrEnrich(context, page, rows) {
  if (rows.length === 0) {
    return {
      rows: [],
      metrics: { totalWorks: 0, processedWorks: 0, successWorks: 0, skippedWorks: 0, failedWorks: 0 },
    };
  }
  if (!CONFIG.detailExportEnabled) {
    return {
      rows,
      metrics: {
        totalWorks: rows.length,
        processedWorks: rows.length,
        successWorks: rows.length,
        skippedWorks: 0,
        failedWorks: 0,
      },
    };
  }

  const cachedRows = await loadCachedRowsById();
  const finalRows = new Array(rows.length);
  const freshRows = [];
  const freshPositions = [];
  const metrics = {
    totalWorks: rows.length,
    processedWorks: 0,
    successWorks: 0,
    skippedWorks: 0,
    failedWorks: 0,
  };

  for (let i = 0; i < rows.length; i += 1) {
    const row = rows[i];
    const workId = String(row?.['作品ID'] || '').trim();
    const cached = workId ? cachedRows.get(workId) : null;
    if (cached && !shouldRefreshWork(row, i)) {
      finalRows[i] = cached;
      metrics.processedWorks += 1;
      metrics.skippedWorks += 1;
      await updateProgress({
        phase: 'detail_exporting',
        message: `跳过已存在的小红书作品：${row['标题'] || workId}`,
        totalWorks: metrics.totalWorks,
        processedWorks: metrics.processedWorks,
        successWorks: metrics.successWorks,
        skippedWorks: metrics.skippedWorks,
        failedWorks: metrics.failedWorks,
        queuedWorks: Math.max(0, metrics.totalWorks - metrics.processedWorks),
        currentIndex: i + 1,
        currentWorkId: workId,
        currentTitle: row['标题'] || '',
      });
    } else {
      freshRows.push(row);
      freshPositions.push(i);
    }
  }

  const resolvedFreshRows = await resolveMissingDetailUrls(page, freshRows);
  for (let i = 0; i < resolvedFreshRows.length; i += 1) {
    const position = freshPositions[i];
    const row = resolvedFreshRows[i];
    const workId = String(row?.['作品ID'] || '').trim();
    const enriched = await enrichOneRowWithDetail(context, row, metrics.processedWorks + 1, metrics.totalWorks);
    finalRows[position] = enriched;
    metrics.processedWorks += 1;
    if (String(enriched?.['详情采集状态'] || '').startsWith('failed')) {
      metrics.failedWorks += 1;
    } else {
      metrics.successWorks += 1;
    }
    await updateProgress({
      phase: 'detail_exporting',
      message: `小红书详情数据采集中：${metrics.processedWorks}/${metrics.totalWorks}`,
      totalWorks: metrics.totalWorks,
      processedWorks: metrics.processedWorks,
      successWorks: metrics.successWorks,
      skippedWorks: metrics.skippedWorks,
      failedWorks: metrics.failedWorks,
      queuedWorks: Math.max(0, metrics.totalWorks - metrics.processedWorks),
      currentIndex: metrics.processedWorks,
      currentWorkId: workId,
      currentTitle: row['标题'] || '',
    });
    await pageSafeDelay(700);
  }

  return { rows: finalRows.filter(Boolean), metrics };
}

async function pageSafeDelay(ms) {
  await new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function scrapeNotes(context, page) {
  await updateProgress({ phase: 'collecting', message: '正在进入小红书笔记管理页' });
  const domFound = new Map();
  const apiFound = new Map();
  let apiTotal = 0;
  const mergedFound = () => {
    return mergeFoundRows(domFound, apiFound);
  };

  page.on('response', async (response) => {
    const url = response.url();
    if (!url.includes('/api/galaxy/v2/creator/note/user/posted')
      && !url.includes('/api/galaxy/creator/datacenter/note/analyze/list')) return;
    if (response.status() !== 200) return;
    try {
      const payload = await response.json();
      const notes = Array.isArray(payload?.data?.notes)
        ? payload.data.notes
        : (Array.isArray(payload?.data?.note_infos) ? payload.data.note_infos : []);
      const total = Number.parseInt(payload?.data?.total ?? 0, 10);
      if (Number.isFinite(total) && total > 0) {
        apiTotal = Math.max(apiTotal, total);
      }
      for (const note of notes) {
        const row = normalizeApiRow(note);
        if (!row) continue;
        apiFound.set(row['作品ID'], row);
      }
    } catch {
      // ignore
    }
  });

  page = await ensureOnNoteManager(context, page);

  let expectedTotal = 0;
  let stableRounds = 0;
  let pageTurns = 0;

  for (let round = 1; round <= CONFIG.maxScrollRounds; round += 1) {
    const snapshot = await collectVisibleNotes(page);
    if (snapshot.totalFromTab > 0) expectedTotal = snapshot.totalFromTab;
    if (apiTotal > 0) expectedTotal = Math.max(expectedTotal, apiTotal);

    let newCount = 0;
    for (const raw of snapshot.notes) {
      const row = normalizeRow(raw);
      if (!row) continue;
      const id = row.__hasRealNoteId === '1' ? row['作品ID'] : (row.__rowKey || row['作品ID']);
      if (!domFound.has(id)) newCount += 1;
      domFound.set(id, row);
    }

    const sourceFound = mergedFound();
    if (newCount === 0) stableRounds += 1;
    else stableRounds = 0;

    const discovered = sourceFound.size;
    const target = expectedTotal || Math.max(discovered, CONFIG.videoLimit || 0);
    const queued = Math.max(0, target - discovered);

    await updateProgress({
      phase: 'collecting',
      message: `扫描中：已采集 ${discovered}${expectedTotal ? ` / ${expectedTotal}` : ''}${apiFound.size > 0 ? '（页面+API）' : '（页面）'}`,
      totalWorks: target,
      queuedWorks: queued,
      processedWorks: discovered,
      successWorks: discovered,
      skippedWorks: 0,
      failedWorks: 0,
      currentIndex: discovered,
      currentWorkId: '',
      currentTitle: '',
    });

    const reachedLimit = CONFIG.videoLimit > 0 && discovered >= CONFIG.videoLimit;
    const reachedTotal = expectedTotal > 0 && discovered >= expectedTotal;
    if ((reachedLimit && stableRounds >= 1)
      || (reachedTotal && stableRounds >= 2)
      || stableRounds >= CONFIG.staleRoundsLimit) {
      break;
    }

    const scrollState = await scrollNoteList(page);
    await page.waitForTimeout(1100);
    if (!scrollState.changed && scrollState.atBottom) {
      stableRounds += 1;
    }
    if (stableRounds >= 1 && page.url().includes('/statistics/data-analysis')) {
      const moved = await goToNextDataAnalysisPage(page);
      if (moved) {
        pageTurns += 1;
        stableRounds = 0;
        await updateProgress({
          phase: 'collecting',
          message: `翻到小红书列表第 ${pageTurns + 1} 页继续扫描`,
        });
      }
    }
  }

  const rows = Array.from(mergedFound().values());
  return applySelectionRules(rows, { stripInternal: false });
}

async function main() {
  await updateProgress({
    ...newProgressState(),
    status: 'running',
    phase: 'boot',
    message: '小红书任务启动',
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
    if (CONFIG.authOnly) {
      await updateProgress({ phase: 'login', message: '等待扫码登录（AUTH_ONLY）' });
      page = await ensureOnNoteManager(context, page);
      await page.waitForTimeout(1500);

      await updateProgress({
        status: 'completed',
        phase: 'done',
        message: '小红书登录完成（AUTH_ONLY）',
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

    page = await ensureOnNoteManager(context, page);
    const listRows = await scrapeNotes(context, page);
    const detailResult = await reuseCachedRowsOrEnrich(context, page, listRows);
    const rows = detailResult.rows.map(stripInternalFields);
    const metrics = detailResult.metrics;

    await updateProgress({
      phase: 'merging',
      message: `写入小红书总表（${rows.length} 条）`,
      totalWorks: rows.length,
      processedWorks: rows.length,
      queuedWorks: 0,
      successWorks: metrics.successWorks,
      failedWorks: metrics.failedWorks,
      skippedWorks: metrics.skippedWorks,
    });

    await writeExcel(rows);

    await updateProgress({
      status: 'completed',
      phase: 'done',
      message: metrics.skippedWorks > 0 && metrics.successWorks === 0 && metrics.failedWorks === 0
        ? `本轮没有新增采集，已沿用已有 ${metrics.skippedWorks} 条小红书本地结果`
        : `小红书任务完成，共 ${rows.length} 条`,
      finishedAt: new Date().toISOString(),
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await updateProgress({
      status: 'failed',
      phase: 'failed',
      message: `小红书链路失败：${message}`,
      finishedAt: new Date().toISOString(),
    });
    throw error;
  } finally {
    await context.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.error(`[xhs-error] ${error instanceof Error ? error.message : String(error)}`);
    process.exit(1);
  });
}
