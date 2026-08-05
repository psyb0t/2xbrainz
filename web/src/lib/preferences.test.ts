import { describe, expect, it } from 'vitest';
import {
  DEFAULT_PREFERENCES,
  PREFERENCES_KEY,
  loadPreferences,
  normalizePreferences,
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
});
