# 0001. Record architecture decisions

Date: 2026-09-20

## Status

Accepted

## Context

netkeeper automates actions against a LinkedIn account and a Gmail mailbox. Several design choices trade throughput for safety, and each one looks like an easy win to undo. The project is open source, so contributors arrive without the conversations that produced those choices.

## Decision

Keep architecture decision records in `docs/adr/`, one file per decision, following the [template](template.md). The [architecture spec](../architecture.md) describes the design; ADRs capture why the contested parts are the way they are. A change that reverses an ADR needs a new ADR, agreed in an issue first.

## Consequences

Contributors can tell deliberate choices from accidents. Reviewers have something concrete to point at. Writing an ADR costs a few minutes per decision, which is cheap next to re-litigating the decision in a pull request.
