# netkeeper architecture spec

| | |
|---|---|
| Status | Draft v0.1 |
| Date | 2026-09-20 |
| Name | `netkeeper`, at [github.com/dsmorgan/netkeeper](https://github.com/dsmorgan/netkeeper) |
| Related | [networking-workflow.md](networking-workflow.md), the method this tool automates; [implementation-guide.md](implementation-guide.md), the backlog and checkpoints; [adr/](adr/), the reasoning behind contested decisions |

## 1. Summary

netkeeper is a local-first application that keeps your professional network warm. It automates the reconnect method in [networking-workflow.md](networking-workflow.md), which today takes a LinkedIn data export, a spreadsheet, a profile-scraping tool, and a bulk-mail tool. The method comes from hellophello's job-search networking program; that name is an acknowledgement, and it appears nowhere in the implementation. netkeeper does the whole thing in one tool with three parts:

1. **Extract.** Pull your 1st-degree LinkedIn connections and their contact info through a browser sidecar attached to the Chrome you already use, with human-like pacing and hard daily budgets.
2. **Organize.** Store, deduplicate, triage, tag, and segment those contacts in a local CRM with CSV import and export and periodic re-sync against LinkedIn.
3. **Reach out.** Run multi-step outreach sequences over Gmail and LinkedIn messaging, with draft, send, and prefill modes, automatic reply detection, and follow-up suppression.

It runs natively on macOS as one Python process (FastAPI plus a scheduler) that serves a React frontend on `127.0.0.1`. A container image exists for Linux hosts. The LinkedIn steps require Chrome running on the same machine.

v1 serves one person on one machine. The schema and service boundaries carry a user boundary from the first migration so that a self-hosted or hosted multi-user deployment is an addition later, not a rewrite. See [Multi-user readiness](#multi-user-readiness).

## 2. Goals and non-goals

### Goals

- Automate all five stages of the reference workflow end to end, so a weekly outreach cycle takes minutes of your attention, not hours.
- Never put your LinkedIn account at more risk than the profile-scraping tools the reference workflow already recommends. Default budgets are conservative and every protection is on by default.
- Keep all data on your machine. No hosted service, no telemetry.
- Make every automated action reviewable before it happens and traceable after it happens.
- Reuse the browser-sidecar patterns that igtracker proved against Instagram: CDP attach, in-page fetch, lognormal pacing, daily budgets, heat-based backoff, cooperative cancel, resumable runs.
- Keep a user boundary in the schema and the service layer from day one, so multi-user deployment later is an addition rather than a migration of every table.

### Non-goals for v1

- Login, hosting, or serving more than one person. v1 runs for one person, one LinkedIn account, one Gmail mailbox. The design still carries `user_id` everywhere (see [Multi-user readiness](#multi-user-readiness)); what is deferred is the operational work, not the data model.
- 2nd- and 3rd-degree connections, connection requests, InMail, or Sales Navigator. The extractor keeps a `degree` field so this can be added later, but v1 only reads people you are already connected to.
- Email open tracking. It needs a public pixel endpoint and hurts deliverability. Reply rate is the metric that matters in the reference workflow anyway.
- Cold outreach to people outside your network. The tool is built for reconnecting, and its guards assume that.
- A general-purpose CRM (deals, pipelines, companies as first-class objects).

## 3. Mapping to the reference workflow

The table shows each manual step in [networking-workflow.md](networking-workflow.md) and what netkeeper does instead. Stage numbers refer to that document.

| Workflow step | Manual today | netkeeper |
|---|---|---|
| 1.1 Export connections | Request the LinkedIn data archive, wait up to 24 h, unzip, open CSV | **Import** the official archive (zero-risk seed) and/or **Connections sync** through the sidecar (minutes, no wait) |
| 1.2 Mark people you have met | Add a column in a spreadsheet | **Triage** screen: keyboard-driven met / not met / skip, with evidence (message history, connected-on date, notes) |
| 1.3 Keep the validated list | Sort, delete rows, save a copy | `met` is an enum field (`unknown`, `met`, `not_met`, `skip`); nothing is deleted. A built-in smart list "Validated" holds `met = met` |
| 2 Enrich contact info | Paste URLs into a scraping tool, 100 at a time, clean the CSV | **Enrichment** job visits profiles under a daily budget, harvests email, phone, location, and current position in one visit, prioritized by who you plan to contact |
| 2 Keep the essential fields | Delete columns in a spreadsheet | Not needed. Export presets produce any column layout, including the nine-column layout in Appendix A |
| 3.1 Build a list of 100, drop no-email | Manual list | Static and smart **lists**; `has_email` is a filter |
| 3.2 Tag by role | The mailing tool tags by title on import | **Auto-tag rules** (regex on title and company), optional LLM classification |
| 3.3 Write the message | Template in the mailing tool | **Templates** with merge fields and lint |
| 3.4 Test send | Send to yourself | **Test send** is required before activation |
| 3.5 Have it reviewed | Ask a coach | **Review gate** with rendered previews of real contacts |
| 4 Folder for campaign replies | Create a mail folder by hand | Gmail **label** per campaign, applied to outbound and detected replies |
| 4 Track who responded | Notes and calendar reminders | **Reply detection** on Gmail threads and the LinkedIn inbox; interaction timeline per contact; follow-up reminders |
| 5.1 Remove responders from the follow-up list | Spreadsheet surgery | Automatic suppression when an enrollment reaches `replied` |
| 5.2 Follow up a week later, in the same conversation | A second campaign built by hand | **Sequences**: step 2 fires N days after step 1 unless a reply was detected, threaded under the first email |
| 5.3 Send Tuesday to Thursday | Remember to | **Send windows** and daily caps, with the next fire shown on the dashboard |
| 5 Weekly cadence, new batch or follow-up | Remember to | Dashboard shows what fired and what fires next; overlapping campaigns are supported |
| 5 Keep enriching 100 per day | Scraping tool batches | Enrichment runs on a schedule inside the budget; new connections arrive through incremental sync |

## 4. Requirements

### 4.1 Functional

**Extractor**

- F1. Attach to a running Chrome over the Chrome DevTools Protocol (CDP) and use the LinkedIn session already there. Never launch a second browser identity.
- F2. Enumerate all 1st-degree connections with name, public profile URL, stable profile URN, headline, and connected-on date. Support a full sync and a cheap incremental sync (newest first, stop at first known).
- F3. For a prioritized subset of contacts, visit the profile and harvest contact info (emails, phones, websites, social handles), location, and current positions in a single visit.
- F4. Pace every action with lognormal delays, human-like scrolling, active-hours windows, a warm-up ramp for new installs, and per-day and per-week budgets by action class.
- F5. Classify failures into throttled, checkpoint, logged out, not found, and endpoint changed; back off, stop, or skip accordingly. Never retry a checkpoint.
- F6. Record every run with counts, budget spend, browser mode, and notes. Runs are cancellable and resumable.
- F7. Detect changes between syncs: new connections, removed connections (debounced), and title, company, or location changes. Keep a history per contact.
- F8. Import LinkedIn's official data archive (`Connections.csv`, `messages.csv`) as a seed and as a triage signal.

**CRM**

- F9. Contacts with multiple emails and phones, a preferred first name for personalization, notes, tags, and an interaction timeline.
- F10. Triage workflow to mark contacts as met, not met, or skipped, with keyboard navigation and evidence panels.
- F11. Manual tags and rule-based auto-tags. Auto-tags never overwrite manual ones and are visibly distinct.
- F12. Static lists and smart lists (saved filters). A filter language that covers tags, fields, met status, last contacted, campaign membership, and data completeness.
- F13. CSV import with a column-mapping UI, saved presets (LinkedIn archive, LinkedHelper CSV, generic nine-column), duplicate detection with a review step, and provenance per imported field.
- F14. Export to CSV, JSON, and vCard with saved column presets, including the nine-column layout in Appendix A.
- F15. `do_not_contact` flag that every send path honors.

**Campaigns**

- F16. Templates per channel (email, LinkedIn) with Jinja merge fields for contact fields and your own profile (website, scheduling link, signature).
- F17. Campaigns are sequences of steps. Each step has a channel, a template, a delay from the previous step, a mode (`draft`, `send`, `prefill`, `auto_send`), and a condition (default: only if no reply yet).
- F18. Enrollment from a list or filter, with guards: no email or no LinkedIn URL for the channel, `do_not_contact`, already enrolled in an active campaign, contacted within the last N days, bounced address.
- F19. Review gate: a campaign cannot activate until you approve rendered previews of a sample and send a test to yourself.
- F20. Scheduler that fires due steps inside a send window, respects per-mailbox and per-campaign daily caps, and spaces sends with jitter.
- F21. Gmail integration through the Gmail API with OAuth: create drafts, send, thread follow-ups into the original conversation, apply a label per campaign, detect replies and bounces.
- F22. LinkedIn messaging: prefill the compose box in Chrome for you to send (default), or send automatically under a separate budget (opt-in).
- F23. Reply detection on both channels moves the enrollment to `replied`, stops remaining steps, and logs an inbound interaction.
- F24. Dashboard: what fired, what fires next, reply rate per campaign and step, budget spend, browser and mailbox health.

**Optional modules**

- F25. LLM assistance (off unless a key is configured): personalized opening line, title classification, profile summary for call prep, triage suggestions.
- F26. Push enriched contacts to Google Contacts (People API) and export vCards for macOS Contacts.

### 4.2 Non-functional

- N1. macOS 14+ is the primary platform. Linux via container is supported for everything; the LinkedIn steps need Chrome on the same host as the backend.
- N2. Chrome (or any Chromium browser) is the only supported browser for LinkedIn steps. Firefox and Safari have no CDP.
- N3. All data lives in one SQLite file plus a secrets store in the macOS Keychain. Backups are one command.
- N4. The web UI binds `127.0.0.1` only and needs no login in local mode, but state-changing API calls are protected against cross-site requests from other local pages.
- N8. Every user-owned row carries `user_id`, every query is scoped through one helper, and a two-user isolation test covers every list endpoint. Nothing in v1 needs this; retrofitting it is what would be expensive.
- N5. Tests run offline. Browser-touching code has an opt-in smoke suite against a loopback site.
- N6. Handles a network of 10,000 contacts with sub-second list and filter responses.
- N7. Every automated outbound message is stored with the exact rendered body, timestamp, and channel identifiers, so you can answer "what did I send this person and when" forever.

## 5. Architecture overview

```mermaid
flowchart LR
  subgraph Mac["Your Mac"]
    Chrome["Google Chrome<br/>(dedicated profile, CDP :9222)<br/>logged in to LinkedIn"]
    subgraph Proc["netkeeper serve (one process)"]
      API["FastAPI<br/>/api/v1 + SSE"]
      Sched["APScheduler<br/>sync, enrich, campaign ticks, reply polls"]
      Runner["Task runner<br/>single activity lock for browser work"]
      LI["linkedin/<br/>attach, voyager, pacing, budget, heat"]
      CRM["crm/<br/>contacts, triage, tags, lists, import, export"]
      CAMP["campaigns/<br/>engine, render, gmail, li_message, replies"]
      LLM["llm/ (optional)"]
    end
    UI["React SPA<br/>served as static build"]
    DB[("SQLite<br/>~/Library/Application Support/netkeeper")]
    KC[("Keychain<br/>OAuth tokens, API keys")]
  end
  Gmail["Gmail API"]
  Anthropic["Claude API"]
  UI <--> API
  API --> CRM & CAMP & LI
  Sched --> Runner --> LI & CAMP
  LI <-->|CDP| Chrome
  Chrome <-->|linkedin.com| LinkedIn[("LinkedIn")]
  CAMP <-->|OAuth| Gmail
  LLM <--> Anthropic
  CRM & CAMP & LI --> DB
  CAMP & LLM --> KC
```

### Process model

- One process: `netkeeper serve` starts FastAPI, mounts the built frontend, starts the scheduler, and owns the task runner. A launchd agent keeps it running.
- Chrome is started separately with a dedicated profile and a debug port (`netkeeper browser launch` wraps the command). You log in to LinkedIn in that profile once. netkeeper attaches and detaches; it never owns that browser.
- Browser work is serialized behind one activity lock. Gmail work and LLM calls are not browser work and run concurrently with it.
- Long-running work never executes inside a request handler. Routes enqueue a task and return; progress streams to the UI over Server-Sent Events (SSE).

### Patterns carried over from igtracker

These are the parts of `~/code/igtracker` that transfer directly, with the reason each one exists:

| Pattern | Why it matters here |
|---|---|
| `attach` browser mode over CDP, reuse `contexts[0]`, never close the context, never write cookies back, never override the UA | LinkedIn sees one device and one fingerprint. Your own browsing is cover traffic |
| In-page `fetch` of the site's internal JSON API from an authenticated tab | Stable data, far fewer page loads than DOM scraping, real cookies and client hints for free |
| Lognormal `human_delay` with a fat tail and occasional long "distraction" | A flat uniform distribution is itself a signature |
| Per-local-day budget enforced between units of work, never mid-unit | Stopping mid-list truncates data and poisons change detection |
| Heat score that rises per soft block and decays exponentially, computed on read | Stretches cooldowns and shrinks runs while the site is pushing back |
| Terminal-vs-retryable classification (`CheckpointRequired`, `LoginRequired`, `ThrottledOut`, `ProfileNotFound`, withdrawn route) | Retrying a checkpoint burns the account; retrying a dead route burns the budget |
| Active-hours window, deferred ticks, next fire time persisted across restarts | A schedule that resets on every deploy never fires |
| Single activity lock across every browser-touching path, including UI buttons | Two CDP clients on one browser drop each other's connection |
| Never `await` browser work inside a request handler | CDP attach blocks on the tab that is waiting for the response: a deadlock |
| Tab-loss recovery: reopen in the same context, at most one reattach per run | Closing the sidecar tab is a one-click accident |
| `rehearse` against a neutral site, `simulate` against a virtual clock, `preflight` on the real host | Verification without touching the real site |
| Posture page that highlights every protection that is off | A page that only displays values presents an unhardened install as configured |

### Multi-user readiness

netkeeper v1 runs for one person on one machine. A self-hosted deployment serving a household or a small team, or a hosted service, is a plausible future once the local form has matured. The constraints below apply from the first migration because retrofitting them later means touching every table, query, and job. Everything else about multi-user is deferred and listed at the end.

- **Users are a table, not an assumption.** A `user` row is created at first start (`kind = local`). Every user-owned table has a `user_id` foreign key, `NOT NULL`, indexed, and every uniqueness constraint includes it (`tag.name` is unique per user, `contact.li_urn` is unique per user).
- **Scoping happens in one place.** Routes resolve the current user through a `CurrentUser` dependency and pass it down. Services take `user` explicitly; the query helper applies `WHERE user_id = :id`. No route or service issues an unscoped query against a user-owned table. A two-user fixture in the test suite asserts isolation for every list endpoint.
- **Authentication is a provider interface.** `AuthProvider.current_user(request)` has one v1 implementation, `LocalSingleUser`, which returns the only row. A hosted deployment adds a session or OIDC provider without changing routes.
- **Per-user resources are rows.** `mailbox` and `linkedin_account` (section 8.4) belong to a user. `settings_kv` is keyed by `(user_id, key)`. Secrets are stored under `netkeeper/<user_id>/<name>`. Exports and backups live under a per-user subdirectory.
- **Concurrency is per account, not global.** The browser activity lock belongs to a `linkedin_account`. Scheduler jobs carry `user_id`. Budgets and heat are keyed by the LinkedIn account.
- **The extractor is a separable agent.** In a hosted deployment the browser sits on the user's laptop while the server is elsewhere, so the extractor must not depend on the database. It talks to the core only through explicit contracts: job specs in (`SyncJobSpec`, `EnrichJobSpec` carrying the contacts to visit and the budget), results out (`ConnectionsPage`, `ProfileHarvest`, `InboxDelta`), and a progress event stream. In v1 both sides run in one process, but no module under `linkedin/` imports ORM models or opens a session. A later `netkeeper agent` command can run the same jobs on a laptop and post results to a server. See section 9.10.
- **The database is portable.** SQLite in v1, but migrations use only constructs SQLAlchemy renders the same on PostgreSQL (portable types, `JSON` rather than text blobs, no SQLite pragmas in the schema). From phase 1, CI runs the migration test against a PostgreSQL service as well.
- **The frontend has a current-user context** from `GET /api/v1/me`, and component state holds mailboxes and LinkedIn accounts as lists, even when the list has one item.

Deferred until the local form has matured, and decided by a future ADR: login and session handling, sharing contacts across users, fairness of LinkedIn and Gmail budgets across tenants, a verified Google OAuth app, encryption at rest per tenant, an admin surface, and billing. Whether that future is self-hosted, hosted, or both is an open question (section 21).

## 6. Technology choices

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | Matches igtracker; Playwright, SQLAlchemy, and the Google client libraries are mature here |
| Package manager | `uv` | Fast, lockfile, `uv run` for scripts |
| Browser driver | Playwright (Python) `connect_over_cdp` | No bundled browser needed in attach mode; any Chromium works |
| Database | SQLite via SQLAlchemy 2.0, Alembic migrations, WAL mode | Single file, zero ops, fast enough for 10k contacts. `UTCDateTime` decorator from igtracker |
| Web framework | FastAPI, Pydantic v2, `sse-starlette` | Typed API, OpenAPI for free, SSE for progress |
| Scheduler | APScheduler 3.x (asyncio) | Proven in igtracker including the restart-safe next-fire logic |
| Templating | Jinja2 sandboxed environment | Merge fields with filters, safe against template injection from imported data |
| Gmail | `google-api-python-client`, `google-auth-oauthlib` | Official; drafts, send, threads, labels, history |
| Secrets | `keyring` (macOS Keychain; file backend in container) | Refresh tokens and API keys never sit in the SQLite file |
| CLI | Typer | Same as igtracker; every UI action has a CLI equivalent |
| Frontend | Vite, React 19, TypeScript, TanStack Router + Query + Table, Tailwind, shadcn/ui, react-hook-form + zod | Rich tables and forms without a heavy framework; components are copied into the repo, not a dependency |
| API client | `openapi-typescript` + `openapi-fetch`, generated from FastAPI's schema in CI | Frontend types never drift from the backend |
| Frontend package manager | `pnpm` | Fast, strict |
| Lint and types | `ruff`, `mypy --strict` on new code, `eslint`, `tsc --noEmit` | |
| Tests | `pytest`, `pytest-asyncio`, `vitest`, Playwright for a handful of UI flows | |
| LLM | `anthropic` SDK; default model `claude-sonnet-5`, bulk classification on `claude-haiku-4-5-20251001` | Optional; model IDs live in config |
| Container | Multi-stage Dockerfile (Node build stage, Python runtime), compose file with `network_mode: host` | Linux hosts only for the LinkedIn steps; see section 16 |

## 7. Repository layout

```
netkeeper/
├── pyproject.toml            # package "netkeeper", console script "netkeeper"
├── uv.lock
├── config.example.toml
├── Dockerfile
├── docker-compose.yml
├── Makefile                  # dev, test, lint, build-ui, serve, backup
├── netkeeper/
│   ├── cli.py                # Typer app
│   ├── config.py             # TOML Settings dataclass, path resolution
│   ├── db.py                 # engine, session_scope()
│   ├── migrations.py         # run Alembic programmatically (db upgrade|current|revision)
│   ├── alembic.ini
│   ├── alembic/              # env.py, script.py.mako, versions/; shipped in the wheel
│   ├── paths.py              # data dir resolution per platform
│   ├── models/               # ORM: contacts.py, campaigns.py, runs.py, settings.py
│   ├── linkedin/
│   │   ├── browser.py        # CDP attach, tab management, activity lock, reattach
│   │   ├── preflight.py      # attach, session state, and fingerprint, without a request
│   │   ├── observe.py        # read the responses the tab's page loads; never alters a request (ADR 0006)
│   │   ├── flight.py         # React Server Components flight payloads, parsed defensively
│   │   ├── flagship.py       # flagship-web constants and the connections parser (P2-17)
│   │   ├── page_connections.py  # the connections source: navigate, scroll, read the page's answers
│   │   ├── flagship_profile.py  # profile and contact-info parsers from flagship-web answers (#190)
│   │   ├── page_profiles.py  # the profile source: navigate, scroll, one Contact info click, read answers
│   │   ├── voyager.py        # result types, headers, and parsers for the Voyager API (messaging; connections unwired)
│   │   ├── connections.py    # full + incremental sync job (pure; crm/apply.py maps its pages)
│   │   ├── enrich.py         # profile visit, harvest everything, snapshot diff
│   │   ├── inbox.py          # conversation polling for reply detection
│   │   ├── messaging.py      # prefill compose; opt-in auto send
│   │   ├── pacing.py         # human_delay, scroll_like_a_person, active hours (pure)
│   │   ├── budget.py         # per-day / per-week counters by action class
│   │   ├── heat.py           # adaptive backoff score
│   │   ├── classify.py       # response → Throttled | Checkpoint | LoggedOut | NotFound | RouteGone | Ok
│   │   └── archive.py        # LinkedIn data export (.zip / CSV) importer
│   ├── crm/
│   │   ├── contacts.py       # CRUD, identity resolution, merge
│   │   ├── apply.py          # extractor results → contacts, edge lifecycle (9.8, 9.10)
│   │   ├── triage.py
│   │   ├── tags.py           # manual + rule-based auto-tags
│   │   ├── lists.py          # static lists, smart-list filter DSL → SQL
│   │   ├── importer.py       # CSV mapping, presets, dedupe review
│   │   ├── exporter.py       # CSV / JSON / vCard, presets
│   │   └── timeline.py       # interactions
│   ├── campaigns/
│   │   ├── engine.py         # enrollment state machine, tick
│   │   ├── render.py         # Jinja sandbox, merge fields, lint
│   │   ├── gmail_oauth.py    # installed-app OAuth, token refresh (P3-01)
│   │   ├── gmail.py          # drafts, send, threads, labels, history (P3-02)
│   │   ├── gmail_fake.py     # in-memory Gmail for engine tests (P3-02)
│   │   ├── replies.py        # reply and bounce detection for both channels
│   │   ├── guards.py         # eligibility checks
│   │   └── windows.py        # send windows, caps, spacing
│   ├── llm/
│   │   ├── client.py
│   │   ├── personalize.py
│   │   ├── classify.py
│   │   └── summarize.py
│   ├── sync/
│   │   ├── google_contacts.py
│   │   └── vcard.py
│   ├── services/
│   │   ├── scheduler.py      # jobs, next-fire persistence, active hours
│   │   ├── tasks.py          # task runner, SSE event bus
│   │   ├── posture.py        # protection summary for Settings
│   │   ├── backup.py
│   │   ├── simulate.py       # virtual-clock schedule replay
│   │   └── rehearse.py       # drive the browser path against a neutral site
│   └── web/
│       ├── app.py            # factory, lifespan, static mount, CSRF guard
│       └── api/              # routers: contacts, tags, lists, triage, imports, exports,
│                             #   linkedin, templates, campaigns, mailboxes, settings, events, llm
├── frontend/
│   ├── package.json
│   ├── vite.config.ts        # dev proxy /api → 127.0.0.1:8000
│   └── src/
│       ├── api/              # generated client
│       ├── routes/           # TanStack Router file routes
│       ├── components/
│       └── features/         # contacts, triage, imports, linkedin, campaigns, settings
├── tests/
│   ├── fixtures/voyager/     # sanitized JSON captures
│   ├── fixtures/archive/     # sample Connections.csv, messages.csv
│   └── ...
├── scripts/
│   ├── install-launchd.sh
│   └── launch-chrome.sh
└── docs/
    ├── architecture.md       # this document
    ├── networking-workflow.md
    ├── implementation-guide.md
    └── adr/                  # architecture decision records; 0001–0005 exist
```

## 8. Data model

SQLite, one file. All datetimes stored as naive UTC and returned timezone-aware (igtracker's `UTCDateTime`). Soft deletes nowhere; contacts are archived, never deleted, because messages reference them.

**Every user-owned table below carries `user_id`** (FK to `user`, `NOT NULL`, indexed) and every unique constraint includes it. The column is omitted from the tables that follow to keep them readable. Tables that are not user-owned: `user`, `alembic_version`.

`user`

| Column | Notes |
|---|---|
| `id` | integer PK |
| `kind` | `local` in v1; later `hosted` |
| `display_name`, `email` | Used for `me.*` merge-field defaults |
| `timezone` | Default for send windows and active hours |
| `created_at` | |

`user_positions` (P1-26, #84): the user's own job history, the source for the you-and-them overlap on the triage card (10.2). One row per stint, shaped like `contact_position` below (`title`, `company`, `company_urn`, `started_on`, `ended_on`, `is_current`, plus `source`/`observed_at`) but hanging directly off `user_id`, because there is exactly one of these tables and it belongs to the person running netkeeper, not to anyone they know. Filled from the LinkedIn archive's `Positions.csv` and editable by hand through `/me/positions`; re-import matches an existing row by company and title case-folded plus the start date. **Manual edits win, permanently** — this is the one place a table shaped like a contact child table (source/observed_at, refreshed chronologically) is also fully user-editable, so it adopts P1-18's contract instead of the weaker one: once a row's `source` is `manual` (added by hand, or an archive-sourced row later edited), no import writes to it again at any date, only another edit or a delete. A row that has never been touched by hand keeps the ordinary chronological rule: an import refreshes it only when the new observation is at least as new as the one on file. There is no `synced_values`-style revert ledger for this table; a manual edit cannot currently be undone back to what the archive reported, unlike a `contacts` LinkedIn field. A manual edit that changes `company`, `title`, or `started_on` — the natural key itself — also moves the row out of the key a later import of the same archive would look for, so that import creates an additional row rather than finding the edited one; this is the same accepted limit of natural-key matching `contact_position` already has.

### 8.1 Contacts and identity

`contact`

| Column | Notes |
|---|---|
| `id` | integer PK |
| `li_urn` | `urn:li:fsd_profile/…`. Stable identity from LinkedIn. Nullable for rows that came only from a CSV |
| `li_public_id` | The `/in/{public_id}` slug. Changes when the person edits their vanity URL |
| `li_url` | Canonical `https://www.linkedin.com/in/{public_id}/` |
| `first_name`, `last_name` | As LinkedIn shows them |
| `preferred_name` | What you call them. Defaults to `first_name`; you fix "Robert" → "Bob" in triage. Templates use this |
| `headline`, `current_title`, `current_company`, `location` | Latest observed |
| `connected_on` | Date, from sync or archive |
| `degree` | 1 in v1 |
| `met` | enum `unknown`, `met`, `not_met`, `skip`; `triaged_at`; `met_source` (`manual` or `automatic`) says whether the person decided it or a triage batch they accepted did (10.2) |
| `do_not_contact` | boolean, with `do_not_contact_reason` |
| `li_missing_count`, `li_disconnected_at` | Debounced removal, see 9.8 |
| `last_enriched_at`, `enrich_priority` | Enrichment scheduling |
| `li_not_found_count`, `li_not_found_since`, `li_not_found_at` | The enrichment NotFound streak, see 9.8 (P2-07, migration 0012) |
| `li_enrich_attempted_at` | The last enrichment visit that finished with the contact, whatever it found; a visit that wrote nothing waits a week before the next (9.6) |
| `last_contacted_at` | Denormalized: the `at` of the newest outbound `interaction` (`email_out`, `li_out`, `call`, `meeting`), so the `last_contacted` filter and sort never scan the timeline. Maintained by the interaction service and the campaign engine, recomputed from the rows on edit or delete |
| `notes` | Markdown |
| `archived_at` | |
| `needs_review_at` | Set when the connections sync created the contact from a card on the connections page (the DOM fallback), not from a row that names the person by URN. Cleared when the person confirms it or a sync attaches a URN; while set, the contact is never enriched, enrolled, or aged (9.8, 10.2; #184, migration 0014) |
| `source` | `sync`, `archive`, `csv`, `manual` (first source; per-field provenance lives in `field_sources`, and each child row carries its own `source`) |
| `synced_values` | JSON, per LinkedIn field: the last value an automated source (`sync`, `archive`, `csv`) reported, with its source and `observed_at`, kept whether or not it reached the column. What a manual override reverts to (10.5) |
| `field_sources` | JSON, per LinkedIn field: the source that last wrote it, which decides who may overwrite it (10.5). `manual` here is an override until reverted |
| `created_at`, `updated_at` | |

Child tables, all `contact_id` FK with `source` and `observed_at`:

- `contact_email` (`email`, `kind` personal/work/other, `is_primary`, `status` ok/bounced/invalid)
- `contact_phone` (`number_e164`, `raw`, `kind`, `is_primary`)
- `contact_link` (`url`, `kind` website/twitter/github/other)
- `contact_position` (`title`, `company`, `company_urn`, `started_on`, `ended_on`, `is_current`)
- `contact_snapshot` (`headline`, `current_title`, `current_company`, `location`) written when any of those changes, so "changed jobs since last sync" is a query
- `interaction` (`kind` note/call/meeting/email_out/email_in/li_out/li_in/li_view, `at`, `summary`, `message_id` nullable)

`triage_decisions` (P1-09, 10.2): the triage log and its undo stack. One row per undoable triage action on one contact: `contact_id`, `kind` (`decide`, `preferred_name`, or a bulk kind such as `bulk_met`), `before_state` and `after_state` (JSON, field name to value as text, for exactly the fields the action touched: `met`, `met_source`, `triaged_at`, or `preferred_name`), `batch_id` (shared by every row of one bulk apply, so one undo takes the batch back as a unit), `reason` (the key of the suggestion a batch came from; `NULL` for a decision the person made), `decided_at`, and `undone_at` (`NULL` while the row is still on the stack). Undo takes the newest row whose `undone_at` is `NULL` (or its whole batch), refuses when a field no longer holds `after_state` or the contact was archived or merged away since, restores `before_state`, and spends the rows with a conditional `UPDATE ... WHERE undone_at IS NULL` whose row count must match, so two concurrent undos on PostgreSQL cannot both spend the same decision (#83). Undo, a decision, and a name edit read their contacts `FOR UPDATE` (freshly, not from the session), so nothing lands between the check and the write; undo then counts its decisions again, so the loser of a race is told it lost (`409` with `"reason": "raced"`, never offered `force`) rather than shown the winner's restore as an edit to override (#222). A decision or a name edit is refused for an archived or merged-away contact, the same liveness the queue filters on (#83). Tagging from the triage screen is not logged here: it goes through the Contacts tag route, so `u` reaches past it, and the screen says so.

### 8.2 Identity resolution

Every import or sync resolves each incoming row to a contact by, in order:

1. `li_urn` exact match.
2. `li_public_id` exact match (case-insensitive). If the incoming row also carries a URN, store it.
3. Any email exact match (lowercased).
4. `first_name` + `last_name` + `current_company` exact match → **candidate**, not a match. Candidates go to the import review screen.
5. Otherwise, new contact.

A `contact_alias` table records old `li_public_id` values so a renamed vanity URL still resolves.

Merging two contacts is a first-class operation that re-points every child row and message and records `merged_into_id` on the loser.

**What "every child row" includes, as built (P1-27).** `list_members` is one of them: the survivor stays in every static list the loser was in, and where both were in a list, the survivor's own membership row is the one kept, with the `added_at` the person joined by. The table is not a child of `contacts` in the way the others are — a membership row belongs to a list and names a contact — which is exactly how it was missed until #81.

**Campaign rows, as built (#242).** Every `messages` row naming the loser moves to the survivor, so its timeline and the recency guard (11.9) see everything that was sent. An enrollment moves when the survivor has none in that campaign. When both have one, `UNIQUE(campaign_id, contact_id)` allows one, so the two combine. Whose state wins: an enrollment that was ended beats one still going, because a merge must never restart a sequence, and of two ended ones the stronger wins, in the order `opted_out`, `bounced`, `replied`, `removed`; otherwise the one furthest along; otherwise the `active` one, then the survivor's own. The combined enrollment keeps what either knew: `current_step` is the higher of the two (it holds both sides' messages), a `paused` one keeps an `active` or `pending` winner paused, `replied_at` survives an opt-out, and the outranked side's unsent messages (`scheduled`, `drafted`, `prefilled`) are `discarded`, so no step waits to go twice. The survivor's row takes the combined state and the messages of both. The loser's row stays on the loser as `removed` with `exit_reason` `merged`, holding no messages.

### 8.3 Organization

- `tag` (`name` unique, `color`, `kind` manual/auto/llm, `met_signal` nullable `met`/`not_met`: what the user says carrying the tag means for triage, 10.2), `contact_tag` (`contact_id`, `tag_id`, `source`, `rule_id` nullable). Unique on (`contact_id`, `tag_id`). Removing an auto-tag manually writes a `contact_tag_suppression` row so the rule does not re-add it. A merge (8.2) keeps at most one of an assignment and a suppression per tag on the survivor: two suppressions of the same tag become one, a suppression on either contact removes an automatic assignment on the other, and a manual assignment on either beats and clears a suppression on either, as tagging by hand does.
- `autotag_rule` (`tag_id`, `field` title/headline/company, `pattern` regex, `enabled`, `order`).
- `list` (`name`, `kind` static/smart, `filter_json` for smart, `builtin`: true on the "Validated" list netkeeper seeds, a record of where the row came from that survives a rename or an edit and does not stop either, or a delete), `list_member` (`list_id`, `contact_id`, `added_at`) for static.
- `saved_view` (table column and sort presets for the Contacts page).

### 8.4 Extraction and import runs

- `sync_run` (`kind` connections_full/connections_incremental/enrich/inbox/message_send, `status` running/completed/aborted/failed, `started_at`, `completed_at`, `progress_json`, `counts_json`, `browser_mode`, `notes`, `resume_of_id`).
  *As built (P2-10):* the table is `sync_runs`, and it also carries `linkedin_account_id`, `trigger` (`manual` or `scheduled`), `stop_reason` (the job's own reason, or the outcome that stopped it: `end_of_list`, `caught_up`, `cancelled`, `throttled`, `heat_skip`, `browser_unavailable` ...), `plan_json` (an enrichment run's plan, below), `cancel_requested_at` (9.9's cancel flag), `max_visits` (a manual enrichment's own cap, which only lowers the day's budget), and `error` (one line). A run is `running` from the moment it is asked for; `completed` means it reached its natural end (the end of the list, an incremental sync caught up, the whole enrichment plan visited), `aborted` that it stopped early and kept what it did (a cancel, the budget, the active window, a stopping response), and `failed` that it could not run or ended by exception. A run a stopped process left `running` is marked `failed` ("interrupted") at the next start, unless some process holds its account's browser lock right now or the run is younger than two minutes (a terminal's run between committing its row and taking the lock); `create_run` and cancel apply the same rule, so a run left behind by a killed CLI never blocks the next one. Nothing is ever resumed on its own. `progress_json` and `counts_json` hold counts and reason words only.
- `import_run` (`source_kind` archive/csv, `filename`, `preset`, `mapping_json`, `status`, counts, including what the auto-tag rules did when it committed: `tagged_contacts`, `tags_added`, `tags_removed`) and `import_row` (`raw_json`, `resolution` matched/created/candidate/skipped, `contact_id`, `decision_json`).
- `linkedin_account` (`user_id`, `label`, `cdp_url`, `timezone`, `active_hours_json`, `session_status` ok/checkpoint/logged_out, `session_flag_at`). One row in v1. Budget counters and heat state are keyed by this row's id in `settings_kv`, and the browser activity lock belongs to it.
  *As built (P2-06):* the table is `linkedin_accounts` with `user_id` and `label` (`default`, unique per user) only. Each other column arrives with the change that moves its value off where it lives today (`[linkedin]` in the config, `users.timezone`, the `linkedin.session_flag` key), so there is never a column nobody reads beside the value that is actually used. Migration 0011 created one account per existing user and moved that user's `settings_kv` budget, heat, and scheduler keys from the id 1 every caller used before onto the new row's id. *As built (P2-10):* the activity lock is keyed by the row, `locks/browser-account-<id>.lock` (#169 F); a hold of the local user's account also claims the old `browser-local.lock` first, so a netkeeper process still running pre-P2-10 code (which only ever acted for that account) can never attach alongside a new one. `scheduled_runs_armed_at` is when a person armed the account's scheduled runs, `NULL` while disarmed; every account starts disarmed (9.4). It is a column, not a `settings_kv` key, because `settings_kv` is seeded from `config.toml` and no config value may be what turns scheduled LinkedIn traffic on.
- `settings_kv` (`user_id`, string key, JSON value) for runtime-adjustable settings, budget counters, heat state, next-fire times, session flags.

### 8.5 Campaigns

- `mailbox` (`email`, `provider` gmail, `keychain_ref`, `daily_cap`, `status` ok/reauth_required/disabled, `label_prefix`). One row in v1.
- `template` (`name`, `channel` email/linkedin, `subject` nullable, `body`, `lint_json`, `updated_at`). Versioned: editing a template used by an active campaign creates a new row and the campaign keeps pointing at the old one until you choose to upgrade.
  *As built (P3-03):* the table is `templates`, with `version` and `previous_id` (the row it replaced; unique per user, so a chain never forks; `ON DELETE SET NULL`). The newest row of a chain is the template; older rows are read-only. Names are unique among a user's newest rows, in the service rather than the schema, since versions share a name. Deleting a template deletes its chain. Whether a version is in use is `netkeeper.campaigns.templates.is_in_use`: *as built (P3-04)*, a version is in use once a campaign that names it has left `draft`. A draft campaign sees the template's edits as they happen. Deleting is refused while any campaign names a version, a draft's included. The API reports it on every template as `TemplateOut.in_use` (#243), read for the whole list in one query (`in_use_ids`).
- `campaign` (`name`, `status` draft/reviewing/active/paused/completed/archived, `source_list_id` or `filter_json`, `mailbox_id`, `send_window_json`, `daily_cap`, `contacted_within_days_guard`, `approved_at`, `test_sent_at`).
- `campaign_step` (`campaign_id`, `position`, `channel`, `template_id`, `delay_days`, `mode` draft/send/prefill/auto_send, `condition` no_reply/always, `same_thread` boolean).
- `enrollment` (`campaign_id`, `contact_id`, `status`, `current_step`, `next_action_at`, `exit_reason`, `replied_at`, `channel_ids_json`). Unique on (`campaign_id`, `contact_id`).
- `message` (`enrollment_id`, `step_id`, `contact_id`, `channel`, `direction` out/in, `status`, `subject`, `body_rendered`, `scheduled_at`, `sent_at`, `gmail_message_id`, `gmail_thread_id`, `gmail_draft_id`, `li_conversation_urn`, `li_message_urn`, `error`).

*As built (P3-01):* the table is `mailboxes`, in `netkeeper/models/mailboxes.py` (migration 0019). Beside the columns above it has `status_reason` (a short code, such as `invalid_grant`, `token_missing`, `client_missing` or `disconnected`, never Google's own text), `checked_at` (the last successful token refresh), and `generation` (migration 0020), which every authorization increments: a health check records its answer only if the generation has not moved while it asked Google, so a check of the old token never marks a grant a person has just renewed. `email` is lower-cased and unique per user. `daily_cap` is copied from `[campaigns] mailbox_daily_cap` when the mailbox is first connected, and held to the hard max of 400 (11.4). The row never holds a secret: `keychain_ref` names the Keychain entry (`gmail/mailbox/<id>`) under the user's key. A mailbox is never deleted, because campaigns name it. **Disconnect** forgets the token and sets `disabled`. "One row in v1" is enforced as one *live* mailbox: authorizing the same address again re-authorizes it, and authorizing a different one while a mailbox is `ok` or `reauth_required` is refused until that one is disconnected.

*As built (P3-04):* the tables are `campaigns`, `campaign_steps`, `enrollments` and `messages`, all user-owned, in `netkeeper/models/campaigns.py` (migration 0018). The main choices:

- **What the database keeps.** A message's `contact_id`, `enrollment_id` and `step_id`, and a step's `template_id`, have no `ON DELETE` action. Deleting a contact, an enrollment, a step, a campaign or a template that a message or step names therefore fails, rather than taking the record of what was sent with it. What was never sent goes with its owner: an enrollment cascades with its contact, and steps and enrollments cascade with their campaign. So an unsent draft, or a contact that an import rollback deletes, still goes cleanly.
- **The mailbox.** `mailbox_id` was a plain integer until the `mailbox` table existed, as `interactions.message_id` was until 0018. 0018 gives `interactions.message_id` its foreign key (`SET NULL`), and 0019 (P3-01) gives `campaigns.mailbox_id` its own, with no `ON DELETE` action. The interactions service only accepts one of the user's messages to that same contact.
- **Checks.** A step's `mode` belongs to its channel (`draft`/`send` for email, `prefill`/`auto_send` for LinkedIn), and `same_thread` is for email only. A campaign has a list or a filter, never both. A campaign's name is unique per user, because it names the Gmail label. A message is `received` exactly when it is inbound.
- **Statuses.** An enrollment's status follows 11.3. A message's status is one of `scheduled`, `drafted`, `prefilled`, `sent`, `stale`, `discarded`, `bounced`, `failed` or `received`.
- **Column meanings.** `current_step` is the position of the step that fired last (NULL before the first one). `contacted_within_days_guard` has no default: whatever creates a campaign (P3-06 or later) copies it from the config. 0 turns the recency guard off.

## 9. LinkedIn extractor

### 9.1 Browser attach

The extractor only supports `attach` mode. igtracker's `legacy` and `persistent` modes exist because Instagram sessions could be pasted; here they would create a second LinkedIn device, which is the main restriction trigger. The `BrowserProvider` interface is kept so a mode can be added later, but `attach_fallback` does not exist: if Chrome is not reachable, the run fails loudly and the scheduler retries later.

Chrome setup, wrapped by `netkeeper browser launch`:

```sh
open -na "Google Chrome" --args \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/netkeeper/chrome-profile"
```

Chrome 136 and later refuse `--remote-debugging-port` on the default profile directory, so a dedicated profile is mandatory. You log in to LinkedIn in that profile once. LinkedIn sees one new device at setup and then a stable one. Use that profile for your own LinkedIn browsing too, so organic activity and sidecar activity share a session.

Invariants inherited from igtracker: reuse `browser.contexts[0]`; open one tab per run and close only that tab; never `add_init_script` or `route` on the shared context; never write cookies; never override the UA or timezone. [ADR 0006](adr/0006-observe-dont-request.md) adds two: never intercept, alter, or answer a request, and never send one of netkeeper's own; netkeeper reads the responses its tab's page loads (9.3). `netkeeper preflight` verifies the attach, that the profile still holds a LinkedIn session, and that its fingerprint looks like a normal Chrome. It answers all three locally and navigates nowhere: the session state comes from the names and expiry of the profile's own cookies, read over CDP, and the fingerprint from `navigator` properties on the blank tab the run opens. Whether a session is not merely present but still accepted is something only a job can learn, from the classification in 9.7. Cookie values are never read, reported, or logged.

### 9.2 Data sources, layered

| Source | Cost | What it gives | Role |
|---|---|---|---|
| LinkedIn data archive (`Connections.csv`, `messages.csv`, `Invitations.csv`) | Zero risk, 24 h wait | Name, URL, company, position, connected-on for everyone; email only for people who opted in; full message history | Seed and triage evidence. The importer skips the three-line preamble LinkedIn puts above the header |
| Connections list (the page's own answers, 9.3) | Cheap: ten contacts per answer the page loads as it is scrolled | URN, public id, name, headline, connected-on | Sync, change detection |
| Profile visit | Expensive: this is what LinkedIn rate-limits | Contact info overlay (emails, phones, websites, handles), location, positions, education | Enrichment. One visit harvests everything |
| Messaging conversations (in-page API) | Cheap | Threads, participants, last message, direction | Reply detection for LinkedIn steps; "have we talked" triage signal |

### 9.3 Read what the page loads

*As built (P2-17, [ADR 0006](adr/0006-observe-dont-request.md)).* The capture in #149 showed that LinkedIn serves the connections list and profiles through `flagship-web`, a React Server Components client, not Voyager, and that PerimeterX and reCAPTCHA Enterprise watch those pages. So netkeeper no longer requests anything for the connections list. It navigates its tab to the connections page and scrolls it like a person; the page itself asks for each next page of ten (`POST /flagship-web/rsc-action/actions/pagination`); and netkeeper reads those answers from the tab's own `response` events (`BrowserRun.observe`). Nothing is intercepted, routed, altered, or sent by netkeeper: the requests LinkedIn sees on the tab are the ones its page decided to send, at its own pace. `linkedin/flight.py` parses the flight payloads; `linkedin/flagship.py` holds the captured constants and the connections parser, each constant dated; `linkedin/page_connections.py` is the source the sync reads through. A payload the parser does not recognize is `RouteChanged` for the run, and a page of connections is handed on only when every card on it parsed, so a shape change stops a run and never writes part of a page. `docs/linkedin-flagship-web-shapes.md` records the shapes, structure only. *As built (P2-18, #190).* Enrichment reads profiles the same way: it navigates to the profile, scrolls it, and reads the profile screen the page loads with it and the lazy cards it loads as it is scrolled; then it scrolls back to the top and makes ADR 0006's one exception to "scroll only", one click on **Contact info**, and reads the overlay's `actions/navigation` answer the click made the page ask for. `linkedin/flagship_profile.py` parses both; `linkedin/page_profiles.py` is the source; `BrowserRun.click_contact_info` is the only method in the package that clicks (`tests/test_browser_safety.py`). The in-page Voyager fetch (`linkedin/fetch.py`) is removed.

The DOM reader for the connections list is removed (#187 review), with `FallbackConnectionsSource`. Its selectors were authored and never seen on the live page, and the first supervised run found nothing with them; reading the page's own answers replaces it rather than falling back to it. Its contact-info half, and the `contact_info.py` seam it implemented, were never wired and are removed too (#190): the overlay is read from its own answer, which names the profile it is for, where the DOM reader could not say whose overlay it was.

*Before P2-17 and P2-18*: LinkedIn's web client talks to an internal REST API under `/voyager/api/`. From a tab on `linkedin.com`, a `fetch` carries the session cookies and Chrome's real client hints. The request needs the `csrf-token` header (the `JSESSIONID` cookie value without its quotes) and `x-restli-protocol-version: 2.0.0`, plus the `accept` and `x-li-*` headers the real client sends.

Endpoint paths, query shapes, and the `decorationId` values are undocumented and change. They live in one module, `linkedin/voyager.py`, each constant annotated with its capture date, and every parser is tested against sanitized fixtures captured from DevTools. When a request answers with a shape the parser does not recognize, the run records `RouteChanged` and gives up on that endpoint for the run rather than retrying (igtracker's withdrawn-route lesson).

*Superseded (#187, #190):* `linkedin/dom.py` held a DOM fallback for the connections page and the contact-info overlay. Both halves are removed; a shape change now stops the run as `RouteChanged` rather than falling back to a reader nobody has seen work.

### 9.4 Jobs

All jobs take the activity lock, hold one tab, and write progress to `sync_run.progress_json` for the SSE stream.

**Connections full sync.** Paginate the connections list to the end. Apply the edge lifecycle in 9.8. Runs on first setup and weekly.
*As built (#200):* a full sync that lost any of the page's answers (Chrome kept no body for them) reads on past them, but is incomplete: it ages nobody, is recorded `aborted` with `stop_reason = answer_lost`, and the scheduler offers the week's full sync again about a day later, at most once per week.

**Connections incremental sync.** Newest-first pages; stop after a full page of already-known URNs. Runs daily. Cheap enough that it is the default keep-fresh mechanism.

*As built (P2-17).* Both syncs read the connections page's own answers (9.3). A page, the unit the budget counts, is 40 connections from the list read so far; the page loads them ten at a time as it is scrolled, so a unit costs about four of its answers and at most a few idle scrolls more. The list's default sort is by recently added, which an incremental sync requires: a list the page sorts any other way stops the run as `RouteChanged`. The first screen states the total count, which completeness uses the way it used `paging.total` (9.8).

**Enrichment.** Pick contacts by priority (9.6), and for each:

1. Navigate to the profile page (a real page view; the reference workflow treats "viewed your profile" as a feature).
2. Scroll like a person (9.5), dwell.
3. Fetch contact info and profile details through the in-page API, falling back to the overlay DOM.
4. Upsert emails, phones, links, positions, location. Write a snapshot if headline, title, company, or location changed.
5. Pause with `human_delay` before the next profile.

*As built (P2-18, #190, ADR 0006).* Step 3 is now: read the profile from the screen and lazy cards the page loaded; if the profile's own id is the contact's URN (the job holds it, `EnrichTarget.li_urn`), scroll back to the top with the wheel, pause as a person does (median 1.5 s), click **Contact info** once (`BrowserRun.click_contact_info`: found by its accessible role and name, refused unless it is alone on the page and its `href` is this profile's `overlay/contact-info/`), and read the overlay's answer, which must name the same profile. A profile under another id gets no click; its harvest carries the details and no contact info, and `crm/apply.py` records the mismatch and writes nothing. The click is part of the visit: the one `profile_visits` unit spent before the navigation covers it, and nothing is clicked twice or retried. The overlay is left open; the next navigation leaves the page. A visit whose profile or overlay does not read, whose control is missing or not alone, or whose overlay never answers is unreadable (the per-run cap below), never partly written; a profile under another id counts toward the same cap, since several in a run mean the id is being read from the wrong place. A checkpoint or login wall the tab shows after a refused click or an overlay that never read is the session's, not the profile's: it stops the run and flags the session. A profile the tab lands on that is neither the one asked for nor one a redirect led to is unreadable. `docs/linkedin-flagship-web-shapes.md` lists which of these shapes the capture showed and which the first supervised enrichment must confirm.

**Inbox poll.** Fetch recent conversations, match participants to contacts by URN, and hand new inbound messages to `campaigns/replies.py`. Runs every few hours while any LinkedIn step is active.

**Message prefill / auto-send.** See 11.6.

*As built (P2-10): scheduled runs start disarmed.* `netkeeper serve` runs the scheduler (P2-09) for the connections full and incremental syncs and enrichment; the inbox poll has no runner yet and is not scheduled. But no scheduled job fires until a person arms the account's scheduled runs (`netkeeper linkedin schedule arm`, which asks first, or `POST /linkedin/schedule/arm` with `confirm: true`); every install, new or migrated, starts disarmed. While disarmed the scheduler keeps its due times and skips each due fire as `disarmed`, the way it skips one as `heat` (or as `session_flagged` while a checkpoint or login-wall flag stands): the cadence moves on, and a kind that runs on first setup keeps that standing and is offered again an hour later, so the first full sync still happens soon after arming. Three independent checks hold this: the scheduler's arm gate before it fires, `services.runs.create_run`, which refuses to record a scheduled run on a disarmed account, and the worker, which refuses to attach for one. Runs started by hand -- `netkeeper linkedin sync`, `netkeeper linkedin enrich`, `POST /linkedin/runs` -- are allowed while disarmed; that is how the first supervised run at CP4 is done. Nothing that only reads (a `GET`, a page load, the event stream, starting the server) starts a run.

*As built (#189, #191): the route-changed breaker.* A wall served in place at the connections url stops a run as `route_changed` with no session flag and no heat (9.3, ADR 0006), so a scheduled connections sync would otherwise load that same wall again at every interval once armed. `services.route_breaker` counts consecutive connections runs -- full or incremental, by hand or by schedule -- that end `route_changed` (an observation failure that keeps a page from being read counts too, since it means the same thing: the route could not be read); two in a row (pinned literally, the same bar spec 9.7 gives a run's own throttle streak) trip it, and the scheduler then skips every scheduled connections fire as `route_changed_breaker`, the same way it skips one as `disarmed` or `heat`: the cadence still advances and first-setup standing is kept. The worker checks again before it attaches, matching the arm gate's own three independent checks. Enrichment does not share this counter -- it has its own unreadable-profile cap (9.6) on a different endpoint. Manual runs are never refused for it, by design: once tripped, no scheduled fire ever reaches a run at all, so running one by hand (`netkeeper linkedin sync`) is the only way to check whether the wall is still there, and its success clears the breaker the same as any success does before it trips. `netkeeper linkedin schedule reset-breaker` (confirmed, like `arm`) clears it directly, and `netkeeper posture` shows the count -- or, for a stored row that could not be read, a warning and a fail-closed "tripped" until it is next written -- and how to reset it.

*As built (#199): the answer-lost limit.* A connections run recorded `answer_lost` (it stopped for an answer whose body the browser could not hand over, or read on to the end without some, #197/#200) moves the route-changed breaker neither way, so if LinkedIn's client started superseding every pagination fetch, each scheduled run would spend page views and end `answer_lost` indefinitely. `services.route_breaker` therefore keeps a streak per connections kind (full and incremental), each in its own `settings_kv` row (`linkedin.answer_lost_breaker.<kind>.<account>`): losses may bite only the weekly full sync's long read while every daily incremental completes, and a shared streak would be cleared daily and never trip (#199 review, M2). Three consecutive runs of one kind recorded `answer_lost` (pinned literally, `ANSWER_LOST_THRESHOLD`) make the scheduler skip every scheduled connections fire, of either kind, as `answer_lost_breaker`, and the worker refuses a scheduled connections run with the same reason as its second check. Manual runs are never refused. A kind's streak clears only when a run of that same kind ends `completed` (a natural end with nothing lost); `netkeeper linkedin schedule reset-breaker` clears every streak, the breaker's included. Any other ending (a route change, a budget stop or a cancel, even with losses, an observation failure) leaves the count where it was, and the streaks never feed each other. A corrupt row, or a negative count, reads as tripped (fail closed) and posture reports it `unknown`. `netkeeper linkedin schedule status` and the posture report (the posture endpoint and the Settings page's protections table) show every count.

### 9.5 Human-like behavior

`linkedin/pacing.py` is pure and unit-tested:

- `human_delay(median, sigma, tail_p, tail_range)`: lognormal with a fat tail and an occasional long distraction. Defaults in Appendix C.
- `scroll_like_a_person(page)`: a sequence of `mouse.wheel` deltas with variable magnitude, brief pauses, an occasional scroll back up, and a final dwell. Total scroll depth is random and sometimes short. *As built (P2-17, [ADR 0006](adr/0006-observe-dont-request.md)'s amendment).* Before the first wheel event of a run's tab, the pointer rests over the page's content: a few short `mouse.move` hops with small jitter, paced -- `BrowserRun`'s `_rest_pointer_over_content`, replaying `pacing.rest_pointer_like_a_person`'s plan. Playwright's virtual pointer starts at `(0, 0)`, which on flagship-web sits under the fixed nav, not the scrolling list, so a wheel replay that never moved it first scrolled nothing (#192). The target is read, not guessed: the on-screen box of the page's `main` landmark (`Locator.bounding_box`, a passive DOM/CDP query outside the page's own script, not `evaluate`), clamped to clear the header; only when there is no such box does it fall back to a fraction of the tab's own viewport size (or a conservative default when even that is unknown). Once per tab, repeated only if the tab is reopened.
- Bursts: 8 to 15 profiles, then a break of 5 to 20 minutes.
- Active hours: a window in your local timezone (default 08:30 to 21:30, all days). Ticks outside it park a one-shot job for the window start.
- Warm-up: a fresh install starts at 20 profile visits per day and grows by 10 per day up to the configured cap.
- Weekend and holiday damping: multiply budgets by 0.5 on Saturday and Sunday by default.
- Never run enrichment and a message send in the same minute; the scheduler interleaves job kinds with a gap.

### 9.6 Budgets and prioritization

Counters live in `settings_kv`, keyed by local day and week, per action class:

| Class | Default per day | Hard max | Notes |
|---|---|---|---|
| `connection_pages` | 150 | 400 | About 6,000 contacts per day at 40 per page (a page is a unit of 40 read from the page's own ten-card answers, 9.4) |
| `profile_visits` | 60 | 100 | The number that matters. The reference workflow's guidance for scraping tools is 100 |
| `contact_info_fetches` | tied to `profile_visits` | | One per visit. *As built (#190):* the one Contact info click, covered by the visit's `profile_visits` unit; no separate counter |
| `inbox_polls` | 8 | 24 | |
| `li_messages_auto` | 15 | 30 | Only when auto-send is enabled |
| `profile_visits` per week | 300 | 500 | |

Enrichment order: contacts you are about to enroll in a campaign that lack the channel's address, then `met` contacts never enriched, then stale (`last_enriched_at` older than 180 days), then everyone else, newest connection first. You can pin up to 5 contacts to the front of the next run (igtracker's pins, same rules: selected within the budget, not on top of it).

*As built (P2-07).* The first tier is `enrich_priority` above 0, highest first: the campaign engine (P3) sets it, nothing does yet, and an applied harvest clears it. "Everyone else" means everyone else never enriched; a contact enriched within `enrich_stale_days` is not visited again unless pinned or asked for. Newest connection first breaks ties inside every tier. Enrichment visits only contacts with a URN and a slug that are not merged, archived, disconnected, do-not-contact, or waiting for review (#184): the URN is how a harvest is matched to its contact (a slug can pass to someone else), and every connection a sync has seen has one. A contact whose last visit wrote nothing -- no profile, a profile under another URN, a slug another contact holds, or a shape the parser could not read -- waits 7 days (`li_enrich_attempted_at`) before the next, so it cannot head every run's queue and cost a visit a day. A visit that wrote something does not wait (#172): its harvest cleared any `enrich_priority`, so only a fresh ask brings the contact back before it goes stale. A pin overrides the tiers and that wait, never those rules. One profile the parser cannot read is that contact's problem; two in a row, on different contacts, or three in one run whether in a row or not (#172), is taken as the route having changed and stops the run. Pins live in `settings_kv` per account and are dropped once a run finishes with the contact. A run's visit budget is today's warm-up-ramped, weekend-damped, heat-shrunk allowance (9.5, 9.7, the chain `netkeeper posture` reports) less what is already spent today and what is left of the week, and the plan is cut at it; `budgets.consume` still runs before every visit. A run also stops between profiles when the active window closes.

### 9.7 Failure classification and heat

`linkedin/classify.py` maps a response to one outcome:

| Signal | Outcome | Action |
|---|---|---|
| HTTP 200 JSON | `Ok` | |
| HTTP 429, or HTTP 999 | `Throttled` | Escalating cooldown, raise heat, at most 3 attempts; two consecutive throttled units abort the run |
| Redirect or body pointing at `/checkpoint/` or `/challenge/` | `Checkpoint` | Stop the run, set the session flag, banner in the UI. Never retry |
| Redirect to `/login`, `/authwall`, `/uas/`, or a body pointing at one of them, or 401 | `LoggedOut` | Stop, set the session flag, banner says log in to the netkeeper Chrome profile |
| HTTP 404 on a profile | `NotFound` | Terminal for the contact this run; increments a not-found streak used by 9.8 |
| HTTP 200 with an unrecognized shape, or 400 on a known endpoint | `RouteChanged` | Give up on that endpoint for the run, log loudly, fall back to DOM if one exists |

*As built (P2-17).* An answer the connections page loaded (9.3) is classified the same way: a non-200 by its status, url, and body; a redirect by where it pointed; a 200 is `Ok`, and its parser decides whether the shape is one it knows. Where the tab itself landed after a navigation or a scroll is classified first, by its url, so a checkpoint or a login wall stops the run before anything is read.

Heat (`linkedin/heat.py`): each `Throttled` or `Checkpoint` adds to a score that decays exponentially, computed on read. While warm, `human_delay` medians stretch and the per-run budget shrinks, never to zero. Above `heat_skip_threshold` the scheduler skips browser jobs entirely. The Settings page shows the level, when it was last raised, and when runs resume. A manual clear exists for the case where the block was something else.

### 9.8 Sync semantics and change detection

Unlike Instagram's follower lists, LinkedIn's connections list is complete, so removal detection is simpler but still debounced:

- A contact seen in a full sync gets `li_missing_count = 0`.
- A contact absent from a full sync gets `li_missing_count += 1`; at 2 consecutive misses `li_disconnected_at` is set. Incremental syncs never age anyone.
- A contact that reappears clears both. Nothing is deleted.
- Position and headline changes write a `contact_snapshot`. The dashboard surfaces "changed jobs in the last 30 days" as an outreach prompt; that is the single best reason to reconnect.
- A `NotFound` streak of 3 across at least 14 days marks the profile as gone (`li_disconnected_at` plus a note). Same two-threshold reasoning as igtracker: a deactivated profile looks exactly like a deleted one and often comes back.
  *As built (P2-07):* the streak is `li_not_found_count` from `li_not_found_since`; the third NotFound at least 14 days after the first sets `li_disconnected_at` (unless one is already set), writes a `note` interaction on the timeline (not into the person's own `notes`), and starts the streak over, so a later gone-mark needs three across 14 days again. A contact whose last visit found nothing waits 7 days for the next, so the three land a week apart. A harvest that finds the profile ends the streak, and so does a sync that sees the contact, which also clears the disconnect as always. A harvest that finds the profile does not clear `li_disconnected_at`: a profile that can be looked up is not proof of a connection.

*As built (P2-06).* The end of the list is an empty page, or a short page (fewer connections than asked for) that reaches the largest total any page reported; a short page before that is logged and read past, and the reported `paging.total` alone never ends a run. Only a *complete* full sync ages anyone: one that reached the end of the list with every response `Ok`, whose pages reported a total above zero, and that saw at least as many distinct connections as the largest total any page reported, minus a small slack (*as revised, #204*): the larger of 5 and one percent of that total, rounded up, since LinkedIn's own total can count people the list itself never serves (a deactivated account, say). Only the completion decision gets this slack; the end-of-list rule for a short final page is unchanged. That bar also catches a list that changed under the run, in either direction, by more than the slack, at the cost of aging nobody that week. A shortfall the slack covers is recorded on the run (`notes`, and `shortfall` in `counts_json`), for example "complete with 4 short of LinkedIn's total." A full sync stopped by the budget, a throttle, a checkpoint, a login wall, a changed route, or an error ages nobody, and a complete one is still refused rather than trusted when it saw no connections at all, when it would age every contact that can age, half of them or more, or more than a tenth of them (floor 10), all counted among the contacts that existed before the sync (a row the sync itself created was seen, and does not dilute the share), or when more than a tenth of the URNs it saw match no stored contact (floor 10 once more than 10 were seen), as after a URN scheme change. In a network of two, one genuine removal never ages: one of two is half, and half is refused. The same holds for any network where the removals are half of it; the price of refusing a replaced network is that a very small one loses nobody until it grows. A contact whose slug the sync saw, or that a candidate row named and is waiting for review, counts as seen: a profile back under a new URN with the same slug is never aged. No sync starts while the session flag is set, so a checkpoint is never retried by the next run either. Being seen in either mode clears the count and any disconnect, since a sighting is evidence whichever job made it; an incremental sync still never adds a miss. Only contacts with a URN, not merged away, and not waiting for review (#184) can age: a CSV-only contact has nothing a sync could have missed, and a contact read off a card is not a connection anything has confirmed. The threshold is `disconnect_after_misses` (Appendix B, default 2). A complete full sync that refused to age anyone says why on its run (`notes`, and `aging.refused` in `counts_json`) and as a `netkeeper posture` warning until a later complete full sync ages as usual (#169 E). One consequence of candidates counting as seen (#169 B): when a removed connection's slug is reassigned to a new connection, the new one resolves as a candidate of the old contact, which holds the old contact for review on every sync, so it never ages until a person merges the two or edits one. That is the safe direction, and the review screen is where it surfaces.

*As built (P2-17): what the page proves.* The connections page states no per-page total, so the end of the list is an answer the page itself received that says so: a connections answer with no cards, or a short answer (fewer than ten) that asks for no next page. A full answer that asks for none is the end only once the run has seen as many distinct people as the first screen's total, less the completion slack (#204), so a visible list that is a multiple of ten under a total counting hidden members still ends (#208: 620 visible of 624) -- the slack applies only once an earlier answer in the run asked for a next page, so a parser that stops seeing next requests cannot end a sync early; short of that it proves nothing, and neither does a page that stops asking; after six scrolls that bring nothing, the run stops as `RouteChanged` and ages nobody. The first screen's `totalConnectionsCount` is the total the rules above use: a full sync is complete only when it reached that proven end, saw at least as many distinct URNs as the total (minus the slack, #204), and every answer was `Ok` and in order (each answer's `startIndex` the one the previous answer asked for; a page asked for twice is read once; a page skipped, or a next page further on than the answer could have filled, stops the run). A first screen without a total completes nothing, and a first screen with no cards is an empty list only when its total is 0. A total that counts a member or two the list never shows (Run 8 on #31: 618 of a claimed 622, a shortfall of 4 against a slack of 7) no longer stops every full sync from completing (#204): any gap no bigger than the slack is let through, whatever its cause -- the rule only checks the gap's size, never whether it looks steady from one sync to the next. That is a deliberate, accepted risk (David, 2026-09-25): a connection removed mid-run can skip exactly one person at a page boundary the same way a hidden member does, so that removal reads as a shortfall the slack absorbs rather than as a caught list change. What bounds it is the same debounce every miss goes through: one sync skipping that one person sets a single miss, and `disconnect_after_misses` (Appendix B, default 2) still asks for a second complete sync missing them before they are marked gone -- a genuine removal is rare enough on LinkedIn that this was judged an acceptable trade against aging nobody every week over LinkedIn's own hidden members. A total that counts far more than the slack allows still means no full sync completes, which stays the safe direction; the first supervised full sync checked it. A card whose "Connected on" day the page shows is stored as that day, not converted through a time zone; the en-US and en-GB phrasings are read, with full or abbreviated months, and any other phrasing leaves the date unknown rather than refusing the page. A document without a first screen is judged by the tab's url alone, never by the paths its HTML links to (every logged-in page links to `/uas/logout`), and a non-200 answer by its status and url, never its body. A failure of the observation itself (an answer too large to keep) hands over the units already read whole and then ends the run by exception: recorded failed, never complete. A card gives one display name, split at its first space; a contact whose stored first and last name join to the same words keeps its own split.

*As built (P2-08, revised by its #173 review, #174, and #184).* A DOM-sourced connection (the fallback's own rows, `ConnectionSummary.urn` always `None`) never goes through identity resolution: `crm/apply.py`'s `apply_page` never resolves or applies it as an `IncomingContact`. Against a contact that already holds its slug it is **sighting-only**: it never writes a field of that contact, and its slug is the only thing it contributes, and only to the seen pass, matched against a contact's *current* stored slug (never an alias) after the same normalization (case fold, url-decode) a Voyager or archive row's slug already gets before it is stored. A slug that no contact holds, as its slug or as an alias, creates one contact **marked needs review** instead (#184, below). The restriction runs the other way too: a Voyager row's *own* slug is never fed into that same slug match, only its URN is -- LinkedIn lets a vanity url pass from one account to another (spec 9.6), and a sync that let a Voyager row's slug clear a disconnect could wrongly reconnect whoever last held that slug the moment someone else is reported wearing it. Matching a URN-holding contact by a DOM sighting's slug is still allowed, deliberately, but (#174 item 4) only resets the contact's miss count: it never clears an *existing* `li_disconnected_at`, because a slug alone is weaker evidence than a URN, and only a URN sighting can undo a disconnect. This closes a gap the F1/S1 fix did not: during a Voyager outage a fallback run sees nothing but DOM pages, and a slug LinkedIn has since handed to a different person would otherwise let that person's DOM card wrongly reconnect whoever the slug's actual, previously-disconnected former holder was. `SyncResult.complete` also refuses outright once a run's source has ever fallen back from Voyager to DOM (`FallbackConnectionsSource.switched`), on top of (not instead of) the existing totals check -- the simple invariant: once DOM has answered for a run, nothing it reported is trusted enough to call that run complete, whatever the totals math alone would say. The DOM connections read and the contact-info overlay read each check a structural marker (the list's own container element; the overlay's `role="dialog"`) before trusting an empty or short result: its absence is `RouteChanged` at once, never an honest empty page, because a wall or an error page rendered in its place is a shape mismatch, not evidence of zero connections or zero shared contact info -- except that a missing container is retried once, paced, before that refusal (#174 item 6), since a slow initial render can still be missing it on the very first read. Running out of scroll-settle attempts without reaching what a page asked for is the same `RouteChanged` *unless* LinkedIn's own end-of-list marker is present, which reports a clean (if still incomplete-by-total) page instead (#174 item 5) -- this module still has no way to tell a stalled render apart from a list that truly ended there when that marker is absent, and a run that ended this way is still never `complete` (`source_switched`/`total=0` still hold). The contact-info overlay read also now chooses among possibly several `role="dialog"` elements on the page (a messaging overlay can render one too): it reads only the one dialog that carries a contact-info marker -- a heading titled "Contact info" or a link back to the public id the read navigated to -- and refuses as `RouteChanged` when zero or more than one qualify (#174 item 1). Its Twitter/X handle reading also excludes X's own reserved path segments (`i`, `intent`, `share`, and the rest of a short denylist), not just `i` (#174 item 2).

*As built (#184): a card for somebody netkeeper has never seen.* During an API outage a new connection shows up only as a card, so a DOM row whose slug no contact holds (any contact, archived included, by its slug or an alias) is not dropped: it creates exactly one contact with the slug, the card's name and headline, no URN, and `needs_review_at` set. The card's text is written with **no recorded source** in `field_sources` and nothing in `synced_values`: a field with no recorded source is open to every source (10.5), so the first sync, archive, or CSV row to reach the contact replaces it, and a card is not a value anybody would revert to. `source` is `sync`. A name the card gave is refused, and the contact named by its slug instead, when it spans lines (the extractor reports it empty), carries a control character or a line separator, runs past 100 characters or 6 words, contains the card's own headline as whole words or run on directly after a lowercase letter (the occupation leaked into the link text; a headline of "Ann" does not refuse "Anna"), or carries the link's label ("View … profile"), one of LinkedIn's visually hidden labels ("Member's name", "Member's occupation", "Status is online"), a url, an address, a digit, or a separator LinkedIn puts between a name and an occupation; a refused name is never trimmed into shape. Direction marks (LRM, RLM) are stripped first, and the joiners ZWNJ and ZWJ are kept, since names in some scripts carry them. Known limit: a name and an occupation run together with no separator and no headline on the card ("Jane DoeSoftware Engineer") passes, because catching it would take camel-case detection that refuses McDonald and DeAndre; the needs-review mark is what covers it. A slug a DOM read could not have come from LinkedIn's routing (whitespace, a `/`, over 100 characters) creates nothing. A later DOM row for the same slug finds the contact and is sighting-only, so repeats never duplicate it; a contact the person rejected keeps its slug and is not asked about again. **A URN confirms it:** a Voyager row that resolves to the contact (by slug, spec 8.2 step 2) gives it the URN, and once the contact holds the URN the row carries, the mark is cleared -- a URN from LinkedIn's own API is the confirmation the mark waits for. The card's values are replaced under the ordinary provenance rules without a `contact_snapshot` (a card is no job history), a card headline the row does not replace is dropped rather than kept under the URN (the slug may have passed to someone else since the card was read), a preferred name that was only ever the card's first name follows the real one, and the run counts the contact among the ones it made connections (`PageCounts.confirmed_contact_ids`), so #169 A's "existed before this sync" denominator leaves it out like a row the sync created. A Voyager row whose URN is another contact's and whose slug is the card contact's (the slug moved, or the card was a renamed vanity url of someone netkeeper already holds) is a candidate naming both, never a silent merge: the card contact keeps its mark and gets no URN, and both are held from aging until a person merges or edits them. Until confirmed, or until a URN confirms it, the contact is never enriched (9.6), never enrolled (11.9), and never ages or counts toward the aging shares (`age_unseen` leaves every contact waiting for review out of "can age", even one that came by a URN some other way). A fallback run is still never complete. A merge with a contact that is not waiting confirms it; a merge of two that are both waiting confirms neither, and a waiting loser's unrecorded fields stay unrecorded on the survivor. When the survivor is the waiting one and the loser is not, the survivor's unrecorded fields count as empty, so the real contact's values and their sources win over the card's text, as if the person had picked the other survivor. The `campaign-audience` export (10.6) leaves out contacts waiting for review, as it leaves out `do_not_contact`. Two accepted edges: a card contact the person rejected (archived, still marked) that a later Voyager row reaches by slug takes the URN and has its mark cleared like any other, and stays archived, so it is a confirmed, archived contact the person can unarchive; and a card contact whose slug changes before any Voyager row sees it (the person renamed their vanity url between the fallback run and the next sync) is never reached by slug, so it waits until a person merges or rejects it. The run's `counts_json` carries `cards_created` and `confirmed_by_urn`.

### 9.9 Locking, cancel, resume, tab loss

- One activity lock per LinkedIn account for all browser paths, in every netkeeper process: scheduled jobs in `netkeeper serve`, `netkeeper preflight`, `posture --probe`, `rehearse`, and the Settings page's "check session" button. It is cross-process, not an in-memory lock: an in-process `asyncio.Lock` alone let `netkeeper preflight` in a terminal open a second CDP client while `serve` held the browser (#153). The mechanism is an OS file lock, `flock(2)` with `LOCK_EX | LOCK_NB`, on `<data dir>/locks/browser-<account>.lock`, taken before `connect_over_cdp`, so a busy account never reaches a second client. The kernel releases it when the holder exits however it exits, so a crashed holder cannot park it and there is no heartbeat or staleness rule; the pid and command written into the file are only for the busy message. An in-process `asyncio.Lock` sits in front of it so coroutines in one process queue in order. A path that cannot take the lock answers `busy`, naming the holding process. Scope: processes sharing one data directory on one machine; a lock that spans machines (a `settings_kv` claim with a heartbeat) waits for a deployment that needs it.
- Cancel is cooperative: a flag in `settings_kv` checked between profiles and inside sliced cooldowns; the run ends `aborted` and whatever completed is kept.
- Resume: an aborted enrichment run stores its plan; `netkeeper linkedin enrich --resume <run_id>` skips what completed. The original plan is reused, never re-planned.
  *As built (P2-07):* every enrichment run stores its plan (ordered contact ids, the ids completed so far, status, a cancel flag) in `settings_kv` under `linkedin.enrich.<account>.plan.<id>` until `sync_run` exists (P2-10), which takes it over. A contact is recorded completed in the same transaction as its harvest. A resume re-reads each contact's slug (a sync may have seen a rename), leaves out contacts that can no longer be visited, runs against today's budget, and can itself be resumed. The cancel flag lives on the plan; a resume clears it. A run that ends by exception, including an in-page fetch that failed with no response to classify (`VoyagerFetchError`), marks its plan aborted and raises neither heat nor the session flag, since no response said anything about the session. No CLI command is wired yet.
  *As built (P2-10):* the plan moved onto the enrichment run's `sync_runs` row (`plan_json`: the contact ids in order and the ones completed); migration 0013 turned every unfinished `settings_kv` plan into an `aborted` run and dropped the finished ones. A resume (`netkeeper linkedin enrich --resume <run id>`, `POST /linkedin/runs/{id}/resume`) is a new run whose plan is the old one's remainder in the old order, with `resume_of_id` naming the old run; a plan is resumed at most once (the old plan records `resumed_by`), so two resumes cannot visit the same people twice, and a resume can itself be resumed. Cancel is `sync_runs.cancel_requested_at` (`netkeeper linkedin cancel <run id>`, `POST /linkedin/runs/{id}/cancel`), read by both runners between units and inside their waits, which are sliced to 5 seconds; because it is in the database, a cancel from one process stops a run in another. A run that ends by exception is recorded `failed`; one cancelled from outside (the process shutting down) `aborted`, "interrupted".
- Tab loss: `_ensure_page()` before every navigation; reopen in the same context at the last profile URL; at most one full reattach per run, then `BrowserUnavailable` aborts the run and the scheduler parks a retry 20 to 50 minutes out.
  *As built (P2-10):* the worker (`netkeeper/worker.py`) records such a run `failed` (`browser_unavailable`, or `browser_busy` when another process holds the account's lock) and answers the scheduler "retry later"; the scheduler parks one retry 20 to 50 minutes after the attempt, unless the kind's next due time is already sooner. A manual run gets no retry: the person sees it failed and why.

### 9.10 Boundary with the core

Nothing under `linkedin/` imports ORM models or opens a database session. The core hands a job a spec and receives results and events:

| Direction | Type | Contents |
|---|---|---|
| In | `SyncJobSpec` | mode full/incremental, known URNs (incremental only), page budget |
| In | `EnrichJobSpec` | ordered list of `(contact_ref, li_public_id)` to visit, visit budget, pacing profile, heat multiplier |
| In | `InboxJobSpec` | conversations since timestamp |
| In | `MessageJobSpec` | recipient, rendered body, mode prefill/auto_send |
| Out | `ConnectionsPage`, `ProfileHarvest`, `InboxDelta`, `MessageOutcome` | Plain dataclasses; the core's `crm/apply.py` maps them onto contacts, snapshots, and messages inside a session |
| Out | `ProgressEvent` | For `sync_run.progress_json` and the SSE stream |

The reason is in [Multi-user readiness](#multi-user-readiness): in a hosted deployment the browser and the database are on different machines. Keeping the boundary explicit now costs one mapping module and buys a `netkeeper agent` later. It also makes the extractor testable with fixtures and no database.

## 10. CRM

### 10.1 Contacts page

A dense table with server-side filtering, sorting, and pagination. Columns are configurable and saved as views. Row actions: tag, add to list, set met, set do-not-contact, pin for enrichment, open on LinkedIn, open in Gmail. Bulk actions apply to the current filter, with a confirmation that states the count.

`GET /contacts/stats` (`netkeeper contacts stats`) gives a dashboard the same counts and triage progress the triage queue's progress bar shows, from the one function, `contact_stats()`, both share (#90; before this item the CLI and the API computed the triage numbers separately and disagreed, #85).

### 10.2 Triage

The Step 6 replacement. A focused screen that shows one contact at a time:

- Name, headline, company, location, connected-on, profile photo.
- Evidence panel: LinkedIn message history (from the archive or the inbox poll), prior interactions, shared companies from positions, the you-and-them overlap from the user's own career, notes.
- Keys: `m` met, `n` not met, `s` skip, `u` undo, `t` tag, `p` edit preferred name, `→` next, `←` back, `?` the map itself.
- The same actions as buttons, each printing its key, so the map is learned by using the screen rather than by reading it.
- Position on the card: which contact of the run this is, and how much of the queue is left.
- The queue itself: the contacts already passed with the decision each carries, the one on screen, and the rest of the queue in the order it will be served. Any of the passed can be opened.
- Progress: triaged / total, with a filter to revisit `skip`.
- Bulk suggestion: "You have message threads with 143 untriaged people. Mark them all met?" Applied with one click, reversible.

**Triage starts from what is already decided (P1-22).** An imported archive is six hundred cold cards, and asking six hundred questions by hand is the thing this screen exists to avoid. So the import tags what it can (10.3), and triage opens on the work already done rather than on the first stranger:

- **A catalogue of batches, strongest evidence first, every one of them arguing from something on file.** `met_with_messages` (you and they wrote to each other); `met_invitation_note` (an invitation carried a personal note one way or the other, and no thread followed); and one batch per tag the user has given a meaning (`tags.met_signal` is `met` or `not_met`, and a tag with no meaning offers nothing). A batch matching nobody is not offered. Counts are taken against the queue as it stands, so accepting one shrinks the others, and where two batches would disagree about a person, whichever is accepted first decides them.
- **Nothing is decided from an absence (#142).** Triage is an affirmative pass — the method says *mark the people you have met*, and not-met is the residue rather than a judgement anybody makes — so having no message history on file is not evidence of never having met someone. There was a `not_met_no_evidence` batch here until CP2.5's first real run; it is gone, along with the clauses that existed only to hold people back from it. A tag batch may still decide `not_met`, because that is the user's own declared rule about their own label rather than an inference netkeeper drew, and `n` still decides one contact not met by hand. `TriageDecisionKind.BULK_NOT_MET` stays, and undo still takes back a `not_met_no_evidence` batch already on disk: undo walks `triage_decisions` by `batch_id` and restores `before_state`, so it never looks a suggestion key up in the catalogue.
- **Nothing applies itself.** A batch is a count, and `GET /triage/suggestions/{key}/contacts` is the list of names behind it, before anything is written. The apply carries the count the banner showed and refuses with `409` when the set has moved since.
- **Every automatic decision says so.** A batch writes `contacts.met_source = automatic` beside `met`, and one `triage_decisions` row per contact carrying the batch id and the key of the suggestion (`reason`). So the log answers "what did netkeeper decide for me, when, and why", a decision netkeeper made is never mistaken for one made by hand, and one undo takes the whole batch back to exactly where each contact was.
- **A review pass, not a cold start.** `GET /triage/next?decided_by=automatic` serves those contacts back on the same cards with the same keys, so the manual pass checks what was decided. Deciding one by hand makes it `manual`, which is how it leaves that queue; undo puts it back. `progress.automatic` is how many are waiting.

An invitation is stored as an `li_in`/`li_out` interaction like a message, distinguished only by the marker its summary starts with, so the message batch excludes them: clicking Connect is not a conversation. Against the reference archive that is 171 people rather than 178.

**What the screen does with it, as built (P1-28).** A line above the card says how many contacts netkeeper decided and offers the queue that walks them, which is a fourth filter beside Untriaged, Skipped and Both; in it, the same line says what this queue is and that answering a contact yourself is what takes it out. No batch is offered there, because a batch only ever reaches contacts nobody has answered for and asking for one over `met` is a `422`. Each offered batch has its own verb — a batch that decides `not_met` says so on its button — and a **See who** control lists the first ten names behind it with a count of the rest, because "which 171?" has no answer anywhere else on the screen. The list ahead is exact in the review pass too: `met_source` is a filterable field, so the same column `decided_by` narrows on is the one `POST /contacts/query` reads, and the panel shows the contacts this queue will actually serve rather than an approximation. That also makes "what did netkeeper decide for me" a question the contacts table answers, not only triage.

**The card is three steps, in the order you work them (CP2.5, #142).** The met/not-met call is the point of the screen and the *last* thing to do on a card, because fixing the name and putting the tags on is what you notice while you are looking at the person. So the card reads top to bottom: **(1) Name**, with the preferred name and an editor that opens in place; **(2) Tags**, with the tags already on and a picker that opens in place; then **(3) Have you met *them*?**, which is the loudest thing on the card. The name editor and the tag picker are sections of the card rather than overlays under it, and step 3 is the **only** place the three answers are offered: the button row above the card leads with Name and Tag and carries no decision at all, because a copy of Met/Not met/Skip up there would put the met call above the very things this order exists to put first, and two rows of the same three buttons are two answers to "where do I decide?". With no card on screen there is nothing to decide about — the empty queue offers what does make sense there, and a stray `m` still says "no contact is on screen yet" rather than doing nothing silently. The `?` overlay reads the whole key map, so it still teaches all nine keys whichever row draws their button. **No key changes**: `m`, `n`, `s`, `t`, and `p` all still fire from anywhere on the screen, so a trained run costs exactly what it cost before and only the reading order moved. Each decision carries its own one-line definition, and a collapsible explainer above the card says what the screen is for in the method's own words ([networking-workflow.md](networking-workflow.md#stage-1-validate-your-network), stage 1): met is anyone you have spoken with in person, on a video call, or in a real conversation, once counts, and seniority, usefulness, and how long ago it was are all beside the point. Its goal line is always on screen; the three definitions are folded behind a toggle and stay open once opened, per browser. **The decision row has to be reachable without scrolling**, and that is what decides the rest of the layout: at 1280x800, including the shell's top bar and with a bulk banner showing, the whole row sits at y=703-735 of an 800px viewport. Three things keep it there and are worth not undoing — the explainer's definitions start folded (they cost 116px open), the notice band sits *below* the card rather than above it, and the card and its steps share one bordered box rather than two. A fourth keeps it *still*: the headline is clamped to two lines with two lines always reserved, because it is the one part of the card whose height follows the contact, and without that a wrapped headline moved the buttons 40px down on that card alone — so the mouse had to re-aim on every contact. jsdom has no layout, so the suite holds the structure and the number is measured in a real browser.

**A contact read off a card is reviewed on the card (#184).** A contact the connections sync created from a connections-page card (9.8) carries `needs_review_at`, and `TriageContactOut` says so. The card shows a **Needs review** badge on its State line, which never adds a line, and the answers lead the column *beside* the card, above the evidence panel (under the card on a screen narrower than `lg`), which moves nothing above the decision row and keeps them on screen at 1280x800: **Confirm** (`POST /contacts/{id}/confirm`) clears the mark; **Reject and archive** (`POST /contacts/{id}/reject`) archives the contact, never deletes it, and keeps the mark, so unarchiving brings back an unconfirmed contact. Neither is a triage decision: the contact stays in the queue for the met question, neither goes on the undo stack, and a rejected contact leaves the queue like any archived one (`→` moves on). The contact page shows the same badge and band, the Contacts table a badge beside the name, and `needs_review_at` is a filterable, sortable field (not empty = waiting).

**Liveness is on the card, not inferred (#91).** `TriageContactOut` carries `archived_at` and `merged_into_id`. The queue is `met IN states AND archived_at IS NULL AND merged_into_id IS NULL`, so those two fields are the whole difference between a restored contact that is back in the queue and one that is not. `TriageUndoOut.forced` never meant that — it lists every contact whose divergence a force overrode, an ordinary field edit included — so a client that read it as certainty put a notice about a contact that had left the queue in front of one that had not.

**Going back is not undo, as built (P1-23).** The CP2 walkthrough asked for both and they are two different things, so the screen keeps them apart by name and by effect. `←` is a **cursor over this run**: the client keeps every card that has left the front of the queue — whole, evidence included, up to the last hundred — with the decision it carries, and `←` walks it. Stepping back sends no request, writes nothing, and leaves the undo stack exactly where it was; the card says which decision the contact already holds and that coming there changed nothing. Deciding from there is an ordinary write on that contact, which the service records as another decision row, so undo still walks back one at a time; the cursor then steps forward, so `← m` returns you to where you were. `u` is the other thing: it asks the server to take back the newest **write**, whatever card is on screen — the stack is server-side, so on a fresh page it can reach a decision from an earlier session, and the line under the buttons says what it will take back. Every move of either kind says what just happened in one live line, which is what stops the two from being confused.

**Where the contacts ahead come from.** There is no triage endpoint that lists the queue — `GET /triage/next` serves one card and its successor — but the queue is not a private ordering, so the list does not have to be guessed at. `netkeeper.crm.triage._queue_where` is `met IN states AND archived_at IS NULL AND merged_into_id IS NULL`, ordered by `id`, and `POST /contacts/query` compiles to exactly that: `met` is a filterable enum field, `compile_where` adds `merged_into_id IS NULL` unconditionally and `archived_at IS NULL` unless `include_archived`, and `apply_sort` ends every ordering with `id ASC`, so an empty `sort` *is* the queue's order. The screen takes a hundred-row page at load and asks for another only when the tail it left runs short, which costs about seven reads across a six-hundred-person run. A contact further down that list cannot be opened: reaching them means passing everybody in between, which is a decision about thirty people rather than a navigation.

Two limits are worth naming. The trail is the client's own memory of the run: a reload starts a new one, and it keeps the last hundred cards — the position counter is the run's own and does not stop there. And a decision whose write is refused leaves the contact in the trail, marked, so `←` reaches them for another go; nothing was lost server-side either way, and the banner says so.

**Two company signals on the card, and they are not the same claim.** The evidence panel carries both, under different names, because having met someone is a separate dimension from having overlapped at an employer — someone can work at a large company alongside a connection for years without ever meeting them, and can also know someone they never worked with at all (CP2 decision).

- **`shared_companies`, as built (P1-09, #84).** Overlap with **the rest of your address book**, not with you: for each company the contact is at now or has been at, how many other live contacts are at that company and how many of those you have already marked met ("you know four people there, three of whom you have met"). Real triage evidence on its own, but not the LinkedIn "you both worked at X" signal the name suggests, and a UI must not label it that way. Two asymmetries follow from how it is counted: the contact's side uses `current_company` plus every one of their positions, while the other contacts are matched on `current_company` only, so somebody who was there with them and has since moved on is not counted; and a company with no overlap is still returned, with `contact_count: 0`, so a contact with ten past employers yields eleven entries and the client filters the empty ones. Unchanged by P1-26: this field, its name, and its behavior stay exactly as they were.
- **`worked_together`, as built (P1-26, #84).** The actual "you both worked at X" signal, now that `user_positions` (8) gives netkeeper a source for the user's own career: computed from the user's positions against this contact's own positions and current company, matched on company name loosely — case, punctuation, and one trailing legal-entity suffix folded away, so "Acme, Inc." and "Acme Inc" count as the same company — because neither source spells a company the same way twice. A match is only reported when the two sides are not *provably* disjoint (a stint known to have ended before the other started is excluded). Every entry carries `confirmed`: years (`started_on`/`ended_on`, the later of the two starts and the earlier of the two ends) are only ever populated from a pairing where **both** sides carry an actual date, never borrowed from whichever side happens to have one. In practice, until something other than the archive importer gives a contact dated positions, the contact's side is usually only a bare `current_company` with no date at all — real evidence a match is possible, since it can never be disproven, but no evidence of *when* — so most matches today come back `confirmed: false` with both dates `null`: "you were both at this company at some point," not a claimed range. An earlier draft of this field computed a range from the user's own dates in exactly that undated case; that was a fabrication (a contact who joined years after the user left would have come back as having overlapped during the user's tenure) and the pre-merge review of #127 caught it before merge.

### 10.3 Tags and auto-tag rules

Manual tags are yours. Auto-tag rules run on every contact create and every enrichment, and on demand. **As built (P1-22, #64):** an import runs them itself, over the contacts it created or enriched and no others, inside the same transaction — so an address book is tagged the moment it lands and a failed import takes its tags with it. The counts are in the archive import's report and on the `import_runs` row (`tagged_contacts`, `tags_added`, `tags_removed`). A contact created by hand, or through the extractor's sync, still waits for a run.

A tag can also carry `met_signal`: the user's own reading of their own label, `met` or `not_met`, which puts a triage batch on offer (10.2). It is the only thing that turns a tag into a decision, and it never does so by itself — the batch is previewed and accepted like any other. Default rules ship for c-suite, vp, director, founder, investor, recruiter, engineering, product, design, sales, marketing, consultant, and academic. Rules are editable in the UI with a live "matches 214 contacts" preview. An auto-tag you remove is suppressed for that contact; a manual tag is never touched by rules. LLM tags (section 12) are a third kind with the same suppression behavior.

### 10.4 Lists and the filter language

Smart lists store a filter tree serialized as JSON and compiled to SQLAlchemy. Supported predicates: field comparisons, tag any/all/none, `met`, `has_email`, `has_phone`, `has_li_url`, `last_contacted before/after`, `enrolled_in`, `replied_in`, `changed_jobs_within`, `connected_on` ranges, `list_member`. The same filter powers the Contacts table, campaign enrollment, and exports, so "the people who get this campaign" is always a visible, re-runnable definition.

Static lists are explicit membership with an `added_at`, for the "First 100" style batches in the training.

**Choosing the list, as built (P1-28).** The builder has a picker over `GET /lists`, so `list_member` is an ordinary predicate rather than a grey row with an explanation. `list_id: 0` is what the palette creates and no list has that id, so a row left alone selects nobody rather than quietly selecting whichever list is first; a saved filter naming a list that has since gone keeps the id and says so, because dropping it would change what the filter means. The same predicate is what a static list's **Export** sends — the button that was withheld while the compiler refused `list_member`, since the only filter the screen could have sent was "everyone".

**How `list_member` compiles, as built (P1-27).** A static list becomes a correlated `EXISTS` over `list_members` for that id, which needs nothing but the id. A smart list is its stored tree, inlined into the filter being compiled — so a list defined in terms of another list is one flat statement, not a query per contact and not a materialized set. Three rules follow from making the predicate mean what the list page means:

- **Liveness.** A static list's members are live contacts: archived or merged away, they are not members, whatever `include_archived` says on the filter around them, because a membership row outlives either. A smart list's own `include_archived` decides its membership, and the outer filter still applies its own on top. So `list_member` selects the list's members and the surrounding tree then rules on them like any other predicate: the two readings agree for every list whose membership excludes archived contacts, and a smart list that sets `include_archived` itself is the one case where its page can show someone that a filter around it, at the default, will not return.
- **Cycles.** Two lists could otherwise define each other. The compiler carries the ids it is already standing in for and refuses a reference back to one. A cap on how many lists one filter may pull in (32) bounds the other blowup, a chain of lists each naming the one below it twice.
- **A reference to a list that is not there** — deleted, or another user's — matches no contact, the way a tag name nobody has used does. Turning it into an error would mean deleting one list could 422 every page that reads another.

**Which leaves the writes, which is where the danger actually is.** A filter that compiles is not the same as a filter that is safe to store, because storing one can break a *different* list. Three guards, all in `netkeeper.crm.lists`:

1. A `list_member` naming a list the user does not have is refused **at write time**, even though compiling one is deliberately lenient. Ids are handed out in order, so the id a new list is about to be given does not exist while its own filter is being validated: a forward reference was accepted and then handed that very row, which closed a cycle in two ordinary `POST`s.
2. `update_list` tells the compiler which list it is about to become, so a tree that reaches back to its own list is refused at the write that would close the cycle.
3. Every list write compiles the user's smart lists before and after itself and refuses to be the change that broke one. The first two guards look *down* from the tree being stored and cannot see the lists that name it: one at the expansion cap is broken by an edit one link below it, whose own save costs a single inline and passes. Comparing against what was already broken, rather than against "nothing is broken", keeps a list that is already unreadable from blocking the edit that would fix it.

And should a broken list exist anyway — data edited around the API — `GET /lists` marks that one list `broken` with the reason instead of failing: it is the page someone would use to find and delete it, so one bad list may not hide the rest. Asking about that list by id still answers 422.

Ids are not reused by a sequence but are by SQLite, so a reference whose list was deleted can come to name a later list with that id. The guards above refuse the version of that which breaks something; deleting a list logs the lists that named it, so the version that does not break anything is at least visible.

Because a smart list's tree lives in the `lists` table, compiling a filter is no longer a pure function of the tree: it takes a session. It reads nothing for a filter with no `list_member` in it, and one indexed row per distinct list id for one that has, at compile time rather than per contact.

### 10.5 Import

1. Upload a CSV or the LinkedIn archive zip.
2. Pick a preset or map columns by hand; mapping is saved as a new preset.
3. Preview: the first 20 rows resolved (matched, created, candidate), counts for the whole file.
4. Resolve candidates one by one or accept all creates.
5. Commit inside one transaction. `import_row` keeps the raw row and the decision, so any import can be audited or rolled back by run.

A file that is only inspected or previewed writes nothing, but step 3's draft is a real row: reading a file, or a commit refused for undecided candidates, leaves an `import_run` in `draft` status and its rows behind. `GET /imports?status=draft` (`netkeeper import runs --status draft`) finds these; `DELETE /imports/{id}` (`netkeeper import rm`) removes one; and a refused commit is finished, not re-read, with `POST /imports/{id}/commit` on the same id (`netkeeper import resume`) — the same run the web wizard's "Finish this import" resumes (#90).

**What a rollback refuses (#78).** A rollback undoes one run, and it is refused whole, with nothing undone, in three cases, each answered with a `409` whose `code` says which. `merged`: a merge has drawn in a contact the run created, so deleting it would take rows the run never created; undo the merge first. `superseded`: a later run, still committed, wrote over a field this run wrote on a contact it enriched. Each row records the value it found, so undoing the earlier run first would leave the later run's rollback putting back a value nothing backs; the refusal names the runs to undo first, and runs that overlap roll back newest first. `created_contacts_changed`: a contact the run created has since gained interactions, tags added by hand, list memberships, triage decisions, the person's own edits, details added later, or changes from a later import, which deleting it would take with it; the refusal counts them, and `force=true` (`netkeeper import rollback --force`) rolls back anyway. `force` overrides only that last one.

Field-level provenance: an imported value never overwrites a value from a more authoritative source. A field with no recorded source is open to every source; that is the lowest provenance, and it is how a connections-page card's text is written (9.8, #184). For a LinkedIn field a manual edit sticks: once you edit it, no later sync, archive, or CSV import overwrites it until you revert it (CP1 decision, #28). Clearing a field by hand is an edit too and sticks the same way. The last value each automated source reported is kept per field in `contacts.synced_values` (value, source, `observed_at`), whether or not it reached the live column, so a revert restores LinkedIn's value and its provenance. Among the automated sources, sync > archive > csv. For `preferred_name`, `notes`, `met`, and tags, manual wins and no import touches them. `met` carries `met_source` beside it, saying whether the person decided it or a triage batch they accepted did (10.2); no import writes either.

The archive zip's upload does not go through steps 2-5 above: `POST /imports/archive` (P1-20) reads it straight from the upload FastAPI already received and runs it through the LinkedIn archive importer (spec 9.2; `crm/archive.py`) in the request's own transaction, answering per-table counts. There is no mapping, preview, or candidate-review step for it, because that pipeline has none — a connection row that resolves to a candidate is counted and left for a later CSV import to resolve instead (see that module's docstring). The CLI's `netkeeper import archive` runs the same importer on a path (a zip, a directory, or one CSV) and reports the same counts for the same archive.

**The wizard's two shapes (P1-21).** Dropping or choosing a file routes it one of two ways, and the screen says which before anything is sent. A `.zip`, or a lone `messages.csv` or `Invitations.csv`, goes to `POST /imports/archive` above: a short "recognize, then import" screen with an explicit Import button — nothing is sent on drop alone — followed by a result screen with each table's own counts (so a headline number like "616 contacts" traces back to `Connections.csv` rather than reading as one opaque total) and the files the archive carried but did not read. `Connections.csv` on its own is deliberately *not* routed to the archive endpoint even though that endpoint would accept it (it dispatches a single CSV upload by sniffing its bytes, the same way the CLI's path argument does): the archive importer's `needs_review` connections have no resolution path of their own, so a lone `Connections.csv` keeps going through steps 2-5, which is the one place a candidate can actually be decided. *As built (#132):* an archive import records an `import_run` of kind `archive`, committed in the same transaction as the import, so it has a history entry and a run page, and rolls back from there like a CSV import (`netkeeper import archive` prints the run id). Each `Connections.csv` row is an `import_row` recording what it did in the CSV rows' shape (a contact it created, or the values it found on one it enriched); the interactions the message and invitation tables wrote are recorded on the run by id (`import_runs.created_json`), because each belongs to a contact rather than to a row, and a rollback deletes them. The run's counts describe `Connections.csv` (updated counts as matched, needs review as candidate), and `import_runs.report_json` keeps every table's counts, which the run page shows. A rollback leaves the user's own job history from `Positions.csv` alone: it is not about a contact, and the import only upserts it. Each of the endpoint's 422s (an export the endpoint does not recognize, a zip guard, an unreadable file) gets its own plain-language explanation in the UI, with the backend's own message kept alongside it. A zip whose central directory lost entries in transit is refused as `damaged` too (#138): `zipfile` reads such a directory without complaint and simply drops the records a garbled length swallowed, so before the archive is opened its directory is walked the way `zipfile` walks it, and the upload is refused when the records found disagree with the count the End Of Central Directory record declares, or a record's name is not the one its local file header carries (`crm/archive_check.py`; nothing is decompressed for it).

**Getting the archive (P1-21).** The upload screen carries this for anyone who has not requested one before, so it is not only in the UI: in LinkedIn, open **Settings & Privacy**, then **Data privacy**, then **Get a copy of your data** (the same path [networking-workflow.md](networking-workflow.md#stage-1-validate-your-network) already documents), and ask for the full data archive rather than Connections alone — netkeeper also reads message and invitation history, which a connections-only export leaves out. The download link arrives by email, usually well under a day; budget for 1 to 24 hours. For someone who already extracted the zip by hand: the whole zip is still the easiest choice, but `Connections.csv`, `messages.csv`, and `Invitations.csv` can each be picked on their own too, with the routing above. The archive path doesn't read anything else LinkedIn's export includes (skills, positions, education, and the rest of `ignored_files`) yet, though a file like that can still be picked and mapped by hand through the CSV pipeline.

### 10.6 Export

Presets: `nine-column`, `linkedin-archive`, `full`, `campaign-audience`, `macos-contacts`. `campaign-audience` never includes a `do_not_contact` contact or one waiting for review (#184). Formats: CSV, JSON, vCard 4.0. `macos-contacts` (P6-02, #249) is vCard only, and 3.0 rather than 4.0: a card per contact with a stable `UID` and its tags in `CATEGORIES`, then a group card per tag (`X-ADDRESSBOOKSERVER-KIND:group`), and no `do_not_contact` contact. [macos-contacts.md](macos-contacts.md) has the import steps. Exports respect the current filter and strip internal counters. A list exports its own members through `list_member` (10.4): the same people its page shows, except for a smart list that sets `include_archived` itself, whose archived members need the export's filter to ask for them too.

An export is streamed, so everything that can refuse it has to happen before the first chunk: the filter is parsed and compiled while the request can still become a `422`. Once a `200` is on the wire a failure can only truncate the file, which is worse than an error because nothing about it looks like one (P1-27).

## 11. Campaign engine

### 11.1 Templates and merge fields

Jinja2 in a sandboxed environment with autoescape off for plain-text email and on for HTML. Merge fields:

- Contact: `first_name` (resolves to `preferred_name`), `last_name`, `company`, `title`, `location`, `connected_year`, `years_since_connected`, `last_position_change`.
- You: `me.name`, `me.website`, `me.scheduling_link`, `me.signature`, `me.city`, plus any keys you add under `[me]` in config.
- Campaign: `campaign.name`, `step.number`, `previous_send_date` ("last week" phrasing is a filter: `{{ previous_send_date | ago }}`).
- Optional LLM: `{{ personal_line }}` (section 12), rendered at preview time and stored with the message.

Template lint at save time: undefined variables, a body with no per-contact merge field (identical bulk mail is a spam signal), missing subject on email, links that do not parse. Lint results are shown in the editor and block activation for errors.

*As built (P3-03):* `netkeeper/campaigns/render.py`. The template language is an **allowlist**, checked by one walker that runs as lint and again before every render. A template may contain only:

- text and `{{ }}` output;
- merge-field names;
- literals: text up to 1,000 characters, whole numbers up to 10,000 bits, true, false and none;
- `me.*`, `campaign.name` and `step.number`, the only attributes;
- the filters `default`, `upper`, `lower`, `title`, `capitalize`, `trim`, `truncate` (length at most 1,000) and `ago`;
- the tests `defined`, `undefined`, `none`, `number`, `string`, `even`, `odd` and `divisibleby`;
- `{% if %}` and `x if y else z`; comparisons, `in`, `and`, `or` and `not`;
- `~` to join text, and `*` and `+` on whole numbers.

The tree may nest at most 50 levels. Everything else is an error, and the render refuses it too: `with`, `for`, `set`, `macro`, `call`, `filter`, `autoescape`, lists, tuples, dicts, calls, subscripts, the other operators and filters, and `self`.

The sandbox bounds what the allowlist lets through a second time:

- `~` and filter results count against one budget of 100,000 characters per render as the text is built. The compiler routes `~` through the environment, which stock Jinja does not.
- `*` and `+` refuse anything but whole numbers, and any result over 10,000 bits.
- The output stops at 100,000 characters.
- There are no globals, and an unsafe attribute fails the render.

All four save-time rules are errors. A field with no value renders as an empty string and adds a warning to the preview; whatever an allowed template does with it (arithmetic, a comparison, `in`), it never raises. The rendered subject is one line: every line break in it becomes a space, so no merge value can add a header.

`connected_year` and `years_since_connected` (whole years) come from `connected_on`. `last_position_change` is the latest `started_on` or `ended_on` on or before today among the contact's positions, because leaving a job is a change just as starting one is. A date after today does not count yet, whether it is an announced departure (an `ended_on`) or an announced new job (a `started_on`): "congrats on the move" before the move is wrong (#255). Positions with no dates are invisible to it, and the end of a side role counts even when the main job is unchanged (#232). `ago` counts whole UTC days: today, yesterday, N days ago, last week (7 to 13 days), N weeks ago, last month (28 to 59), N months ago, last year (365 to 729), N years ago.

Activation lints again rather than reading `lint_json`, because the `me.*` keys can change with the config.

*As built (P3-10):* the editor lints as you type through `POST /templates/lint`, which lints unsaved text exactly as a save of it would and stores nothing (a read-only `POST`). The preview renders a *saved* version for the contact you pick, as plain text, so an unsaved edit is not in it until you save. The version history walks `previous_id` from the current version. *As built (#243):* the list marks each template in use, and the editor says before a save that it will create a new version ("Save as version N"). An unsaved draft is never discarded without a confirm: opening another template, starting a new one, or leaving the page asks first, and closing the tab gets the browser's prompt (`components/unsaved-changes.tsx`, the pattern for any editor).

Autoescape is off, because every template is plain text today. **An HTML body needs autoescape on.** Merge values come from imported data, so without it a contact field could inject markup into the message. Whoever adds HTML email must render the HTML part with autoescape on, not reuse this plain-text environment as it is.

### 11.2 Sequences

A campaign is an ordered list of steps. The default sequence matching the training:

| Step | Channel | Delay | Mode | Condition |
|---|---|---|---|---|
| 1 | email | 0 | `draft` or `send` | always |
| 2 | email | 7 days | same mode, `same_thread = true` | no reply |
| 3 | linkedin | 7 days | `prefill` | no reply |

### 11.3 Enrollment state machine

```mermaid
stateDiagram-v2
  [*] --> pending: enrolled (guards passed)
  pending --> active: campaign activated
  active --> active: step fired, next_action_at advanced
  active --> replied: reply detected on any channel
  active --> completed: last step fired, no reply
  active --> bounced: bounce detected
  active --> opted_out: do_not_contact set or unsubscribe phrase detected
  active --> paused: campaign paused
  paused --> active: campaign resumed
  active --> removed: manual removal
  replied --> [*]
  completed --> [*]
  bounced --> [*]
  opted_out --> [*]
  removed --> [*]
```

`next_action_at` for step n+1 is computed from the actual `sent_at` of step n, not the scheduled time, and is then pushed into the campaign's send window.

*As built (P3-06):* `netkeeper/services/campaign_engine.py`. `enroll` makes `pending` enrollments of the contacts the guards pass, and only while the campaign is `draft` or `reviewing`. `activate` takes a `reviewing` campaign with `approved_at` recorded, steps, a mailbox for its email steps, and lint-clean templates to `active`, and each `pending` enrollment to `active`, its first step due after the step's delay inside the window. `next_action_at` counts from the enrollment's **latest** sent outbound message, not only step n's: after a merge the enrollment can hold a newer message from the other contact (#242 review). A step still waiting (a draft nobody has sent) leaves `next_action_at` empty until `schedule_next` sees it sent (P3-07). A campaign pause holds on the campaign, not on its enrollments: the tick fires only for an `active` enrollment of an `active` campaign, so nothing fires while it is paused, and each enrollment keeps its own state and `next_action_at`. Pausing the enrollments too would lose, on resume, which of them a person or a merge had paused on their own. At a step fire, a do-not-contact contact moves the enrollment to `opted_out` and a bounced address to `bounced`; a reply found on the enrollment moves it to `replied`.

### 11.4 Scheduler and send windows

- A campaign tick runs every minute. It selects enrollments with `next_action_at <= now`, inside the campaign's send window, in a stable order, up to the remaining per-campaign and per-mailbox daily cap, and up to a per-tick batch of 1.
- Spacing between sends is `human_delay` with a median of 4 minutes and a floor of 90 seconds; the tick skips when the last send was too recent. Sends are never a burst.
- Default window: Tuesday to Thursday, 09:00 to 16:30, local time. Per campaign override. A holiday list in config.
- Per-mailbox cap default 80 recipients per local day across all campaigns, hard max 400 (Gmail's consumer limit is 500 and the account can be locked for less if the mail looks bulk).
- The tick and the next fire time are persisted so a restart never skips or doubles a send.

*As built (P3-06):* `CampaignEngine` runs `run_tick` every minute under `netkeeper serve`, in a worker thread, so no SQLite write happens on the event loop (#259). It fires only through the `Sender` it is given; the Gmail one is P3-07's, and without one the tick does nothing. Per local user, per tick:

- **Selection** is on status: an `active` enrollment of an `active` campaign with `next_action_at <= now`, oldest due first. A held pause keeps its due time, so the due time alone selects nothing (#242 review). A LinkedIn step is left unfired with the reason `linkedin_step` (P4). LinkedIn rows, and those of a campaign or mailbox blocked this tick, are left out of the query itself, so however many there are they never crowd out a row that could fire. A spacing median or floor that is not a positive number holds every send, checked before anything is claimed.
- **The window** (`netkeeper/campaigns/schedule.py`) is `[campaigns] send_window_days` and `send_window_hours`, overridden per campaign by `send_window_json` (`{"days": [...], "hours": [start, end]}`), in the user's time zone (`linkedin.timezone`). The hours are `[start, end)`. `[campaigns] holidays` are local dates with no window. A step due outside it is deferred to the next opening. A window that cannot be read, or never opens, sends nothing for that campaign.
- **Caps** count per local day, every outbound message fired that day whatever became of it (a failed send may still have gone out). The mailbox's `daily_cap` counts email across all its campaigns, never over 400. The campaign's is `daily_cap`, or `[campaigns] mailbox_daily_cap` when that is unset.
- **Spacing:** one firing per tick, then `human_delay` with a median of `send_spacing_median_s` and a floor of `send_spacing_floor_s`, counted from when the message went out. The next send time is persisted per mailbox in `settings_kv`, and the floor after the last firing holds even without it.
- **Never twice.** A step with an outbound message on the enrollment, of any status, is refused and parked (`next_action_at` cleared), whatever `current_step` says (#242 review). A `discarded` one counts too, because a merge can discard a `scheduled` message the sender is holding. The message is written `scheduled` and committed before the sender is called, so a crash between the two leaves the step claimed rather than free to fire again. Whether it went out is P3-07's to reconcile. A failed send is recorded on the message and parked, never retried by the tick.
- **Guards** run at every fire (11.9). An exclusion that can pass (contacted recently, waiting for review, another campaign, a duplicate address) is checked again a day later.
- `simulate_campaign` replays the tick on a virtual clock; three weeks of a 100-contact campaign keep every window, cap and cadence (`tests/test_simulate_campaign.py`). `netkeeper simulate` for campaigns is P3-13.

### 11.5 Gmail integration

**Auth.** OAuth installed-app flow with a loopback redirect, run from the Settings page or `netkeeper gmail login`. Scope: `https://www.googleapis.com/auth/gmail.modify` (read, compose, send, labels; no delete). Refresh token stored in Keychain under a per-mailbox key.

**Consumer account specifics.** You create your own Google Cloud project and OAuth client. With the consent screen in *Testing*, refresh tokens expire after 7 days; with the app *published* and unverified, you click through a warning once and the token persists. netkeeper detects `invalid_grant`, sets the mailbox to `reauth_required`, pauses email steps, and shows a banner with a re-auth button. Setup docs walk through both options and recommend publishing.

*As built (P3-01):* `netkeeper/campaigns/gmail_oauth.py` runs the flow over the standard library, with no Google SDK: PKCE (`S256`), a random `state`, `access_type=offline` and `prompt=consent`, so every authorization, including a re-authorization, returns a refresh token. The flow refuses a result without the Gmail scope (Google lets a person untick it) and asks `users.getProfile` for the account's address. That call also catches a Gmail API that isn't enabled. The OAuth client (a **Desktop app** client; a Web client is refused) is stored in the Keychain beside the token, as `gmail/oauth_client`. From the Settings page, the redirect is `/api/v1/mailboxes/oauth/callback` on the host the page used. From `netkeeper gmail login`, it's a one-shot server on `127.0.0.1` and a free port, and the CLI prints the URL rather than opening a browser. A pending web authorization lives in memory for ten minutes and is handed out once, to the user who started it. The access log never shows the callback's query string, which carries the code, however its path is spelled (a trailing or repeated slash, another case). `netkeeper serve` refreshes every `ok` mailbox's token every `[campaigns] reply_poll_minutes` (`MailboxMonitor`). `invalid_grant`, `invalid_client` or `unauthorized_client` (the codes that mean the grant or client is dead, `REAUTH_CODES`), or a token or client missing from the Keychain, sets `reauth_required` and publishes `mailbox.status`, which the banner listens for. The status is read before the body, so a 5xx, a `408` or a `429` is never read as a dead grant, and an error code is read only from a `400` or `401` body (RFC 6749 5.2). Those, a network failure, an answer cut off part way, and any other refusal (a proxy's `407`, a `403` or `404` with an HTML body, `invalid_request`, a `200` with no access token) change nothing: the next poll tries again. `mailbox_health()` is what the guards will read (P3-06). The setup guide is `docs/gmail-setup.md`.

*As built (P3-02):* `netkeeper/campaigns/gmail.py` defines the `Gmail` interface the engine takes: `profile`, `send`, `create_draft`, `get_draft`, `get_message` and `get_thread` (metadata headers, labels and snippet, never the body), `search`, `list_labels`, `create_label`, `modify_labels`, and `history` (`messageAdded`, every page). `GmailClient` implements it over `googleapiclient` with the bundled discovery document; `services.mailboxes.open_gmail()` builds one for an `ok` mailbox, renewing its access token with P3-01's `refresh_access_token`. Every call takes a `purpose` ("send step 2 for enrollment 14") and logs it with the method; a purpose with an `@` in it is refused, and `googleapiclient`'s own request logging is held at `WARNING`, because its URLs carry search queries. Failures are five types: rate limited (429, or a 403 rate or quota reason, or a 403 whose `status` is `RESOURCE_EXHAUSTED`, which newer answers can carry without a legacy reason), not found (404, including a history start Gmail no longer keeps), auth, transient (network, timeout, 5xx), and rejected (other 4xx, with conflict as a subtype). An auth failure, from Google's token endpoint or a 401 or other 403 from Gmail, sets the mailbox `reauth_required` through P3-01's path, unless the mailbox was re-authorized or disconnected meanwhile. A transient failure of a write (send, draft, label) carries `outcome_unknown`: the caller searches `rfc822msgid:` before trying again. Nothing retries by itself, the transport included: `httplib2` would silently send any request again when the connection drops before an answer, so the client's transport (`WriteOnceHttp`) turns that off for every method that is not idempotent, and sends each write on a fresh connection; it follows no redirect, and an answer cut short is transient with the outcome unknown (#267). `netkeeper/campaigns/gmail_fake.py`'s `FakeGmail` implements the same interface in memory, with Gmail's threading rule (a follow-up joins `threadId` only when `In-Reply-To` or `References` cites a message in it and the subject matches without `Re:`; otherwise it silently starts a thread), a growing `historyId`, draft-to-sent with a new message id, and the person's side (`reply`, `deliver`, `bounce`, `send_draft`, `discard_draft`). Its search reads `after:` and `before:` as epoch seconds only, since Gmail reads a date in Pacific time. Where P3-08 could be caught out, it behaves as Gmail does (#267): history keeps `messageAdded` for a message deleted since (a sent or discarded draft, a deleted reply), and reading one is not found; snippets arrive HTML-escaped (`don&#39;t`), and `Message.snippet` is the unescaped text in both implementations; `from:` and `to:` match whole words; only `in:anywhere` searches spam and trash. It also rewrites a `From` that is neither the mailbox nor a verified alias, refuses `DRAFT` and `SENT` in `modify_labels`, and accepts a draft with no recipient (sending it is what fails).

**Send.** `users.messages.send` with an RFC 2822 message built by `email.message`. Text and optional HTML alternative. Follow-up steps with `same_thread` set `threadId` and the `In-Reply-To` and `References` headers of the step-1 message, and a `Re:` subject, so the follow-up lands in the same Gmail conversation for both of you.

**Draft mode.** `users.drafts.create`, with `threadId` for follow-ups. The message row stores `gmail_draft_id` and status `drafted`. A drafts poll detects when the draft is gone and a message with the `SENT` label exists in that thread, then records `sent_at` from the message's internal date. Drafts you delete instead of sending set the message to `discarded` and the enrollment to `removed`.

**Labels.** One label per campaign, `netkeeper/<campaign name>`, applied to every outbound message and every detected reply. This is the training's "First 100 folder", created for you.

**Bounces.** A thread message from `mailer-daemon@googlemail.com` marks the message `bounced`, the email `status = bounced`, and the enrollment `bounced`. The contact stays eligible for LinkedIn steps.

**Quotas.** The API allows 250 quota units per second per user; a send costs 100. Irrelevant at netkeeper's pace. The daily recipient limit is enforced by netkeeper's cap, not by waiting for Gmail to refuse.

### 11.6 LinkedIn messaging

**Prefill (default).** The job opens the contact's profile in the sidecar tab, opens the message compose, types the rendered body with human-like keystroke timing, and stops. The message row is `prefilled`. The next inbox poll finds an outbound message in that conversation and marks it `sent`. If no send is seen within 3 days the message goes to `stale` and the UI lists it under "waiting for you". Only one prefilled message is left open at a time; the tab is not closed so you can find it.

**Auto-send (opt-in).** `campaigns.linkedin_auto_send = true` in config plus a per-step `auto_send` mode. The job clicks Send after a dwell, under the `li_messages_auto` budget, inside active hours, with heat applied. The Settings page shows this as a highlighted, non-default posture. LinkedIn restricts accounts for automated messaging more readily than for profile views; the docs say so plainly.

### 11.7 Reply detection

**Gmail.** Every 10 minutes while any email enrollment is active:

1. `users.history.list` from the last stored `historyId`, filtered to `messageAdded`. On a 404 (history expired), fall back to `users.threads.get` for each active thread and re-baseline.
2. A message in a tracked thread whose `From` matches any of the contact's emails and whose date is after the last outbound → `replied`.
3. Also `users.messages.list` with `from:<email> after:<step-1 date>` for active enrollments, because people sometimes reply in a fresh email. Bounded to active enrollments only, so this is a few dozen calls a day.
4. Inbound messages are stored (`direction = in`, subject, snippet, thread id, not the full body unless you open it) and an `email_in` interaction is written.

**LinkedIn.** The inbox poll (9.4) matches conversation participants to contacts and looks for an inbound message after the last outbound in that conversation.

**Unsubscribe phrases.** "unsubscribe", "remove me", "stop emailing" in an inbound message set `do_not_contact` with a reason and move the enrollment to `opted_out`. You can undo it from the contact page.

### 11.8 Review gate and test send

A campaign moves from `draft` to `reviewing` when it has at least one step and an audience. Activation requires:

- Rendered previews of 10 sampled enrollments viewed and approved, plus any enrollment you searched for.
- A test send of each email step to your own address, recorded on the campaign.
- Zero lint errors.
- Every guard result acknowledged: the screen shows "212 in audience, 37 excluded: 30 no email, 4 do-not-contact, 3 contacted in the last 30 days".

### 11.9 Guards

Checked at enrollment and again at every step fire, because state changes between them:

- Channel address present and not bounced.
- Not `do_not_contact`, not archived, not disconnected, not waiting for review (`needs_review_at`, a contact read off a connections-page card and not yet confirmed, 9.8; #184).
- Not in another active campaign (configurable to allow).
- No outbound message on any channel within `contacted_within_days_guard` (default 30) unless the message is the next step of this same campaign.
- Mailbox healthy and under cap; browser healthy and under budget for LinkedIn steps.

*As built (P3-05):* `netkeeper/services/campaign_guards.py`. Each guard is a pure function over a snapshot of one contact (`ContactFacts`) and gives a reason to exclude, or nothing. `load_facts` reads the snapshots in a few scoped queries and never writes. `check_enrollment` and `check_step` are the two moments above. A guard that cannot decide excludes: a contact nobody found, or a channel with no guard for its address. How each bullet above is decided:

- **Channel address.** An email step needs an address that is neither bounced nor invalid. It reuses `crm.contacts.sendable_email`, which the `campaign-audience` export also uses; the export refuses bounces only. A LinkedIn step needs a URN or a public id. Only the channel being sent on is checked, and enrollment checks the first step's channel.
- **The address, across contacts (#238, Part A).** Address statuses are per contact, so two more checks look past the contact to its sendable address. `address_bounced_elsewhere`: another contact of the user holds the same address as bounced or invalid (email steps only, like the address guard). `duplicate_address`: the address is already on another enrollment in this campaign, on every channel, because a shared address is one person. At enrollment every enrollment already in the campaign counts, and of a batch sharing an address only the first by contact id gets in; at a step fire only an older enrollment counts, so the older proceeds. An enrollment of any status holds its address, not only a live one, because one that ended may have been sent to; the enrollment of a contact since merged away does not. Both reasons are in the excluded summary.
- **Another campaign.** A live enrollment (pending, active or paused) in another campaign that is reviewing, active or paused. At a step fire, only an enrollment older than this one counts, so two campaigns that enrolled the same person never block each other at the same time. "Configurable to allow" is a `GuardPolicy` flag, but where the setting is stored is still open, so nothing turns it on yet.
- **Recent contact.** The newest outbound interaction (`email_out`, `li_out`, `call` or `meeting`) or sent campaign message is within `contacted_within_days_guard` days. At a step fire, only the firing enrollment's own messages are exempt, not the rest of the campaign's. After a merge, the survivor holds the merged-away contact's interactions, and a step that went to the other row still counts (#235 review). At enrollment time nothing is exempt. A window of 0 turns the guard off.
- **Only an active enrollment of an active campaign fires.** Anything else is excluded with `enrollment_not_active` or `campaign_not_active`, so a finished or paused enrollment is never sent to, even by a caller that forgets to filter it out.
- **Fresh reads.** Sessions keep their objects across commits (`expire_on_commit=False`), so the guards read the contact, its addresses, and the enrollment's and campaign's statuses fresh on every check. Nothing a caller is holding is trusted (#235 review).
- **Mailbox and browser.** `check_channel` works over a `ChannelState` its caller fills in, and anything left unknown excludes. Since P3-06 the engine fills it for email steps from `netkeeper.services.mailboxes.mailbox_health` and the mailbox's count for the day: a campaign with no mailbox, a mailbox that is not the user's, and one that is `reauth_required` or `disabled` pause its email steps, and nothing is marked sent. The browser side waits for P4.

`excluded_summary` writes the 11.8 line from the verdicts. Each excluded contact counts once, under its first reason, so the parts add up to the number excluded.

## 12. LLM module (optional)

Disabled until `[llm] api_key` is in Keychain. Uses the `anthropic` SDK, `claude-sonnet-5` by default, `claude-haiku-4-5-20251001` for bulk classification. The system prompt is cached.

| Feature | Input | Output | Where it shows up |
|---|---|---|---|
| Personal line | Contact headline, positions, your notes, the shared history you wrote, the template's intent | One or two sentences | `{{ personal_line }}` merge field, rendered at preview time, editable in the review gate, stored with the message. Never sent without the review gate |
| Classify title | Headline and current position | Tags from the existing tag set, with confidence | Applied as `kind = llm` tags above a threshold, otherwise suggested |
| Call prep | Everything on the contact, recent snapshots, message history | A short brief | Contact page, on demand |
| Triage suggestion | Message history, notes, positions | met / not met with a reason | Shown in the triage evidence panel, never applied automatically |

Cost controls: a per-day call cap, batch size limits, and a token estimate shown before bulk runs. Contact data is sent to the API only for the contacts you act on; the docs say so.

## 13. Contacts push (later phase)

- **Google Contacts.** Adds the `contacts` scope to the same OAuth client. One-way push: create or update a person per contact, tags become contact groups under a `netkeeper` prefix, and `people_resource_name` is stored on the contact. Deleting in netkeeper never deletes in Google.
- **macOS Contacts.** vCard export with a group per tag; you import into Contacts and iCloud does the rest. Direct Contacts framework integration through `pyobjc` is a possible follow-up.

## 14. Web API and frontend

### 14.1 API

- JSON under `/api/v1`. Every route resolves the current user through the `CurrentUser` dependency; `GET /api/v1/me` returns it. Resources: `contacts`, `tags`, `autotag-rules`, `lists`, `triage`, `imports`, `exports`, `linkedin` (`status`, `runs`, `budget`, `heat`, `pins`), `templates`, `campaigns` (with `steps`, `preview`, `test-send`, `activate`, `pause`), `enrollments`, `messages`, `mailboxes` (`oauth/start`, `oauth/callback`, `status`), `settings`, `posture`, `llm`, `events`.
- `POST /imports/archive` (P1-20) is the one multipart upload in the API: the LinkedIn export zip as downloaded. FastAPI/Starlette parses the whole upload before the handler runs — a file part is spooled to memory and, past 1 MiB, to the server's temp directory, on Starlette's own multipart defaults, which this endpoint accepts rather than overrides; nothing in the handler copies it again, reading straight from that spooled file instead. It is refused before anything is decompressed when the upload is over a size limit, or, once it is open as a zip, when its declared total uncompressed size, member count (read from the End Of Central Directory record before the zip's central directory is even parsed), or a member's compression ratio is past a limit a real export never approaches, or when a member's path would escape wherever it was ever used as one; a zip that is not a LinkedIn export answers `422` naming the table it expected, including the specific case of a zip that holds only another zip (LinkedIn does not nest its own download — checked by hand — but a person's own file manager might). Every refusal's body (`ArchiveRefusalOut`) carries a `code` (`ArchiveRefusalCode`: `not_a_zip`, `wrong_archive`, `nested_zip`, `encrypted`, `damaged`, `malformed_table`, `too_large`, `too_many_members`, `compression_ratio_too_high`, `unsafe_member_path`) alongside the human-readable `detail`, so the import wizard (P1-21) can act on why without matching words in a message that is free to be reworded — the message changed under it once already. The response carries per-table counts (connections, messages, invitations) and the tables it did not read (`ignored_files`); a later item may add a field here for another table without breaking the ones already there.
- `GET /api/v1/events` is an SSE stream of task progress, run status, and mailbox and browser health. The UI subscribes once.
- Every browser-touching route enqueues through `services/tasks.py` and returns `202` with a task id.
- *As built (P2-10), `linkedin`:* `GET /linkedin/runs` (paged, filter by `kind` and `status`) and `GET /linkedin/runs/{id}`; `POST /linkedin/runs` (`kind`, and `max_visits` for enrichment) and `POST /linkedin/runs/{id}/resume`, each `202` with `run_id` and `task_id`, refused `409` while the session flag is set, heat is over its skip threshold, or another run of the account is running, and `503` in a process with no browser worker; `POST /linkedin/runs/{id}/cancel`; `GET /linkedin/budget` (every counter, and today's profile-visit chain), `GET /linkedin/heat`; `GET`/`POST /linkedin/pins` and `DELETE /linkedin/pins/{contact_id}` (at most 5); `GET /linkedin/schedule`, `POST /linkedin/schedule/arm` (with `confirm: true`) and `/disarm`; `GET /linkedin/status`, the page's banner. The event stream carries `run.started`, `run.progress` (counts only), and `run.finished`. Only `netkeeper serve` (`netkeeper.worker.serve_app`) gives the app a browser worker; the app and the routes hold it only as a `RunExecutor`, so no request handler imports the browser.
- *As built (P3-01), `mailboxes`:* `GET /mailboxes` (every mailbox, disconnected ones included); `GET /mailboxes/status` (whether a client is stored and its ID, the mailboxes, and `reauth_required`; it reads the Keychain, so it answers `503` while the Keychain is locked, and the app-wide re-auth banner reads `GET /mailboxes` instead, from the database alone); `PUT /mailboxes/oauth/client`; `POST /mailboxes/oauth/start` (optionally with a `mailbox_id` to preselect its account) answers the Google URL; `GET /mailboxes/oauth/callback` always answers `303` to `/settings?gmail=connected` or `?gmail=error&reason=<code>`; `POST /mailboxes/{id}/check` refreshes the token now; `POST /mailboxes/{id}/disconnect`. The CLI mirrors them as `netkeeper gmail client|login|status|check|disconnect`.
- A query whose input does not fit a query string is a `POST` that reads: the contacts list carries a filter tree. Such a route is marked read-only so its session never takes the SQLite write lock, and it registers with the isolation test like any list.
- A bulk action applies to a filter, so the person confirms a count, not a list of rows. The client asks for the count, gets a signed token bound to that user, action, selection, and count, and sends it back to execute; the server re-counts and refuses when the count has moved. The token expires in five minutes and is never stored.
- OpenAPI schema is exported in CI and the TypeScript client is regenerated from it; a diff fails the build.

### 14.2 Local security

- Bind `127.0.0.1` only; no login in local mode (`LocalSingleUser` provider).
- State-changing requests must carry `X-Netkeeper-Client: 1` and pass a same-origin check on `Origin` or `Sec-Fetch-Site`. A page on another site cannot POST to the local API from your browser.
- The OAuth callback is the one route reachable from a browser navigation without the header; it validates the `state` parameter.
- *As built (#175 review):* every request, reads included, must name this machine in `Host` (`127.0.0.1`, `localhost`, `::1`, or the configured `web.host`), or it answers `421`. That is the DNS-rebinding guard: a page whose name an attacker re-points at `127.0.0.1` sends its own name as both `Host` and `Origin`, which the same-origin check alone would accept. The port is not checked, because the Vite dev proxy forwards the dev server's `Host` unchanged.

### 14.3 Frontend pages

| Route | Purpose |
|---|---|
| `/` | Dashboard: the setup path (P1-24) |
| `/contacts`, `/contacts/:id` | Table with saved views; detail with fields, tags, timeline, snapshots, messages, LLM brief |
| `/triage` | Step 6 workflow |
| `/lists` | Three tabs: static and smart lists with the filter builder; tags and their auto-tag rules (edited and reordered in place, with a live match count); and saved views, the same ones the Contacts table applies, built and edited with the same filter builder |
| `/imports`, `/exports` | Mapping, review, presets |
| `/imports/runs`, `/imports/runs/:id` | Import history, paged newest first; one run's counts (every file's, for an archive), its rows, finishing a draft, and rollback (P1-13, #132) |
| `/linkedin` | Runs, live progress, budget and heat, pins, preflight, browser launch instructions |
| `/templates` | Editor with lint and live preview against a chosen contact |
| `/campaigns`, `/campaigns/:id` | Builder, review gate, progress per step, replies, waiting-for-you prefill list |
| `/inbox` | Detected replies across campaigns, with mark-handled and add-note |
| `/settings` | Gmail auth, pacing, budgets, send windows, LLM, `[me]` merge fields, backups, posture summary |

The dashboard is the setup path, not a status board: import your data, review what was tagged by a rule, triage, build a list, export. Each step shows a real count and the control that advances it, so someone who has just installed netkeeper and imported nothing is told exactly what to do first. Where a step's progress is genuinely knowable, its badge is one of three states — done, in progress, not started, all describing the *person's* progress, not a system fact about them; where it is not, there is no badge, because a state nothing can verify is not a fourth rung on that ladder, and the detail line says the true thing in words instead. `GET /contacts/stats` (10.1) drives the import, review-tags, and triage steps; the open-imports query on `GET /imports?status=draft` (10.5, #90) tells the import step whether a draft is mid-flight rather than letting a finished-looking count hide unfinished work — including during the query's own first fetch, which the import step treats the same as "unknown" rather than as "done", since a contact count that resolves before the drafts query does is not evidence that no draft is open; the list step reads `GET /lists`.

Review-tags reads `stats.tagged_by_rule` (`TagSource.RULE` only), not `stats.tagged` (any source, hand-applied tags included) — the distinction is the whole reason the field exists — and its label says "tagged by a rule", not "tagged automatically": `TagSource` also has `LLM`, unwritten today, but "automatically" would already be the wrong word for a count that deliberately excludes it. So `tagged_by_rule === 0` is a genuine not-started, with "run the rules" as the concrete action, and that holds whether or not import itself comes to trigger the rules, since the step reads the live count rather than assuming what import leaves behind. A non-zero count is not `done`, though: it says the rules ran, not that the person reviewed what they did, and this step's own title is an instruction to *them* — the same conflation build-a-list's raw count would make if it claimed anything. Sharpest case: a 200-contact import that auto-tags 150 must not render "Done" before anyone has opened the app. So review-tags joins build-a-list and export as a "no badge" step once there are contacts and something has been tagged, showing the real count with no claim about who reviewed it.

Build-a-list is stuck at "no badge" for every count once there are contacts, for a different reason: `GET /lists` always includes the built-in "Validated" smart list the backend seeds at every server start (`ensure_validated_list`), with nothing in the response marking it as built-in, so raw length would read "done" for a person who has built nothing — there is no fix inside this response shape, and it is tracked as its own backend issue. Its control still matches the count, though: "Build a list" when the count is genuinely zero, "Open lists" once one exists. Export is the third "no badge" step, for a third reason: nothing records that one ran at all (it is a browser download, not a tracked action).

Later phases add their own steps to the same plain array (phase 2 "connect LinkedIn", budgets, heat, mailbox and browser health; phase 3 "connect Gmail", replies this week, changed-jobs prompts, next scheduled sends) rather than rewriting the page around them.

## 15. Configuration, secrets, and data locations

- Config: `config.toml`, resolved as `--config`, then `$NETKEEPER_CONFIG`, then `./config.toml`, then `<data_dir>/config.toml`. Example in Appendix B.
- Data dir: `$NETKEEPER_DATA`, else `~/Library/Application Support/netkeeper` on macOS, else `./data`. Holds `netkeeper.sqlite3`, `chrome-profile/`, `backups/`, `exports/`, logs.
- Runtime-adjustable settings (budgets, windows, pacing, active hours) are in `settings_kv` and edited on the Settings page; config values only seed them on first start.
- Secrets: Keychain via `keyring`, service `netkeeper`, key `<user_id>/<name>`. In the container, `keyring`'s file backend under the data volume with `0600` permissions.
- Logs: structured, one line per event, `NETKEEPER_LOG_LEVEL`. Bodies of messages are never logged.
- Backups: `netkeeper backup` runs `VACUUM INTO` to `backups/netkeeper-<timestamp>.sqlite3`, keeps the last 14 by default, and is scheduled nightly.

## 16. Packaging, running, and deployment

### macOS (primary)

```sh
uv venv && uv pip install -e '.[dev]' --python .venv/bin/python
cd frontend && pnpm install && pnpm build && cd ..
.venv/bin/netkeeper browser launch      # dedicated Chrome profile with CDP on :9222
.venv/bin/netkeeper preflight            # attach, login, fingerprint, Gmail token
.venv/bin/netkeeper serve                # http://127.0.0.1:8000
```

`scripts/install-launchd.sh` installs `~/Library/LaunchAgents/fun.tnkr.netkeeper.plist` so `serve` starts at login and restarts on failure. Chrome is not managed by launchd; the dashboard tells you when it is not reachable.

### Linux (container)

Multi-stage Dockerfile: a Node stage builds `frontend/dist`, the Python stage installs the package and copies the build. Only `pyproject.toml` and `uv.lock` sit in the dependency layer. Compose uses `network_mode: host` with an explicit `--host 127.0.0.1` so CDP on the host's loopback is reachable and the UI is not exposed. Same `down` before `up` rule as igtracker after a rebuild.

### Why not a container on macOS

Docker Desktop and `podman machine` run a Linux VM. `127.0.0.1:9222` inside it is the VM's loopback, and Chrome's DevTools endpoint rejects non-localhost `Host` headers, so the only workaround is exposing the debug port on all interfaces, which hands control of a logged-in LinkedIn browser to the network. Run natively on the Mac.

## 17. Testing strategy

- **Unit and service tests** run offline against in-memory SQLite. Cover identity resolution, the filter DSL, guards, the enrollment state machine, render and lint, budget and heat math, pacing distributions (statistical bounds, seeded), and classification.
- **Fixture tests** for every Voyager parser and archive importer, using sanitized captures in `tests/fixtures/`. A parser change without a fixture update fails review.
- **Gmail** is tested against a fake service object that records calls and replays canned responses; one opt-in test hits the real API with your own account (`NETKEEPER_GMAIL_TESTS=1`). *As built (P3-02):* the client runs over a recording transport (`tests/gmail_fakes.py`, `RecordingHttp`); the engine runs against `FakeGmail`; the opt-in test is `tests/test_gmail_live.py`, run as `docs/gmail-setup.md` describes.
- **Migration test** migrates from the previous revision and diffs against `Base.metadata`, so a forgotten migration fails on upgraded installs in CI, not on yours. From phase 1 it also runs against a PostgreSQL service container, so SQLite-only constructs are caught early.
- **Isolation test**: a two-user fixture calls every list endpoint as each user and asserts neither sees the other's rows. Every new list endpoint registers itself with this test.
- **Browser smoke** (`NETKEEPER_BROWSER_TESTS=1`) attaches to a real Chrome and drives a loopback site, verifying headers, client hints, scroll events, and tab recovery. Nothing external.
- **`netkeeper rehearse`** drives the whole enrichment path against a neutral site. **`netkeeper simulate`** replays sync and campaign schedules against a virtual clock with injected throttles, so backoff and window logic can be seen before they matter.
- **Frontend**: `vitest` for components and the filter builder; three Playwright flows (import a CSV, triage ten contacts, build and approve a campaign) against a backend seeded with fixtures.
- **CI**: GitHub Actions on push and PR: `ruff`, `mypy`, `pytest`, `pnpm lint`, `tsc`, `vitest`, client generation diff, container build.

## 18. Security, privacy, and terms of service

- **LinkedIn's User Agreement** prohibits automated access and scraping. Reading your own 1st-degree connections' contact info is data LinkedIn already shows you, but the method is still automation, and LinkedIn can restrict or close the account. The README states this, describes the safeguards, and recommends the conservative defaults. This is the same risk the reference workflow accepts by recommending a scraping tool.
- **Your contacts' data** is personal data. Keep it local, back it up encrypted if you sync the data directory anywhere, honor `do_not_contact`, and delete on request. The tool never sends contact data anywhere except Gmail (as recipients) and, if enabled, the Claude API (for the contacts you act on).
- **Gmail**: the `gmail.modify` scope is broad. Tokens live in Keychain, the tool never deletes mail, and every API call is logged with its purpose.
- **Local API**: loopback bind plus the CSRF header and same-origin check. No secrets in the SQLite file.
- **Repository**: open source under the MIT license from the first commit. The name, README, and docs avoid presenting the tool as a LinkedIn product, and fixtures ship no captured LinkedIn data beyond sanitized shapes.

## 19. Delivery phases

Each phase ends with a usable tool. Estimates assume evenings and weekends with AI-assisted development.

| Phase | Scope | Exit criterion |
|---|---|---|
| 0. Scaffold | Repo, `pyproject`, config, DB and migrations with the `user` table, `CurrentUser` and `LocalSingleUser`, CLI skeleton, FastAPI app, frontend shell with generated client, CI, launchd script | `netkeeper serve` shows an empty dashboard; CI green; the isolation test harness exists |
| 1. Import and CRM | Archive and CSV import with mapping and dedupe review, contacts table and detail, tags and auto-tag rules, static and smart lists, triage screen, exports with the nine-column preset | You can import your archive, triage your network, and export the nine-column CSV a mailing tool imports. Already replaces workflow stages 1 and 3.1 |
| 2. LinkedIn extractor | Attach, preflight, connections full and incremental sync, enrichment with pacing, budgets, heat, classification, snapshots, pins, runs page with live progress, `rehearse` and `simulate`, job contracts with no database access | A week of scheduled runs completes without a throttle; contacts have emails |
| 3. Email campaigns | Gmail OAuth, templates and lint, sequences, enrollment guards, review gate, test send, scheduler and windows, draft and send modes, threading, labels, reply and bounce detection, inbox page | First "First 100" campaign runs start to finish with automatic follow-up suppression |
| 4. LinkedIn messaging | Prefill step, inbox poll and reply detection, waiting-for-you list, opt-in auto-send with its own budget | Step 3 of the default sequence works |
| 5. LLM module | Personal line, classification, call prep, triage suggestions, cost controls | Optional and off by default |
| 6. Polish and reach | Google Contacts push, vCard export, container image, backups UI, posture page, docs, hosted-mode ADR and spike | Public-ready; the multi-user path is decided, not built |

## 20. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| LinkedIn restricts the account | Medium | High | Attach-only, conservative budgets, warm-up, heat, no retries on checkpoints, manual LinkedIn sends by default, clear docs |
| LinkedIn changes what its pages load (it moved from Voyager to flagship-web in 2026) | High over a year | Medium | One module per client and page (`flagship.py`, `flagship_profile.py`, `voyager.py`), dated constants, sanitized fixtures and a shape note, `RouteChanged` stops the run and never writes part of a page, completeness proven by the page itself |
| Chrome changes CDP or profile rules again | Low | Medium | Attach is the only dependency; `preflight` catches it; docs pin the launch flags |
| Gmail flags outbound as bulk | Low at 80 per day with personalization | Medium | Lint for missing merge fields, cap, spacing, same-thread follow-ups, no tracking pixels or link wrappers |
| OAuth token expiry every 7 days in Testing mode | Certain if not published | Low | Detect, pause, banner; docs recommend publishing |
| Scope creep into a general CRM | Medium | Medium | Non-goals list; every feature maps to a workflow step |
| Data loss | Low | High | Nightly `VACUUM INTO` backups, retention, restore command |
| Multi-user retrofit cost | Certain if not designed for | High | `user_id` from the first migration, one scoping helper, isolation tests, extractor contracts with no database access, PostgreSQL in CI |

## 21. Open questions

1. **Full profile harvest depth.** Enrichment reads positions and education because the page is already loaded. Should it also read the About section and skills for LLM personalization, at no extra request cost but more stored data?
2. **Second mailbox later.** The `mailbox` table supports several rows. Is there a case (a Workspace account for a business) worth designing the sender picker for now?
3. **Interaction logging from your side.** Should netkeeper read your own outbound Gmail to contacts outside campaigns, so "last contacted" is true rather than campaign-only? It needs no new scope but widens what the tool reads.
4. **Calendar.** A "schedule a call" outcome could create a placeholder event through the Google Calendar API. Worth a scope, or leave it to your scheduling link?
5. **Name.** Resolved: `netkeeper`, under `dsmorgan/netkeeper`.
6. **Shape of multi-user.** Self-hosted single-tenant (a family or a small team on one server), a hosted service, or both? The answer decides whether the extractor agent needs an installer and an authenticated channel, and whether the Google OAuth app must be verified. Deferred until the local form has matured; the readiness constraints in section 5 hold either way.

## Appendix A: Nine-column export preset

This is the layout the reference workflow's mailing tool imports. It exists so someone partway through that workflow can move data in either direction.

| Column | netkeeper field |
|---|---|
| LinkedIn Profile URL | `li_url` |
| Email Address | primary `contact_email` |
| First Name | `preferred_name` |
| Last Name | `last_name` |
| CityState | `location` |
| Current Company | `current_company` |
| Current Job Title | `current_title` |
| Phone Number | primary `contact_phone` |

The `headerless` option omits the header row, which some mailing tools require.

## Appendix B: `config.example.toml`

```toml
[web]
host = "127.0.0.1"
port = 8000

[me]
name = "Your Name"
website = "https://example.com"
scheduling_link = ""
signature = "Your first name"
city = ""

[linkedin]
cdp_url = "http://127.0.0.1:9222"
timezone = "America/New_York"
active_hours = ["08:30", "21:30"]
weekend_multiplier = 0.5
enrich_stale_days = 180
disconnect_after_misses = 2

[linkedin.budget]
connection_pages_per_day = 150
profile_visits_per_day = 60
profile_visits_per_week = 300
inbox_polls_per_day = 8
li_messages_auto_per_day = 15
warmup_start = 20
warmup_step = 10

[linkedin.pacing]
profile_delay_median_s = 25
profile_delay_sigma = 0.6
distraction_p = 0.08
distraction_range_s = [120, 480]
burst_size = [8, 15]
burst_break_s = [300, 1200]

[linkedin.heat]
per_block = 1.0
half_life_hours = 6
skip_threshold = 2.5

[campaigns]
send_window_days = ["Tue", "Wed", "Thu"]
send_window_hours = ["09:00", "16:30"]
mailbox_daily_cap = 80
send_spacing_median_s = 240
send_spacing_floor_s = 90
contacted_within_days_guard = 30
linkedin_auto_send = false
reply_poll_minutes = 10
holidays = []

[llm]
enabled = false
model = "claude-sonnet-5"
bulk_model = "claude-haiku-4-5-20251001"
daily_call_cap = 200

[backup]
nightly = true
keep = 14
```

## Appendix C: default pacing and budget values

| Knob | Default | Reasoning |
|---|---|---|
| Profile visits per day | 60 (max 100) | Below the 100-per-day guidance the reference workflow gives for scraping tools, leaving room for your own browsing |
| Delay between profiles | lognormal, median 25 s, sigma 0.6 | Median matches a person reading a profile; sigma gives a long tail without absurd waits |
| Distraction pause | 8% chance, 2 to 8 minutes | People get interrupted |
| Burst | 8 to 15 profiles, then 5 to 20 minutes off | Sessions, not streams |
| Warm-up | 20 per day, +10 per day | New profile, new device: ramp |
| Email sends per day | 80 | Well under Gmail's 500 recipient limit; a batch of 100 spans two days |
| Email spacing | median 4 minutes, floor 90 s | 80 sends fit inside a 7.5-hour window with room |
| Reply poll | every 10 minutes | Fast enough to suppress a follow-up scheduled the same day |
| Heat half-life | 6 hours | One throttle stretches the rest of the day; a clean day resets |
