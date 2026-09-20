# netkeeper

Keep your professional network warm.

netkeeper pulls your 1st-degree LinkedIn connections and their contact info into a local database, helps you sort out who you actually know, and runs reconnect-and-follow-up sequences over Gmail and LinkedIn messaging. It replaces the manual workflow of exporting from LinkedIn, sorting a spreadsheet, scraping with LinkedHelper, and mailing through Phello with one tool that runs on your Mac.

**Status: pre-alpha.** The [architecture spec](docs/architecture.md) is complete. There is no runnable code yet. Watch the repository or check the [changelog](CHANGELOG.md) for progress.

## What it does

netkeeper is three tools in one, and they only make sense together:

1. **Extract.** A browser sidecar attaches to the Chrome you already use and reads your connections and their contact info with human-like pacing and hard daily budgets. It never runs a second browser identity.
2. **Organize.** A local CRM with a triage screen for "have I actually met this person", manual and rule-based tags, static and smart lists, CSV import with column mapping, and export presets. LinkedIn stays the source of truth through periodic re-sync.
3. **Reach out.** Multi-step sequences: an email, a follow-up a week later into the same thread, and a LinkedIn message after that, each skipped automatically when the person replies. Draft mode writes into Gmail for you to send by hand; send mode sends for you inside a daily cap and a send window.

Everything stays on your machine: one SQLite file, tokens in the macOS Keychain, a web UI on `127.0.0.1`.

## Where the method comes from

The workflow is the one taught by [hellophello](https://hellophello.com): validate your network, enrich it, reconnect in batches of 100, follow up a week later, keep going weekly. Steps 6 through 10 of that training are in [docs/hp-training.md](docs/hp-training.md), and [the spec](docs/architecture.md#3-mapping-to-the-hp-workflow) maps every manual step to a netkeeper feature.

## Requirements

- macOS 14 or later. Linux works through the container image, but the LinkedIn steps need Chrome on the same host as the backend, so a Mac runs netkeeper natively.
- Google Chrome, or any Chromium-based browser. Firefox and Safari have no DevTools protocol and cannot be used for the LinkedIn steps.
- A Gmail account and your own Google Cloud OAuth client for the email steps. The spec explains the setup.
- Optional: an Anthropic API key for the LLM features. They are off unless you configure one.

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

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for how the project works, and the [architecture decision records](docs/adr/) before proposing a change to the browser, Gmail, or messaging design. This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE).
