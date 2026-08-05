import { describe, expect, it } from 'vitest';
import { EMPTY_SNAPSHOT, isWebSnapshot } from './contracts';

describe('web snapshot boundary', () => {
  it('accepts the complete structured snapshot contract', () => {
    expect(isWebSnapshot(EMPTY_SNAPSHOT)).toBe(true);
  });

  it.each([
    { ...EMPTY_SNAPSHOT, activeAudio: { microphone: null, system: null } },
    {
      ...EMPTY_SNAPSHOT,
      activeAudio: {
        ...EMPTY_SNAPSHOT.activeAudio,
        microphone: { label: 'Mic', nodeName: 'mic', level: 101 }
      }
    },
    {
      ...EMPTY_SNAPSHOT,
      audioSetup: {
        microphones: [{ index: -1 }],
        systemMonitors: []
      }
    }
  ])('rejects malformed nested state %#', (value) => {
    expect(isWebSnapshot(value)).toBe(false);
  });
});
