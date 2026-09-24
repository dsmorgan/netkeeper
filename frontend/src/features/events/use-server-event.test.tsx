import { act, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { FakeEventSource, resetFakeEventSource } from '@/test/fake-event-source'

import { EventStreamContext } from './event-stream-context'
import { useServerEvent } from './use-server-event'

/** Mounts `useServerEvent` inside the context it reads, so a test can swap `source`. */
function Probe({ onEvent }: { onEvent: (data: unknown) => void }) {
  useServerEvent('run.progress', onEvent)
  return null
}

/**
 * A subscriber whose handler closes over its own state, with a button that
 * changes that state without touching `source` or `type` — for proving the
 * handler an event reaches is always built from the *current* render, not
 * the one that first subscribed. The registration effect's dependency array
 * is `[source, type]` (`use-server-event.ts`), so a state change alone never
 * reruns it; only the ref-refresh effect (`[onEvent]`) does. Reading through
 * `handler.current` rather than the `onEvent` the registration effect closed
 * over is what makes that safe.
 */
function StatefulProbe() {
  const [count, setCount] = useState(0)
  const [seenAt, setSeenAt] = useState<number | null>(null)
  useServerEvent<{ run_id: number }>('run.progress', () => setSeenAt(count))
  return (
    <>
      <p>count: {count}</p>
      <p>seenAt: {seenAt ?? 'nothing yet'}</p>
      <button type="button" onClick={() => setCount((current) => current + 1)}>
        bump
      </button>
    </>
  )
}

function withStatefulProbe(source: EventSource | null) {
  return (
    <EventStreamContext value={{ status: source === null ? 'disconnected' : 'connected', source }}>
      <StatefulProbe />
    </EventStreamContext>
  )
}

function withSource(source: EventSource | null, onEvent: (data: unknown) => void) {
  return (
    <EventStreamContext value={{ status: source === null ? 'disconnected' : 'connected', source }}>
      <Probe onEvent={onEvent} />
    </EventStreamContext>
  )
}

afterEach(() => {
  resetFakeEventSource()
})

describe('useServerEvent', () => {
  it('does nothing while there is no live connection', () => {
    const onEvent = vi.fn()
    render(withSource(null, onEvent))
    expect(onEvent).not.toHaveBeenCalled()
  })

  it('calls the handler with the parsed payload of a matching event', () => {
    const source = new FakeEventSource('/api/v1/events')
    const onEvent = vi.fn()
    render(withSource(source as unknown as EventSource, onEvent))

    act(() => source.emit('run.progress', { run_id: 7, seen: 3 }))
    expect(onEvent).toHaveBeenCalledExactlyOnceWith({ run_id: 7, seen: 3 })
  })

  it('unwraps the real envelope the backend sends, not just the fake helper', () => {
    // Built by hand from Event.to_dict() (netkeeper/services/events.py), pinned by
    // tests/test_web_events.py::test_published_events_arrive_on_the_stream — not
    // routed through FakeEventSource.emit's own wrapping, so a bug shared by both
    // that wrapping and the hook's unwrap could not hide here. This is the case
    // H1 was: the hook used to treat this whole envelope as the payload, so
    // `onEvent` would have received `{type, data, ts, user_id}`, not `{run_id: 5}`.
    const source = new FakeEventSource('/api/v1/events')
    const onEvent = vi.fn()
    render(withSource(source as unknown as EventSource, onEvent))

    const wire = JSON.stringify({
      type: 'run.progress',
      data: { run_id: 5, visited: 2, harvested: 1, not_found: 0, planned: 10 },
      user_id: 1,
      ts: '2026-09-24T10:00:00+00:00',
    })
    act(() => source.emitRaw('run.progress', wire))

    expect(onEvent).toHaveBeenCalledExactlyOnceWith({
      run_id: 5,
      visited: 2,
      harvested: 1,
      not_found: 0,
      planned: 10,
    })
  })

  it('ignores an event of a different type', () => {
    const source = new FakeEventSource('/api/v1/events')
    const onEvent = vi.fn()
    render(withSource(source as unknown as EventSource, onEvent))

    act(() => source.emit('run.finished', { run_id: 7 }))
    expect(onEvent).not.toHaveBeenCalled()
  })

  it('swallows a malformed payload instead of throwing', () => {
    const source = new FakeEventSource('/api/v1/events')
    const onEvent = vi.fn()
    render(withSource(source as unknown as EventSource, onEvent))

    expect(() => act(() => source.emitRaw('run.progress', 'not json'))).not.toThrow()
    expect(onEvent).not.toHaveBeenCalled()
  })

  it('stops listening once unmounted', () => {
    const source = new FakeEventSource('/api/v1/events')
    const onEvent = vi.fn()
    const { unmount } = render(withSource(source as unknown as EventSource, onEvent))

    unmount()
    act(() => source.emit('run.progress', { run_id: 1 }))
    expect(onEvent).not.toHaveBeenCalled()
  })

  it('re-subscribes to the new connection after a reconnect', () => {
    const first = new FakeEventSource('/api/v1/events')
    const onEvent = vi.fn()
    const { rerender } = render(withSource(first as unknown as EventSource, onEvent))

    const second = new FakeEventSource('/api/v1/events')
    rerender(withSource(second as unknown as EventSource, onEvent))

    act(() => second.emit('run.progress', { run_id: 9 }))
    expect(onEvent).toHaveBeenCalledExactlyOnceWith({ run_id: 9 })

    // The old connection no longer reaches the handler once it has been replaced.
    onEvent.mockClear()
    act(() => first.emit('run.progress', { run_id: 1 }))
    expect(onEvent).not.toHaveBeenCalled()
  })

  it('a handler that closes over state sees the render it came from, not the one that subscribed', () => {
    const source = new FakeEventSource('/api/v1/events')
    render(withStatefulProbe(source as unknown as EventSource))

    // Neither click touches `source`, so the listener-registration effect
    // never reruns; only the ref-refresh effect does.
    fireEvent.click(screen.getByRole('button', { name: 'bump' }))
    fireEvent.click(screen.getByRole('button', { name: 'bump' }))
    expect(screen.getByText('count: 2')).toBeInTheDocument()

    act(() => source.emit('run.progress', { run_id: 5 }))

    expect(screen.getByText('seenAt: 2')).toBeInTheDocument()
  })
})
