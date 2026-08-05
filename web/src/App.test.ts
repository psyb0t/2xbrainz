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
  activeAudio: {
    microphone: { label: 'Desk microphone', nodeName: 'desk-mic', level: 41 },
    system: { label: 'Headphones', nodeName: 'headphones.monitor', level: 72 }
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
  it('renders live state and sends pause and resume controls', async () => {
    render(App);
    const socket = await connectedSocket();
    await publish(socket, SNAPSHOT);

    expect(screen.getByText(/Can we ship this/)).toBeTruthy();
    expect(screen.getByText('Run the final checks before shipping.')).toBeTruthy();
    expect(screen.getByText('The team is preparing a release.')).toBeTruthy();

    await fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Resume' }));

    expect(socket.sent.map((value) => JSON.parse(value))).toContainEqual({
      type: 'control',
      command: 'pause'
    });
    expect(socket.sent.map((value) => JSON.parse(value))).toContainEqual({
      type: 'control',
      command: 'resume'
    });
  });

  it('meters every setup candidate and submits the selected pair', async () => {
    render(App);
    const socket = await connectedSocket();
    await publish(socket, { ...SNAPSHOT, requiresAudioSetup: true });

    expect(screen.getByRole('dialog').hasAttribute('open')).toBe(true);
    expect(screen.getByText('desk-mic · node 11')).toBeTruthy();
    expect(screen.getByText('headphones.monitor · node 22')).toBeTruthy();
    expect(screen.getAllByText('Default')).toHaveLength(2);

    await fireEvent.click(screen.getByRole('button', { name: 'Save sources' }));
    const messages = socket.sent.map((value) => JSON.parse(value));
    expect(messages).toContainEqual({ type: 'audio_metering', enabled: true });
    expect(messages).toContainEqual({
      type: 'audio_selection',
      microphone_index: 0,
      system_index: 0
    });
    expect(messages).toContainEqual({ type: 'audio_metering', enabled: false });
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
