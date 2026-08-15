/// <reference types="vite/client" />

import { useEffect, useRef } from 'react';
import './app.css';
import { StartupCenter } from './startup/StartupCenter';
import { useStartupState } from './startup/useStartupState';
import { startupBridge } from './tauri/bridge';

// Windows 包没有 macOS 那个「打开数据中心」助手 App，就绪后由壳内
// 直接拉起 legacy 控制台窗口（对齐源码模式 start_monitor 的自动开页行为）。
function useAutoOpenConsoleOnWindows(ready: boolean): void {
  const requested = useRef(false);
  useEffect(() => {
    if (!ready || requested.current) return;
    if (!/Windows NT/.test(navigator.userAgent)) return;
    requested.current = true;
    void startupBridge.openConsole().catch(() => undefined);
  }, [ready]);
}

export default function App() {
  const startup = useStartupState(startupBridge);
  useAutoOpenConsoleOnWindows(startup.state.app_installed);

  return (
    <StartupCenter
      state={startup.state}
      onRetry={startup.retry}
      onOpenLogs={startup.openLogs}
      onOpenConsole={startup.openConsole}
    />
  );
}
