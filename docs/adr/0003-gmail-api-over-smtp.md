# 0003. Gmail API with OAuth instead of SMTP

Date: 2026-09-20

## Status

Accepted

## Context

The campaign engine needs to send email, create drafts the user sends by hand, thread follow-ups into the original conversation, label campaign mail, and detect replies and bounces so follow-ups stop. SMTP with an app password covers sending only. The Gmail API covers all of it but requires the user to create a Google Cloud project and an OAuth client, and a consumer account's OAuth app either lives in Testing mode with 7-day token expiry or is published unverified with a warning screen.

## Decision

Use the Gmail API with the `gmail.modify` scope, refresh tokens in the macOS Keychain. Draft mode uses `drafts.create`; send mode uses `messages.send`; follow-ups reply in-thread; each campaign gets a label; reply detection uses the history API with a threads fallback. netkeeper detects an expired token, pauses email steps, and shows a re-auth banner. The setup guide recommends publishing the OAuth app so tokens persist.

## Consequences

- Reply suppression is automatic, which is the single biggest reduction in weekly manual work compared to the reference workflow's mailing tool.
- Setup takes about fifteen minutes the first time. The guide has to be good.
- The tool holds a broad scope on the mailbox. It never deletes mail, logs every call's purpose, and keeps the token out of the database.
- Non-Gmail providers are out of scope until someone proposes an ADR with a provider abstraction and a reply-detection story.

## Amendments

- 2026-09-27 (#269, P3-07): **A precondition for PostgreSQL or more than one sending process.** A campaign step is never sent twice because the tick claims it, by writing its message `scheduled`, in a writer session, and on SQLite that session begins with `BEGIN IMMEDIATE`, so no other writer can claim the same step at the same time. On PostgreSQL, `mark_for_write` does nothing, and two processes could claim one step. P3-07 needed no migration, so no index was added. Before any PostgreSQL or multi-process deployment, the claim needs a guard the database enforces. A plain unique index on outbound messages over (enrollment, step) is not enough on its own: a merge moves both contacts' messages onto one enrollment, so one enrollment can hold two messages of a step. The index must leave those out, for example a partial unique index on claims made by the tick, or the claim must lock the enrollment row (`SELECT ... FOR UPDATE`).
