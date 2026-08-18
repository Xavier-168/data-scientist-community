import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { StartupBridge } from '../tauri/bridge';
import type { StartupState } from './startupTypes';

const initialState: StartupState = {
  phase: 'window_ready',
  message: '正在启动数据科学家 Community',
  progress: 0,
  can_retry: false,
  core_ready: false,
  collector_ready: false,
  app_installed: false,
  error_code: null,
};

export function useStartupState(bridge: StartupBridge) {
  const [state, setState] = useState(initialState);
  const actionGeneration = useRef(0);

  useLayoutEffect(
    () => () => {
      actionGeneration.current += 1;
    },
    [bridge],
  );

  useEffect(() => {
    let active = true;
    let receivedEvent = false;
    let unsubscribe: (() => void) | undefined;

    void (async () => {
      unsubscribe = await bridge.subscribe((next) => {
        receivedEvent = true;
        if (active) setState(next);
      });

      if (!active) {
        unsubscribe();
        return;
      }

      const snapshot = await bridge.snapshot();
      if (active && !receivedEvent) setState(snapshot);
    })().catch(() => {
      if (!active) return;
      setState((current) => ({
        ...current,
        phase: 'recoverable_error',
        message: '桌面启动服务暂时不可用',
        can_retry: true,
        error_code: 'desktop_bridge_unavailable',
      }));
    });

    return () => {
      active = false;
      unsubscribe?.();
    };
  }, [bridge]);

  return {
    state,
    retry: (): void => {
      const generation = actionGeneration.current;
      // 重试第一个未就绪的阶段，与 Rust 侧 RetryStage 的取值对齐
      const stage = !state.core_ready
        ? ('core' as const)
        : !state.collector_ready
          ? ('collector' as const)
          : !state.app_installed
            ? ('install' as const)
            : ('sidecar' as const);
      void bridge.retry(stage).catch(() => {
        if (generation !== actionGeneration.current) return;
        setState((current) => ({
          ...current,
          phase: 'recoverable_error',
          message: '启动重试暂时不可用',
          can_retry: true,
          error_code: 'desktop_retry_unavailable',
        }));
      });
    },
    openLogs: (): void => {
      const generation = actionGeneration.current;
      void bridge.openLogs().catch(() => {
        if (generation !== actionGeneration.current) return;
        setState((current) => ({
          ...current,
          message: '暂时无法打开启动日志',
          error_code: 'startup_log_unavailable',
        }));
      });
    },
    openConsole: (): void => {
      const generation = actionGeneration.current;
      void bridge.openConsole().catch(() => {
        if (generation !== actionGeneration.current) return;
        setState((current) => ({
          ...current,
          message: '暂时无法打开控制台',
          error_code: 'legacy_console_unavailable',
        }));
      });
    },
  };
}
