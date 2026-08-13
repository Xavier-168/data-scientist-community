import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { chmod, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const TEST_TIMEOUT_MS = 8_000;

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function pidExists(pid) {
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === 'ESRCH') return false;
    throw error;
  }
}

function processGroupExists(groupPid) {
  if (!groupPid) return false;
  try {
    process.kill(-groupPid, 0);
    return true;
  } catch (error) {
    if (error?.code === 'ESRCH') return false;
    throw error;
  }
}

async function readPid(path) {
  const deadline = Date.now() + TEST_TIMEOUT_MS;
  while (Date.now() < deadline) {
    try {
      const pid = Number((await readFile(path, 'utf8')).trim());
      if (Number.isSafeInteger(pid) && pid > 0) return pid;
    } catch {}
    await delay(25);
  }
  throw new Error('pid_file_timeout');
}

async function waitForClose(child) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return { code: child.exitCode, signal: child.signalCode };
  }
  return await Promise.race([
    new Promise((resolve) => child.once('close', (code, signal) => resolve({ code, signal }))),
    delay(TEST_TIMEOUT_MS).then(() => {
      throw new Error('helper_close_timeout');
    }),
  ]);
}

async function waitForOwnedProcessesToExit(groupPid, childPid) {
  const deadline = Date.now() + TEST_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (!processGroupExists(groupPid) && !pidExists(childPid)) return;
    await delay(25);
  }
  throw new Error('owned_process_group_leaked');
}

const tempDirectory = await mkdtemp(join(tmpdir(), 'rust-tool-signal-'));
const fakeRustup = join(tempDirectory, 'rustup');
const groupPidFile = join(tempDirectory, 'group.pid');
const childPidFile = join(tempDirectory, 'child.pid');
const fakeRustupSource = `#!/bin/sh
if [ "$1" = "which" ]; then
  printf '/usr/bin/true\\n'
  exit 0
fi
if [ "$1" = "run" ]; then
  trap 'exit 0' TERM INT HUP
  /bin/sh -c 'trap "" TERM INT HUP; while :; do /bin/sleep 1; done' &
  child_pid=$!
  printf '%s\\n' "$$" > "$RUST_TOOL_TEST_GROUP_PID_FILE"
  printf '%s\\n' "$child_pid" > "$RUST_TOOL_TEST_CHILD_PID_FILE"
  while :; do /bin/sleep 1; done
fi
exit 2
`;

let helper;
let helperClosed = false;
let groupPid;
let childPid;
try {
  await writeFile(fakeRustup, fakeRustupSource, 'utf8');
  await chmod(fakeRustup, 0o755);
  helper = spawn(process.execPath, [join(ROOT, 'scripts', 'run_rust_tool.mjs'), 'noop'], {
    cwd: ROOT,
    env: {
      ...process.env,
      RUSTUP_BIN: fakeRustup,
      RUST_TOOL_TEST_GROUP_PID_FILE: groupPidFile,
      RUST_TOOL_TEST_CHILD_PID_FILE: childPidFile,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  helper.once('close', () => {
    helperClosed = true;
  });
  groupPid = await readPid(groupPidFile);
  childPid = await readPid(childPidFile);
  assert.equal(processGroupExists(groupPid), true);
  assert.equal(pidExists(childPid), true);

  helper.kill('SIGTERM');
  const result = await waitForClose(helper);
  assert.equal(result.code, null);
  assert.equal(result.signal, 'SIGTERM');
  await waitForOwnedProcessesToExit(groupPid, childPid);
  process.stdout.write('rust_tool_signal_cleanup: ok\n');
} finally {
  if (!helperClosed && helper?.pid) {
    try {
      helper.kill('SIGKILL');
    } catch {}
  }
  if (processGroupExists(groupPid)) {
    try {
      process.kill(-groupPid, 'SIGKILL');
    } catch {}
  }
  await rm(tempDirectory, { recursive: true, force: true });
}
