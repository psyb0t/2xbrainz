export type ConnectionState = 'connecting' | 'connected' | 'disconnected';

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
  activeAudio: {
    microphone: ActiveAudioSource;
    system: ActiveAudioSource;
  };
  audioSetup: {
    microphones: AudioMeter[];
    systemMonitors: AudioMeter[];
  };
}

export const EMPTY_SNAPSHOT: WebSnapshot = {
  type: 'snapshot',
  status: '2xbrainz  STARTING',
  notice: 'Connecting to the local session…',
  conversation: 'Waiting for the first finalized turn…',
  reply: 'WAITING\n—',
  coach: 'PRIVATE COACH\n—',
  story: 'STORY SO FAR\n—',
  requiresAudioSetup: false,
  activeAudio: {
    microphone: { label: 'Not selected', nodeName: '', level: 0 },
    system: { label: 'Not selected', nodeName: '', level: 0 }
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

function isActiveAudioSource(value: unknown): value is ActiveAudioSource {
  return (
    isRecord(value) &&
    typeof value.label === 'string' &&
    typeof value.nodeName === 'string' &&
    isPercent(value.level)
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
