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
