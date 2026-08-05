<script lang="ts">
  import { onMount, tick } from 'svelte';
  import {
    EMPTY_SNAPSHOT,
    isWebSnapshot,
    type AudioMeter,
    type ConnectionState,
    type WebSnapshot
  } from './lib/contracts';
  import {
    DEFAULT_PREFERENCES,
    loadPreferences,
    savePreferences,
    type LayoutPreferences
  } from './lib/preferences';

  const RECONNECT_DELAY_MS = 1_500;
  const FOLLOW_DISTANCE_PX = 72;

  let snapshot: WebSnapshot = EMPTY_SNAPSHOT;
  let connection: ConnectionState = 'connecting';
  let socket: WebSocket | null = null;
  let reconnectTimer: number | null = null;
  let preferences: LayoutPreferences = { ...DEFAULT_PREFERENCES };
  let workspace: HTMLElement;
  let guidanceStack: HTMLElement;
  let conversationScroller: HTMLElement;
  let settingsDialog: HTMLDialogElement;
  let microphoneIndex = 0;
  let systemIndex = 0;
  let selectionDirty = false;
  let shouldFollowConversation = true;

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
      return;
    }
    if (!isWebSnapshot(value)) return;
    snapshot = value;
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

  function control(command: 'pause' | 'resume'): void {
    send({ type: 'control', command });
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

  function beginGuidanceResize(event: PointerEvent, panel: 'reply' | 'coach'): void {
    event.preventDefault();
    const bounds = guidanceStack.getBoundingClientRect();
    trackPointer(event, (moveEvent) => {
      const percent = ((moveEvent.clientY - bounds.top) / bounds.height) * 100;
      if (panel === 'reply') {
        setPreference('replyHeightPercent', clamp(percent, 15, 70));
        return;
      }
      const coachPercent = percent - preferences.replyHeightPercent;
      setPreference('coachHeightPercent', clamp(coachPercent, 15, 70));
    });
  }

  function resizeWithKeyboard(event: KeyboardEvent, panel: 'main' | 'reply' | 'coach'): void {
    const direction = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 2 : -2;
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
    event.preventDefault();
    if (panel === 'main') {
      setPreference('mainSplitPercent', clamp(preferences.mainSplitPercent + direction, 28, 78));
      return;
    }
    if (panel === 'reply') {
      setPreference(
        'replyHeightPercent',
        clamp(preferences.replyHeightPercent + direction, 15, 70)
      );
      return;
    }
    setPreference('coachHeightPercent', clamp(preferences.coachHeightPercent + direction, 15, 70));
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
      <button class="quiet-button" onclick={() => control('pause')}>Pause</button>
      <button class="quiet-button" onclick={() => control('resume')}>Resume</button>
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
          <span>Microphone</span><strong>{snapshot.activeAudio.microphone.level}%</strong>
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
          <span>System audio</span><strong>{snapshot.activeAudio.system.level}%</strong>
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
      onkeydown={(event) => resizeWithKeyboard(event, 'main')}
    ></button>

    <section class="guidance-stack" bind:this={guidanceStack} aria-label="Copilot guidance">
      <article
        class:collapsed={preferences.replyCollapsed}
        class="panel guidance-card reply-card"
        style={`--panel-size:${preferences.replyHeightPercent}%`}
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
        {#if !preferences.replyCollapsed}<div class="panel-scroll guidance-copy">
            <pre>{snapshot.reply}</pre>
          </div>{/if}
      </article>
      {#if !preferences.replyCollapsed}
        <button
          class="splitter guidance-splitter"
          aria-label="Resize reply guidance"
          onpointerdown={(event) => beginGuidanceResize(event, 'reply')}
          onkeydown={(event) => resizeWithKeyboard(event, 'reply')}
        ></button>
      {/if}

      <article
        class:collapsed={preferences.coachCollapsed}
        class="panel guidance-card coach-card"
        style={`--panel-size:${preferences.coachHeightPercent}%`}
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
        {#if !preferences.coachCollapsed}<div class="panel-scroll guidance-copy">
            <pre>{snapshot.coach}</pre>
          </div>{/if}
      </article>
      {#if !preferences.coachCollapsed}
        <button
          class="splitter guidance-splitter"
          aria-label="Resize private coaching"
          onpointerdown={(event) => beginGuidanceResize(event, 'coach')}
          onkeydown={(event) => resizeWithKeyboard(event, 'coach')}
        ></button>
      {/if}

      <article class:collapsed={preferences.storyCollapsed} class="panel guidance-card story-card">
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
        {#if !preferences.storyCollapsed}<div class="panel-scroll guidance-copy">
            <pre>{snapshot.story}</pre>
          </div>{/if}
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
      {#if !snapshot.requiresAudioSetup}<button
          class="icon-button"
          aria-label="Close source settings"
          onclick={closeAudioSettings}>×</button
        >{/if}
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
          : 'Changes apply on the next live session.'}</span
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
