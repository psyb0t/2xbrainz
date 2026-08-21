export const PREFERENCES_KEY = '2xbrainz.web.layout.v1';

export interface LayoutPreferences {
  mainSplitPercent: number;
  replyHeightPercent: number;
  replyFastHeightPercent: number;
  coachHeightPercent: number;
  storyHeightPercent: number;
  researchHeightPercent: number;
  sourceBarCollapsed: boolean;
  replyCollapsed: boolean;
  replyFastCollapsed: boolean;
  coachCollapsed: boolean;
  storyCollapsed: boolean;
  researchCollapsed: boolean;
}

export const DEFAULT_PREFERENCES: LayoutPreferences = {
  mainSplitPercent: 62,
  replyHeightPercent: 28,
  replyFastHeightPercent: 22,
  coachHeightPercent: 24,
  storyHeightPercent: 28,
  researchHeightPercent: 20,
  sourceBarCollapsed: false,
  replyCollapsed: false,
  replyFastCollapsed: false,
  coachCollapsed: false,
  storyCollapsed: false,
  researchCollapsed: true
};

const MIN_MAIN_PERCENT = 28;
const MAX_MAIN_PERCENT = 78;
const MIN_GUIDANCE_PERCENT = 15;
const MAX_GUIDANCE_PERCENT = 70;

export function loadPreferences(storage: Storage): LayoutPreferences {
  try {
    const raw = storage.getItem(PREFERENCES_KEY);
    if (raw === null) return { ...DEFAULT_PREFERENCES };
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed)) return { ...DEFAULT_PREFERENCES };
    return normalizePreferences(parsed);
  } catch {
    return { ...DEFAULT_PREFERENCES };
  }
}

export function savePreferences(storage: Storage, value: LayoutPreferences): void {
  try {
    storage.setItem(PREFERENCES_KEY, JSON.stringify(normalizePreferences(value)));
  } catch {
    return;
  }
}

export function normalizePreferences(
  value: Partial<LayoutPreferences> | Record<string, unknown>
): LayoutPreferences {
  return {
    mainSplitPercent: clampNumber(value.mainSplitPercent, 62, MIN_MAIN_PERCENT, MAX_MAIN_PERCENT),
    replyHeightPercent: clampNumber(
      value.replyHeightPercent,
      DEFAULT_PREFERENCES.replyHeightPercent,
      MIN_GUIDANCE_PERCENT,
      MAX_GUIDANCE_PERCENT
    ),
    replyFastHeightPercent: clampNumber(
      value.replyFastHeightPercent,
      DEFAULT_PREFERENCES.replyFastHeightPercent,
      MIN_GUIDANCE_PERCENT,
      MAX_GUIDANCE_PERCENT
    ),
    coachHeightPercent: clampNumber(
      value.coachHeightPercent,
      DEFAULT_PREFERENCES.coachHeightPercent,
      MIN_GUIDANCE_PERCENT,
      MAX_GUIDANCE_PERCENT
    ),
    storyHeightPercent: clampNumber(
      value.storyHeightPercent,
      DEFAULT_PREFERENCES.storyHeightPercent,
      MIN_GUIDANCE_PERCENT,
      MAX_GUIDANCE_PERCENT
    ),
    researchHeightPercent: clampNumber(
      value.researchHeightPercent,
      DEFAULT_PREFERENCES.researchHeightPercent,
      MIN_GUIDANCE_PERCENT,
      MAX_GUIDANCE_PERCENT
    ),
    sourceBarCollapsed: booleanOrDefault(value.sourceBarCollapsed, false),
    replyCollapsed: booleanOrDefault(value.replyCollapsed, false),
    replyFastCollapsed: booleanOrDefault(value.replyFastCollapsed, false),
    coachCollapsed: booleanOrDefault(value.coachCollapsed, false),
    storyCollapsed: booleanOrDefault(value.storyCollapsed, false),
    researchCollapsed: booleanOrDefault(value.researchCollapsed, true)
  };
}

export function resizeAdjacentPanelWeights(
  upperWeight: number,
  lowerWeight: number,
  upperStartPixels: number,
  lowerStartPixels: number,
  deltaPixels: number
): [number, number] {
  const pairPixels = upperStartPixels + lowerStartPixels;
  const pairWeight = upperWeight + lowerWeight;
  const values = [upperWeight, lowerWeight, upperStartPixels, lowerStartPixels, deltaPixels];
  if (!values.every(Number.isFinite) || pairPixels <= 0 || pairWeight <= 0) {
    return [upperWeight, lowerWeight];
  }
  const upperPixels = upperStartPixels + deltaPixels;
  return adjacentWeights(pairWeight, (upperPixels / pairPixels) * pairWeight);
}

export function nudgeAdjacentPanelWeights(
  upperWeight: number,
  lowerWeight: number,
  deltaWeight: number
): [number, number] {
  const pairWeight = upperWeight + lowerWeight;
  if (![upperWeight, lowerWeight, deltaWeight].every(Number.isFinite) || pairWeight <= 0) {
    return [upperWeight, lowerWeight];
  }
  return adjacentWeights(pairWeight, upperWeight + deltaWeight);
}

function adjacentWeights(pairWeight: number, requestedUpperWeight: number): [number, number] {
  const minimum = Math.min(MIN_GUIDANCE_PERCENT, pairWeight / 2);
  const upperWeight = Math.min(pairWeight - minimum, Math.max(minimum, requestedUpperWeight));
  return [upperWeight, pairWeight - upperWeight];
}

function clampNumber(value: unknown, fallback: number, minimum: number, maximum: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return fallback;
  return Math.min(maximum, Math.max(minimum, value));
}

function booleanOrDefault(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}
