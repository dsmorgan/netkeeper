# CLAUDE.md

Guidance for agents and contributors working in this repo. Read [docs/architecture.md](docs/architecture.md) for the design and [docs/implementation-guide.md](docs/implementation-guide.md) for the backlog before writing code.

## What this is

netkeeper keeps a professional network warm: a LinkedIn 1st-degree connection extractor (browser sidecar over CDP), a local CRM, and reconnect sequences over Gmail and LinkedIn messaging. Python 3.12 backend (FastAPI, SQLAlchemy 2, Alembic, Typer, APScheduler), React/TypeScript frontend (Vite, TanStack, Tailwind, shadcn/ui). Runs natively on macOS; container for Linux.

## Commands

```sh
make install          # uv sync --all-extras → .venv
make check            # ruff, mypy --strict, pytest (what CI runs)
make fmt              # ruff format + autofix
make serve | make dev # backend; `pnpm dev` in frontend/ alongside for the UI
make gen-client       # export OpenAPI → frontend/src/api/schema.d.ts
make changelog-draft  # preview changelog.d/ fragments
```

Frontend: `cd frontend && pnpm install && pnpm dev | build | lint | test`.

## Conventions

- **User scoping.** Every user-owned table has `user_id` (use the `UserOwned` mixin). Every query against one goes through the scoping helper; a runtime guard fails unscoped statements. Every list endpoint registers with the two-user isolation test. See spec section 5, "Multi-user readiness", and ADR 0005.
- **Extractor boundary.** Nothing under `netkeeper/linkedin/` imports `netkeeper.models` or opens a session. Job specs in, result dataclasses out; `linkedin/apply.py` is the only module that touches the database. ADR 0005, spec 9.10.
- **Browser identity.** Attach mode only, never launch a browser, never retry a checkpoint, budgets enforced between units of work, never `await` browser work inside a request handler, one activity lock per LinkedIn account. ADR 0002, spec section 9.
- **Datetimes** are stored naive UTC and returned timezone-aware through `UTCDateTime`. Never store a naive local time.
- **Migrations** are linear and PostgreSQL-portable (no SQLite-only constructs). The migration diff test runs on both databases.
- **API modules** live in `netkeeper/web/api/<name>.py` and expose `router`; the app factory discovers them, so adding an endpoint never edits `app.py`.
- **Config** is TOML (`config.py` dataclasses); runtime-adjustable values live in `settings_kv`, seeded from config on first start.
- **Secrets** go to Keychain via `keyring` under `netkeeper/<user_id>/<name>`, never into the database or logs. Message bodies, cookies, and tokens never appear in logs.
- **Tests are offline.** No network, no linkedin.com, no Gmail, no Anthropic. Browser-touching code has an opt-in smoke suite (`NETKEEPER_BROWSER_TESTS=1`). Fixtures captured from LinkedIn are sanitized: fake names, fake URNs, no real emails or phones.
- **Naming.** The reference workflow's mailing tool is never named in code, docs, or tests. Export preset is `nine-column`.
- **Style.** ruff and mypy `--strict` are the arbiters. Type everything. Prefer plain dataclasses and Pydantic models over dicts across module boundaries. Log with the module logger.

## Pull request process

- One implementation-guide item per PR. Branch from `origin/main`, named `p<phase>-<nn>-<slug>`. Title `[P1-04] Generic CSV importer`. Body follows the template and contains `Closes #<issue>`.
- `make check` green before pushing. Frontend PRs also `pnpm lint && pnpm build`.
- Add a `changelog.d/<issue>.<type>.md` fragment when behavior changed.
- A review agent checks every PR for bugs, test coverage against the item's "done when", and alignment with the spec and ADRs. The maintainer merges; agents never merge.
- Parallel lanes work in sibling worktrees: `git worktree add ../netkeeper-<lane>`. Never create worktrees inside the repository.

## Where things are

See the repository layout in [docs/architecture.md](docs/architecture.md#7-repository-layout). Docs: `docs/architecture.md` (design), `docs/implementation-guide.md` (backlog, checkpoints), `docs/networking-workflow.md` (the method), `docs/adr/` (decisions).
