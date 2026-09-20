import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

import { installOfflineFetch, resetFetch } from './fetch'

// Tests are offline (CLAUDE.md): anything that reaches the network fails
// unless a test routes it through `mockFetch`.
installOfflineFetch()

// The router restores scroll position on navigation; jsdom only logs "not implemented".
Object.defineProperty(window, 'scrollTo', { value: () => undefined, writable: true })

afterEach(() => {
  cleanup()
  resetFetch()
})
