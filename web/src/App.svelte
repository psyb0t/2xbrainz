<script lang="ts">
  import { onMount, tick } from 'svelte';
  import {
    EMPTY_SNAPSHOT,
    isWebSnapshot,
    type AudioMeter,
    type ConnectionState,
    type FrontendDebugMessage,
    type ProviderOutputKind,
    type WebSnapshot
  } from './lib/contracts';
  import ProviderFeed from './lib/ProviderFeed.svelte';
  import {
    DEFAULT_PREFERENCES,
    loadPreferences,
    nudgeAdjacentPanelWeights,
    resizeAdjacentPanelWeights,
    savePreferences,
    type LayoutPreferences
  } from './lib/preferences';

  const RECONNECT_DELAY_MS = 1_500;
  const FOLLOW_DISTANCE_PX = 72;
  const KEYBOARD_RESIZE_STEP = 2;
  type GuidancePanel = 'reply' | 'coach' | 'story';
  const PROVIDER_FLOWS: ReadonlyArray<{
    kind: ProviderOutputKind;
    label: string;
    description: string;
  }> = [
    { kind: 'draft', label: 'Reply', description: 'Say this next' },
    { kind: 'commentary', label: 'Coach', description: 'Private signal' },
    { kind: 'summary', label: 'Story', description: 'Working memory' }
  ];

  let snapshot: WebSnapshot = EMPTY_SNAPSHOT;
  let connection: ConnectionState = 'connecting';
  let socket: WebSocket | null = null;
  let reconnectTimer: number | null = null;
  let preferences: LayoutPreferences = { ...DEFAULT_PREFERENCES };
  let workspace: HTMLElement;
  let conversationScroller: HTMLElement;
  let settingsDialog: HTMLDialogElement;
  let microphoneIndex = 0;
  let systemIndex = 0;
  let selectionDirty = false;
  let shouldFollowConversation = true;
  let providerPanelOpen = false;
  let modelPickerFlow: ProviderOutputKind | null = null;
  let modelFilter = '';
  let modelOptions: HTMLElement;
  let modelFilterInput: HTMLInputElement;
  let replyPanel: HTMLElement;
  let coachPanel: HTMLElement;
  let storyPanel: HTMLElement;
  let lastSnapshotDebugSignature = '';

  onMount(() => {
    preferences = loadPreferences(localStorage);
    connect();
    return () => {
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  });

  function connect(): void {
    connection = 'connecting';
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
    socket.addEventListener('open', () => {
      connection = 'connected';
      sendDebug({ type: 'client_debug', event: 'websocket_opened' });
      if (settingsDialog?.open) send({ type: 'audio_metering', enabled: true });
    });
    socket.addEventListener('message', handleMessage);
    socket.addEventListener('close', scheduleReconnect);
    socket.addEventListener('error', () => socket?.close());
  }

  async function handleMessage(event: MessageEvent<string>): Promise<void> {
    let value: unknown;
    try {
      value = JSON.parse(event.data);
    } catch {
      sendDebug({ type: 'client_debug', event: 'snapshot_rejected', reason: 'invalid_json' });
      return;
    }
    if (!isWebSnapshot(value)) {
      sendDebug({ type: 'client_debug', event: 'snapshot_rejected', reason: 'invalid_snapshot' });
      return;
    }
    snapshot = value;
    const debugSignature = snapshotDebugSignature(snapshot);
    if (debugSignature !== lastSnapshotDebugSignature) {
      lastSnapshotDebugSignature = debugSignature;
      sendDebug({
        type: 'client_debug',
        event: 'snapshot_received',
        activity_count: snapshot.provider.activity.length
      });
    }
    if (!selectionDirty) syncSelectedDevices();
    if (snapshot.requiresAudioSetup && !settingsDialog.open) openAudioSettings();
    await tick();
    if (shouldFollowConversation) {
      conversationScroller.scrollTop = conversationScroller.scrollHeight;
    }
  }

  function scheduleReconnect(): void {
    connection = 'disconnected';
    socket = null;
    if (reconnectTimer !== null) return;
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, RECONNECT_DELAY_MS);
  }

  function send(message: Record<string, unknown>): void {
    if (socket?.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify(message));
  }

  function sendDebug(message: FrontendDebugMessage): void {
    if (socket?.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify(message));
  }

  function snapshotDebugSignature(value: WebSnapshot): string {
    const last = value.provider.activity.at(-1);
    const textLength = value.provider.activity.reduce(
      (total, activity) =>
        total + (activity.output?.length ?? 0) + (activity.reasoning?.length ?? 0),
      0
    );
    return `${value.provider.activity.length}:${last?.flow_id ?? ''}:${last?.phase ?? ''}:${textLength}`;
  }

  function control(command: 'pause' | 'resume'): void {
    send({ type: 'control', command });
  }

  function updateProvider(flow: ProviderOutputKind, model: string, reasoningEffort: string): void {
    if (!model) return;
    send({
      type: 'provider_settings',
      flow,
      model,
      reasoning_effort: reasoningEffort
    });
  }

  function filteredModels(): string[] {
    const query = modelFilter.trim().toLowerCase();
    if (!query) return snapshot.provider.models;
    return snapshot.provider.models.filter((model) => model.toLowerCase().includes(query));
  }

  function toggleProviderPanel(): void {
    providerPanelOpen = !providerPanelOpen;
    if (!providerPanelOpen) modelPickerFlow = null;
  }

  async function openModelPicker(flow: ProviderOutputKind): Promise<void> {
    modelPickerFlow = flow;
    modelFilter = '';
    await tick();
    modelFilterInput?.focus();
    modelOptions
      ?.querySelector<HTMLElement>('[aria-selected="true"]')
      ?.scrollIntoView({ block: 'center' });
  }

  function chooseModel(model: string): void {
    if (modelPickerFlow === null) return;
    const flow = modelPickerFlow;
    const assignment = snapshot.provider.assignments[flow];
    updateProvider(flow, model, assignment.reasoningEffort);
    optimisticallyAssign(flow, model, assignment.reasoningEffort);
    modelPickerFlow = null;
  }

  function chooseReasoning(flow: ProviderOutputKind, event: Event): void {
    const target = event.currentTarget;
    if (!(target instanceof HTMLSelectElement)) return;
    const model = snapshot.provider.assignments[flow].model;
    updateProvider(flow, model, target.value);
    optimisticallyAssign(flow, model, target.value);
  }

  function optimisticallyAssign(
    flow: ProviderOutputKind,
    model: string,
    reasoningEffort: string
  ): void {
    snapshot = {
      ...snapshot,
      provider: {
        ...snapshot.provider,
        assignments: {
          ...snapshot.provider.assignments,
          [flow]: { model, reasoningEffort }
        }
      }
    };
  }

  function openAudioSettings(): void {
    syncSelectedDevices();
    selectionDirty = false;
    if (!settingsDialog.open) settingsDialog.showModal();
    send({ type: 'audio_metering', enabled: true });
  }

  function closeAudioSettings(): void {
    if (snapshot.requiresAudioSetup) return;
    send({ type: 'audio_metering', enabled: false });
    settingsDialog.close();
  }

  function handleDialogCancel(event: Event): void {
    if (snapshot.requiresAudioSetup) event.preventDefault();
    else send({ type: 'audio_metering', enabled: false });
  }

  function saveAudioSelection(): void {
    send({
      type: 'audio_selection',
      microphone_index: microphoneIndex,
      system_index: systemIndex
    });
    selectionDirty = false;
    send({ type: 'audio_metering', enabled: false });
    settingsDialog.close();
  }

  function syncSelectedDevices(): void {
    microphoneIndex = selectedIndex(snapshot.audioSetup.microphones);
    systemIndex = selectedIndex(snapshot.audioSetup.systemMonitors);
  }

  function selectedIndex(devices: AudioMeter[]): number {
    return devices.find((device) => device.isSelected)?.index ?? devices[0]?.index ?? 0;
  }

  function setPreference<K extends keyof LayoutPreferences>(
    key: K,
    value: LayoutPreferences[K]
  ): void {
    preferences = { ...preferences, [key]: value };
    savePreferences(localStorage, preferences);
  }

  function beginMainResize(event: PointerEvent): void {
    event.preventDefault();
    const bounds = workspace.getBoundingClientRect();
    trackPointer(event, (moveEvent) => {
      const percent = ((moveEvent.clientX - bounds.left) / bounds.width) * 100;
      setPreference('mainSplitPercent', clamp(percent, 28, 78));
    });
  }

  function beginGuidanceResize(
    event: PointerEvent,
    upperPanel: GuidancePanel,
    lowerPanel: GuidancePanel
  ): void {
    event.preventDefault();
    const upperElement = guidancePanelElement(upperPanel);
    const lowerElement = guidancePanelElement(lowerPanel);
    const upperStartPixels = upperElement.getBoundingClientRect().height;
    const lowerStartPixels = lowerElement.getBoundingClientRect().height;
    const upperStartWeight = guidancePanelWeight(upperPanel);
    const lowerStartWeight = guidancePanelWeight(lowerPanel);
    const startY = event.clientY;
    trackPointer(event, (moveEvent) => {
      const [upperWeight, lowerWeight] = resizeAdjacentPanelWeights(
        upperStartWeight,
        lowerStartWeight,
        upperStartPixels,
        lowerStartPixels,
        moveEvent.clientY - startY
      );
      setGuidancePanelWeights(upperPanel, upperWeight, lowerPanel, lowerWeight);
    });
  }

  function resizeMainWithKeyboard(event: KeyboardEvent): void {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const direction = event.key === 'ArrowRight' ? KEYBOARD_RESIZE_STEP : -KEYBOARD_RESIZE_STEP;
    setPreference('mainSplitPercent', clamp(preferences.mainSplitPercent + direction, 28, 78));
  }

  function resizeGuidanceWithKeyboard(
    event: KeyboardEvent,
    upperPanel: GuidancePanel,
    lowerPanel: GuidancePanel
  ): void {
    if (!['ArrowUp', 'ArrowDown'].includes(event.key)) return;
    event.preventDefault();
    const direction = event.key === 'ArrowDown' ? KEYBOARD_RESIZE_STEP : -KEYBOARD_RESIZE_STEP;
    const [upperWeight, lowerWeight] = nudgeAdjacentPanelWeights(
      guidancePanelWeight(upperPanel),
      guidancePanelWeight(lowerPanel),
      direction
    );
    setGuidancePanelWeights(upperPanel, upperWeight, lowerPanel, lowerWeight);
  }

  function guidancePanelElement(panel: GuidancePanel): HTMLElement {
    if (panel === 'reply') return replyPanel;
    if (panel === 'coach') return coachPanel;
    return storyPanel;
  }

  function guidancePanelWeight(panel: GuidancePanel): number {
    if (panel === 'reply') return preferences.replyHeightPercent;
    if (panel === 'coach') return preferences.coachHeightPercent;
    return preferences.storyHeightPercent;
  }

  function setGuidancePanelWeights(
    upperPanel: GuidancePanel,
    upperWeight: number,
    lowerPanel: GuidancePanel,
    lowerWeight: number
  ): void {
    preferences = withGuidancePanelWeight(preferences, upperPanel, upperWeight);
    preferences = withGuidancePanelWeight(preferences, lowerPanel, lowerWeight);
    savePreferences(localStorage, preferences);
  }

  function withGuidancePanelWeight(
    value: LayoutPreferences,
    panel: GuidancePanel,
    weight: number
  ): LayoutPreferences {
    if (panel === 'reply') return { ...value, replyHeightPercent: weight };
    if (panel === 'coach') return { ...value, coachHeightPercent: weight };
    return { ...value, storyHeightPercent: weight };
  }

  function trackPointer(event: PointerEvent, onMove: (event: PointerEvent) => void): void {
    const target = event.currentTarget;
    if (!(target instanceof HTMLElement)) return;
    target.setPointerCapture(event.pointerId);
    const finish = () => {
      target.removeEventListener('pointermove', onMove);
      target.removeEventListener('pointerup', finish);
      target.removeEventListener('pointercancel', finish);
    };
    target.addEventListener('pointermove', onMove);
    target.addEventListener('pointerup', finish);
    target.addEventListener('pointercancel', finish);
  }

  function trackConversationScroll(): void {
    const remaining =
      conversationScroller.scrollHeight -
      conversationScroller.scrollTop -
      conversationScroller.clientHeight;
    shouldFollowConversation = remaining <= FOLLOW_DISTANCE_PX;
  }

  function clamp(value: number, minimum: number, maximum: number): number {
    return Math.min(maximum, Math.max(minimum, value));
  }
</script>

<svelte:head
  ><meta name="description" content="Live conversation copilot operator console" /></svelte:head
>

<div class="app-shell">
  <header class="topbar">
    <div class="brand-lockup">
      <div class="brain-mark" aria-hidden="true">2×</div>
      <div>
        <h1>brainz</h1>
        <p>live conversation copilot</p>
      </div>
    </div>
    <div class="session-status" title={snapshot.status}>
      <span class:online={connection === 'connected'} class="connection-dot"></span>
      <div>
        <strong>{connection}</strong>
        <span>{snapshot.status}</span>
      </div>
    </div>
    <div class="header-actions">
      <button class="quiet-button" onclick={openAudioSettings}>Sources</button>
      <div class="model-picker">
        <span>Provider routing</span>
        <button
          class="model-picker-trigger"
          aria-label="Models"
          aria-expanded={providerPanelOpen}
          onclick={toggleProviderPanel}>3 flow models</button
        >
        {#if providerPanelOpen}
          <div class="model-picker-popover">
            <div class="model-picker-heading">
              <div>
                <strong>Flow models</strong>
                <span>Independent routing for every generation job</span>
              </div>
              <button
                class="collapse-button"
                aria-label="Close model settings"
                onclick={() => {
                  providerPanelOpen = false;
                  modelPickerFlow = null;
                }}>Close</button
              >
            </div>
            <div class="provider-assignments">
              {#each PROVIDER_FLOWS as flow}
                <section class="provider-assignment">
                  <div class="provider-assignment-copy">
                    <strong>{flow.label}</strong>
                    <span>{flow.description}</span>
                  </div>
                  <div class="provider-assignment-controls">
                    <button
                      class="model-picker-trigger"
                      aria-label={`${flow.label} model`}
                      aria-expanded={modelPickerFlow === flow.kind}
                      title={snapshot.provider.assignments[flow.kind].model}
                      onclick={() => openModelPicker(flow.kind)}
                      >{snapshot.provider.assignments[flow.kind].model || 'Choose model'}</button
                    >
                    <label class="compact-select">
                      <span>{flow.label} reasoning</span>
                      <select
                        aria-label={`${flow.label} reasoning`}
                        value={snapshot.provider.assignments[flow.kind].reasoningEffort}
                        onchange={(event) => chooseReasoning(flow.kind, event)}
                      >
                        <option value="none">Default</option>
                        <option value="minimal">Minimal</option>
                        <option value="low">Low</option>
                        <option value="medium">Medium</option>
                        <option value="high">High</option>
                      </select>
                    </label>
                  </div>
                </section>
              {/each}
            </div>
            {#if modelPickerFlow !== null}
              <div class="model-search">
                <div class="model-picker-heading">
                  <strong
                    >Choose {PROVIDER_FLOWS.find((flow) => flow.kind === modelPickerFlow)
                      ?.label}</strong
                  >
                  <span>{filteredModels().length} of {snapshot.provider.models.length}</span>
                </div>
                <input
                  aria-label="Filter models"
                  placeholder="Filter models…"
                  bind:this={modelFilterInput}
                  bind:value={modelFilter}
                  onkeydown={(event) => event.key === 'Escape' && (modelPickerFlow = null)}
                />
                <div class="model-options" role="listbox" bind:this={modelOptions}>
                  {#each filteredModels() as model}
                    <button
                      class="model-option"
                      role="option"
                      aria-selected={model === snapshot.provider.assignments[modelPickerFlow].model}
                      class:selected={model ===
                        snapshot.provider.assignments[modelPickerFlow].model}
                      title={model}
                      onclick={() => chooseModel(model)}
                    >
                      <span>{model}</span>
                      {#if model === snapshot.provider.assignments[modelPickerFlow].model}<b
                          >Current</b
                        >{/if}
                    </button>
                  {:else}<p>No matching models.</p>{/each}
                </div>
                <div class="model-picker-hint">Type to filter · Esc returns to flows</div>
              </div>
            {/if}
          </div>
        {/if}
      </div>
      {#if snapshot.sessionState === 'running'}
        <button class="stop-button" onclick={() => control('pause')}>Stop listening</button>
      {:else}
        <button
          class="primary-button"
          disabled={snapshot.requiresAudioSetup}
          onclick={() => control('resume')}>Start listening</button
        >
      {/if}
    </div>
  </header>

  <section class="event-strip" aria-live="polite">
    <span class="event-label">Now</span>
    <span>{snapshot.notice}</span>
  </section>

  <section class:collapsed={preferences.sourceBarCollapsed} class="source-strip">
    <button
      class="collapse-button source-collapse"
      aria-label="Toggle active audio sources"
      aria-expanded={!preferences.sourceBarCollapsed}
      onclick={() => setPreference('sourceBarCollapsed', !preferences.sourceBarCollapsed)}
    >
      {preferences.sourceBarCollapsed ? 'Show audio' : 'Hide'}
    </button>
    {#if !preferences.sourceBarCollapsed}
      <div class="active-source microphone">
        <div class="source-heading">
          <span>Microphone · {snapshot.activeAudio.microphone.state}</span><strong
            >{snapshot.activeAudio.microphone.level}%</strong
          >
        </div>
        <div class="level-track">
          <span style={`width:${snapshot.activeAudio.microphone.level}%`}></span>
        </div>
        <small
          >{snapshot.activeAudio.microphone.label ||
            snapshot.activeAudio.microphone.nodeName}</small
        >
      </div>
      <div class="active-source system">
        <div class="source-heading">
          <span>System audio · {snapshot.activeAudio.system.state}</span><strong
            >{snapshot.activeAudio.system.level}%</strong
          >
        </div>
        <div class="level-track">
          <span style={`width:${snapshot.activeAudio.system.level}%`}></span>
        </div>
        <small>{snapshot.activeAudio.system.label || snapshot.activeAudio.system.nodeName}</small>
      </div>
    {/if}
  </section>

  <main
    class="workspace"
    bind:this={workspace}
    style={`--conversation-width:${preferences.mainSplitPercent}%`}
  >
    <section class="panel conversation-panel">
      <div class="panel-heading">
        <div>
          <span class="eyebrow">Live transcript</span>
          <h2>Conversation</h2>
        </div>
        <span class="history-hint">scroll for history</span>
      </div>
      <div
        class="panel-scroll conversation-scroll"
        bind:this={conversationScroller}
        onscroll={trackConversationScroll}
      >
        <pre>{snapshot.conversation}</pre>
      </div>
    </section>

    <button
      class="splitter main-splitter"
      aria-label="Resize conversation and guidance"
      onpointerdown={beginMainResize}
      onkeydown={resizeMainWithKeyboard}
    ></button>

    <section class="guidance-stack" aria-label="Copilot guidance">
      <article
        class:collapsed={preferences.replyCollapsed}
        class="panel guidance-card reply-card"
        style={`--panel-weight:${preferences.replyHeightPercent}`}
        bind:this={replyPanel}
      >
        <div class="panel-heading compact">
          <div>
            <span class="eyebrow">Say this next</span>
            <h2>Reply</h2>
          </div>
          <button
            class="collapse-button"
            aria-expanded={!preferences.replyCollapsed}
            onclick={() => setPreference('replyCollapsed', !preferences.replyCollapsed)}
            >{preferences.replyCollapsed ? 'Expand' : 'Collapse'}</button
          >
        </div>
        {#if !preferences.replyCollapsed}<ProviderFeed
            kind="draft"
            activity={snapshot.provider.activity}
            activeModel={snapshot.provider.assignments.draft.model}
            fallbackOutput={snapshot.reply}
            onDebug={sendDebug}
          />{/if}
      </article>
      {#if !preferences.replyCollapsed && !preferences.coachCollapsed}
        <button
          class="splitter guidance-splitter"
          aria-label="Resize reply and private coaching"
          onpointerdown={(event) => beginGuidanceResize(event, 'reply', 'coach')}
          onkeydown={(event) => resizeGuidanceWithKeyboard(event, 'reply', 'coach')}
        ></button>
      {/if}

      <article
        class:collapsed={preferences.coachCollapsed}
        class="panel guidance-card coach-card"
        style={`--panel-weight:${preferences.coachHeightPercent}`}
        bind:this={coachPanel}
      >
        <div class="panel-heading compact">
          <div>
            <span class="eyebrow">Private signal</span>
            <h2>Coach</h2>
          </div>
          <button
            class="collapse-button"
            aria-expanded={!preferences.coachCollapsed}
            onclick={() => setPreference('coachCollapsed', !preferences.coachCollapsed)}
            >{preferences.coachCollapsed ? 'Expand' : 'Collapse'}</button
          >
        </div>
        {#if !preferences.coachCollapsed}<ProviderFeed
            kind="commentary"
            activity={snapshot.provider.activity}
            activeModel={snapshot.provider.assignments.commentary.model}
            fallbackOutput={snapshot.coach}
            onDebug={sendDebug}
          />{/if}
      </article>
      {#if !preferences.coachCollapsed && !preferences.storyCollapsed}
        <button
          class="splitter guidance-splitter"
          aria-label="Resize private coaching and story"
          onpointerdown={(event) => beginGuidanceResize(event, 'coach', 'story')}
          onkeydown={(event) => resizeGuidanceWithKeyboard(event, 'coach', 'story')}
        ></button>
      {/if}

      <article
        class:collapsed={preferences.storyCollapsed}
        class="panel guidance-card story-card"
        style={`--panel-weight:${preferences.storyHeightPercent}`}
        bind:this={storyPanel}
      >
        <div class="panel-heading compact">
          <div>
            <span class="eyebrow">Working memory</span>
            <h2>Story so far</h2>
          </div>
          <button
            class="collapse-button"
            aria-expanded={!preferences.storyCollapsed}
            onclick={() => setPreference('storyCollapsed', !preferences.storyCollapsed)}
            >{preferences.storyCollapsed ? 'Expand' : 'Collapse'}</button
          >
        </div>
        {#if !preferences.storyCollapsed}<ProviderFeed
            kind="summary"
            activity={snapshot.provider.activity}
            activeModel={snapshot.provider.assignments.summary.model}
            fallbackOutput={snapshot.story}
            onDebug={sendDebug}
          />{/if}
      </article>
    </section>
  </main>
</div>

<dialog
  class="source-modal"
  bind:this={settingsDialog}
  oncancel={handleDialogCancel}
  onclose={() => send({ type: 'audio_metering', enabled: false })}
>
  <div class="modal-shell">
    <header class="modal-header">
      <div>
        <span class="eyebrow">Audio routing</span>
        <h2>Choose live sources</h2>
      </div>
      <div class="modal-actions">
        <button class="quiet-button" onclick={() => send({ type: 'audio_rescan' })}
          >Redetect devices</button
        >
        {#if !snapshot.requiresAudioSetup}<button
            class="icon-button"
            aria-label="Close source settings"
            onclick={closeAudioSettings}>×</button
          >{/if}
      </div>
    </header>
    <p class="modal-intro">
      Speak and play system audio. Every compatible source is metered live so you can select by
      behavior, not by guessing a device name.
    </p>
    <div class="device-columns">
      <fieldset>
        <legend>Microphone input</legend>
        <div class="device-list">
          {#each snapshot.audioSetup.microphones as device (device.nodeName)}
            <label
              class:selected={microphoneIndex === device.index}
              class:unavailable={!device.isAvailable}
              class="device-card"
            >
              <input
                type="radio"
                name="microphone"
                value={device.index}
                bind:group={microphoneIndex}
                onchange={() => (selectionDirty = true)}
              />
              <span class="device-copy"
                ><strong>{device.label}</strong><small
                  >{device.nodeName} · node {device.nodeId}</small
                ></span
              >
              {#if device.isDefault}<span class="default-badge">Default</span>{/if}
              <span class="device-level"><span style={`width:${device.level}%`}></span></span>
              <b>{device.isAvailable ? `${device.level}%` : 'Unavailable'}</b>
            </label>
          {:else}<p class="empty-devices">No compatible microphone inputs are visible.</p>{/each}
        </div>
      </fieldset>
      <fieldset>
        <legend>System audio</legend>
        <div class="device-list">
          {#each snapshot.audioSetup.systemMonitors as device (device.nodeName)}
            <label
              class:selected={systemIndex === device.index}
              class:unavailable={!device.isAvailable}
              class="device-card"
            >
              <input
                type="radio"
                name="system"
                value={device.index}
                bind:group={systemIndex}
                onchange={() => (selectionDirty = true)}
              />
              <span class="device-copy"
                ><strong>{device.label}</strong><small
                  >{device.nodeName} · node {device.nodeId}</small
                ></span
              >
              {#if device.isDefault}<span class="default-badge">Default</span>{/if}
              <span class="device-level"><span style={`width:${device.level}%`}></span></span>
              <b>{device.isAvailable ? `${device.level}%` : 'Unavailable'}</b>
            </label>
          {:else}<p class="empty-devices">
              No compatible system-audio monitors are visible.
            </p>{/each}
        </div>
      </fieldset>
    </div>
    <footer class="modal-footer">
      <span
        >{snapshot.requiresAudioSetup
          ? 'Choose both sources to begin.'
          : 'Changes apply immediately; a disconnected channel retries independently.'}</span
      >
      <button
        class="primary-button"
        disabled={snapshot.audioSetup.microphones.length === 0 ||
          snapshot.audioSetup.systemMonitors.length === 0}
        onclick={saveAudioSelection}>Save sources</button
      >
    </footer>
  </div>
</dialog>
