# Architecture decision records

An ADR records one decision, the context that forced it, and its consequences. They exist so a contributor who arrives later can tell a deliberate choice from an accident.

- Numbered, never edited after acceptance. A reversal is a new ADR that supersedes the old one.
- Keep them short. The spec holds the design; the ADR holds the reasoning.
- Propose one by opening an issue, then a pull request that adds `NNNN-short-title.md` from the [template](template.md).

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-attach-only-browser-mode.md) | Attach-only browser mode for LinkedIn | Accepted |
| [0003](0003-gmail-api-over-smtp.md) | Gmail API with OAuth instead of SMTP | Accepted |
| [0004](0004-manual-linkedin-sends-by-default.md) | LinkedIn messages are prefilled, not sent, by default | Accepted |
| [0005](0005-user-boundary-from-the-first-migration.md) | Carry a user boundary from the first migration | Accepted |
| [0006](0006-observe-dont-request.md) | Observe, don't request | Accepted |
| [0007](0007-prefill-inputs.md) | The prefill's inputs: one Message click, typing into one verified composer, never Enter | Proposed |
