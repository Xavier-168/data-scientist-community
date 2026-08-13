import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { useReportInteractive } from './startup/reportInteractive';

function DesktopShell() {
  useReportInteractive();
  return <App />;
}

const rootElement = document.getElementById('root');

if (!rootElement) {
  throw new Error('missing_root_element');
}

createRoot(rootElement).render(
  <StrictMode>
    <DesktopShell />
  </StrictMode>,
);
