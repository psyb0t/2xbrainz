import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App.svelte';
import { EMPTY_SNAPSHOT, type WebSnapshot } from './lib/contracts';
import { PREFERENCES_KEY } from './lib/preferences';

type SocketListener = (event: Event) => void;

class MockWebSocket {
  static readonly OPEN = 1;
  static readonly CLOSED = 3;
  static readonly instances: MockWebSocket[] = [];

  readonly sent: string[] = [];
  readyState = MockWebSocket.OPEN;
  private readonly listeners = new Map<string, SocketListener[]>();

  constructor(readonly url: string) {
    MockWebSocket.instances.push(this);
  }

  addEventListener(type: string, listener: SocketListener): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  send(value: string): void {
    this.sent.push(value);
  }

  close(): void {
    this.readyState = MockWebSocket.CLOSED;
  }

  emit(type: string, event: Event = new Event(type)): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

const SNAPSHOT: WebSnapshot = {
  ...EMPTY_SNAPSHOT,
  status: 'Listening',
  notice: 'Calling the language model · 1.2s',
  conversation: `You: Can we ship this?
Remote: Verify the release gates.`,
  reply: 'Run the final checks before shipping.',
  coach: 'Keep the answer concrete.',
  story: 'The team is preparing a release.',
  sessionState: 'running',
  provider: {
    models: ['model-a', 'model-b'],
    assignments: {
      draft: { model: 'model-a', reasoningEffort: 'none' },
      commentary: { model: 'model-b', reasoningEffort: 'low' },
      summary: { model: 'model-a', reasoningEffort: 'medium' }
    },
    activity: [
      {
        phase: 'request_started',
        flow_id: 'draft-flow',
        output_kind: 'draft',
        model: 'model-a',
        tools_enabled: true
      },
      {
        phase: 'reasoning_streaming',
        flow_id: 'draft-flow',
        output_kind: 'draft',
        model: 'model-a',
        reasoning: 'Checking the release evidence.'
      },
      {
        phase: 'tool_started',
        flow_id: 'draft-flow',
        output_kind: 'draft',
        model: 'model-a',
        tool_call_id: 'release-search',
        tool: 'search_web',
        tool_input: { query: 'release gates' }
      },
      {
        phase: 'tool_completed',
        flow_id: 'draft-flow',
        output_kind: 'draft',
        model: 'model-a',
        tool_call_id: 'release-search',
        tool: 'search_web',
        tool_result: '{"results":[]}'
      },
      {
        phase: 'reasoning_streaming',
        flow_id: 'draft-flow',
        output_kind: 'draft',
        model: 'model-a',
        reasoning: 'Turning the evidence into a concise reply.'
      },
      {
        phase: 'request_completed',
        flow_id: 'draft-flow',
        output_kind: 'draft',
        model: 'model-a',
        output: 'Run the final checks before shipping.'
      }
    ]
  },
  activeAudio: {
    microphone: { label: 'Desk microphone', nodeName: 'desk-mic', level: 41, state: 'ready' },
    system: { label: 'Headphones', nodeName: 'headphones.monitor', level: 72, state: 'ready' }
  },
  audioSetup: {
    microphones: [
      {
        index: 0,
        nodeId: '11',
        nodeName: 'desk-mic',
        label: 'Desk microphone',
        isDefault: true,
        isSelected: true,
        level: 41,
        isAvailable: true
      }
    ],
    systemMonitors: [
      {
        index: 0,
        nodeId: '22',
        nodeName: 'headphones.monitor',
        label: 'Headphones',
        isDefault: true,
        isSelected: true,
        level: 72,
        isAvailable: true
      }
    ]
  }
};

beforeEach(() => {
  MockWebSocket.instances.length = 0;
  localStorage.clear();
  vi.stubGlobal('WebSocket', MockWebSocket);
  HTMLDialogElement.prototype.showModal = function showModal(): void {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function close(): void {
    this.removeAttribute('open');
  };
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function connectedSocket(): Promise<MockWebSocket> {
  await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
  const socket = MockWebSocket.instances.at(0);
  if (socket === undefined) throw new Error('operator console did not open a WebSocket');
  socket.emit('open');
  return socket;
}

async function publish(socket: MockWebSocket, snapshot: WebSnapshot): Promise<void> {
  socket.emit('message', new MessageEvent('message', { data: JSON.stringify(snapshot) }));
  await waitFor(() => expect(screen.getByText(snapshot.notice)).toBeTruthy());
}

describe('operator console', () => {
  it('renders live state and sends stop and start controls', async () => {
    render(App);
    const socket = await connectedSocket();
    await publish(socket, SNAPSHOT);

    expect(screen.getByText(/Can we ship this/)).toBeTruthy();
    expect(screen.getByText('Run the final checks before shipping.')).toBeTruthy();
    expect(screen.getByText('The team is preparing a release.')).toBeTruthy();

    expect(screen.getByText('request started')).toBeTruthy();
    expect(screen.getByText('tool completed')).toBeTruthy();
    const replyItems = document.querySelectorAll('.reply-card .stream-item');
    expect(Array.from(replyItems).map((item) => item.classList[0])).toEqual([
      'stream-status',
      'stream-event',
      'stream-event',
      'stream-event',
      'stream-response'
    ]);
    expect(
      Array.from(document.querySelectorAll<HTMLDetailsElement>('.reply-card details')).every(
        (item) => !item.open
      )
    ).toBe(true);
    const diagnostics = socket.sent.map((value) => JSON.parse(value));
    expect(diagnostics).toContainEqual({ type: 'client_debug', event: 'websocket_opened' });
    expect(diagnostics).toContainEqual({
      type: 'client_debug',
      event: 'snapshot_received',
      activity_count: 6
    });
    expect(diagnostics).toContainEqual(
      expect.objectContaining({
        type: 'client_debug',
        event: 'provider_feed_rendered',
        output_kind: 'draft',
        item_count: 5
      })
    );
    await fireEvent.click(screen.getByRole('button', { name: 'Stop listening' }));
    await publish(socket, { ...SNAPSHOT, sessionState: 'paused' });
    await fireEvent.click(screen.getByRole('button', { name: 'Start listening' }));

    expect(socket.sent.map((value) => JSON.parse(value))).toContainEqual({
      type: 'control',
      command: 'pause'
    });
    expect(socket.sent.map((value) => JSON.parse(value))).toContainEqual({
      type: 'control',
      command: 'resume'
    });
  });

  it('changes model and reasoning effort for future generations', async () => {
    render(App);
    const socket = await connectedSocket();
    await publish(socket, SNAPSHOT);

    await fireEvent.click(screen.getByRole('button', { name: 'Models' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Reply model' }));
    expect(screen.getByRole('textbox', { name: 'Filter models' })).toBeTruthy();
    expect(screen.getByText('2 of 2')).toBeTruthy();
    expect(screen.getByText('Current')).toBeTruthy();
    await fireEvent.input(screen.getByRole('textbox', { name: 'Filter models' }), {
      target: { value: 'model-b' }
    });
    await fireEvent.click(screen.getByRole('option', { name: 'model-b' }));
    await fireEvent.change(screen.getByRole('combobox', { name: 'Reply reasoning' }), {
      target: { value: 'high' }
    });

    expect(socket.sent.map((value) => JSON.parse(value))).toContainEqual({
      type: 'provider_settings',
      flow: 'draft',
      model: 'model-b',
      reasoning_effort: 'none'
    });
    expect(socket.sent.map((value) => JSON.parse(value))).toContainEqual({
      type: 'provider_settings',
      flow: 'draft',
      model: 'model-b',
      reasoning_effort: 'high'
    });
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
  });

  it('meters every setup candidate and submits the selected pair', async () => {
    render(App);
    const socket = await connectedSocket();
    await publish(socket, { ...SNAPSHOT, requiresAudioSetup: true });

    expect(screen.getByRole('dialog').hasAttribute('open')).toBe(true);
    expect(screen.getByText('desk-mic · node 11')).toBeTruthy();
    expect(screen.getByText('headphones.monitor · node 22')).toBeTruthy();
    expect(document.querySelectorAll('.default-badge')).toHaveLength(2);

    await fireEvent.click(screen.getByRole('button', { name: 'Redetect devices' }));

    await fireEvent.click(screen.getByRole('button', { name: 'Save sources' }));
    const messages = socket.sent.map((value) => JSON.parse(value));
    expect(messages).toContainEqual({ type: 'audio_metering', enabled: true });
    expect(messages).toContainEqual({
      type: 'audio_selection',
      microphone_index: 0,
      system_index: 0
    });
    expect(messages).toContainEqual({ type: 'audio_metering', enabled: false });
    expect(messages).toContainEqual({ type: 'audio_rescan' });
  });

  it('persists collapsed layout state across mounts', async () => {
    const first = render(App);
    await connectedSocket();
    await fireEvent.click(screen.getByRole('button', { name: 'Toggle active audio sources' }));

    expect(JSON.parse(localStorage.getItem(PREFERENCES_KEY) ?? '{}')).toMatchObject({
      sourceBarCollapsed: true
    });
    first.unmount();

    render(App);
    await waitFor(() => expect(screen.getByText('Show audio')).toBeTruthy());
  });
});
