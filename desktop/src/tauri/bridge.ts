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
  retry: () => invoke<void>('retry_startup'),
  openLogs: () => invoke<void>('open_startup_logs'),
  markInteractive: () => invoke<void>('mark_react_interactive'),
};
