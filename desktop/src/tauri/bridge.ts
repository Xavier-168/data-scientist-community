import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { isStartupState, type StartupState } from '../startup/startupTypes';

export interface StartupBridge {
  snapshot(): Promise<StartupState>;
  subscribe(listener: (state: StartupState) => void): Promise<() => void>;
  retry(): Promise<void>;
  openLogs(): Promise<void>;
  markInteractive(): Promise<void>;
}

export const startupBridge: StartupBridge = {
  async snapshot() {
    const value = await invoke<unknown>('get_startup_snapshot');
    if (!isStartupState(value)) throw new Error('invalid_startup_snapshot');
    return value;
  },
  async subscribe(listener) {
    return listen<unknown>('startup://state', (event) => {
      if (isStartupState(event.payload)) listener(event.payload);
    });
  },
  // 命令名与 Rust 侧 tauri::command 注册名保持一致
  // （retry_startup_stage / open_startup_log，此前为错误的复数/缺后缀形式）
  retry: () => invoke<void>('retry_startup_stage'),
  openLogs: () => invoke<void>('open_startup_log'),
  markInteractive: () => invoke<void>('mark_react_interactive'),
};
