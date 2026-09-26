import { QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { createQueryClient } from './api/query-client'
import { createAppRouter } from './router'

import './index.css'

const queryClient = createQueryClient()
const router = createAppRouter()

const rootElement = document.getElementById('root')
if (!rootElement) {
  throw new Error('index.html has no #root element')
}

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
