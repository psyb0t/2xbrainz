import { describe, expect, it } from 'vitest';
import {
  DEFAULT_PREFERENCES,
  PREFERENCES_KEY,
  loadPreferences,
  nudgeAdjacentPanelWeights,
  normalizePreferences,
  resizeAdjacentPanelWeights,
  savePreferences
} from './preferences';

describe('layout preferences', () => {
  it('falls back when local storage is malformed', () => {
    localStorage.setItem(PREFERENCES_KEY, '{bad');
    expect(loadPreferences(localStorage)).toEqual(DEFAULT_PREFERENCES);
  });

  it('clamps split sizes and rejects wrong field types', () => {
    expect(
      normalizePreferences({
        mainSplitPercent: 99,
        replyHeightPercent: -5,
        coachHeightPercent: Number.NaN,
        storyCollapsed: true,
        replyCollapsed: 'yes'
      })
    ).toEqual({
      ...DEFAULT_PREFERENCES,
      mainSplitPercent: 78,
      replyHeightPercent: 15,
      storyCollapsed: true
    });
  });

  it('round trips valid preferences', () => {
    const preferences = { ...DEFAULT_PREFERENCES, mainSplitPercent: 48, coachCollapsed: true };
    savePreferences(localStorage, preferences);
    expect(loadPreferences(localStorage)).toEqual(preferences);
  });

  it('resizes only the adjacent pair while preserving its total weight', () => {
    const resized = resizeAdjacentPanelWeights(34, 33, 200, 200, 50);
    expect(resized[0]).toBeCloseTo(41.875);
    expect(resized[1]).toBeCloseTo(25.125);
    expect(resized[0] + resized[1]).toBe(67);
    expect(resizeAdjacentPanelWeights(34, 33, 200, 200, 10_000)).toEqual([52, 15]);
  });

  it('nudges adjacent weights without changing the third panel', () => {
    expect(nudgeAdjacentPanelWeights(34, 33, 2)).toEqual([36, 31]);
    expect(nudgeAdjacentPanelWeights(34, 33, Number.NaN)).toEqual([34, 33]);
  });
});
