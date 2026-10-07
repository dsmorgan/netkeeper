# 0004. LinkedIn messages are prefilled, not sent, by default

Date: 2026-09-20

## Status

Accepted

## Context

LinkedIn restricts accounts for automated messaging faster than for automated profile views. The value of the LinkedIn step in a sequence is high (it reaches people whose email bounced or is missing), but a restricted account ends the whole workflow, not just that step.

## Decision

The default LinkedIn step mode is `prefill`: the sidecar opens the conversation in the user's Chrome, types the rendered message, and stops. The user clicks Send. The next inbox poll confirms the send and records it. An `auto_send` mode exists behind a config flag and a per-step setting, with its own daily budget, active hours, and heat backoff, and the posture page highlights it as a non-default risk.

## Consequences

- A weekly batch still needs a few minutes of clicking Send. That is the price of the account staying open.
- The engine has to reconcile a `prefilled` message with what actually happened, including "the user never sent it", which the `stale` state and the waiting-for-you list handle.
- Anyone enabling auto-send does so knowingly. Pull requests that make it the default, or weaken its budget, will be declined without a superseding ADR.

## Amendments

- 2026-10-07 (#447): **Raised LinkedIn message budgets.** After CP8 (#35) showed the budgets too low for real use, the maintainer raised them: `li_messages_auto` keeps its default of 15 a day and its hard maximum goes from 30 to 50, and `li_prefills` goes from 10 a day (hard maximum 20) to 15 a day (hard maximum 50). Setting either above 20 a day shows a warning. The maintainer approved raising the auto-send hard maximum as an exception to the rule in Consequences that weakening its budget needs a superseding ADR. Auto-send stays off by default, and each budget stays separate.
