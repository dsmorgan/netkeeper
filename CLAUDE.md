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
- **Extractor boundary.** Nothing under `netkeeper/linkedin/` imports `netkeeper.models` or opens a session. Job specs in, result dataclasses out. The module that maps results onto rows lives under `netkeeper/crm/`, not under `linkedin/` — a module at `linkedin/apply.py` opening a session would break the rule in the same sentence. P1-03's archive importer is `crm/archive.py`; the extractor's is `crm/apply.py`. ADR 0005, spec 9.10.
- **Browser identity.** Attach mode only, never launch a browser, never retry a checkpoint, budgets enforced between units of work, never `await` browser work inside a request handler, one activity lock per LinkedIn account. ADR 0002, spec section 9.
- **Datetimes** are stored naive UTC and returned timezone-aware through `UTCDateTime`. Never store a naive local time.
- **Writer sessions.** Any session that reads and then writes must be marked for write (`session_scope(factory, write=True)`, or the request dependency, which marks non-GET requests). On SQLite that emits `BEGIN IMMEDIATE`; an unmarked read-then-write can fail instantly with "database is locked" and `busy_timeout` does not help. Scheduler jobs and CLI commands are writers too. A `POST` that only reads — a query whose input does not fit a query string — opts back out with `@read_only` under its route decorator, so it never takes the write lock.
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
- Rebase onto `origin/main` before asking for review, especially when the branch touches shared files (`tests/conftest.py`, `tests/test_migrations.py`, `pyproject.toml`). A conflicting PR gets no CI run.
- Add a `changelog.d/<issue>.<type>.md` fragment when behavior changed.
- A review agent checks every PR for bugs, test coverage against the item's "done when", and alignment with the spec and ADRs. The maintainer merges; agents never merge.
- Parallel lanes work in sibling worktrees: `git worktree add ../netkeeper-<lane>`. Never create worktrees inside the repository.

## Where things are

See the repository layout in [docs/architecture.md](docs/architecture.md#7-repository-layout). Docs: `docs/architecture.md` (design), `docs/implementation-guide.md` (backlog, checkpoints), `docs/networking-workflow.md` (the method), `docs/adr/` (decisions).
