import type { ProviderAssignment, ProviderOutputKind, WebSnapshot } from './contracts';

export const RUNTIME_SETTINGS_KEY = '2xbrainz.web.settings.v1';
export const RUNTIME_SETTINGS_SCHEMA_VERSION = 1;
export const MAX_SESSION_BRIEF_CHARACTERS = 4_000;
const REASONING_EFFORTS = new Set(['none', 'minimal', 'low', 'medium', 'high']);
const CLAUDEBOX_REASONING_EFFORTS = new Set(['low', 'medium', 'high']);

export interface BrowserRuntimeSettings {
  schemaVersion: 1;
  providers: Record<ProviderOutputKind, ProviderAssignment>;
  talkiesModel: string;
  sessionBrief: string;
  webResearchEnabled: boolean;
  microphoneNode: string | null;
  systemNode: string | null;
}

export function settingsFromSnapshot(snapshot: WebSnapshot): BrowserRuntimeSettings {
  return {
    schemaVersion: RUNTIME_SETTINGS_SCHEMA_VERSION,
    providers: structuredClone(snapshot.provider.assignments),
    talkiesModel: snapshot.settings.talkiesModel,
    sessionBrief: snapshot.settings.sessionBrief,
    webResearchEnabled: snapshot.settings.webResearchEnabled,
    microphoneNode: snapshot.activeAudio.microphone.nodeName || null,
    systemNode: snapshot.activeAudio.system.nodeName || null
  };
}

export function defaultRuntimeSettings(snapshot: WebSnapshot): BrowserRuntimeSettings {
  return {
    schemaVersion: RUNTIME_SETTINGS_SCHEMA_VERSION,
    providers: structuredClone(snapshot.settings.defaults.assignments),
    talkiesModel: snapshot.settings.defaults.talkiesModel,
    sessionBrief: snapshot.settings.defaults.sessionBrief,
    webResearchEnabled: snapshot.settings.defaults.webResearchEnabled,
    microphoneNode: null,
    systemNode: null
  };
}

export function loadRuntimeSettings(storage: Storage): BrowserRuntimeSettings | null {
  try {
    const raw = storage.getItem(RUNTIME_SETTINGS_KEY);
    if (raw === null) return null;
    return parseRuntimeSettings(JSON.parse(raw));
  } catch {
    return null;
  }
}

export function saveRuntimeSettings(storage: Storage, settings: BrowserRuntimeSettings): void {
  storage.setItem(RUNTIME_SETTINGS_KEY, JSON.stringify(settings));
}

export function clearRuntimeSettings(storage: Storage): void {
  storage.removeItem(RUNTIME_SETTINGS_KEY);
}

export function applicableRuntimeSettings(
  saved: BrowserRuntimeSettings,
  snapshot: WebSnapshot
): BrowserRuntimeSettings {
  const availableModels = new Set(snapshot.provider.models);
  const availableTalkiesModels = new Set(snapshot.settings.talkiesModels);
  const defaults = settingsFromSnapshot(snapshot);
  const microphoneAvailable = snapshot.audioSetup.microphones.some(
    (device) => device.nodeName === saved.microphoneNode
  );
  const systemAvailable = snapshot.audioSetup.systemMonitors.some(
    (device) => device.nodeName === saved.systemNode
  );
  return {
    ...saved,
    providers: {
      draft: availableAssignment(
        saved.providers.draft,
        defaults.providers.draft,
        availableModels,
        CLAUDEBOX_REASONING_EFFORTS
      ),
      commentary: availableAssignment(
        saved.providers.commentary,
        defaults.providers.commentary,
        availableModels,
        REASONING_EFFORTS
      ),
      summary: availableAssignment(
        saved.providers.summary,
        defaults.providers.summary,
        availableModels,
        REASONING_EFFORTS
      )
    },
    talkiesModel: availableTalkiesModels.has(saved.talkiesModel)
      ? saved.talkiesModel
      : defaults.talkiesModel,
    microphoneNode: microphoneAvailable && systemAvailable ? saved.microphoneNode : null,
    systemNode: microphoneAvailable && systemAvailable ? saved.systemNode : null
  };
}

export function runtimeSettingsMessage(settings: BrowserRuntimeSettings): Record<string, unknown> {
  return {
    type: 'runtime_settings',
    schema_version: settings.schemaVersion,
    providers: Object.fromEntries(
      Object.entries(settings.providers).map(([flow, assignment]) => [
        flow,
        { model: assignment.model, reasoning_effort: assignment.reasoningEffort }
      ])
    ),
    talkies_model: settings.talkiesModel,
    session_brief: settings.sessionBrief,
    web_research_enabled: settings.webResearchEnabled,
    microphone_node: settings.microphoneNode,
    system_node: settings.systemNode
  };
}

function parseRuntimeSettings(value: unknown): BrowserRuntimeSettings | null {
  if (!isRecord(value) || value.schemaVersion !== RUNTIME_SETTINGS_SCHEMA_VERSION) return null;
  const expectedKeys = [
    'microphoneNode',
    'providers',
    'schemaVersion',
    'sessionBrief',
    'systemNode',
    'talkiesModel',
    'webResearchEnabled'
  ];
  if (Object.keys(value).sort().join(',') !== expectedKeys.sort().join(',')) return null;
  if (!isProviderAssignments(value.providers)) return null;
  if (typeof value.talkiesModel !== 'string' || value.talkiesModel.length === 0) return null;
  if (
    typeof value.sessionBrief !== 'string' ||
    value.sessionBrief.length > MAX_SESSION_BRIEF_CHARACTERS
  )
    return null;
  if (typeof value.webResearchEnabled !== 'boolean') return null;
  if (!isNullableString(value.microphoneNode) || !isNullableString(value.systemNode)) return null;
  if ((value.microphoneNode === null) !== (value.systemNode === null)) return null;
  return value as unknown as BrowserRuntimeSettings;
}

function isProviderAssignments(value: unknown): boolean {
  if (!isRecord(value)) return false;
  if (Object.keys(value).sort().join(',') !== 'commentary,draft,summary') return false;
  return ['draft', 'commentary', 'summary'].every((flow) => isAssignment(value[flow]));
}

function isAssignment(value: unknown): value is ProviderAssignment {
  return (
    isRecord(value) &&
    Object.keys(value).sort().join(',') === 'model,reasoningEffort' &&
    typeof value.model === 'string' &&
    value.model.length > 0 &&
    typeof value.reasoningEffort === 'string' &&
    REASONING_EFFORTS.has(value.reasoningEffort)
  );
}

function availableAssignment(
  saved: ProviderAssignment,
  fallback: ProviderAssignment,
  available: Set<string>,
  allowedReasoningEfforts: Set<string>
): ProviderAssignment {
  if (!available.has(saved.model)) return fallback;
  if (allowedReasoningEfforts.has(saved.reasoningEffort)) return saved;
  return { ...saved, reasoningEffort: fallback.reasoningEffort };
}

function isNullableString(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && value.length > 0 && value.length <= 512);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}
