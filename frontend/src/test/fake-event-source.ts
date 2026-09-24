/**
 * A stand-in `EventSource` for tests that need named events, not just the
 * connect/error lifecycle `use-event-stream.test.ts` already covers.
 *
 * jsdom has no `EventSource` at all, which is why `useEventStream` guards on
 * `typeof EventSource === 'undefined'` and every test that needs one installs
 * this with `vi.stubGlobal('EventSource', FakeEventSource)` first. `emit`
 * dispatches to whichever `addEventListener(type, ...)` calls are live,
 * exactly the way the backend's `ServerSentEvent(data=..., event=event.type)`
 * (`netkeeper/web/api/events.py`) reaches the real one.
 */
export class FakeEventSource {
  static instances: FakeEventSource[] = []
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  readonly url: string
  private readonly listeners = new Map<string, Set<(event: MessageEvent<string>) => void>>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    let set = this.listeners.get(type)
    if (set === undefined) {
      set = new Set()
      this.listeners.set(type, set)
    }
    set.add(listener)
  }

  removeEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.get(type)?.delete(listener)
  }

  close(): void {
    this.closed = true
  }

  /** Simulates the backend sending a named event with this JSON-serializable payload. */
  emit(type: string, data: unknown): void {
    this.emitRaw(type, JSON.stringify(data))
  }

  /** Like {@link emit}, but with the wire text as-is — for a payload that is not valid JSON. */
  emitRaw(type: string, data: string): void {
    const event = { data } as MessageEvent<string>
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }
}

export function resetFakeEventSource(): void {
  FakeEventSource.instances = []
}
