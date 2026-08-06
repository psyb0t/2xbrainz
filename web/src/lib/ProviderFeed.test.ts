import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ProviderFeed from './ProviderFeed.svelte';
import type { ProviderActivity } from './contracts';

const FIRST_FLOW: ProviderActivity[] = [
  { phase: 'request_started', flow_id: 'flow-1', output_kind: 'draft', model: 'model-a' },
  {
    phase: 'reasoning_streaming',
    flow_id: 'flow-1',
    output_kind: 'draft',
    model: 'model-a',
    reasoning: 'Checking the available facts.'
  },
  {
    phase: 'tool_started',
    flow_id: 'flow-1',
    output_kind: 'draft',
    model: 'model-a',
    tool_call_id: 'call-1',
    tool: 'research_web',
    tool_input: { query: 'example' }
  },
  {
    phase: 'tool_completed',
    flow_id: 'flow-1',
    output_kind: 'draft',
    model: 'model-a',
    tool_call_id: 'call-1',
    tool: 'research_web',
    tool_result: '{"results":[]}'
  },
  {
    phase: 'reasoning_streaming',
    flow_id: 'flow-1',
    output_kind: 'draft',
    model: 'model-a',
    reasoning: 'Using the research result.'
  },
  {
    phase: 'output_streaming',
    flow_id: 'flow-1',
    output_kind: 'draft',
    model: 'model-a',
    output: 'First'
  },
  {
    phase: 'request_completed',
    flow_id: 'flow-1',
    output_kind: 'draft',
    model: 'model-a',
    output: 'First reply.'
  }
];

afterEach(cleanup);

describe('provider feed', () => {
  it('renders one flat chronological stream with independently collapsed events', () => {
    const view = renderFeed(FIRST_FLOW);
    const feed = requireFeed(view.container);
    const classes = Array.from(feed.children).map((child) => child.classList[0]);

    expect(classes).toEqual([
      'stream-status',
      'stream-event',
      'stream-event',
      'stream-event',
      'stream-response'
    ]);
    expect(view.container.querySelector('.generation-entry')).toBeNull();
    expect(Array.from(feed.querySelectorAll('details')).every((item) => !item.open)).toBe(true);
    expect(screen.getByText('Checking the available facts.')).toBeTruthy();
    expect(screen.getByText('Using the research result.')).toBeTruthy();
    expect(screen.getByText('First reply.')).toBeTruthy();
  });

  it('keeps prior outputs in the same scrolling transcript', async () => {
    const view = renderFeed(FIRST_FLOW);
    await view.rerender(
      feedProps([
        ...FIRST_FLOW,
        {
          phase: 'request_completed',
          flow_id: 'flow-2',
          output_kind: 'draft',
          model: 'model-a',
          output: 'Second reply.'
        }
      ])
    );

    expect(screen.getByText('First reply.')).toBeTruthy();
    expect(screen.getByText('Second reply.')).toBeTruthy();
    expect(view.container.querySelectorAll('.stream-response')).toHaveLength(2);
    expect(view.container.querySelector('.generation-entry')).toBeNull();
  });

  it('updates the active streamed response in place', async () => {
    const streaming = FIRST_FLOW.slice(0, -1);
    const view = renderFeed(streaming);

    expect(view.container.querySelectorAll('.stream-response')).toHaveLength(1);
    await view.rerender(
      feedProps([
        ...streaming.slice(0, -1),
        { ...streaming.at(-1)!, output: 'First reply continues.' }
      ])
    );

    expect(view.container.querySelectorAll('.stream-response')).toHaveLength(1);
    expect(screen.getByText('First reply continues.')).toBeTruthy();
  });

  it('stops following while scrolled up and resumes at the bottom', async () => {
    const view = renderFeed(FIRST_FLOW);
    const feed = requireFeed(view.container);
    Object.defineProperties(feed, {
      scrollHeight: { configurable: true, value: 1_000 },
      clientHeight: { configurable: true, value: 200 }
    });
    feed.scrollTop = 100;
    await fireEvent.scroll(feed);
    await view.rerender(feedProps([...FIRST_FLOW]));
    expect(feed.scrollTop).toBe(100);

    feed.scrollTop = 800;
    await fireEvent.scroll(feed);
    await view.rerender(
      feedProps([
        ...FIRST_FLOW,
        {
          phase: 'output_streaming',
          flow_id: 'flow-2',
          output_kind: 'draft',
          model: 'model-a',
          output: 'New streamed reply.'
        }
      ])
    );
    expect(feed.scrollTop).toBe(1_000);
  });

  it('never renders provider HTML or external links as active content', async () => {
    const view = renderFeed([
      {
        phase: 'reasoning_streaming',
        flow_id: 'hostile-flow',
        output_kind: 'draft',
        model: 'model-a',
        reasoning: '[external target](https://attacker.example/path)'
      },
      {
        phase: 'request_completed',
        flow_id: 'hostile-flow',
        output_kind: 'draft',
        model: 'model-a',
        output: 'Safe reply.\n\n<script>globalThis.compromised = true</script>'
      }
    ]);
    const reasoning = view.container.querySelector<HTMLDetailsElement>('.stream-reasoning');
    if (reasoning === null) throw new Error('reasoning event was not rendered');
    await fireEvent.click(reasoning.querySelector('summary') as HTMLElement);

    expect(view.container.querySelector('script')).toBeNull();
    expect(view.container.querySelector('a')).toBeNull();
    expect(screen.getByText('Safe reply.')).toBeTruthy();
    expect(screen.getByText(/external target/)).toBeTruthy();
  });

  it('reports bounded render diagnostics without exposing stream text', () => {
    const onDebug = vi.fn();
    render(ProviderFeed, { ...feedProps(FIRST_FLOW), onDebug });

    expect(onDebug).toHaveBeenLastCalledWith({
      type: 'client_debug',
      event: 'provider_feed_rendered',
      output_kind: 'draft',
      item_count: 5,
      text_characters: 81
    });
    expect(JSON.stringify(onDebug.mock.calls)).not.toContain('Checking the available facts');
  });

  it('shows exact bounded failure reasons and distinguishes cancellation', () => {
    const view = renderFeed([
      {
        phase: 'request_failed',
        flow_id: 'failed-flow',
        output_kind: 'draft',
        model: 'model-a',
        error_type: 'RemoteServiceError',
        error_message: 'AIGate returned HTTP 503'
      },
      {
        phase: 'request_cancelled',
        flow_id: 'cancelled-flow',
        output_kind: 'draft',
        model: 'model-a'
      }
    ]);

    expect(screen.getByText(/AIGate returned HTTP 503/)).toBeTruthy();
    expect(screen.getByText('request cancelled')).toBeTruthy();
    expect(view.container.querySelectorAll('.stream-status.failed')).toHaveLength(1);
    expect(view.container.querySelectorAll('.stream-status.cancelled')).toHaveLength(1);
  });
});

function renderFeed(activity: ProviderActivity[]) {
  return render(ProviderFeed, feedProps(activity));
}

function feedProps(activity: ProviderActivity[]) {
  return { kind: 'draft' as const, activity, activeModel: 'model-a', fallbackOutput: 'Waiting' };
}

function requireFeed(container: HTMLElement): HTMLElement {
  const feed = container.querySelector<HTMLElement>('.provider-feed');
  if (feed === null) throw new Error('provider feed was not rendered');
  return feed;
}
