# Import the old mailing tool's history

Before netkeeper sends its first campaign, tell it whom your old mailing tool already emailed, and what those people said back. Without this history, someone the old tool emailed, even someone who asked to be left alone, passes every campaign guard: the guards read only what netkeeper itself sent and what you set by hand.

You run two commands, each first as a dry run:

1. `netkeeper history import` reads the old tool's workbook and records who each campaign reached and when.
2. `netkeeper history scan-gmail` searches your Gmail, read-only, for what those people sent back: replies, unsubscribe requests, automatic answers, and bounces.

Then you triage the people who replied.

You need:

- The old tool's report, exported from Google Sheets as an `.xlsx` workbook, one tab per campaign. Keep it outside the repository; `.gitignore` refuses `*.xlsx` files anyway.
- For the scan, the Gmail account where replies to the old campaigns arrived, connected to netkeeper ([Gmail setup](gmail-setup.md)). The scan doesn't need the mailbox armed: it sends nothing.

## Step 1: import the workbook

Run the dry run first. It does every step, prints what it would write, and writes nothing:

```sh
netkeeper history import ~/path/to/old-campaigns.xlsx
```

For each campaign tab, the report shows:

- **LISTED**: the people the workbook names. The workbook lists the people who opened, clicked, or bounced, and nobody else.
- **MATCHED**, **UNMATCHED**, **AMBIGUOUS**: whether a contact holds each address. An ambiguous address is one that more than one contact holds.
- **CLICKS**, **OPENS**, **BOUNCES**: the workbook's own counts.
- **SENT TO** and **UNLISTED**: the campaign's recipient count, and how many of those recipients the workbook doesn't name. netkeeper can't import the unlisted people, because the workbook doesn't say who they are. The report ends with a warning that gives the total.

Below the table, the report lists any warnings for a tab, such as rows it dropped because they hold no usable address, or formula cells with no saved value (open the sheet, let it calculate, and export it again). A tab that isn't a campaign report is skipped. The report names skipped tabs again at the end, and the command exits with an error so that you notice.

Below the table, the report lists every unmatched and ambiguous address. Merge duplicate contacts so that each ambiguous address belongs to one contact. To create a contact (name and address only) for each unmatched address, add `--create-missing`.

When the report looks right, apply it:

```sh
netkeeper history import ~/path/to/old-campaigns.xlsx --apply
```

For each matched recipient, netkeeper adds one **email out** entry to the contact's timeline, dated at the campaign's start and labeled **Imported history**. The campaign guard that skips people you contacted recently counts these entries. A person on a campaign's bounce list goes on the do-not-send list as bounced. That happens once: if you later take the address off the do-not-send list, a re-run doesn't put it back.

You can run the import again at any time, for example after you merge contacts or export the workbook again. It adds nothing twice.

## Step 2: scan Gmail

Run the dry run first. It searches Gmail and prints what it found, but writes nothing:

```sh
netkeeper history scan-gmail
```

For each imported recipient, the scan makes two searches, from the day before the campaign started until 120 days after its last batch: messages from the recipient's address, and delivery-failure notices that name it. It reads only each message's headers and Gmail's short preview, never the body, and it changes nothing in Gmail: no labels, no read state, no deletes.

The workbook names only the people who opened, clicked, or bounced, so the scan also looks for everyone else who replied. Replies usually keep the campaign's subject, so for each campaign it searches for messages with that subject, not sent by you, in the same window. A message counts only when its subject, without `Re:` or `Fwd:`, is exactly the campaign's subject, it arrived on or after the campaign's start day, and its sender isn't one of your own mailboxes or already a recipient of that campaign. Each such sender becomes a recipient of the campaign, marked as found by subject, is matched to a contact, gets the same imported **email out** entry, and is handled like any other recipient in the table below. The report's **FOUND BY SUBJECT** column counts them per campaign, and the first 20 addresses are listed. Each campaign's subject search runs once, unless you add `--rescan`.

To try the scan on a few recipients first, add `--limit 5`. If your account has more than one mailbox, name the one that received the replies with `--mailbox you@example.com`.

The report counts what the scan found in each campaign and lists the first 20 addresses of each kind. When the counts look right, apply them:

```sh
netkeeper history scan-gmail --apply
```

| What the scan found | What `--apply` does |
|---|---|
| A bounce | Puts the address on the do-not-send list as bounced. |
| An unsubscribe request | Sets the contact's **Do not contact**, with the reason "unsubscribe (old campaign)", and puts every address the contact has on the do-not-send list as opted out. It also adds the message to the timeline. |
| Any other reply | Adds the message to the contact's timeline as an **email in** entry labeled **Imported history**, and marks the contact **Needs review**, so that no campaign enrolls them until you look. |
| An automatic answer, such as an out-of-office message | Nothing beyond recording it. |

The scan can't tell a polite "no, thanks" from a friendly reply, so every replier with a contact waits for you. The scan first tries again to match a recipient that had no contact at import time, so a contact you created or gave the address to since then is flagged too. A replier whose address no contact holds, or more than one contact holds, can't be flagged: the report lists them instead. An unsubscribe request from such an address still puts the address on the do-not-send list.

An unsubscribe phrase counts even inside an automatic answer. A failure notice counts as a bounce only when it lists the address as a failed recipient or, if it lists none, names the exact address. A notice about `jim.bob@example.com` isn't a bounce of `bob@example.com`. The report counts the notices it skipped this way.

A scan that Gmail stops, for example at a rate limit, saves what it finished and exits with an error. Run the same command again later, and it continues with the recipients it didn't reach. A recipient already scanned is skipped unless you add `--rescan`.

If the report lists people who wrote back but whom no single contact holds, import again with `--create-missing` (or merge the duplicates), and then scan again with `--rescan`.

## What stays unknown

Someone the old tool emailed who neither opened, clicked, nor bounced, and who never replied, appears nowhere: not in the workbook, and not in Gmail. netkeeper can't know the old tool emailed them, so no guard skips them. The import's **UNLISTED** column tells you how many such people each campaign had. When you pick the contacts for your first batch, check them by hand against what you remember of the old campaigns, and leave out anyone the old tool reached recently.

## Step 3: triage the people who replied

Open each contact the scan flagged. In the contacts table, filter on **needs review since**. On the contact page, read the reply in the timeline, and then do one of the following:

- If they're happy to hear from you, choose **Confirm**. Campaigns may include them again.
- If they declined, set **Do not contact**, and then choose **Confirm**.

The **Needs review** notice on the contact page describes a contact read off a LinkedIn card, because that is the other way a contact gets this mark. For a contact the scan flagged, the reply in the timeline is the reason. A LinkedIn sync never clears the mark from a contact the scan flagged, and neither does a merge: when either contact in a merge is waiting for you to read a reply, the merged contact keeps the earlier mark. Only you clear it, by confirming the contact.
