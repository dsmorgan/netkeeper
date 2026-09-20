import { vi } from 'vitest'

type FetchHandler = (request: Request) => Response | Promise<Response>

const offline: FetchHandler = () => {
  throw new TypeError('network disabled in tests')
}

let handler: FetchHandler = offline

/**
 * Node's `Request` rejects relative URLs, but the app uses them: the client's
 * `baseUrl` is empty and a browser resolves against the page origin. Resolve
 * against jsdom's location instead so the same code runs under vitest.
 */
class RelativeRequest extends Request {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    super(typeof input === 'string' ? new URL(input, window.location.href) : input, init)
  }
}

/**
 * Installs an offline `fetch` and the relative-URL `Request`. Must run before
 * the API client module is imported: `openapi-fetch` binds both globals when
 * `createClient` runs, so the vitest setup file is the place.
 */
export function installOfflineFetch(): void {
  vi.stubGlobal('Request', RelativeRequest)
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) =>
    Promise.resolve().then(() =>
      handler(input instanceof Request ? input : new RelativeRequest(input, init)),
    ),
  )
}

/** Routes every fetch in the current test to `next`. Reset after each test. */
export function mockFetch(next: FetchHandler): void {
  handler = next
}

export function resetFetch(): void {
  handler = offline
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}
