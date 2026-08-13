#!/usr/bin/env node

import fs from 'node:fs/promises';
import { chromium } from 'playwright';

const userDataDir = String(process.env.USER_DATA_DIR || '').trim();
const browserExecutablePath = String(process.env.BROWSER_EXECUTABLE_PATH || '').trim();

if (!userDataDir) {
  throw new Error('USER_DATA_DIR is required');
}

await fs.mkdir(userDataDir, { recursive: true });

const context = await chromium.launchPersistentContext(userDataDir, {
  ...(browserExecutablePath ? { executablePath: browserExecutablePath } : {}),
  headless: true,
  viewport: { width: 1280, height: 900 },
  args: ['--disable-blink-features=AutomationControlled', '--disable-session-crashed-bubble'],
});

await context.close();
console.log(`[seed-profile] ready ${userDataDir}`);
