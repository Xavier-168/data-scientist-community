import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { StartupState } from './startupTypes';
import { StartupCenter } from './StartupCenter';

const base: StartupState = {
  phase: 'core_preparing',
  message: '正在准备核心服务',
  progress: 0.36,
  can_retry: false,
  core_ready: false,
  collector_ready: false,
  app_installed: false,
  error_code: null,
};

afterEach(cleanup);

describe('StartupCenter', () => {
  it('renders progress and exposes startup logs immediately', async () => {
    const openLogs = vi.fn();
    render(<StartupCenter state={base} onRetry={vi.fn()} onOpenLogs={openLogs} />);

    expect(screen.getByRole('heading', { name: '数据科学家 Community' })).toBeVisible();
    expect(screen.getByText('正在准备核心服务')).toBeVisible();
    expect(screen.getByRole('progressbar', { name: '启动进度' })).toHaveAttribute(
      'aria-valuenow',
      '36',
    );

    const logsButton = screen.getByRole('button', { name: '查看启动日志' });
    expect(logsButton).toBeEnabled();
    await userEvent.click(logsButton);
    expect(openLogs).toHaveBeenCalledOnce();
  });

  it('shows each preparation stage in plain language', () => {
    render(<StartupCenter state={base} onRetry={vi.fn()} onOpenLogs={vi.fn()} />);

    expect(screen.getByText('核心服务').closest('.status-item')).toHaveTextContent('准备中');
    expect(screen.getByText('采集引擎').closest('.status-item')).toHaveTextContent('后台准备');
    expect(screen.getByText('应用安装').closest('.status-item')).toHaveTextContent('后台进行');
  });

  it('does not offer retry while startup can still make progress', () => {
    render(<StartupCenter state={base} onRetry={vi.fn()} onOpenLogs={vi.fn()} />);

    expect(screen.queryByRole('button', { name: '重新准备' })).not.toBeInTheDocument();
  });

  it('enables retry only for recoverable errors', async () => {
    const retry = vi.fn();
    render(
      <StartupCenter
        state={{
          ...base,
          phase: 'recoverable_error',
          message: '启动被中断，可以安全重试',
          can_retry: true,
          error_code: 'core_hash_mismatch',
        }}
        onRetry={retry}
        onOpenLogs={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: '重新准备' }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it('announces the changing startup message without announcing static chrome', () => {
    const { container } = render(
      <StartupCenter state={base} onRetry={vi.fn()} onOpenLogs={vi.fn()} />,
    );

    const liveRegion = container.querySelector('[aria-live="polite"]');
    expect(liveRegion).toHaveTextContent('正在准备核心服务');
    expect(liveRegion).not.toHaveTextContent('查看启动日志');
  });
});
