export const STARTUP_PHASES = [
  'window_ready',
  'core_checking',
  'core_preparing',
  'api_starting',
  'api_ready',
  'collector_preparing',
  'ready',
  'recoverable_error',
] as const;

export type StartupPhase = (typeof STARTUP_PHASES)[number];

export interface StartupState {
  phase: StartupPhase;
  message: string;
  progress: number;
  can_retry: boolean;
  core_ready: boolean;
  collector_ready: boolean;
  app_installed: boolean;
  error_code: string | null;
}

export function isStartupState(value: unknown): value is StartupState {
  if (!value || typeof value !== 'object') return false;
  const item = value as Record<string, unknown>;
  return (
    STARTUP_PHASES.includes(item.phase as StartupPhase) &&
    typeof item.message === 'string' &&
    typeof item.progress === 'number' &&
    item.progress >= 0 &&
    item.progress <= 1 &&
    typeof item.can_retry === 'boolean' &&
    typeof item.core_ready === 'boolean' &&
    typeof item.collector_ready === 'boolean' &&
    typeof item.app_installed === 'boolean' &&
    (item.error_code === null || typeof item.error_code === 'string')
  );
}

// —— Rust 壳的线上格式（desktop/src-tauri/src/startup/model.rs 的 StartupSnapshot）——
// 与 UI 层的扁平契约不同：lane 分组 + camelCase 字段；映射在 bridge 层完成，
// 组件与共享契约（desktop/contracts/startup-state.json）保持不变。

export const STARTUP_WIRE_PHASES = [
  'window_ready',
  'core_checking',
  'core_preparing',
  'api_starting',
  'api_ready',
  'collector_preparing',
  'ready',
  'degraded',
] as const;

export type StartupWirePhase = (typeof STARTUP_WIRE_PHASES)[number];
export type StartupLaneStatus = 'idle' | 'checking' | 'preparing' | 'ready' | 'failed';
export type StartupRetryStage = 'core' | 'collector' | 'install' | 'sidecar';

export interface StartupLaneWire {
  status: StartupLaneStatus;
  percent: number;
  message: string;
}

export interface StartupRecoverableErrorWire {
  stage: StartupRetryStage;
  code: string;
  message: string;
  retryable: boolean;
}

export interface StartupWireState {
  phase: StartupWirePhase;
  core: StartupLaneWire;
  collector: StartupLaneWire;
  install: StartupLaneWire;
  apiReady: boolean;
  canCollect: boolean;
  recoverableError: StartupRecoverableErrorWire | null;
  occurredAtMs: number;
}

const LANE_STATUSES: readonly StartupLaneStatus[] = ['idle', 'checking', 'preparing', 'ready', 'failed'];
const RETRY_STAGES: readonly StartupRetryStage[] = ['core', 'collector', 'install', 'sidecar'];

function isStartupLaneWire(value: unknown): value is StartupLaneWire {
  if (!value || typeof value !== 'object') return false;
  const lane = value as Record<string, unknown>;
  return (
    LANE_STATUSES.includes(lane.status as StartupLaneStatus) &&
    typeof lane.percent === 'number' &&
    Number.isInteger(lane.percent) &&
    lane.percent >= 0 &&
    lane.percent <= 100 &&
    typeof lane.message === 'string'
  );
}

export function isStartupWireState(value: unknown): value is StartupWireState {
  if (!value || typeof value !== 'object') return false;
  const item = value as Record<string, unknown>;
  if (!STARTUP_WIRE_PHASES.includes(item.phase as StartupWirePhase)) return false;
  if (!isStartupLaneWire(item.core) || !isStartupLaneWire(item.collector) || !isStartupLaneWire(item.install)) {
    return false;
  }
  if (typeof item.apiReady !== 'boolean' || typeof item.canCollect !== 'boolean') return false;
  if (typeof item.occurredAtMs !== 'number' || item.occurredAtMs < 0) return false;
  const error = item.recoverableError;
  if (error === null) return true;
  if (!error || typeof error !== 'object') return false;
  const failure = error as Record<string, unknown>;
  return (
    RETRY_STAGES.includes(failure.stage as StartupRetryStage) &&
    typeof failure.code === 'string' &&
    typeof failure.message === 'string' &&
    typeof failure.retryable === 'boolean'
  );
}

function laneMessageForPhase(state: StartupWireState): string {
  const lane =
    state.phase === 'collector_preparing'
      ? state.collector
      : state.phase === 'ready'
        ? state.install
        : state.core;
  const fallback = [state.core, state.collector, state.install].find((item) => item.message.length > 0);
  return lane.message.length > 0 ? lane.message : (fallback?.message ?? '');
}

/** 把 Rust 壳的 lane 快照映射成 UI 层的扁平 StartupState。 */
export function toStartupState(state: StartupWireState): StartupState {
  const phase: StartupPhase = state.phase === 'degraded' ? 'recoverable_error' : state.phase;
  const recoverable = phase === 'recoverable_error' ? state.recoverableError : null;
  const laneProgress =
    (clampPercent(state.core.percent) + clampPercent(state.collector.percent) + clampPercent(state.install.percent)) /
    300;
  const laneDerivedMessage = laneMessageForPhase(state);
  const message =
    phase === 'ready'
      ? laneDerivedMessage || '启动完成，控制台已就绪'
      : phase === 'recoverable_error'
        ? recoverable?.message || '启动遇到可恢复错误'
        : laneDerivedMessage || '正在启动数据科学家 Community';
  return {
    phase,
    message,
    progress: laneProgress,
    can_retry: recoverable?.retryable === true,
    core_ready: state.core.status === 'ready',
    collector_ready: state.collector.status === 'ready',
    app_installed: state.install.status === 'ready',
    error_code: recoverable ? (recoverable.code || 'startup_degraded') : null,
  };
}

function clampPercent(percent: number): number {
  return Math.min(100, Math.max(0, percent));
}
