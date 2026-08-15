import fixture from '../../contracts/startup-state.json';
import canonicalPhases from '../../contracts/startup-phases.json';
import { describe, expect, it } from 'vitest';
import {
  isStartupState,
  isStartupWireState,
  STARTUP_PHASES,
  toStartupState,
  type StartupWireState,
} from './startupTypes';

// 与 desktop/src-tauri/src/startup/model.rs 的 StartupSnapshot 序列化保持一致
const wireFixture: StartupWireState = {
  phase: 'core_checking',
  core: { status: 'checking', percent: 40, message: '正在检查核心服务' },
  collector: { status: 'idle', percent: 0, message: '' },
  install: { status: 'idle', percent: 0, message: '' },
  apiReady: false,
  canCollect: false,
  recoverableError: null,
  occurredAtMs: 1786773507000,
};

describe('startup state contract', () => {
  it('uses the complete shared canonical phase list', () => {
    expect(STARTUP_PHASES).toEqual(canonicalPhases);
  });

  it('accepts every canonical phase', () => {
    for (const phase of canonicalPhases) {
      expect(isStartupState({ ...fixture, phase })).toBe(true);
    }
  });

  it('accepts the shared fixture', () => {
    expect(isStartupState(fixture)).toBe(true);
  });

  it('rejects unknown phases', () => {
    expect(isStartupState({ ...fixture, phase: 'waiting_forever' })).toBe(false);
  });

  it.each([-0.01, 1.01])('rejects out-of-range progress %s', (progress) => {
    expect(isStartupState({ ...fixture, progress })).toBe(false);
  });

  it('requires error_code even when its value is nullable', () => {
    const withoutErrorCode: Record<string, unknown> = { ...fixture };
    delete withoutErrorCode.error_code;

    expect(isStartupState(withoutErrorCode)).toBe(false);
  });
});

describe('startup wire state (Rust shell snapshot)', () => {
  it('accepts the Rust wire fixture', () => {
    expect(isStartupWireState(wireFixture)).toBe(true);
  });

  it('rejects unknown lane status and non-integer percent', () => {
    expect(isStartupWireState({ ...wireFixture, core: { ...wireFixture.core, status: 'booting' } })).toBe(false);
    expect(isStartupWireState({ ...wireFixture, core: { ...wireFixture.core, percent: 12.5 } })).toBe(false);
    expect(isStartupWireState({ ...wireFixture, recoverableError: { stage: 'kernel' } })).toBe(false);
  });

  it('maps lanes into the flat contract shape', () => {
    const state = toStartupState(wireFixture);
    expect(isStartupState(state)).toBe(true);
    expect(state).toMatchObject({
      phase: 'core_checking',
      message: '正在检查核心服务',
      progress: 40 / 300,
      core_ready: false,
      collector_ready: false,
      app_installed: false,
      error_code: null,
    });
  });

  it('maps degraded with retryable failure into recoverable_error', () => {
    const degraded: StartupWireState = {
      ...wireFixture,
      phase: 'degraded',
      recoverableError: {
        stage: 'collector',
        code: 'collector_install_failed',
        message: '采集引擎安装失败',
        retryable: true,
      },
    };
    expect(toStartupState(degraded)).toMatchObject({
      phase: 'recoverable_error',
      message: '采集引擎安装失败',
      can_retry: true,
      error_code: 'collector_install_failed',
    });
  });

  it('maps an all-ready snapshot to the completed state', () => {
    const ready: StartupWireState = {
      ...wireFixture,
      phase: 'ready',
      core: { status: 'ready', percent: 100, message: '核心运行时已就绪' },
      collector: { status: 'ready', percent: 100, message: '采集引擎已就绪' },
      install: { status: 'ready', percent: 100, message: '应用安装已完成' },
      apiReady: true,
      canCollect: true,
    };
    expect(toStartupState(ready)).toMatchObject({
      phase: 'ready',
      progress: 1,
      core_ready: true,
      collector_ready: true,
      app_installed: true,
      message: '应用安装已完成',
    });
  });
});
