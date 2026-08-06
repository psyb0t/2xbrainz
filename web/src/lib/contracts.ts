export type ConnectionState = 'connecting' | 'connected' | 'disconnected';
export type ProviderOutputKind = 'draft' | 'commentary' | 'summary';

export interface ProviderAssignment {
  model: string;
  reasoningEffort: string;
}

export interface AudioMeter {
  index: number;
  nodeId: string;
  nodeName: string;
  label: string;
  isDefault: boolean;
  isSelected: boolean;
  level: number;
  isAvailable: boolean;
}

export interface ActiveAudioSource {
  label: string;
  nodeName: string;
  level: number;
  state: string;
}

export interface WebSnapshot {
  type: 'snapshot';
  status: string;
  notice: string;
  conversation: string;
  reply: string;
  coach: string;
  story: string;
  requiresAudioSetup: boolean;
  sessionState: string;
  provider: {
    models: string[];
    assignments: Record<ProviderOutputKind, ProviderAssignment>;
    activity: ProviderActivity[];
  };
  activeAudio: {
    microphone: ActiveAudioSource;
    system: ActiveAudioSource;
  };
  audioSetup: {
    microphones: AudioMeter[];
    systemMonitors: AudioMeter[];
  };
}

export interface ProviderActivity {
  phase: string;
  flow_id?: string;
  output_kind?: ProviderOutputKind;
  model?: string;
  reasoning_effort?: string;
  tool?: string;
  tool_call_id?: string;
  tool_input?: unknown;
  tool_result?: string;
  reasoning?: string;
  reasoning_exposed?: boolean;
  output?: string;
  tools_enabled?: boolean;
  error_type?: string;
  error_message?: string;
}

export type FrontendDebugMessage = {
  type: 'client_debug';
  event: 'websocket_opened' | 'snapshot_received' | 'snapshot_rejected' | 'provider_feed_rendered';
  output_kind?: ProviderOutputKind;
  activity_count?: number;
  item_count?: number;
  text_characters?: number;
  reason?: 'invalid_json' | 'invalid_snapshot';
};

export const EMPTY_SNAPSHOT: WebSnapshot = {
  type: 'snapshot',
  status: '2xbrainz  STARTING',
  notice: 'Connecting to the local session…',
  conversation: 'Waiting for the first finalized turn…',
  reply: 'WAITING\n—',
  coach: 'PRIVATE COACH\n—',
  story: 'STORY SO FAR\n—',
  requiresAudioSetup: false,
  sessionState: 'starting',
  provider: {
    models: [],
    assignments: {
      draft: { model: '', reasoningEffort: 'none' },
      commentary: { model: '', reasoningEffort: 'none' },
      summary: { model: '', reasoningEffort: 'none' }
    },
    activity: []
  },
  activeAudio: {
    microphone: { label: 'Not selected', nodeName: '', level: 0, state: 'idle' },
    system: { label: 'Not selected', nodeName: '', level: 0, state: 'idle' }
  },
  audioSetup: { microphones: [], systemMonitors: [] }
};

export function isWebSnapshot(value: unknown): value is WebSnapshot {
  if (!isRecord(value) || value.type !== 'snapshot') return false;
  return (
    typeof value.status === 'string' &&
    typeof value.notice === 'string' &&
    typeof value.conversation === 'string' &&
    typeof value.reply === 'string' &&
    typeof value.coach === 'string' &&
    typeof value.story === 'string' &&
    typeof value.requiresAudioSetup === 'boolean' &&
    typeof value.sessionState === 'string' &&
    isRecord(value.provider) &&
    Array.isArray(value.provider.models) &&
    value.provider.models.every((model) => typeof model === 'string') &&
    isProviderAssignments(value.provider.assignments) &&
    Array.isArray(value.provider.activity) &&
    value.provider.activity.every(isProviderActivity) &&
    isRecord(value.activeAudio) &&
    isActiveAudioSource(value.activeAudio.microphone) &&
    isActiveAudioSource(value.activeAudio.system) &&
    isRecord(value.audioSetup) &&
    Array.isArray(value.audioSetup.microphones) &&
    value.audioSetup.microphones.every(isAudioMeter) &&
    Array.isArray(value.audioSetup.systemMonitors) &&
    value.audioSetup.systemMonitors.every(isAudioMeter)
  );
}

function isProviderAssignments(value: unknown): value is WebSnapshot['provider']['assignments'] {
  if (!isRecord(value)) return false;
  return (
    isProviderAssignment(value.draft) &&
    isProviderAssignment(value.commentary) &&
    isProviderAssignment(value.summary)
  );
}

function isProviderAssignment(value: unknown): value is ProviderAssignment {
  return (
    isRecord(value) && typeof value.model === 'string' && typeof value.reasoningEffort === 'string'
  );
}

function isProviderActivity(value: unknown): value is ProviderActivity {
  return isRecord(value) && typeof value.phase === 'string';
}

function isActiveAudioSource(value: unknown): value is ActiveAudioSource {
  return (
    isRecord(value) &&
    typeof value.label === 'string' &&
    typeof value.nodeName === 'string' &&
    isPercent(value.level) &&
    typeof value.state === 'string'
  );
}

function isAudioMeter(value: unknown): value is AudioMeter {
  return (
    isRecord(value) &&
    Number.isInteger(value.index) &&
    Number(value.index) >= 0 &&
    typeof value.nodeId === 'string' &&
    typeof value.nodeName === 'string' &&
    typeof value.label === 'string' &&
    typeof value.isDefault === 'boolean' &&
    typeof value.isSelected === 'boolean' &&
    isPercent(value.level) &&
    typeof value.isAvailable === 'boolean'
  );
}

function isPercent(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 100;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}
