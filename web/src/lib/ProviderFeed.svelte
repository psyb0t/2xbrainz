<script lang="ts">
  import { afterUpdate } from 'svelte';
  import { Streamdown } from 'svelte-streamdown';
  import type { FrontendDebugMessage, ProviderActivity, ProviderOutputKind } from './contracts';

  const FOLLOW_DISTANCE_PX = 48;
  const STREAMDOWN_CONTROLS = { code: false, mermaid: false, table: false };

  export let kind: ProviderOutputKind;
  export let activity: ProviderActivity[];
  export let activeModel: string;
  export let fallbackOutput: string;
  export let onDebug: (event: FrontendDebugMessage) => void = () => undefined;

  let scroller: HTMLElement;
  let shouldFollow = true;
  let lastDebugSignature = '';

  interface StreamItemBase {
    id: string;
    flowId: string;
    phase: string;
  }

  interface StatusItem extends StreamItemBase {
    type: 'status';
    model: string;
    active: boolean;
    message?: string;
  }

  interface ReasoningItem extends StreamItemBase {
    type: 'reasoning';
    content: string;
  }

  interface ToolItem extends StreamItemBase {
    type: 'tool';
    callId: string;
    tool: string;
    input?: unknown;
    result?: string;
  }

  interface OutputItem extends StreamItemBase {
    type: 'output';
    content: string;
    streaming: boolean;
  }

  type StreamItem = StatusItem | ReasoningItem | ToolItem | OutputItem;

  $: items = streamItems(kind, activity, activeModel, fallbackOutput);

  afterUpdate(() => {
    if (shouldFollow && scroller) scroller.scrollTop = scroller.scrollHeight;
    const signature = debugSignature(items);
    if (signature === lastDebugSignature) return;
    lastDebugSignature = signature;
    onDebug({
      type: 'client_debug',
      event: 'provider_feed_rendered',
      output_kind: kind,
      item_count: items.length,
      text_characters: textCharacters(items)
    });
  });

  function trackScroll(): void {
    const remaining = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
    shouldFollow = remaining <= FOLLOW_DISTANCE_PX;
  }

  function streamItems(
    outputKind: string,
    events: ProviderActivity[],
    model: string,
    fallback: string
  ): StreamItem[] {
    const result: StreamItem[] = [];
    let sequence = 0;

    for (const event of events) {
      if (event.output_kind !== outputKind || !event.flow_id) continue;
      sequence += 1;
      const flowId = event.flow_id;
      const phase = event.phase;

      if (
        phase === 'request_started' ||
        phase === 'followup_started' ||
        phase === 'tool_call_retry_started'
      ) {
        settleStatus(result, flowId);
        result.push({
          type: 'status',
          id: `${flowId}:status:${sequence}`,
          flowId,
          phase,
          model: event.model ?? model,
          active: true,
          message: phase === 'tool_call_retry_started' ? event.error_message : undefined
        });
        continue;
      }

      settleStatus(result, flowId);
      if (isTerminalPhase(phase)) settleOutput(result, flowId, phase !== 'request_completed');
      if (phase === 'stream_completed') continue;
      if (event.reasoning !== undefined) {
        result.push({
          type: 'reasoning',
          id: `${flowId}:reasoning:${sequence}`,
          flowId,
          phase,
          content: event.reasoning
        });
        continue;
      }

      if (isToolPhase(phase)) {
        const callId = event.tool_call_id ?? `${event.tool ?? 'tool'}:${sequence}`;
        const existingIndex = adjacentToolIndex(result, flowId, event.tool_call_id, event.tool);
        if (existingIndex !== undefined) {
          const existing = result[existingIndex];
          if (existing.type === 'tool') {
            result[existingIndex] = {
              ...existing,
              phase,
              input: event.tool_input ?? existing.input,
              result: event.tool_result ?? existing.result
            };
          }
          continue;
        }
        result.push({
          type: 'tool',
          id: `${flowId}:tool:${callId}`,
          flowId,
          phase,
          callId,
          tool: event.tool ?? 'tool',
          input: event.tool_input,
          result: event.tool_result
        });
        continue;
      }

      if (event.output !== undefined) {
        const existingIndex = outputIndex(result, flowId);
        if (existingIndex !== undefined) {
          const existing = result[existingIndex];
          if (existing.type !== 'output') continue;
          result[existingIndex] = {
            ...existing,
            phase,
            content: event.output,
            streaming: phase === 'output_streaming'
          };
          continue;
        }
        result.push({
          type: 'output',
          id: `${flowId}:output`,
          flowId,
          phase,
          content: event.output,
          streaming: phase === 'output_streaming'
        });
        continue;
      }

      if (phase === 'request_failed' || phase === 'request_cancelled') {
        result.push({
          type: 'status',
          id: `${flowId}:status:${sequence}`,
          flowId,
          phase,
          model: event.model ?? model,
          active: false,
          message: event.error_message
        });
      }
    }

    if (result.length > 0) return result;
    return [
      {
        type: 'output',
        id: 'waiting:output',
        flowId: 'waiting',
        phase: 'waiting',
        content: fallback,
        streaming: false
      }
    ];
  }

  function settleStatus(items: StreamItem[], flowId: string): void {
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (item.flowId !== flowId || item.type !== 'status') continue;
      if (item.active) item.active = false;
      return;
    }
  }

  function isToolPhase(phase: string): boolean {
    return phase === 'tool_started' || phase === 'tool_completed' || phase === 'tool_failed';
  }

  function isTerminalPhase(phase: string): boolean {
    return (
      phase === 'request_completed' || phase === 'request_failed' || phase === 'request_cancelled'
    );
  }

  function outputIndex(items: StreamItem[], flowId: string): number | undefined {
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (item.flowId === flowId && item.type === 'output') return index;
    }
    return undefined;
  }

  function settleOutput(items: StreamItem[], flowId: string, discardEmpty: boolean): void {
    const index = outputIndex(items, flowId);
    if (index === undefined) return;
    const item = items[index];
    if (item.type !== 'output') return;
    if (discardEmpty && item.content.length === 0) {
      items.splice(index, 1);
      return;
    }
    item.streaming = false;
  }

  function adjacentToolIndex(
    items: StreamItem[],
    flowId: string,
    callId: string | undefined,
    tool: string | undefined
  ): number | undefined {
    const index = items.length - 1;
    const item = items[index];
    if (item?.type !== 'tool' || item.flowId !== flowId || item.phase !== 'tool_started') {
      return undefined;
    }
    if (callId !== undefined) return item.callId === callId ? index : undefined;
    return item.tool === (tool ?? 'tool') ? index : undefined;
  }

  function phaseLabel(phase: string): string {
    return phase.replaceAll('_', ' ');
  }

  function debugSignature(stream: StreamItem[]): string {
    return stream
      .map((item) => {
        if (item.type === 'reasoning' || item.type === 'output') {
          return `${item.id}:${item.phase}:${item.content.length}`;
        }
        if (item.type === 'tool') {
          return `${item.id}:${item.phase}:${item.result?.length ?? 0}`;
        }
        return `${item.id}:${item.phase}:${item.active}`;
      })
      .join('|');
  }

  function textCharacters(stream: StreamItem[]): number {
    return stream.reduce((total, item) => {
      if (item.type === 'reasoning' || item.type === 'output') return total + item.content.length;
      if (item.type === 'tool') return total + (item.result?.length ?? 0);
      return total;
    }, 0);
  }
</script>

<div
  class="provider-feed"
  bind:this={scroller}
  onscroll={trackScroll}
  role="log"
  aria-live="polite"
  aria-relevant="additions text"
>
  {#each items as item (item.id)}
    {#if item.type === 'status'}
      <div
        class:active={item.active}
        class:failed={item.phase === 'request_failed'}
        class:cancelled={item.phase === 'request_cancelled'}
        class="stream-status stream-item"
      >
        <span aria-hidden="true"></span>
        <strong>{phaseLabel(item.phase)}</strong>
        <small>{item.model}{item.message ? ` · ${item.message}` : ''}</small>
      </div>
    {:else if item.type === 'reasoning'}
      <details class="stream-event stream-reasoning stream-item">
        <summary><span>Thinking</span><small>{phaseLabel(item.phase)}</small></summary>
        <div class="stream-event-content">
          <Streamdown
            content={item.content || 'Provider has not exposed reasoning yet.'}
            allowedImagePrefixes={[]}
            allowedLinkPrefixes={[]}
            renderHtml={false}
            controls={STREAMDOWN_CONTROLS}
            animation={{
              enabled: item.phase === 'reasoning_streaming',
              animateOnMount: false,
              tokenize: 'word',
              duration: 120,
              type: 'fade'
            }}
          />
        </div>
      </details>
    {:else if item.type === 'tool'}
      <details
        class:failed={item.phase === 'tool_failed'}
        class="stream-event stream-tool stream-item"
      >
        <summary><span>{item.tool}</span><small>{phaseLabel(item.phase)}</small></summary>
        <div class="stream-event-content">
          {#if item.input !== undefined}<pre>{JSON.stringify(item.input, null, 2)}</pre>{/if}
          {#if item.result !== undefined}<pre>{item.result}</pre>{/if}
        </div>
      </details>
    {:else}
      <div
        class:streaming={item.streaming}
        class="stream-response stream-item"
        aria-busy={item.streaming}
      >
        <Streamdown
          content={item.content || (item.streaming ? 'Generating…' : 'Waiting for output…')}
          allowedImagePrefixes={[]}
          allowedLinkPrefixes={[]}
          renderHtml={false}
          controls={STREAMDOWN_CONTROLS}
          animation={{
            enabled: item.streaming,
            animateOnMount: false,
            tokenize: 'word',
            duration: 120,
            type: 'fade'
          }}
        />
      </div>
    {/if}
  {/each}
</div>
