import { execFile, spawn } from 'node:child_process';
import { constants as fsConstants } from 'node:fs';
import { access } from 'node:fs/promises';
import { createServer, connect } from 'node:net';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { chromium } from 'playwright';

const execFileAsync = promisify(execFile);
const ROOT = fileURLToPath(new URL('..', import.meta.url));
const HOST = '127.0.0.1';
const START_TIMEOUT_MS = 15_000;
const STOP_TIMEOUT_MS = 2_000;
const OUTPUT_LIMIT_BYTES = 16 * 1024;
const MAX_PORT_RETRIES = 3;

class SmokeError extends Error {
  constructor(code, details = []) {
    super(code);
    this.name = 'SmokeError';
    this.code = code;
    this.details = details;
  }
}

function fixedError(error, fallbackCode) {
  return error instanceof SmokeError ? error : new SmokeError(fallbackCode);
}

function aggregateFailures(entries, aggregateCode = 'smoke_multiple_failures') {
  const failures = entries.filter(([, error]) => error);
  if (failures.length === 0) return null;
  if (failures.length === 1) return failures[0][1];
  return new SmokeError(
    aggregateCode,
    failures.map(([stage, error]) => `${stage}=${error.code}`),
  );
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function boundedOperation(operation, timeoutCode, failureCode) {
  let timeout;
  try {
    return await Promise.race([
      Promise.resolve()
        .then(operation)
        .catch(() => {
          throw new SmokeError(failureCode);
        }),
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new SmokeError(timeoutCode)), STOP_TIMEOUT_MS);
      }),
    ]);
  } finally {
    clearTimeout(timeout);
  }
}

async function reserveFreePort() {
  const reservation = createServer();
  try {
    await new Promise((resolve, reject) => {
      reservation.once('error', reject);
      reservation.listen({ host: HOST, port: 0, exclusive: true }, resolve);
    });
    const address = reservation.address();
    if (!address || typeof address === 'string') {
      throw new SmokeError('dynamic_port_unavailable');
    }
    return address.port;
  } catch (error) {
    throw fixedError(error, 'dynamic_port_unavailable');
  } finally {
    if (reservation.listening) {
      await new Promise((resolve) => reservation.close(() => resolve()));
    }
  }
}

async function occupyReservedPort(port) {
  const blocker = createServer((socket) => socket.destroy());
  try {
    await new Promise((resolve, reject) => {
      blocker.once('error', reject);
      blocker.listen({ host: HOST, port, exclusive: true }, resolve);
    });
    return blocker;
  } catch {
    try {
      blocker.close();
    } catch {}
    throw new SmokeError('port_conflict_test_setup_failed');
  }
}

async function closePortBlocker(blocker) {
  if (!blocker?.listening) return null;
  try {
    await new Promise((resolve, reject) => {
      blocker.close((error) => {
        if (error) reject(error);
        else resolve();
      });
    });
    return null;
  } catch {
    return new SmokeError('port_conflict_test_cleanup_failed');
  }
}

function captureBoundedOutput(...streams) {
  let output = Buffer.alloc(0);
  for (const stream of streams) {
    stream?.on('data', (chunk) => {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      output = Buffer.concat([output, bytes]);
      if (output.length > OUTPUT_LIMIT_BYTES) {
        output = output.subarray(output.length - OUTPUT_LIMIT_BYTES);
      }
    });
  }
  return () => output.toString('utf8').trim();
}

function appendBoundedOutput(current, next) {
  const joined = Buffer.from([current, next].filter(Boolean).join('\n'), 'utf8');
  return joined.subarray(Math.max(0, joined.length - OUTPUT_LIMIT_BYTES)).toString('utf8');
}

function processGroupExists(pid) {
  if (!pid) return false;
  try {
    process.kill(-pid, 0);
    return true;
  } catch (error) {
    if (error?.code === 'ESRCH') return false;
    throw new SmokeError('vite_process_check_failed');
  }
}

function pidExists(pid) {
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === 'ESRCH') return false;
    throw new SmokeError('browser_process_check_failed');
  }
}

function signalProcessGroup(pid, signal) {
  if (!pid) return;
  try {
    process.kill(-pid, signal);
  } catch (error) {
    if (error?.code !== 'ESRCH') throw new SmokeError('vite_signal_failed');
  }
}

async function portAcceptsConnections(port) {
  return await new Promise((resolve) => {
    const socket = connect({ host: HOST, port });
    let settled = false;
    const finish = (connected) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      socket.destroy();
      resolve(connected);
    };
    const timeout = setTimeout(() => finish(false), 200);
    socket.once('connect', () => finish(true));
    socket.once('error', () => finish(false));
  });
}

async function waitForVite(child, port, readSpawnError) {
  const deadline = Date.now() + START_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (readSpawnError()) throw new SmokeError('vite_spawn_failed');
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new SmokeError('vite_exited_early');
    }
    try {
      const response = await fetch(`http://${HOST}:${port}`, {
        signal: AbortSignal.timeout(300),
      });
      if (response.ok) return;
    } catch {}
    await delay(75);
  }
  throw new SmokeError('vite_start_timeout');
}

async function waitForOwnedViteGroupExit(ownedGroupPid) {
  const deadline = Date.now() + STOP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (!processGroupExists(ownedGroupPid)) return true;
    await delay(50);
  }
  return !processGroupExists(ownedGroupPid);
}

async function waitForPortCleanup(port) {
  const deadline = Date.now() + STOP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (!(await portAcceptsConnections(port))) return true;
    await delay(50);
  }
  return !(await portAcceptsConnections(port));
}

async function cleanupVite(
  child,
  childClosed,
  childDidClose,
  port,
  allowOccupiedPort,
) {
  const errors = [];
  const ownedGroupPid = !childDidClose() && child.pid ? child.pid : null;
  if (ownedGroupPid) {
    try {
      signalProcessGroup(ownedGroupPid, 'SIGTERM');
    } catch (error) {
      errors.push(fixedError(error, 'vite_term_failed'));
    }
    let groupExited = false;
    try {
      groupExited = await waitForOwnedViteGroupExit(ownedGroupPid);
    } catch (error) {
      errors.push(fixedError(error, 'vite_process_check_failed'));
    }
    if (!groupExited) {
      try {
        signalProcessGroup(ownedGroupPid, 'SIGKILL');
      } catch (error) {
        errors.push(fixedError(error, 'vite_kill_failed'));
      }
      try {
        if (!(await waitForOwnedViteGroupExit(ownedGroupPid))) {
          errors.push(new SmokeError('vite_process_group_leaked'));
        }
      } catch (error) {
        errors.push(fixedError(error, 'vite_process_check_failed'));
      }
    }
  }

  try {
    await boundedOperation(
      () => childClosed,
      'vite_child_close_timeout',
      'vite_child_close_failed',
    );
  } catch (error) {
    errors.push(error);
  }
  if (!allowOccupiedPort) {
    try {
      if (!(await waitForPortCleanup(port))) {
        errors.push(new SmokeError('vite_port_cleanup_timeout'));
      }
    } catch (error) {
      errors.push(fixedError(error, 'vite_port_cleanup_failed'));
    }
  }
  return aggregateFailures(
    errors.map((error, index) => [`vite_cleanup_${index + 1}`, error]),
    'vite_cleanup_multiple_failures',
  );
}

async function listProcessTreePids(rootPid) {
  try {
    const { stdout } = await execFileAsync('/bin/ps', ['-axo', 'pid=,ppid='], {
      maxBuffer: 4 * 1024 * 1024,
    });
    const children = new Map();
    for (const line of stdout.split('\n')) {
      const match = line.match(/^\s*(\d+)\s+(\d+)\s*$/);
      if (!match) continue;
      const pid = Number(match[1]);
      const parentPid = Number(match[2]);
      const siblings = children.get(parentPid) ?? [];
      siblings.push(pid);
      children.set(parentPid, siblings);
    }
    const processTree = new Set([rootPid]);
    const queue = [rootPid];
    while (queue.length > 0) {
      const parentPid = queue.shift();
      for (const childPid of children.get(parentPid) ?? []) {
        if (processTree.has(childPid)) continue;
        processTree.add(childPid);
        queue.push(childPid);
      }
    }
    return processTree;
  } catch {
    throw new SmokeError('browser_process_scan_failed');
  }
}

async function waitForProcessTreeExit(processTreePids, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const remaining = [...processTreePids].filter((pid) => pidExists(pid));
    if (remaining.length === 0) return [];
    await delay(50);
  }
  return [...processTreePids].filter((pid) => pidExists(pid));
}

async function cleanupBrowserServer(
  browserServer,
  browserProcess,
  browserProcessClosed,
  browserProcessDidClose,
) {
  const errors = [];
  let closeError = null;
  let processTreePids = new Set();

  if (browserServer) {
    const processHandleOpen = () =>
      Boolean(
        browserProcess &&
          !browserProcessDidClose() &&
          browserProcess.exitCode === null &&
          browserProcess.signalCode === null,
      );
    if (processHandleOpen()) {
      try {
        processTreePids = await listProcessTreePids(browserProcess.pid);
      } catch (error) {
        errors.push(error);
        processTreePids = new Set([browserProcess.pid]);
      }
    }
    try {
      await boundedOperation(
        () => browserServer.close(),
        'browser_close_timeout',
        'browser_close_failed',
      );
    } catch (error) {
      closeError = error;
      errors.push(error);
    }

    if (closeError || processHandleOpen()) {
      try {
        await boundedOperation(
          () => browserServer.kill(),
          'browser_kill_timeout',
          'browser_kill_failed',
        );
      } catch (error) {
        errors.push(error);
      }
    }

    try {
      await boundedOperation(
        () => browserProcessClosed,
        'browser_process_close_timeout',
        'browser_process_close_failed',
      );
    } catch (error) {
      errors.push(error);
    }
    if (processHandleOpen()) {
      errors.push(new SmokeError('browser_process_leaked'));
    }
    try {
      const remainingPids = await waitForProcessTreeExit(
        processTreePids,
        STOP_TIMEOUT_MS,
      );
      if (remainingPids.length > 0) {
        errors.push(new SmokeError('browser_process_tree_leaked'));
      }
    } catch (error) {
      errors.push(fixedError(error, 'browser_process_tree_check_failed'));
    }
  }

  return aggregateFailures(
    errors.map((error, index) => [`browser_cleanup_${index + 1}`, error]),
    'browser_cleanup_multiple_failures',
  );
}

async function runBrowserChecks(browser, url) {
  let page;
  try {
    page = await browser.newPage();
  } catch {
    throw new SmokeError('browser_page_create_failed');
  }
  const pageErrors = [];
  const consoleErrors = [];
  page.on('pageerror', () => pageErrors.push(true));
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(true);
  });

  try {
    await page.goto(url, {
      timeout: 10_000,
      waitUntil: 'domcontentloaded',
    });
  } catch {
    throw new SmokeError('browser_navigation_failed');
  }
  if ((await page.title()) !== '数据科学家 Community') {
    throw new SmokeError('startup_title_mismatch');
  }
  try {
    await page.getByRole('heading', { name: '数据科学家 Community', exact: true }).waitFor();
  } catch {
    throw new SmokeError('startup_heading_unavailable');
  }
  const logsButton = page.getByRole('button', { name: '查看启动日志', exact: true });
  try {
    await logsButton.waitFor({ state: 'visible' });
  } catch {
    throw new SmokeError('startup_logs_button_unavailable');
  }
  if (!(await logsButton.isEnabled())) {
    throw new SmokeError('startup_logs_button_disabled');
  }
  try {
    await logsButton.click();
    await page.waitForTimeout(50);
  } catch {
    throw new SmokeError('startup_logs_button_click_failed');
  }
  if (pageErrors.length > 0) throw new SmokeError('browser_page_error');
  if (consoleErrors.length > 0) throw new SmokeError('browser_console_error');
}

function confirmedStrictPortConflict(error, output, port) {
  if (error?.code !== 'vite_exited_early') return false;
  const escapedPort = String(port).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`(?:Error:\\s*)?Port ${escapedPort} is already in use`).test(output);
}

async function runSmokeAttempt(executablePath, { occupyPort = false } = {}) {
  let port;
  try {
    port = await reserveFreePort();
  } catch (error) {
    return { error: fixedError(error, 'dynamic_port_unavailable'), output: '', portConflict: false };
  }

  let portBlocker;
  if (occupyPort) {
    try {
      portBlocker = await occupyReservedPort(port);
    } catch (error) {
      return {
        error: fixedError(error, 'port_conflict_test_setup_failed'),
        output: '',
        portConflict: false,
      };
    }
  }

  let child;
  try {
    child = spawn(
      'npm',
      [
        '--prefix',
        'desktop',
        'run',
        'dev:web',
        '--',
        '--host',
        HOST,
        '--port',
        String(port),
        '--strictPort',
      ],
      {
        cwd: ROOT,
        detached: true,
        env: { ...process.env, NO_COLOR: '1' },
        stdio: ['ignore', 'pipe', 'pipe'],
      },
    );
  } catch {
    const blockerCleanupError = await closePortBlocker(portBlocker);
    return {
      error: aggregateFailures([
        ['primary', new SmokeError('vite_spawn_failed')],
        ['port_blocker_cleanup', blockerCleanupError],
      ]),
      output: '',
      portConflict: false,
    };
  }

  const readOutput = captureBoundedOutput(child.stdout, child.stderr);
  let childDidClose = child.exitCode !== null || child.signalCode !== null;
  const childClosed = childDidClose
    ? Promise.resolve()
    : new Promise((resolve) => {
        child.once('close', () => {
          childDidClose = true;
          resolve();
        });
      });
  let spawnError;
  child.once('error', () => {
    spawnError = true;
  });

  let primaryError = null;
  let browserCleanupError = null;
  let viteCleanupError = null;
  let browserServer;
  let browserProcess;
  let browserPid;
  let browserProcessClosed = Promise.resolve();
  let browserProcessDidClose = () => true;
  let strictPortConflict = false;
  let blockerCleanupError = null;

  try {
    await waitForVite(child, port, () => spawnError);
    try {
      browserServer = await chromium.launchServer({ executablePath, headless: true });
    } catch {
      throw new SmokeError('browser_launch_failed');
    }
    browserProcess = browserServer.process();
    browserPid = browserServer.process().pid;
    if (!Number.isSafeInteger(browserPid) || browserPid <= 0) {
      throw new SmokeError('browser_process_unavailable');
    }
    let browserDidClose =
      browserProcess.exitCode !== null || browserProcess.signalCode !== null;
    browserProcessClosed = browserDidClose
      ? Promise.resolve()
      : new Promise((resolve) => {
          browserProcess.once('close', () => {
            browserDidClose = true;
            resolve();
          });
        });
    browserProcessDidClose = () => browserDidClose;

    let browser;
    try {
      browser = await chromium.connect(browserServer.wsEndpoint());
    } catch {
      throw new SmokeError('browser_connect_failed');
    }
    await runBrowserChecks(browser, `http://${HOST}:${port}`);
  } catch (error) {
    primaryError = fixedError(error, 'browser_interaction_failed');
  } finally {
    if (primaryError?.code === 'vite_exited_early') {
      await Promise.race([childClosed, delay(500)]);
      strictPortConflict = confirmedStrictPortConflict(
        primaryError,
        readOutput(),
        port,
      );
    }
    try {
      browserCleanupError = await cleanupBrowserServer(
        browserServer,
        browserProcess,
        browserProcessClosed,
        browserProcessDidClose,
      );
    } catch (error) {
      browserCleanupError = fixedError(error, 'browser_cleanup_failed');
    } finally {
      try {
        viteCleanupError = await cleanupVite(
          child,
          childClosed,
          () => childDidClose,
          port,
          strictPortConflict,
        );
      } catch (error) {
        viteCleanupError = fixedError(error, 'vite_cleanup_failed');
      } finally {
        blockerCleanupError = await closePortBlocker(portBlocker);
      }
    }
  }

  const output = readOutput();
  const error = aggregateFailures([
    ['primary', primaryError],
    ['browser_cleanup', browserCleanupError],
    ['vite_cleanup', viteCleanupError],
    ['port_blocker_cleanup', blockerCleanupError],
  ]);
  return {
    error,
    output,
    portConflict:
      !browserCleanupError &&
      !viteCleanupError &&
      !blockerCleanupError &&
      strictPortConflict,
  };
}

async function runSmoke(executablePath, runNumber) {
  let diagnostics = '';
  for (let retry = 0; retry <= MAX_PORT_RETRIES; retry += 1) {
    const occupyPort =
      process.env.TAURI_SMOKE_OCCUPY_FIRST_PORT === '1' &&
      runNumber === 1 &&
      retry === 0;
    const result = await runSmokeAttempt(executablePath, { occupyPort });
    diagnostics = appendBoundedOutput(diagnostics, result.output);
    if (!result.error) return;
    if (result.portConflict && retry < MAX_PORT_RETRIES) continue;

    if (diagnostics) {
      process.stderr.write(`smoke_run_${runNumber}_vite_output:\n${diagnostics}\n`);
    }
    if (result.portConflict) {
      throw new SmokeError('vite_port_conflict_retries_exhausted');
    }
    throw result.error;
  }
}

async function main() {
  const executablePath = chromium.executablePath();
  try {
    await access(executablePath, fsConstants.X_OK);
  } catch {
    throw new SmokeError('playwright_chromium_missing');
  }

  for (let runNumber = 1; runNumber <= 2; runNumber += 1) {
    await runSmoke(executablePath, runNumber);
  }
  process.stdout.write('tauri_startup_ui_smoke: ok (2/2)\n');
}

try {
  await main();
} catch (error) {
  const smokeError = fixedError(error, 'unexpected_error');
  const details = smokeError.details.length > 0 ? `:${smokeError.details.join(',')}` : '';
  process.stderr.write(`tauri_startup_ui_smoke: failed:${smokeError.code}${details}\n`);
  process.exitCode = 1;
}
