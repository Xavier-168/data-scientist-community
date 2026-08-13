import { useLayoutEffect } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { StartupBridge } from '../tauri/bridge';
import type { StartupState } from './startupTypes';
import { useStartupState } from './useStartupState';

const first: StartupState = {
  phase: 'window_ready',
  message: '窗口已就绪',
  progress: 0,
  can_retry: false,
  core_ready: false,
  collector_ready: false,
  app_installed: false,
  error_code: null,
};

const newer: StartupState = {
  ...first,
  phase: 'api_ready',
  message: '服务已就绪',
  progress: 0.7,
  core_ready: true,
};

function fakeBridge(overrides: Partial<StartupBridge> = {}): StartupBridge {
  return {
    snapshot: vi.fn().mockResolvedValue(first),
    subscribe: vi.fn().mockResolvedValue(() => undefined),
    retry: vi.fn().mockResolvedValue(undefined),
    openLogs: vi.fn().mockResolvedValue(undefined),
    markInteractive: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function useActionInLayout(bridge: StartupBridge, action: 'retry' | 'openLogs') {
  const startup = useStartupState(bridge);
  useLayoutEffect(() => {
    startup[action]();
  }, [action, bridge]);
  return startup;
}

describe('useStartupState', () => {
  it('hydrates a snapshot and accepts pushed transitions', async () => {
    let push: (state: StartupState) => void = () => undefined;
    const bridge = fakeBridge({
      subscribe: vi.fn(async (listener: (state: StartupState) => void) => {
        push = listener;
        return () => undefined;
      }),
    });

    const { result } = renderHook(() => useStartupState(bridge));

    await waitFor(() => expect(result.current.state.message).toBe('窗口已就绪'));
    act(() => push(newer));
    expect(result.current.state).toEqual(newer);
  });

  it('does not let a late old snapshot overwrite a newer event', async () => {
    let resolveSnapshot!: (state: StartupState) => void;
    let push: (state: StartupState) => void = () => undefined;
    const bridge = fakeBridge({
      snapshot: vi.fn(
        () =>
          new Promise<StartupState>((resolve) => {
            resolveSnapshot = resolve;
          }),
      ),
      subscribe: vi.fn(async (listener: (state: StartupState) => void) => {
        push = listener;
        return () => undefined;
      }),
    });

    const { result } = renderHook(() => useStartupState(bridge));
    await waitFor(() => expect(bridge.snapshot).toHaveBeenCalledOnce());

    act(() => push({ ...newer, message: '新事件' }));
    act(() => resolveSnapshot(first));

    await waitFor(() => expect(result.current.state.message).toBe('新事件'));
  });

  it('preserves an event pushed synchronously before subscribe resolves', async () => {
    const bridge = fakeBridge({
      subscribe: vi.fn((listener: (state: StartupState) => void) => {
        listener({ ...newer, message: '订阅期间的新事件' });
        return Promise.resolve(() => undefined);
      }),
    });

    const { result } = renderHook(() => useStartupState(bridge));

    await waitFor(() => expect(bridge.snapshot).toHaveBeenCalledOnce());
    expect(result.current.state.message).toBe('订阅期间的新事件');
  });

  it('unsubscribes without reading a snapshot when unmounted before subscribe resolves', async () => {
    let resolveSubscribe!: (unsubscribe: () => void) => void;
    const unsubscribe = vi.fn();
    const bridge = fakeBridge({
      subscribe: vi.fn(
        () =>
          new Promise<() => void>((resolve) => {
            resolveSubscribe = resolve;
          }),
      ),
    });

    const { unmount } = renderHook(() => useStartupState(bridge));
    await waitFor(() => expect(bridge.subscribe).toHaveBeenCalledOnce());
    unmount();

    await act(async () => resolveSubscribe(unsubscribe));

    expect(unsubscribe).toHaveBeenCalledOnce();
    expect(bridge.snapshot).not.toHaveBeenCalled();
  });

  it.each(['subscribe', 'snapshot'] as const)(
    'enters the fixed recoverable error when %s fails',
    async (failurePoint) => {
      const failure = vi.fn().mockRejectedValue(new Error('sensitive host detail'));
      const bridge = fakeBridge({ [failurePoint]: failure });

      const { result } = renderHook(() => useStartupState(bridge));

      await waitFor(() => expect(result.current.state.phase).toBe('recoverable_error'));
      expect(result.current.state).toMatchObject({
        message: '桌面启动服务暂时不可用',
        can_retry: true,
        error_code: 'desktop_bridge_unavailable',
      });
      expect(result.current.state.message).not.toContain('sensitive host detail');
    },
  );

  it('contains a rejected retry action and exposes only a fixed UI error', async () => {
    const bridge = fakeBridge({
      retry: vi.fn().mockRejectedValue(new Error('sensitive retry host detail')),
    });
    const { result } = renderHook(() => useStartupState(bridge));
    await waitFor(() => expect(bridge.snapshot).toHaveBeenCalledOnce());

    let returned: unknown;
    act(() => {
      returned = result.current.retry();
    });

    expect(returned).toBeUndefined();
    await waitFor(() =>
      expect(result.current.state).toMatchObject({
        phase: 'recoverable_error',
        message: '启动重试暂时不可用',
        can_retry: true,
        error_code: 'desktop_retry_unavailable',
      }),
    );
    expect(result.current.state.message).not.toContain('sensitive retry host detail');
  });

  it('contains a rejected log action and exposes only a fixed UI error', async () => {
    const bridge = fakeBridge({
      openLogs: vi.fn().mockRejectedValue(new Error('sensitive log host detail')),
    });
    const { result } = renderHook(() => useStartupState(bridge));
    await waitFor(() => expect(bridge.snapshot).toHaveBeenCalledOnce());

    let returned: unknown;
    act(() => {
      returned = result.current.openLogs();
    });

    expect(returned).toBeUndefined();
    await waitFor(() =>
      expect(result.current.state).toMatchObject({
        phase: 'window_ready',
        message: '暂时无法打开启动日志',
        can_retry: false,
        error_code: 'startup_log_unavailable',
      }),
    );
    expect(result.current.state.message).not.toContain('sensitive log host detail');
  });

  it.each(['retry', 'openLogs'] as const)(
    'ignores a deferred %s rejection after unmount',
    async (action) => {
      const failure = deferred<void>();
      const bridge = fakeBridge({ [action]: vi.fn(() => failure.promise) });
      const { result, unmount } = renderHook(() => useStartupState(bridge));
      await waitFor(() => expect(bridge.snapshot).toHaveBeenCalledOnce());

      act(() => result.current[action]());
      unmount();
      await act(async () => failure.reject(new Error('stale unmounted failure')));

      expect(bridge[action]).toHaveBeenCalledOnce();
    },
  );

  it.each(['retry', 'openLogs'] as const)(
    'does not let an old bridge %s rejection overwrite the replacement bridge state',
    async (action) => {
      const failure = deferred<void>();
      const oldBridge = fakeBridge({ [action]: vi.fn(() => failure.promise) });
      const replacementState = { ...newer, message: '新 bridge 状态' };
      const replacementBridge = fakeBridge({
        snapshot: vi.fn().mockResolvedValue(replacementState),
      });
      const { result, rerender } = renderHook(
        ({ bridge }: { bridge: StartupBridge }) => useStartupState(bridge),
        { initialProps: { bridge: oldBridge } },
      );
      await waitFor(() => expect(oldBridge.snapshot).toHaveBeenCalledOnce());

      act(() => result.current[action]());
      rerender({ bridge: replacementBridge });
      await waitFor(() => expect(result.current.state).toEqual(replacementState));
      await act(async () => failure.reject(new Error('stale bridge failure')));

      expect(result.current.state).toEqual(replacementState);
    },
  );

  it.each(['retry', 'openLogs'] as const)(
    'keeps a deferred %s rejection started by the caller layout effect current',
    async (action) => {
      const failure = deferred<void>();
      const bridge = fakeBridge({ [action]: vi.fn(() => failure.promise) });
      const { result } = renderHook(() => useActionInLayout(bridge, action));
      await waitFor(() => expect(bridge.snapshot).toHaveBeenCalledOnce());

      await act(async () => failure.reject(new Error('layout action failure')));

      await waitFor(() =>
        expect(result.current.state.error_code).toBe(
          action === 'retry' ? 'desktop_retry_unavailable' : 'startup_log_unavailable',
        ),
      );
    },
  );

  it.each(['retry', 'openLogs'] as const)(
    'keeps a new bridge layout %s rejection current after rerender',
    async (action) => {
      const failure = deferred<void>();
      const oldBridge = fakeBridge();
      const replacementState = { ...newer, message: '新 bridge 状态' };
      const replacementBridge = fakeBridge({
        snapshot: vi.fn().mockResolvedValue(replacementState),
        [action]: vi.fn(() => failure.promise),
      });
      const { result, rerender } = renderHook(
        ({ bridge }: { bridge: StartupBridge }) => useActionInLayout(bridge, action),
        { initialProps: { bridge: oldBridge } },
      );
      await waitFor(() => expect(oldBridge.snapshot).toHaveBeenCalledOnce());

      rerender({ bridge: replacementBridge });
      await waitFor(() => expect(result.current.state).toEqual(replacementState));
      await act(async () => failure.reject(new Error('new bridge layout failure')));

      await waitFor(() =>
        expect(result.current.state.error_code).toBe(
          action === 'retry' ? 'desktop_retry_unavailable' : 'startup_log_unavailable',
        ),
      );
    },
  );
});
