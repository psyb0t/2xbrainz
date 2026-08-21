import type { ProviderAssignment, ProviderOutputKind, WebSnapshot } from './contracts';

export const RUNTIME_SETTINGS_KEY = '2xbrainz.web.settings.v3';
export const RUNTIME_SETTINGS_SCHEMA_VERSION = 3;
const LEGACY_RUNTIME_SETTINGS_KEY = '2xbrainz.web.settings.v2';
export const MAX_SESSION_BRIEF_CHARACTERS = 4_000;
const REASONING_EFFORTS = new Set(['none', 'minimal', 'low', 'medium', 'high']);
const CLAUDEBOX_REASONING_EFFORTS = new Set(['low', 'medium', 'high']);

export interface BrowserRuntimeSettings {
  schemaVersion: 3;
  providers: Record<ProviderOutputKind, ProviderAssignment>;
  talkiesModel: string;
  sessionBrief: string;
  webResearchEnabled: boolean;
  autoDispatchEnabled: boolean;
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
    autoDispatchEnabled: snapshot.settings.autoDispatchEnabled,
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
    autoDispatchEnabled: snapshot.settings.defaults.autoDispatchEnabled,
    microphoneNode: null,
    systemNode: null
  };
}

export function loadRuntimeSettings(storage: Storage): BrowserRuntimeSettings | null {
  try {
    const raw =
      storage.getItem(RUNTIME_SETTINGS_KEY) ?? storage.getItem(LEGACY_RUNTIME_SETTINGS_KEY);
    if (raw === null) return null;
    return parseRuntimeSettings(JSON.parse(raw));
  } catch {
    return null;
  }
}

export function saveRuntimeSettings(storage: Storage, settings: BrowserRuntimeSettings): void {
  storage.setItem(RUNTIME_SETTINGS_KEY, JSON.stringify(settings));
  storage.removeItem(LEGACY_RUNTIME_SETTINGS_KEY);
}

export function clearRuntimeSettings(storage: Storage): void {
  storage.removeItem(RUNTIME_SETTINGS_KEY);
  storage.removeItem(LEGACY_RUNTIME_SETTINGS_KEY);
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
        REASONING_EFFORTS
      ),
      fast_draft: availableAssignment(
        saved.providers.fast_draft,
        defaults.providers.fast_draft,
        availableModels,
        REASONING_EFFORTS
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
      ),
      research: availableAssignment(
        saved.providers.research,
        defaults.providers.research,
        availableModels,
        CLAUDEBOX_REASONING_EFFORTS
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
    auto_dispatch_enabled: settings.autoDispatchEnabled,
    microphone_node: settings.microphoneNode,
    system_node: settings.systemNode
  };
}

function parseRuntimeSettings(value: unknown): BrowserRuntimeSettings | null {
  const migrated = migrateLegacyRuntimeSettings(value);
  if (!isRecord(migrated) || migrated.schemaVersion !== RUNTIME_SETTINGS_SCHEMA_VERSION)
    return null;
  const expectedKeys = [
    'microphoneNode',
    'autoDispatchEnabled',
    'providers',
    'schemaVersion',
    'sessionBrief',
    'systemNode',
    'talkiesModel',
    'webResearchEnabled'
  ];
  if (Object.keys(migrated).sort().join(',') !== expectedKeys.sort().join(',')) return null;
  if (!isProviderAssignments(migrated.providers)) return null;
  if (typeof migrated.talkiesModel !== 'string' || migrated.talkiesModel.length === 0) return null;
  if (
    typeof migrated.sessionBrief !== 'string' ||
    migrated.sessionBrief.length > MAX_SESSION_BRIEF_CHARACTERS
  )
    return null;
  if (typeof migrated.webResearchEnabled !== 'boolean') return null;
  if (typeof migrated.autoDispatchEnabled !== 'boolean') return null;
  if (!isNullableString(migrated.microphoneNode) || !isNullableString(migrated.systemNode))
    return null;
  if ((migrated.microphoneNode === null) !== (migrated.systemNode === null)) return null;
  return migrated as unknown as BrowserRuntimeSettings;
}

function migrateLegacyRuntimeSettings(value: unknown): unknown {
  return migrateRuntimeSettingsV2ToV3(migrateRuntimeSettingsV1ToV2(value));
}

function migrateRuntimeSettingsV1ToV2(value: unknown): unknown {
  if (!isRecord(value) || value.schemaVersion !== 1 || !isRecord(value.providers)) return value;
  if (!isAssignment(value.providers.draft)) return value;
  return {
    ...value,
    schemaVersion: 2,
    autoDispatchEnabled: true,
    providers: {
      ...value.providers,
      research: { model: 'claudebox-sonnet', reasoningEffort: 'high' }
    }
  };
}

function migrateRuntimeSettingsV2ToV3(value: unknown): unknown {
  if (!isRecord(value) || value.schemaVersion !== 2 || !isRecord(value.providers)) return value;
  if (!isAssignment(value.providers.draft)) return value;
  return {
    ...value,
    schemaVersion: RUNTIME_SETTINGS_SCHEMA_VERSION,
    providers: {
      ...value.providers,
      fast_draft: { model: 'groq-gpt-oss-120b', reasoningEffort: 'none' }
    }
  };
}

function isProviderAssignments(value: unknown): boolean {
  if (!isRecord(value)) return false;
  if (Object.keys(value).sort().join(',') !== 'commentary,draft,fast_draft,research,summary')
    return false;
  return ['draft', 'fast_draft', 'commentary', 'summary', 'research'].every((flow) =>
    isAssignment(value[flow])
  );
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
