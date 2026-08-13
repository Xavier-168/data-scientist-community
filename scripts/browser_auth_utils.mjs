import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

export function isPlaceholderBrowserUrl(value) {
  const text = String(value ?? '').trim().toLowerCase();
  return (
    !text
    || text === 'about:blank'
    || text.startsWith('chrome://new-tab-page')
    || text.startsWith('chrome://newtab')
    || text.startsWith('edge://newtab')
  );
}

function escapeAppleScriptText(value) {
  return String(value ?? '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

export function resolveBrowserAppName(env = process.env) {
  const executablePath = String(env?.BROWSER_EXECUTABLE_PATH ?? '').trim();
  if (executablePath) {
    const parts = executablePath.split(path.sep);
    const appSegment = parts.find((item) => item && item.toLowerCase().endsWith('.app'));
    if (appSegment) {
      return appSegment.replace(/\.app$/i, '').trim();
    }
  }

  const channel = String(env?.BROWSER_CHANNEL ?? '').trim().toLowerCase();
  if (channel === 'chromium') return 'Chromium';
  if (channel === 'msedge') return 'Microsoft Edge';
  return 'Google Chrome';
}

export async function activateBrowserApp(options = {}) {
  const platform = options.platform ?? process.platform;
  if (platform !== 'darwin') return false;

  const env = options.env ?? process.env;
  const isHeadless = String(env?.HEADLESS ?? '').trim().toLowerCase() === 'true';
  if (isHeadless) return false;

  const appName = String(options.appName || resolveBrowserAppName(env)).trim();
  if (!appName) return false;

  const runExecFile = options.execFile ?? execFileAsync;
  const timeout = Number.isFinite(options.timeoutMs) ? options.timeoutMs : 3000;
  try {
    await runExecFile(
      'osascript',
      ['-e', `tell application "${escapeAppleScriptText(appName)}" to activate`],
      { timeout },
    );
    return true;
  } catch {
    return false;
  }
}

async function safeBringToFront(page, options = {}) {
  if (!page || typeof page.bringToFront !== 'function') return;
  try {
    await page.bringToFront();
  } catch {
    // ignore
  }
  await activateBrowserApp(options.browserActivation || {});
}

async function safeClose(page) {
  if (!page || typeof page.isClosed !== 'function' || page.isClosed()) return;
  try {
    await page.close();
  } catch {
    // ignore
  }
}

export async function prepareAuthPage(context, page = null, options = {}) {
  const pages = (context?.pages?.() || []).filter((item) => item && typeof item.isClosed === 'function' && !item.isClosed());
  let activePage = page && !page.isClosed?.() ? page : null;

  if (!activePage || isPlaceholderBrowserUrl(activePage.url?.())) {
    activePage = pages.find((item) => !isPlaceholderBrowserUrl(item.url?.())) || activePage;
  }
  if (!activePage || isPlaceholderBrowserUrl(activePage.url?.())) {
    activePage = await context.newPage();
  }

  await safeBringToFront(activePage, options);

  for (const item of pages) {
    if (item === activePage) continue;
    if (isPlaceholderBrowserUrl(item.url?.())) {
      await safeClose(item);
    }
  }

  return activePage;
}

export async function navigateAuthCandidates(context, page, candidates, options = {}) {
  const urls = Array.from(new Set((candidates || []).map((item) => String(item || '').trim()).filter(Boolean)));
  const waitUntil = options.waitUntil || 'domcontentloaded';
  const timeout = Number.isFinite(options.timeout) ? options.timeout : 60000;
  const settleMs = Number.isFinite(options.settleMs) ? options.settleMs : 1200;
  const errors = [];

  let activePage = await prepareAuthPage(context, page, options);

  for (let index = 0; index < urls.length; index += 1) {
    const targetUrl = urls[index];
    try {
      await activePage.goto(targetUrl, { waitUntil, timeout });
      if (settleMs > 0 && typeof activePage.waitForTimeout === 'function') {
        await activePage.waitForTimeout(settleMs);
      }
      await safeBringToFront(activePage, options);
      if (!isPlaceholderBrowserUrl(activePage.url?.())) {
        return activePage;
      }
      errors.push(`${targetUrl}: stayed on ${String(activePage.url?.() || 'about:blank')}`);
    } catch (error) {
      errors.push(`${targetUrl}: ${error instanceof Error ? error.message : String(error)}`);
    }

    if (index < urls.length - 1) {
      const retryPage = await context.newPage();
      await safeBringToFront(retryPage, options);
      if (isPlaceholderBrowserUrl(activePage.url?.())) {
        await safeClose(activePage);
      }
      activePage = retryPage;
    }
  }

  throw new Error(`无法打开授权页面：${errors.join(' | ') || 'no_candidate_url'}`);
}
