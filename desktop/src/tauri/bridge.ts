import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import {
  isStartupWireState,
  toStartupState,
  type StartupRetryStage,
  type StartupState,
} from '../startup/startupTypes';

export interface StartupBridge {
  snapshot(): Promise<StartupState>;
  subscribe(listener: (state: StartupState) => void): Promise<() => void>;
  retry(stage?: StartupRetryStage): Promise<void>;
  openLogs(): Promise<void>;
  openConsole(): Promise<void>;
  markInteractive(): Promise<void>;
}

export const startupBridge: StartupBridge = {
  // Rust 壳返回 lane 结构（StartupSnapshot），在这里校验并映射为 UI 层的扁平契约
  async snapshot() {
    const value = await invoke<unknown>('get_startup_snapshot');
    if (!isStartupWireState(value)) throw new Error('invalid_startup_snapshot');
    return toStartupState(value);
  },
  async subscribe(listener) {
    return listen<unknown>('startup://state', (event) => {
      if (isStartupWireState(event.payload)) listener(toStartupState(event.payload));
    });
  },
  // 命令名与 Rust 侧 tauri::command 注册名保持一致
  // （retry_startup_stage / open_startup_log，此前为错误的复数/缺后缀形式）
  retry: (stage: StartupRetryStage = 'core') => invoke<void>('retry_startup_stage', { stage }),
  openLogs: () => invoke<void>('open_startup_log'),
  openConsole: () => invoke<void>('open_legacy_console'),
  markInteractive: () => invoke<void>('mark_react_interactive'),
};
