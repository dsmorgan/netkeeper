# The reconnect workflow

This document describes the networking method that netkeeper automates. It is written generically: the tools it names are categories, not products. The method comes from hellophello's job-search networking program, whose training material this replaces. The [architecture spec](architecture.md#3-mapping-to-the-reference-workflow) maps each step here to a netkeeper feature.

## Overview

You have built a LinkedIn network over years. Most of those people would take a call from you, and a few of them will lead to your next role, client, or collaborator. The method has five stages and one rule about cadence:

| Stage | Outcome |
|---|---|
| 1. Validate | A list of the connections you have actually met |
| 2. Enrich | Email, phone, and current role for each of them |
| 3. Reconnect | A short, warm email to a batch of about 100 |
| 4. Respond | Calls scheduled with the people who reply |
| 5. Follow up | A second email to everyone who did not reply, a week later |

The cadence rule: every week, either send a new batch or follow up on the last one. Consistency matters more than copy.

The goal is a 35% response rate across the two emails. A first email alone typically gets 12% to 15%.

## Stage 1: Validate your network

Decide who in your network you have actually met. This is the foundation for everything after it.

1. Export your connections. In LinkedIn, go to **Settings & Privacy > Data privacy > Get a copy of your data** and request only **Connections**. The download link arrives within 24 hours, usually sooner. The archive holds a `Connections.csv` with name, profile URL, company, position, and the date you connected.
2. Mark the people you have met. Go down the list and flag anyone you have spoken with in person, on a video call, or in a real conversation. Do not judge whether they can help you, what their title is, or how long it has been. If you have met them once, they count.
3. Keep the flagged rows as your validated list. Keep the original export as a backup.

Do not look up contact details or update anyone's information yet. That is stage 2.

## Stage 2: Enrich contact information

Add an email address, a phone number if available, and the current company and title for each validated contact.

You can do this by hand or with a tool:

- **By hand.** Open each profile URL, open **Contact info**, and copy the email and phone. Slow, but it makes you revisit the relationship, and the person may notice that you viewed their profile.
- **With a profile-scraping tool.** Load up to 100 profile URLs at a time and let the tool collect email, phone, location, company, and title. Stay at or below 100 profiles per day to reduce the chance LinkedIn flags the account.

Whichever way you do it, keep these fields per contact:

- Profile URL
- Email address
- First name, written the way you would greet them
- Last name
- Location
- Current company
- Current title
- Phone number, if available

If you have 110 validated contacts, enrich all of them. If you have 500, work in batches of 100. Aim for speed and accuracy, not completeness.

## Stage 3: Reconnect

Send a short email to your first batch.

1. Build a list of about 100 contacts from the enriched set. Drop anyone without an email address. Optionally drop anyone you have spoken with recently.
2. Tag contacts by role (executive, investor, recruiter, and so on) so you can filter later.
3. Write one message that:
   - Acknowledges that it has been a while.
   - Shares a brief personal update and links to your personal website.
   - Suggests a casual catch-up.
   - Does not say you are looking for a job. The message is about the relationship.
4. Send a test to yourself and check links, merge fields, and formatting.
5. Have someone review the message before it goes out.
6. Send.

## Stage 4: Respond

Replies are the point. Handle them deliberately.

- You drive the process. When someone replies, even with a generic "good to hear from you," answer with something personal and propose a call.
- Offer a time and include a scheduling link, without letting the link do the talking. Put placeholders on your calendar until the time is confirmed.
- Keep campaign replies in a dedicated mail folder.
- Before each call, read up on the person's company and what is happening in their world. Lead with curiosity and a wish to help. If you are experienced, be ready to offer advice or consulting. If you are early in your career, ask for guidance, not a job.
- Track who responded and when, and set reminders for anyone you owe a follow-up.

## Stage 5: Follow up

Most of the value comes from the second email.

1. About a week after the first send, take the batch and remove everyone who replied. The rest are the follow-up list.
2. Send a short, warm follow-up that references the email you sent "last week" (or "a couple of weeks ago" if you slipped). Do not overthink the copy; your name in their inbox is what matters.
3. Send on a Tuesday, Wednesday, or Thursday.
4. Expect a higher response rate than the first email. Many people apologize for missing the first one.

Then keep the cadence: each week, either send a follow-up to the previous batch or send a first email to the next batch of 100. Overlapping batches is fine, but only scale as fast as you can handle the replies. Keep enriching new contacts in the background, up to 100 profiles per day.

If a follow-up batch gets a low response, try individual emails instead of a batch, and note which approaches work.

## Benchmarks

| Measure | Typical |
|---|---|
| Response to the first email | 12% to 15% |
| Response to the follow-up | Often higher than the first |
| Combined response target | 35% |
| Open rate | 65% to 70% (netkeeper does not measure opens; see the spec's non-goals) |

## Weekly checklist

- [ ] Send: a new batch of up to 100, or a follow-up to last week's batch.
- [ ] Reply to every response within a day and propose a call.
- [ ] Prepare for each scheduled call.
- [ ] Log outcomes and set reminders.
- [ ] Enrich the next batch in the background.
