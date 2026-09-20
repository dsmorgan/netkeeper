# 0005. Carry a user boundary from the first migration

Date: 2026-09-20

## Status

Accepted

## Context

netkeeper v1 serves one person on one machine, and login, hosting, and multi-tenant operation are out of scope. But the tool could plausibly become self-hosted for a small group or a hosted service once the local form has matured. Adding a user dimension to an existing schema means touching every table, every query, every scheduler job, and every secret key, and it is the kind of change that stalls a project. The extractor has an additional wrinkle: in a hosted deployment the browser is on the user's machine and the database is not.

## Decision

From the first migration:

- A `user` table exists and every user-owned table carries a non-null, indexed `user_id`; every unique constraint includes it.
- Routes resolve the current user through one dependency; services take the user explicitly; one query helper applies the scope. A two-user isolation test covers every list endpoint.
- Authentication is a provider interface with one v1 implementation that returns the only user.
- Per-user resources (`mailbox`, `linkedin_account`, `settings_kv`, secrets, exports, backups) are keyed by user.
- The browser activity lock, budgets, and heat belong to a `linkedin_account`, not the process.
- Code under `linkedin/` never imports ORM models or opens a session. It exchanges job specs, results, and progress events with the core through explicit dataclasses.
- Migrations use only constructs that render the same on SQLite and PostgreSQL, and CI runs the migration test against both.

Login, sharing, fairness across tenants, a verified OAuth app, encryption per tenant, an admin surface, and billing stay deferred, and whether the future is self-hosted or hosted is decided by a later ADR.

## Consequences

- Every new table and list endpoint costs a column and an isolation test. That is a few minutes each and the discipline is enforced by tests, not by review.
- The extractor is testable with fixtures and no database, and a future `netkeeper agent` reuses it unchanged.
- v1 carries a little structure it does not use. The user row is invisible in the UI.
- Pull requests that add an unscoped query, or an import from `linkedin/` into `models/`, are declined without a superseding ADR.
