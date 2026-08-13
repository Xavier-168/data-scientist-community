/// <reference types="vite/client" />

import './app.css';
import { StartupCenter } from './startup/StartupCenter';
import { useStartupState } from './startup/useStartupState';
import { startupBridge } from './tauri/bridge';

export default function App() {
  const startup = useStartupState(startupBridge);

  return (
    <StartupCenter
      state={startup.state}
      onRetry={startup.retry}
      onOpenLogs={startup.openLogs}
    />
  );
}
