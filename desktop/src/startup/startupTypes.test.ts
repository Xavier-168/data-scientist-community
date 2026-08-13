import fixture from '../../contracts/startup-state.json';
import canonicalPhases from '../../contracts/startup-phases.json';
import { describe, expect, it } from 'vitest';
import { isStartupState, STARTUP_PHASES } from './startupTypes';

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
