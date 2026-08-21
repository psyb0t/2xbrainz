import { describe, expect, it } from 'vitest';
import { EMPTY_SNAPSHOT } from './contracts';
import {
  applicableRuntimeSettings,
  loadRuntimeSettings,
  RUNTIME_SETTINGS_KEY,
  runtimeSettingsMessage,
  saveRuntimeSettings,
  settingsFromSnapshot
} from './runtimeSettings';

const SNAPSHOT = {
  ...EMPTY_SNAPSHOT,
  provider: {
    ...EMPTY_SNAPSHOT.provider,
    models: ['reply-default', 'saved-model'],
    assignments: {
      draft: { model: 'reply-default', reasoningEffort: 'none' },
      fast_draft: { model: 'reply-default', reasoningEffort: 'none' },
      commentary: { model: 'reply-default', reasoningEffort: 'none' },
      summary: { model: 'reply-default', reasoningEffort: 'none' },
      research: { model: 'saved-model', reasoningEffort: 'high' }
    }
  },
  settings: {
    ...EMPTY_SNAPSHOT.settings,
    talkiesModels: ['asr-default'],
    talkiesModel: 'asr-default',
    defaults: {
      assignments: {
        draft: { model: 'reply-default', reasoningEffort: 'none' },
        fast_draft: { model: 'reply-default', reasoningEffort: 'none' },
        commentary: { model: 'reply-default', reasoningEffort: 'none' },
        summary: { model: 'reply-default', reasoningEffort: 'none' },
        research: { model: 'saved-model', reasoningEffort: 'high' }
      },
      talkiesModel: 'asr-default',
      sessionBrief: '',
      webResearchEnabled: true,
      autoDispatchEnabled: true
    }
  },
  audioSetup: {
    microphones: [
      {
        index: 0,
        nodeId: '1',
        nodeName: 'mic',
        label: 'Mic',
        isDefault: true,
        isSelected: false,
        level: 0,
        isAvailable: true
      }
    ],
    systemMonitors: [
      {
        index: 0,
        nodeId: '2',
        nodeName: 'system',
        label: 'System',
        isDefault: true,
        isSelected: false,
        level: 0,
        isAvailable: true
      }
    ]
  }
};

describe('runtime settings', () => {
  it('round trips only the safe versioned settings object', () => {
    const settings = {
      ...settingsFromSnapshot(SNAPSHOT),
      sessionBrief: 'A trusted interview context.',
      microphoneNode: 'mic',
      systemNode: 'system'
    };
    saveRuntimeSettings(localStorage, settings);
    expect(loadRuntimeSettings(localStorage)).toEqual(settings);
    expect(localStorage.getItem(RUNTIME_SETTINGS_KEY)).not.toContain('token');
  });

  it('rejects malformed, extra, and credential-shaped fields', () => {
    localStorage.setItem(RUNTIME_SETTINGS_KEY, '{bad');
    expect(loadRuntimeSettings(localStorage)).toBeNull();
    localStorage.setItem(
      RUNTIME_SETTINGS_KEY,
      JSON.stringify({ ...settingsFromSnapshot(SNAPSHOT), token: 'not-allowed' })
    );
    expect(loadRuntimeSettings(localStorage)).toBeNull();
  });

  it('drops stale audio and falls back from unavailable models', () => {
    const saved = {
      ...settingsFromSnapshot(SNAPSHOT),
      providers: {
        ...SNAPSHOT.provider.assignments,
        draft: { model: 'missing', reasoningEffort: 'high' }
      },
      microphoneNode: 'missing-mic',
      systemNode: 'missing-system'
    };
    const applicable = applicableRuntimeSettings(saved, SNAPSHOT);
    expect(applicable.providers.draft).toEqual({ model: 'reply-default', reasoningEffort: 'none' });
    expect(applicable.microphoneNode).toBeNull();
    expect(applicable.systemNode).toBeNull();
  });

  it('repairs a reasoning value that the Claudebox researcher does not accept', () => {
    const saved = {
      ...settingsFromSnapshot(SNAPSHOT),
      providers: {
        ...SNAPSHOT.provider.assignments,
        research: { model: 'saved-model', reasoningEffort: 'minimal' }
      }
    };
    const compatibleSnapshot = {
      ...SNAPSHOT,
      provider: {
        ...SNAPSHOT.provider,
        models: ['reply-default', 'saved-model'],
        assignments: {
          ...SNAPSHOT.provider.assignments,
          research: { model: 'saved-model', reasoningEffort: 'high' }
        }
      }
    };

    const applicable = applicableRuntimeSettings(saved, compatibleSnapshot);

    expect(applicable.providers.research).toEqual({
      model: 'saved-model',
      reasoningEffort: 'high'
    });
  });

  it('maps browser names to the strict websocket contract', () => {
    const message = runtimeSettingsMessage(settingsFromSnapshot(SNAPSHOT));
    expect(message).toEqual(
      expect.objectContaining({
        type: 'runtime_settings',
        schema_version: 3,
        talkies_model: 'asr-default',
        web_research_enabled: true,
        auto_dispatch_enabled: true
      })
    );
    expect(message.providers).toEqual(
      expect.objectContaining({
        fast_draft: { model: 'reply-default', reasoning_effort: 'none' }
      })
    );
  });

  it('migrates a stored v2 payload to v3 with a default fast reply lane', () => {
    localStorage.setItem(
      RUNTIME_SETTINGS_KEY,
      JSON.stringify({
        schemaVersion: 2,
        providers: {
          draft: { model: 'reply-default', reasoningEffort: 'none' },
          commentary: { model: 'reply-default', reasoningEffort: 'none' },
          summary: { model: 'reply-default', reasoningEffort: 'none' },
          research: { model: 'saved-model', reasoningEffort: 'high' }
        },
        talkiesModel: 'asr-default',
        sessionBrief: '',
        webResearchEnabled: true,
        autoDispatchEnabled: true,
        microphoneNode: null,
        systemNode: null
      })
    );

    const loaded = loadRuntimeSettings(localStorage);
    if (loaded === null) throw new Error('v2 payload did not migrate to v3');

    expect(loaded.schemaVersion).toBe(3);
    expect(loaded.providers.fast_draft).toEqual({
      model: 'groq-gpt-oss-120b',
      reasoningEffort: 'none'
    });
  });
});
