import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { backoffDelay, useEventStream } from './use-event-stream'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  readonly url: string

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  close(): void {
    this.closed = true
  }
}

afterEach(() => {
  FakeEventSource.instances = []
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('backoffDelay', () => {
  it('doubles from one second and caps at thirty', () => {
    expect([0, 1, 2, 3, 4, 5, 10].map(backoffDelay)).toEqual([
      1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000,
    ])
  })
})

describe('useEventStream', () => {
  it('stays disconnected when EventSource does not exist', () => {
    const { result } = renderHook(() => useEventStream('/api/v1/events'))
    expect(result.current.status).toBe('disconnected')
    expect(result.current.source).toBeNull()
  })

  it('reports open and error, and reconnects with growing delays', () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', FakeEventSource)

    const { result, unmount } = renderHook(() => useEventStream('/api/v1/events'))
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0]?.url).toBe('/api/v1/events')
    expect(result.current.status).toBe('disconnected')
    // The instance is exposed as soon as it is created, before `open` fires,
    // so a listener attached early still catches whatever arrives first.
    expect(result.current.source).toBe(FakeEventSource.instances[0])

    act(() => FakeEventSource.instances[0]?.onopen?.())
    expect(result.current.status).toBe('connected')

    // First failure: the hook closes the source itself and waits 1s.
    act(() => FakeEventSource.instances[0]?.onerror?.())
    expect(result.current.status).toBe('disconnected')
    expect(result.current.source).toBeNull()
    expect(FakeEventSource.instances[0]?.closed).toBe(true)
    act(() => vi.advanceTimersByTime(999))
    expect(FakeEventSource.instances).toHaveLength(1)
    act(() => vi.advanceTimersByTime(1))
    expect(FakeEventSource.instances).toHaveLength(2)
    expect(result.current.source).toBe(FakeEventSource.instances[1])

    // Second failure in a row: 2s.
    act(() => FakeEventSource.instances[1]?.onerror?.())
    act(() => vi.advanceTimersByTime(1_999))
    expect(FakeEventSource.instances).toHaveLength(2)
    act(() => vi.advanceTimersByTime(1))
    expect(FakeEventSource.instances).toHaveLength(3)

    // A successful open resets the delay to 1s.
    act(() => FakeEventSource.instances[2]?.onopen?.())
    expect(result.current.status).toBe('connected')
    act(() => FakeEventSource.instances[2]?.onerror?.())
    act(() => vi.advanceTimersByTime(1_000))
    expect(FakeEventSource.instances).toHaveLength(4)

    // Unmount closes the live source and cancels the pending retry.
    act(() => FakeEventSource.instances[3]?.onerror?.())
    unmount()
    act(() => vi.advanceTimersByTime(60_000))
    expect(FakeEventSource.instances).toHaveLength(4)
  })
})
