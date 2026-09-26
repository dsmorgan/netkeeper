# netkeeper

[![CI](https://github.com/dsmorgan/netkeeper/actions/workflows/ci.yml/badge.svg)](https://github.com/dsmorgan/netkeeper/actions/workflows/ci.yml)

Keep your professional network warm.

netkeeper pulls your 1st-degree LinkedIn connections and their contact info into a local database, helps you sort out who you actually know, and runs reconnect-and-follow-up sequences over Gmail and LinkedIn messaging. It replaces the manual workflow of exporting from LinkedIn, sorting a spreadsheet, scraping profiles with a separate tool, and mailing through a bulk-mail tool, with one application that runs on your Mac.

**Status: pre-alpha.** The [architecture spec](docs/architecture.md) and the [implementation guide](docs/implementation-guide.md) are complete. Phase 0 (scaffold, API, frontend shell, CI, launchd) is done; phase 1 (import, CRM, triage) is in progress. See the changelog. Watch the repository or check the [changelog](CHANGELOG.md) for progress.

## What it does

netkeeper is three tools in one, and they only make sense together:

1. **Extract.** A browser sidecar attaches to the Chrome you already use and reads your connections and their contact info with human-like pacing and hard daily budgets. It never runs a second browser identity.
2. **Organize.** A local CRM with a triage screen for "have I actually met this person", manual and rule-based tags, static and smart lists, CSV import with column mapping, and export presets. LinkedIn stays the source of truth through periodic re-sync.
3. **Reach out.** Multi-step sequences: an email, a follow-up a week later into the same thread, and a LinkedIn message after that, each skipped automatically when the person replies. Draft mode writes into Gmail for you to send by hand; send mode sends for you inside a daily cap and a send window.

Everything stays on your machine: one SQLite file, tokens in the macOS Keychain, a web UI on `127.0.0.1`. v1 is for one person, but the schema carries a user boundary from the start so a self-hosted or hosted multi-user deployment can follow later without a rewrite.

## Where the method comes from

The workflow is a five-stage reconnect method: validate your network, enrich it, reconnect in batches of about 100, handle responses, follow up a week later, and keep going weekly. It is described generically in [docs/networking-workflow.md](docs/networking-workflow.md), and [the spec](docs/architecture.md#3-mapping-to-the-reference-workflow) maps every manual step to a netkeeper feature. The method comes from [hellophello](https://hellophello.com)'s job-search networking program, which is acknowledged here as the inspiration and appears nowhere in the implementation.

## Requirements

- macOS 14 or later. Linux works through the container image, but the LinkedIn steps need Chrome on the same host as the backend, so a Mac runs netkeeper natively.
- Google Chrome, or any Chromium-based browser. Firefox and Safari have no DevTools protocol and cannot be used for the LinkedIn steps.
- A Gmail account and your own Google Cloud OAuth client for the email steps. [docs/gmail-setup.md](docs/gmail-setup.md) walks through the setup.
- Optional: an Anthropic API key for the LLM features. They are off unless you configure one.

## Running

```sh
make install                # create .venv and install the package with dev extras
.venv/bin/netkeeper serve   # http://127.0.0.1:8000
```

### The browser sidecar

The LinkedIn steps attach to a Chrome you start yourself, with its own profile and a
debug port. netkeeper never launches a browser: a browser it started would be a
second device on your LinkedIn account, which is the thing accounts get restricted
for ([ADR 0002](docs/adr/0002-attach-only-browser-mode.md)).

```sh
.venv/bin/netkeeper browser launch   # prints the Chrome command to run yourself
.venv/bin/netkeeper preflight        # attach, session state, browser fingerprint
```

Log in to LinkedIn once in that window and use it for your own LinkedIn browsing too,
so your activity and netkeeper's share one session. `preflight` reads the profile's
cookie names and a few `navigator` properties on a blank tab; it visits no website.

### Run at login (macOS)

`scripts/install-launchd.sh` installs a user LaunchAgent, `fun.tnkr.netkeeper`, that starts `netkeeper serve` when you log in and restarts it if it crashes. It runs the copy in this repository's `.venv`, so run `make install` first. Re-running the script replaces the agent.

```sh
scripts/install-launchd.sh                     # install or replace the agent
scripts/install-launchd.sh --port 8080         # --host and --port pass through to serve
scripts/install-launchd.sh --data-dir ~/nk     # another data directory (sets NETKEEPER_DATA)
launchctl print gui/$UID/fun.tnkr.netkeeper    # status, including the PID
tail -f "$HOME/Library/Application Support/netkeeper/logs/serve.err.log"   # application log
scripts/install-launchd.sh --uninstall         # stop the agent and remove the plist
```

Logs live under `<data_dir>/logs/`: `serve.log` for stdout and `serve.err.log` for stderr, which is where the application logs. Add `--dry-run` to any invocation to see the plist and the `launchctl` commands without changing anything. Chrome is not managed by launchd; the dashboard tells you when it is not reachable.

## A plain note about LinkedIn's terms

LinkedIn's User Agreement prohibits automated access, including reading data it already shows you. netkeeper reads only your own 1st-degree connections, ships conservative defaults, warms up slowly, backs off when LinkedIn pushes back, and never retries a security checkpoint. Those safeguards reduce the risk of an account restriction; they do not remove it. Read [the security and terms section](docs/architecture.md#18-security-privacy-and-terms-of-service) before you run it, and treat the daily budgets as the ceiling, not a target.

## Roadmap

| Phase | Delivers |
|---|---|
| 0 | Repository scaffold, config, database, CLI, API, frontend shell, CI |
| 1 | Import (LinkedIn archive, CSV), contacts, tags, lists, triage, exports |
| 2 | LinkedIn extractor: sync, enrichment, pacing, budgets, backoff |
| 3 | Email campaigns over Gmail: templates, sequences, review gate, reply detection |
| 4 | LinkedIn messaging: prefill by default, auto-send opt-in |
| 5 | Optional LLM module |
| 6 | Google Contacts push, container image, polish |

Details and exit criteria are in [the spec](docs/architecture.md#19-delivery-phases).

## Documentation

- [Architecture spec](docs/architecture.md): design, data model, safety rules, phases.
- [Implementation guide](docs/implementation-guide.md): the backlog, what runs in parallel, and the human checkpoints.
- [Reconnect workflow](docs/networking-workflow.md): the method the tool automates.
- [Architecture decision records](docs/adr/): why the contested parts are the way they are.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for how the project works, and the [architecture decision records](docs/adr/) before proposing a change to the browser, Gmail, messaging, or user-scoping design. This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE).
