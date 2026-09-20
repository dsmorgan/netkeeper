# Contributing to netkeeper

Thanks for your interest. This page explains how the project works so your time is well spent.

## Before you start

- Read the [architecture spec](docs/architecture.md). It is the source of truth for scope, data model, and the safety rules around the LinkedIn sidecar and Gmail.
- Read the [architecture decision records](docs/adr/). If your change reverses one of them, open an issue first and propose a new ADR. Discussion is welcome; silent reversals are not.
- Check the [non-goals](docs/architecture.md#non-goals). Features outside them need an issue and a maintainer's agreement before code.

## Picking something to build

The [implementation guide](docs/implementation-guide.md) is the backlog: every work item there is, or will be, an issue with the item ID in its title. Pick an open one whose dependencies are closed, comment that you are taking it, and go. Items labeled `checkpoint` are for the maintainer.

## How to propose work

1. **Bugs:** open an issue with the bug template. Include the run notes from the Settings or LinkedIn page when the bug involves the sidecar; never include cookies, tokens, or other people's contact data.
2. **Features:** open an issue with the feature template and say which stage of the [reconnect workflow](docs/networking-workflow.md) or which spec section it serves.
3. **Small fixes** (typos, docs, obvious one-liners): send a pull request directly.

## Pull requests

- Branch from `main`. Keep one change per pull request.
- Fill in the pull request template. Say what changed and why, and which spec section or ADR it relates to.
- Tests must run offline. Nothing in the test suite may touch linkedin.com, Gmail, or the Anthropic API. Browser-touching changes need the opt-in smoke suite to pass on a real Chrome; say so in the PR.
- Fixtures captured from LinkedIn responses must be sanitized: fake names, fake URNs, no real emails or phone numbers.
- Commit messages: an imperative subject line under 72 characters, a blank line, then the why. The what is in the diff.
- Add a changelog fragment in `changelog.d/` (see its README) when behavior changed. Do not edit `CHANGELOG.md` directly; it is assembled at release time.

## Development setup

Phase 0 in the spec adds the runnable scaffold. Until then there is nothing to build. When it lands, the commands in [section 16 of the spec](docs/architecture.md#16-packaging-running-and-deployment) are the setup, and this section will link to them.

## AI-assisted contributions

Contributions written with AI tools are welcome. You are the author: review what you submit, make sure it follows the spec and the ADRs, and be ready to explain it in review. A pull request the submitter cannot explain will be closed.

## Safety rules that reviewers enforce

These come straight from the spec and from production incidents on the sibling project the sidecar design is based on:

- Only `attach` browser mode. No code path may launch a second browser identity.
- No `await` on browser work inside a request handler.
- Every browser-touching path takes the activity lock for its LinkedIn account.
- Nothing under `linkedin/` imports ORM models or opens a database session.
- Every query against a user-owned table goes through the scoping helper, and every list endpoint has an isolation test.
- A security checkpoint is never retried.
- Budgets are enforced between units of work, never mid-unit.
- Every protection defaults to on, and the posture page highlights anything that is off.
- Message bodies, cookies, and tokens never appear in logs.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be kind, be direct, assume good intent.
