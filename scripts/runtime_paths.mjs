import { existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
export const PROJECT_ROOT = path.resolve(SCRIPT_DIR, '..');

export function resolveStateDir({
  env = process.env,
  platform = process.platform,
  homeDir = os.homedir(),
  projectRoot = PROJECT_ROOT,
  communityEdition = existsSync(path.join(projectRoot, 'COMMUNITY_EDITION')),
} = {}) {
  const explicit = String(env.YIRENGONGIS_STATE_DIR ?? '').trim();
  if (explicit) return path.resolve(explicit);
  if (platform === 'win32') {
    // 与 Python 侧 core/paths.py 保持一致：Windows 状态默认放
    // %APPDATA%\数据科学家 Community，不落在源码/安装目录。
    const appData = env.APPDATA || path.join(homeDir, 'AppData', 'Roaming');
    return path.join(appData, communityEdition ? '数据科学家 Community' : '数据科学家');
  }
  if (platform !== 'darwin') return path.resolve(projectRoot);
  const appName = communityEdition ? '数据科学家 Community' : '数据科学家';
  return path.join(homeDir, 'Library', 'Application Support', appName);
}

export function resolveDownloadsDir(options = {}) {
  return path.join(resolveStateDir(options), 'downloads');
}

export function resolveProfileDir(platformId, browserChannel, options = {}) {
  return path.join(
    resolveStateDir(options),
    '.auth',
    'profiles',
    `${platformId}-${browserChannel}`,
  );
}
