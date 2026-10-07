# 0008. Auto-send: one click on Send, behind every gate

Date: 2026-10-07

## Status

Proposed (#384). It becomes Accepted when the maintainer approves the pull request that adds it with the code it authorizes.

This is a new ADR rather than an amendment to [ADR 0007](0007-prefill-inputs.md), for three reasons:

- ADR 0007 is accepted, and accepted ADRs aren't edited ([README](README.md)). Its consequences say "no code path presses Enter or clicks Send", and it says auto-send "needs its own amendment". This ADR is that amendment, and it reverses that one sentence for one mode.
- The decision is a different kind. ADR 0007 makes a person-triggered input safe while the person watches. Auto-send runs on a schedule, with nobody watching, and sends what can't be taken back.
- It needs its own status. The prefill must keep working, under ADR 0007 alone, whatever happens to this one.

Everything in ADR 0006 and ADR 0007 still holds unless this ADR says otherwise.

## Context

[ADR 0004](0004-manual-linkedin-sends-by-default.md) makes `prefill` the default LinkedIn step mode, and says an `auto_send` mode exists behind a config flag and a per-step setting, with its own daily budget, active hours, and heat backoff, highlighted on the posture page. The flag (`[campaigns] linkedin_auto_send`), the step mode, the `li_messages_auto` budget class, and the posture row exist. Nothing sends. ADR 0004's budget numbers are amended by #447's note (PR #450); its decision that auto-send is opt-in, off by default, stands, and this ADR doesn't change it.

On 2026-10-07 (CP8, #35) the maintainer decided to build auto-send now: a handful of supervised prefills, then auto-send. The first supervised prefill opened the right conversation, typed like a person, and stopped; the inbox poll then saw both the send and the reply. The same CP8 moved the budget numbers to #447 and the Message click's fix to #444.

Auto-send has the prefill's risks and two more:

- **A send can't be taken back, and nobody is watching.** ADR 0007's last remaining risk was a Shift+Enter landing on Send while focus moved, which the person watching would see. Here the click on Send is the point, and a wrong recipient or a wrong body goes out.
- **An automated send is what LinkedIn restricts accounts for fastest** (ADR 0004). A restricted account ends the whole workflow, not one step.

## Decision

netkeeper may give a LinkedIn page three more inputs, all in `BrowserRun` (`netkeeper/linkedin/browser.py`), all for an auto-send alone:

- **one click on Send**, in `BrowserRun.click_send`, reachable only when every gate below held;
- after a Send click that landed, **one click on the sent bubble's own close control**, in `BrowserRun.close_sent_bubble` (the maintainer's decision D1, 2026-10-07);
- after that bubble closed, **closing the run's own tab**, in `BrowserRun.close_sent_tab` (decision D3).

Everything else in ADR 0007 stays: the bring-to-front at the start, the Message click, typing into one verified composer with checks before every key, the one focus call, Shift+Enter as the only key, never Enter, and the hand-over for every other ending. There's still no script in the page, no request of netkeeper's own, and no interception (ADR 0006).

### Who triggers it

The scheduler, never a person. `netkeeper serve` schedules one more job kind, `auto_send`, every 10 minutes inside active hours, only when `[campaigns] linkedin_auto_send` is on **when `serve` starts**: with the flag off it has no schedule and no handler at all. Changing the flag takes a restart of `netkeeper serve`. To stop auto-send at once without a restart, pause the LinkedIn schedule, disarm scheduled runs, or cancel the run. To stop one message, pause its campaign, remove its enrollment, change its step to `prefill`, or mark the contact do not contact; a reply that arrives does the same. Each of these stops a run that's already going before its Send click (gate 6). Each fire:

1. checks the flag, arming, a pause, and the auto-send hold (below);
2. waits out the spacing since the newest `message_send` run of any kind, an auto-send or a prefill a person started: 20 to 45 minutes, drawn once per run;
3. claims the oldest due `auto_send` step (`linkedin_steps.claim_auto_send`, which reads `auto_send` steps directly, so a queue of prefill steps never hides one). The claim runs every check a prefill claim runs (the campaign and enrollment active, the step due, the sending hours, no reply, the guards, one open prefill, active hours, the session flag, heat, a fresh inbox poll, a clean render), plus the step's mode and the flag, with `li_messages_auto` in place of `li_prefills` (a spent budget refuses as `browser_out_of_budget`);
4. records a **scheduled** `message_send` run. `runs.create_run` records one only with `AUTO_SEND_GATE`, which only `start_auto_send_run` passes, and refuses it on a disarmed account like any scheduled run;
5. runs it through the worker, the same path as a prefill.

At most one auto-send runs per fire, and the scheduler keeps its usual gap between job kinds.

Prefill steps stay person-triggered. A person may still prefill an `auto_send` step by hand; that run is a prefill and never clicks Send.

### The gates, in the order the code checks them

A run stopped before it opened anything (gate 1, gate 2, gate 3, a lapsed claim, a busy or unreachable browser, a worker refusal) isn't `not_typed`: its step is given back with its due time, never listed for Try again, and a later fire claims it by itself. Nothing was opened and no budget was spent: every one of these comes before `budgets.consume`. The typing plan's own refusals (a body too long, a character it can't type, no usable URN) do record `not_typed`, since the same body would fail again. A run that refuses after the profile opened, and before the first key, records `not_typed` and gives the claim back for Try again, as a prefill does.

1. **The flag and the mode, before the lock.** `message_send.prepare` treats a scheduled `message_send` run as an auto-send only while `settings.campaigns.linkedin_auto_send` is true and the step's mode, read from the database, is `auto_send`. Anything else records `not_typed`, and nothing opens.
2. **Under the lock, before the navigation (`gates`).** The flag again; scheduled runs armed; the schedule not paused; no auto-send hold; the claim still wanted, read live (`still_wanted`: the message still claimed, the step still `auto_send`, no reply on the enrollment, and the step still passing `check_step`, so the campaign and the enrollment are `active` and the contact isn't do not contact); inside `[linkedin] active_hours`; the session not flagged; heat under its skip threshold; no cancel. Heat doesn't shrink the auto-send budget: as for every LinkedIn message run, it pauses auto-send outright at the skip threshold.
3. **The budget.** Today's `li_messages_auto` count must be below its daily limit (the configured value, clamped to its hard maximum), so an auto-send never goes one unit over, which `consume` alone allows. Then `budgets.consume(LI_MESSAGES_AUTO)` and `budgets.consume(PROFILE_VISITS)`. An auto-send never spends `li_prefills`, and a prefill never spends `li_messages_auto`.
4. **The permit.** Only then does `message_send.run_prefill` build a `SendPermit` (`netkeeper/linkedin/messaging.py`), the one place one is built. `PagePrefill.prefill` calls `click_send` only when the whole body was typed, the spec's mode is `auto_send`, and it holds a permit.
5. **The dwell.** After the last key, a pause like a person reading what they wrote: lognormal, median 4 seconds, held between 2 and 9 seconds.
6. **The recheck.** After the dwell, `SendPermit.recheck` runs gate 2 again, with live reads: disarming, pausing, a hold, a paused campaign, a removed enrollment, a reply, a step changed to `prefill`, a contact marked do not contact, leaving active hours, a session flag, heat, or a cancel that came while it typed stops the click. The typed message is then left for the person (`prefilled`, with the reason in **Waiting for you**), and auto-send holds.
7. **The composer, focus aside.** As before every key (ADR 0007): exactly one composer on the page, hidden ones counted; the bubble and its recipient; the tab's URL unchanged; no later compose option; and the composer's text exactly what typing put in and verified (the typing plan's text, so a `\r\n` in the body is the one newline it typed).
8. **The Send control.** Exactly one button named exactly **Send** on the page, hidden ones counted; it sits in the verified composer's own form (`xpath=ancestor::form[1]`), carries `type="submit"`, and is visible and enabled. **Open send options** beside it never matches.
9. **The composer once more,** every check of gate 7 and, last, focus in that composer, as it was while typing.

Then the click: once, at the control's own box, with a person's press length (Playwright may wait 1 second for it), and nothing awaited between the focus read and the click. A click that fails is never tried again. `click_send` also refuses without a permit, without a landed Message click, before the whole body was typed, and after a Send click was already sent.

### After a landed Send: proof, then closing the bubble and the tab (D1, D3)

A landed click means the page took it without an error. That isn't proof the message went, so nothing is closed on it alone:

1. **Proof the message went.** Before the Message click, the run also observes the page's own `POST /voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage` (ADR 0006's observation, read only: netkeeper sends nothing). Within 10 seconds of the Send click, the first such answer must be status 200, with `value.body.text` equal to the typed text (CR and CRLF as one newline, a no-break space as a space), and with `value.conversationUrn` this conversation's whenever the run knows it. No answer, an error status, other text, another conversation, or an answer that can't be read: the message is still recorded `send_clicked` (the inbox poll may yet confirm it), with `send not confirmed: <why>` in the run's notes, and the bubble and the tab are left for the person. The run never falls back to reading the bubble's newest message: it can always observe the answer.
2. **The composer must empty.** LinkedIn clears the composer once the message went. `close_sent_bubble` reads, with no input, for up to 5 seconds, until the one composer on the page reads empty, and reads it again right before the click. Text back in it, or a second composer, means nothing is closed.
3. **The close control.** Exactly one `Messaging` dialog on the page, hidden ones counted, holding that composer; its header `h2` holds exactly one link, to `/in/<the contact's profile id>/`; and exactly one button in that dialog is named exactly `Close your conversation with <that link's name>` (the capture's name, `docs/linkedin-messaging-shapes.md`), and it's visible. One click, never retried. The bubble counts as closed only when its composer then leaves the page.
4. **The tab.** Once the bubble closed, `close_sent_tab` closes the run's own tab and detaches. If the tab won't close, the run doesn't count it closed. Nothing else in the context or the browser changes.

The same checks apply to both bubble layouts. A never-messaged bubble is closed only if, after the proven send, it passes every one of them: one `Messaging` dialog holding the composer, a header link to the contact's profile id, and the exact close-button name. The capture showed the never-messaged bubble before a send only (`New message`, `Close your draft conversation`, root role unknown), so until LinkedIn is seen turning it into a conversation bubble, expect it to be left open, with auto-send held.

A send whose outcome is uncertain (a Send click that raised, or no proof), and every refusal, leaves the bubble and the tab for the person, handed over as ADR 0007 says. A failed close is recorded as `bubble left open: <why>` in the run's notes.

### The auto-send hold

The rule: **an auto-send run whose Message click was attempted, and whose own tab it didn't close, holds auto-send.** That covers every ending after the Message click except a proven, closed send: a refusal after the click, `partially_typed`, `unknown`, a typed message not sent, an unproven send, a bubble that didn't close, and a tab that didn't close. It holds whatever the run's status by then (a run already failed as interrupted, or cancelled), and when recording the outcome itself fails, the hold is still written in a transaction of its own. A refusal before the Message click holds nothing, with three exceptions, #444's (#459) pre-click checks, each of which a person must clear and the next auto-send would meet again: a page that already shows a message bubble or a composer, minimized ones included (`BUBBLE_ALREADY_OPEN`); a page where that couldn't be read (`BUBBLE_UNREADABLE`), held as a bubble because it may be one; and no Message control on screen with nothing over it (`MESSAGE_NOT_ON_SCREEN`), held as `the Message button isn't on screen with nothing over it in Chrome`. A hold written because recording the outcome failed keeps the same reason. These come after the profile opened, so they follow the `not_typed` path (the step waits for Try again), not the give-back for runs that opened nothing. Every other refusal before the click (no Message control, one for another profile, an unreadable control) holds nothing: the next contact's profile is a different page.

An auto-send run that stopped mid-run with nothing recorded (the process went away) holds auto-send too: the next auto-send claim finds its message still claimed and its run over, writes the hold (`an auto-send stopped mid-run, so a message bubble may be open`), and claims nothing.

A held auto-send would otherwise find the bubble, refuse, and move on to the next enrollment, spending a click and a profile visit each time. While it holds, the scheduler claims nothing (`nothing_to_send`) and the runner's gates refuse. The posture row, the LinkedIn queue, and `GET /campaigns/linkedin/options` say why (`a message bubble is open in Chrome`, or `a tab is left open in Chrome` when only the tab stayed) and how to clear it: close every message bubble (and the tab netkeeper left) in the netkeeper Chrome window, then either click **I fixed it in Chrome, resume auto-send** on the LinkedIn queue (`POST /campaigns/linkedin/auto-send/resume` with `{"confirm": true}`, as arming and clearing the session flag ask), or run `netkeeper linkedin auto-send-resume`, which asks for confirmation and runs only at a terminal. netkeeper never lifts it by itself.

### Refunds

An auto-send that typed nothing and attempted no Send click sent nothing, so its `li_messages_auto` unit goes back (`budgets.release`), to the local day it was spent in. The `profile_visits` unit doesn't: by then the profile was almost always opened, and a visit LinkedIn saw is a visit. Once a key or the Send click was attempted, nothing is refunded. An attempted Send click counts as a key: whatever fails after it is `unknown`, never `not_typed`, so it's never refunded, given back, or offered to try again. A failure writing the refund (or a wall's heat) never keeps the outcome and the hold from being recorded.

### Bringing the tab forward (D2)

An auto-send brings its tab to the front once at the start, as a prefill does (ADR 0007, decision 1), though nobody may be watching. The risk, stated plainly: if you're typing in that Chrome window when an auto-send starts, your keystrokes can land in the composer. The check before every key refuses text that isn't what netkeeper typed, and the run stops `partially_typed`. But a bare Enter typed into the composer while LinkedIn's **Press Enter to Send** setting is on would send whatever is in the composer at that moment, part of the message. Keep **Press Enter to Send** off (LinkedIn's default, "Click Send") in the netkeeper Chrome profile, and keep your own browsing out of that window while auto-send is on.

### Budget numbers

`li_messages_auto`'s numbers belong to #447 (PR #450), which also adds an amendment note to [ADR 0004](0004-manual-linkedin-sends-by-default.md): 15 a day by default, a hard maximum of 50, and a warning above 20. This ADR doesn't set them, and the code reads whatever `budgets.py` and `config.toml` say. Wherever auto-send is shown as on (the posture row, the LinkedIn queue's auto-send note, and `netkeeper serve`'s log when it schedules auto-send), the warning above 20 a day is shown with it.

### The pins

`tests/test_browser_safety.py` changes in the same pull request:

- `ALLOWED_INPUTS` names two more inputs: `click` in `BrowserRun.click_send` and `click` in `BrowserRun.close_sent_bubble`.
- `click_send` is reached only from `PagePrefill.prefill`, after `type_into_composer`, under `TypingEnd.TYPED`, `spec.mode == "auto_send"` and a permit.
- `close_sent_bubble` is reached only from `PagePrefill.prefill`, after `click_send`, under `send.clicked` and the send proof, with `confirmed=`; `close_sent_tab` only from `PagePrefill._end_after_click`, under `bubble_closed`.
- `SendPermit(...)` is built only in `message_send.run_prefill`, under `if auto:`, after the `li_messages_auto` consume and the strict allowance.
- The pin that no locator under `netkeeper/linkedin/` names Send or Submit now allows exactly one string, `"Send"` (`SEND_CONTROL_NAME`), and only inside `BrowserRun._send_control`, which `click_send` alone calls.
- The only `press` is still Shift+Enter.

Runtime tests cover each gate with a fake page, and each one is mutation-tested: removing the gate makes a test fail. A smoke test clicks Send, then the close control, against the loopback replica only.

## Consequences

- With the flag off, nothing schedules an auto-send, no claim makes one, no run built from one types, and nothing reaches `click_send`. ADR 0004's default stands.
- With it on, netkeeper sends LinkedIn messages by itself, during active hours, at most once per fire and at least 20 minutes after any message run, within a daily budget, and not at all while heat is at its skip threshold. **The account risk is real**: LinkedIn restricts accounts for automated messaging faster than for anything else netkeeper does. The posture page keeps its warning while the flag is on, and the README says so.
- The recipient sees "typing…" while the body is typed (ADR 0007), now also when nobody watches.
- A proven sent message's bubble and tab close, so the next auto-send starts on a clean page. Anything else leaves a bubble open, and auto-send holds until the person closes it and resumes. That's deliberate: working through the queue past an open bubble only spends clicks and visits, and a second attempt could send the body twice.
- An auto-send can be wrong in ways a prefill can't, because nobody reads the composer before the click. The checks before the click are the same ones that bind every key to the contact, and the text must be what was typed exactly. If LinkedIn changes the Send control or the close control, the click refuses and the message waits for the person.
- A future contributor keeps these true: the only clicks are Contact info, Message, Send, and the sent bubble's close control, each once; Send is clicked only by `click_send`, only for a scheduled auto-send run with the flag on, armed, unpaused and unheld, the step `auto_send`, `li_messages_auto` spent, inside active hours with heat under its skip threshold, after the dwell, and after the composer was verified again; the close control and the tab are closed only after a landed Send the page's own `createMessage` answer proved; and nothing ever clicks Send twice.
