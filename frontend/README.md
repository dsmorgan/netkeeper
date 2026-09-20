# netkeeper frontend

React 19, TypeScript, Vite, TanStack Router and Query, Tailwind CSS v4, and shadcn/ui (Base UI primitives, `base-nova` preset, components copied into `src/components/ui/`). In development Vite proxies `/api` to `http://127.0.0.1:8000`; in production the backend serves `dist/` itself, so the API client uses an empty `baseUrl`.

## Commands

| Command                          | What it does                                                                                             |
| -------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `pnpm install --frozen-lockfile` | Install from `pnpm-lock.yaml`.                                                                           |
| `pnpm dev`                       | Dev server on http://localhost:5173 with the `/api` proxy. Run `make dev` at the repo root alongside it. |
| `pnpm build`                     | `tsc -b` (type check) then `vite build` into `dist/`.                                                    |
| `pnpm lint`                      | eslint (typescript-eslint, react-hooks, react-refresh) then `tsc -b`.                                    |
| `pnpm test`                      | vitest with jsdom and Testing Library. Tests are offline.                                                |
| `pnpm gen`                       | Regenerate `src/api/schema.d.ts` from `openapi.json`.                                                    |
| `pnpm format`                    | prettier. `pnpm format:check` verifies without writing.                                                  |

## Generated client

`src/api/schema.d.ts` is generated from `openapi.json` by `pnpm gen` and committed; never edit it by hand. `src/api/client.ts` wraps it with `openapi-fetch` and adds the `X-Netkeeper-Client: 1` header the backend's CSRF rule requires.

Until P0-04 merges, `openapi.json` is a hand-written stub with `GET /api/v1/health` and `GET /api/v1/me`. P0-04 replaces the stub with `make gen-client` at the repo root, which exports the schema from FastAPI and reruns `pnpm gen`.

## Where things are

- `src/routes/`: TanStack file-based routes, one file per page; `__root.tsx` is the shell. The router plugin regenerates `src/routeTree.gen.ts` on `dev`, `build`, and `test`; it is committed so a clean checkout type-checks.
- `src/components/layout/`: sidebar, top bar, backend-health indicator. `nav.ts` is the navigation list from spec section 14.3.
- `src/components/ui/`: shadcn components. Edit them freely; they are ours.
- `src/features/<area>/`: page components and hooks per area (`dashboard`, `settings`, `events`).
- `src/api/`: the client and the generated schema.
- `src/test/`: vitest setup, the offline `fetch` stub, and `renderApp`, which mounts the real route tree at a path.

To add a page: create `src/routes/<name>.tsx` exporting `Route = createFileRoute('/<name>')({ component })`, then add the entry to `NAV_ITEMS` in `src/components/layout/nav.ts`.
