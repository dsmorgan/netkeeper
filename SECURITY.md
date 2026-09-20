# Security policy

netkeeper handles other people's contact details, your Gmail OAuth token, and a logged-in LinkedIn browser session. Security reports are taken seriously.

## Supported versions

There is no release yet. Once there is, the latest minor release receives fixes.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue for anything that could expose contact data, tokens, or the local API.

Include what you found, how to reproduce it, and what you think the impact is. You will get an acknowledgement within 7 days and a fix or a plan within 30.

## What counts

Reports in these areas are especially welcome:

- Token or API key exposure (Keychain handling, logs, exports, backups).
- The local API accepting state-changing requests from another origin.
- Contact data leaving the machine through any path other than the ones the spec documents (Gmail recipients, the optional Claude API for contacts you act on, the optional Google Contacts push).
- Anything that makes the sidecar behave in a way that increases the risk to the user's LinkedIn account beyond what the spec describes, such as bypassing budgets or retrying checkpoints.
- Template injection through imported contact data.

Out of scope: LinkedIn or Google rate limits and account restrictions themselves, which the spec documents as accepted risk.
