import { execFile, spawn } from 'node:child_process';
import { constants as fsConstants } from 'node:fs';
import { access, stat } from 'node:fs/promises';
import { homedir } from 'node:os';
import { delimiter, dirname, isAbsolute, join, resolve } from 'node:path';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const RUST_TOOL_STOP_TIMEOUT_MS = 2_000;

function delay(milliseconds) {
  return new Promise((resolveDelay) => setTimeout(resolveDelay, milliseconds));
}

function ownedProcessGroupExists(groupPid, isChildClosed) {
  if (!groupPid) return false;
  if (process.platform === 'win32') return !isChildClosed();
  try {
    process.kill(-groupPid, 0);
    return true;
  } catch (error) {
    if (error?.code === 'ESRCH') return false;
    return true;
  }
}

function signalOwnedProcessGroup(child, groupPid, signal) {
  if (!groupPid) return;
  try {
    if (process.platform === 'win32') child.kill(signal);
    else process.kill(-groupPid, signal);
  } catch (error) {
    if (error?.code !== 'ESRCH') process.stderr.write('rust_tool_signal_failed\n');
  }
}

function killOwnedProcessGroup(child, groupPid) {
  if (!groupPid) return;
  try {
    if (process.platform === 'win32') child.kill('SIGKILL');
    else process.kill(-groupPid, 'SIGKILL');
  } catch (error) {
    if (error?.code !== 'ESRCH') process.stderr.write('rust_tool_kill_failed\n');
  }
}

async function waitForOwnedProcessGroupExit(groupPid, isChildClosed) {
  const deadline = Date.now() + RUST_TOOL_STOP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (!ownedProcessGroupExists(groupPid, isChildClosed)) return true;
    await delay(50);
  }
  return !ownedProcessGroupExists(groupPid, isChildClosed);
}

async function stopOwnedProcessGroup(child, isChildClosed, signal) {
  const groupPid = !isChildClosed() && child.pid ? child.pid : null;
  if (!groupPid) return;
  signalOwnedProcessGroup(child, groupPid, signal);
  if (await waitForOwnedProcessGroupExit(groupPid, isChildClosed)) return;
  killOwnedProcessGroup(child, groupPid);
  await waitForOwnedProcessGroupExit(groupPid, isChildClosed);
}

async function isExecutable(file) {
  try {
    await access(file, fsConstants.X_OK);
    return (await stat(file)).isFile();
  } catch {
    return false;
  }
}

async function findRustup() {
  const candidates = [];
  if (process.env.RUSTUP_BIN) {
    candidates.push(
      isAbsolute(process.env.RUSTUP_BIN)
        ? process.env.RUSTUP_BIN
        : resolve(process.env.RUSTUP_BIN),
    );
  }
  for (const directory of (process.env.PATH ?? '').split(delimiter)) {
    if (directory) candidates.push(join(directory, 'rustup'));
  }
  candidates.push(
    '/opt/homebrew/bin/rustup',
    '/opt/homebrew/opt/rustup/bin/rustup',
    '/usr/local/bin/rustup',
    '/usr/local/opt/rustup/bin/rustup',
    join(process.env.HOME || homedir(), '.cargo/bin/rustup'),
  );

  for (const candidate of new Set(candidates)) {
    if (await isExecutable(candidate)) return candidate;
  }
  return null;
}

async function run() {
  const [tool, ...args] = process.argv.slice(2);
  if (!tool) {
    process.stderr.write('rust_tool_missing\n');
    process.exitCode = 2;
    return;
  }

  const rustup = await findRustup();
  if (!rustup) {
    process.stderr.write('rustup_not_found\n');
    process.exitCode = 127;
    return;
  }

  let toolchainBin;
  try {
    const { stdout } = await execFileAsync(
      rustup,
      ['which', '--toolchain', 'stable', 'rustc'],
      { env: process.env },
    );
    toolchainBin = dirname(stdout.trim());
  } catch {
    process.stderr.write('rust_toolchain_unavailable\n');
    process.exitCode = 1;
    return;
  }

  const childEnv = {
    ...process.env,
    PATH: [toolchainBin, process.env.PATH].filter(Boolean).join(delimiter),
  };
  const child = spawn(rustup, ['run', 'stable', tool, ...args], {
    detached: process.platform !== 'win32',
    env: childEnv,
    stdio: 'inherit',
  });
  const signals = ['SIGINT', 'SIGTERM', 'SIGHUP'];
  const handlers = new Map();
  let childClosed = false;
  let spawnFailed = false;
  let forwardedSignal;
  let shutdownPromise;
  let resolveShutdown;
  const shutdownDone = new Promise((resolveDone) => {
    resolveShutdown = resolveDone;
  });
  for (const signal of signals) {
    const handler = () => {
      if (forwardedSignal) return;
      forwardedSignal = signal;
      shutdownPromise = stopOwnedProcessGroup(child, () => childClosed, signal)
        .finally(resolveShutdown);
    };
    handlers.set(signal, handler);
    process.on(signal, handler);
  }

  child.once('error', () => {
    spawnFailed = true;
  });
  const childResult = new Promise((resolveResult) => {
    child.once('close', (code, signal) => {
      childClosed = true;
      resolveResult({ code, signal });
    });
  });
  const outcome = await Promise.race([
    childResult.then((result) => ({ result })),
    shutdownDone.then(() => ({ shutdown: true })),
  ]);

  for (const [signal, handler] of handlers) {
    process.off(signal, handler);
  }
  if (forwardedSignal) {
    await shutdownPromise;
    process.kill(process.pid, forwardedSignal);
  } else if (spawnFailed) {
    process.stderr.write('rust_tool_spawn_failed\n');
    process.exitCode = 127;
  } else if (outcome.result.signal) {
    process.kill(process.pid, outcome.result.signal);
  } else {
    process.exitCode = outcome.result.code ?? 1;
  }
}

await run();
