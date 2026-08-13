import { StrictMode } from 'react';
import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  type AnimationFrameDriver,
  type InteractiveReportGate,
  useReportInteractive,
} from './reportInteractive';

function frameDriver() {
  let nextId = 1;
  const callbacks = new Map<number, FrameRequestCallback>();
  const driver: AnimationFrameDriver = {
    request(callback) {
      const id = nextId++;
      callbacks.set(id, callback);
      return id;
    },
    cancel(id) {
      callbacks.delete(id);
    },
  };

  return {
    driver,
    pending: () => callbacks.size,
    flushOne() {
      const entry = callbacks.entries().next().value as
        | [number, FrameRequestCallback]
        | undefined;
      if (!entry) throw new Error('missing_scheduled_frame');
      callbacks.delete(entry[0]);
      entry[1](performance.now());
    },
  };
}

function gate(): InteractiveReportGate {
  return { reported: false };
}

describe('useReportInteractive', () => {
  it('reports only after two animation frames', () => {
    const frames = frameDriver();
    const report = vi.fn().mockResolvedValue(undefined);

    renderHook(() => useReportInteractive(report, frames.driver, gate()));

    expect(report).not.toHaveBeenCalled();
    act(() => frames.flushOne());
    expect(report).not.toHaveBeenCalled();
    act(() => frames.flushOne());
    expect(report).toHaveBeenCalledOnce();
  });

  it('reports only once across rerenders', () => {
    const frames = frameDriver();
    const report = vi.fn().mockResolvedValue(undefined);
    const sharedGate = gate();
    const { rerender } = renderHook(
      ({ value }: { value: number }) => {
        void value;
        useReportInteractive(report, frames.driver, sharedGate);
      },
      { initialProps: { value: 1 } },
    );

    act(() => {
      frames.flushOne();
      frames.flushOne();
    });
    rerender({ value: 2 });

    expect(report).toHaveBeenCalledOnce();
    expect(frames.pending()).toBe(0);
  });

  it('cancels pending frames during cleanup', () => {
    const frames = frameDriver();
    const report = vi.fn().mockResolvedValue(undefined);
    const { unmount } = renderHook(() =>
      useReportInteractive(report, frames.driver, gate()),
    );

    act(() => frames.flushOne());
    expect(frames.pending()).toBe(1);
    unmount();

    expect(frames.pending()).toBe(0);
    expect(report).not.toHaveBeenCalled();
  });

  it('reports once under StrictMode remount behavior', () => {
    const frames = frameDriver();
    const report = vi.fn().mockResolvedValue(undefined);
    const sharedGate = gate();

    renderHook(
      () => useReportInteractive(report, frames.driver, sharedGate),
      { wrapper: StrictMode },
    );

    act(() => {
      frames.flushOne();
      frames.flushOne();
    });

    expect(report).toHaveBeenCalledOnce();
  });

  it('reports once across an HMR-style unmount and remount', () => {
    const frames = frameDriver();
    const report = vi.fn().mockResolvedValue(undefined);
    const sharedGate = gate();
    const first = renderHook(() =>
      useReportInteractive(report, frames.driver, sharedGate),
    );

    act(() => {
      frames.flushOne();
      frames.flushOne();
    });
    first.unmount();
    renderHook(() => useReportInteractive(report, frames.driver, sharedGate));

    expect(report).toHaveBeenCalledOnce();
    expect(frames.pending()).toBe(0);
  });

  it('contains a rejected report without an unhandled rejection', async () => {
    const frames = frameDriver();
    const report = vi.fn().mockRejectedValue(new Error('sensitive native detail'));
    const unhandled = vi.fn();
    window.addEventListener('unhandledrejection', unhandled);

    renderHook(() => useReportInteractive(report, frames.driver, gate()));
    act(() => {
      frames.flushOne();
      frames.flushOne();
    });
    await act(async () => Promise.resolve());

    expect(report).toHaveBeenCalledOnce();
    expect(unhandled).not.toHaveBeenCalled();
    window.removeEventListener('unhandledrejection', unhandled);
  });
});

describe('desktop IPC boundary', () => {
  it('keeps direct Tauri invokes inside tauri/bridge.ts', () => {
    const sources = import.meta.glob(['/src/**/*.ts', '/src/**/*.tsx'], {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>;
    const tauriCoreModule = ['@tauri-apps', 'api', 'core'].join('/');
    const offenders = Object.entries(sources)
      .filter(([path]) => !path.endsWith('/tauri/bridge.ts'))
      .filter(([, source]) => source.includes(tauriCoreModule))
      .map(([path]) => path);

    expect(offenders).toEqual([]);
    expect(sources['/src/tauri/bridge.ts']).toContain(tauriCoreModule);
  });
});
