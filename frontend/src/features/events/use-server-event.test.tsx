import { act, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { FakeEventSource, resetFakeEventSource } from '@/test/fake-event-source'

import { EventStreamContext } from './event-stream-context'
import { useServerEvent } from './use-server-event'

/** Mounts `useServerEvent` inside the context it reads, so a test can swap `source`. */
function Probe({ onEvent }: { onEvent: (data: unknown) => void }) {
  useServerEvent('run.progress', onEvent)
  return null
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
})
