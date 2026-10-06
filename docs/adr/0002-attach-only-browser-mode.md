# 0002. Attach-only browser mode for LinkedIn

Date: 2026-09-20

## Status

Accepted

## Context

The sidecar design comes from a sibling project that scrapes Instagram. That project supports three browser modes: launch a fresh browser with pasted cookies, launch a persistent profile, or attach over the Chrome DevTools Protocol to a browser the user already runs. Its account was restricted for "multiple sessions" while using the launch modes, and attach mode fixed it. LinkedIn treats a new browser identity on an account the same way, and restricts more readily.

## Decision

netkeeper supports only `attach`. It connects over CDP to a Chrome the user launched with a dedicated profile directory and a debug port, reuses the existing context, and never launches a browser of its own. There is no fallback: if Chrome is unreachable, the run fails and the scheduler retries later. The `BrowserProvider` interface stays so a mode can be added by a future ADR, but no code path may create a second identity.

## Consequences

- LinkedIn sees one device, one fingerprint, one cookie jar, and the user's own browsing is cover traffic.
- The user has to start Chrome with two flags and log in once. `netkeeper browser launch` wraps the command and the dashboard reports when Chrome is down.
- The container image cannot run the LinkedIn steps on macOS, because Chrome's debug port is on the Mac's loopback and the container VM cannot reach it. macOS runs natively.
- Firefox and Safari are unsupported for the LinkedIn steps; neither speaks CDP.

## Amendments

- 2026-09-24: [ADR 0006](0006-observe-dont-request.md) records how netkeeper reads LinkedIn through the attached tab: it reads the responses the page itself loads and never intercepts or sends a request. It also records the one exception to scrolling being the only input netkeeper gives a page: one click on **Contact info** per profile visit.
- 2026-10-06, #375: [ADR 0007](0007-prefill-inputs.md) adds the prefill's inputs: one click on **Message**, at most one focus on the verified composer, and typing into that one verified, empty composer, never Enter and never a Send click. It also adds a way to end a run that leaves its tab open: the prefill hands its tab to the user, and no later run reuses or closes it.
