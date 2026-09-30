# Gmail setup

netkeeper sends campaign email through your own Gmail account, using the Gmail API and an OAuth client you create in your own Google Cloud project (ADR 0003). Nobody else's server is involved: your browser talks to Google, and Google hands netkeeper a token for your mailbox only. This guide takes about fifteen minutes the first time.

You need:

- The Gmail account you want campaigns to send from.
- netkeeper installed and its database set up (`netkeeper db upgrade`).
- A browser where you can sign in to that Gmail account.

## What netkeeper asks Google for

One scope: `https://www.googleapis.com/auth/gmail.modify`. It lets netkeeper read mail (to see replies and bounces), write drafts, send, and add labels. It doesn't allow permanent deletion, and netkeeper never deletes mail.

The refresh token goes into your macOS Keychain, under the service `netkeeper`, as `<user id>/gmail/mailbox/<mailbox id>`. The OAuth client's ID and secret are stored next to it as `<user id>/gmail/oauth_client`. Neither is ever written to the database or a log.

## 1. Create a Google Cloud project

1. Open <https://console.cloud.google.com/> and sign in. Any Google account can own the project; it doesn't have to be the one you send from.
2. Open the project picker at the top of the page and choose **New project**.
3. Name it something you'll recognize, such as `netkeeper`. Leave the organization as it is and choose **Create**.
4. Make sure the new project is selected in the project picker before you go on.

## 2. Enable the Gmail API

1. Go to **APIs & Services** → **Library**.
2. Search for **Gmail API**, open it, and choose **Enable**.

If you skip this step, authorization still looks successful on Google's page. netkeeper then reports **"The Gmail API refused the token"** (`gmail_api_refused`), because it asks Gmail for your address before it stores anything.

## 3. Configure the consent screen

Google calls this the **Google Auth Platform** (older consoles call it **OAuth consent screen**).

1. Go to **Google Auth Platform** → **Branding**, or choose **Get started** if the console offers it.
2. **App name:** `netkeeper` (anything works; you're the only one who sees it). **User support email:** your address.
3. **Audience:** choose **External**. (Internal is only available to Google Workspace organizations.)
4. **Contact information:** your address. Accept the policy and choose **Create**.

### Testing or published: choose now

Look at **Audience** → **Publishing status**. There are two options, and the difference matters:

| | Testing | In production (unverified) |
|---|---|---|
| Who can authorize | Only the test users you list | Any Google account (an unverified app is capped at 100 users) |
| Token lifetime | **Expires after 7 days** | Persists until you revoke it |
| Warning screen | "Google hasn't verified this app" | The same warning |

**We recommend publishing.** In Testing, the token dies every seven days: netkeeper notices within one poll, marks the mailbox **needs re-authorizing**, pauses every email step, and shows a banner until you authorize again. That is safe, but it's a chore.

- **To publish:** under **Audience**, choose **Publish app** and confirm. Google doesn't need to verify an app that only you use. You click through the warning once, in step 6.
- **To stay in Testing:** under **Audience** → **Test users**, choose **Add users** and add the Gmail address you send from.

You don't need to add the scope under **Data Access**. netkeeper requests it when you authorize.

## 4. Create the OAuth client

1. Go to **Google Auth Platform** → **Clients** (older consoles: **APIs & Services** → **Credentials** → **Create credentials** → **OAuth client ID**).
2. Choose **Create client**.
3. **Application type:** choose **Desktop app**. Don't choose **Web application**. netkeeper refuses a web client, because Google only sends a web client back to a fixed list of addresses, and netkeeper's address includes a port it picks at run time.
4. Name it `netkeeper` and choose **Create**.
5. Choose **Download JSON** and keep the file (`client_secret_….json`). It holds the client ID and client secret.

## 5. Give netkeeper the client

Use either the web UI or the command line.

**Web UI:** run `netkeeper serve`, open <http://127.0.0.1:8000/settings>, and under **Gmail** → **1. OAuth client**, paste the **Client ID** (it ends in `.apps.googleusercontent.com`) and the **Client secret**. Then choose **Save client**.

**Command line:**

```sh
netkeeper gmail client ~/Downloads/client_secret_XXXX.json
```

You can delete the downloaded file afterward. netkeeper keeps its own copy in the Keychain.

## 6. Authorize your mailbox

**Web UI:** on the Settings page, under **2. Mailbox**, choose **Connect Gmail**. Your browser goes to Google.

**Command line:**

```sh
netkeeper gmail login
```

netkeeper prints a Google URL and waits for up to five minutes. Open the URL in the browser where you're signed in to the Gmail account. netkeeper never opens a browser itself.

On Google's page:

1. Choose the Gmail account you send from.
2. Google shows **"Google hasn't verified this app."** This is expected: the app is yours, and it hasn't been through Google's review. Choose **Advanced**, then **Go to netkeeper (unsafe)**.
3. Google lists what netkeeper may do. Make sure the Gmail box is **ticked**. If you untick it, netkeeper refuses the result (`scope_not_granted`). Choose **Continue**.

Google sends your browser back to netkeeper. The web UI shows **Gmail is connected**. The command line prints `connected you@gmail.com (mailbox 1, cap 80/day)` and your browser shows a page you can close.

## 7. Check it

- Settings shows the mailbox as **connected**, with its daily cap (`[campaigns] mailbox_daily_cap`, 80 by default and never more than 400) and when its token was last refreshed.
- `netkeeper gmail status` lists it without asking Google.
- `netkeeper gmail check` (or **Check now** in Settings) refreshes the token right away.

While `netkeeper serve` runs, it refreshes every connected mailbox's token every `[campaigns] reply_poll_minutes` (10 by default).

## 8. Arm the mailbox: drafts first, then send

A connected mailbox does nothing on its own. `netkeeper serve` hands it campaign steps only once you arm it, and every mailbox starts disarmed. Arming takes two separate steps.

1. **Arm for drafts**: **Arm for drafts** in Settings, or `netkeeper gmail arm you@gmail.com`. From the next minute, every due step on the mailbox becomes a Gmail draft, `send` steps included, and you send each one yourself. netkeeper never calls `messages.send` for a mailbox armed for drafts only.
2. **Arm to send**: **Arm to send** in Settings, or `netkeeper gmail arm you@gmail.com --send`. From then on, `send` steps go out on their own. This step is refused until netkeeper has found one of its own drafts on the mailbox by its Message-ID. It checks the first draft it makes on its next drafts check, within about 10 minutes. On a new mailbox, that first draft is usually a campaign's test: while the mailbox is armed for drafts, **Send a test** in the campaign's review makes each step's test a draft addressed to you, with a `[Test]` subject, in your Drafts, and never sends it. The test draft counts for the review, so you can activate the campaign while armed for drafts, and the drafts check verifies the mailbox from it. netkeeper doesn't delete test drafts; discard them when you're done. That's the live check that Gmail keeps the Message-ID netkeeper sets, which netkeeper relies on to find out whether a send whose answer never came went out.

**Disarm** (in Settings, or `netkeeper gmail disarm you@gmail.com`) undoes both steps. From the next minute, nothing more is claimed on the mailbox, and netkeeper makes no Gmail call for it. A step already handed to Gmail isn't recalled. Disconnecting disarms too, and a mailbox you connect again starts disarmed. Settings, the dashboard's mailbox card and `netkeeper gmail status` show the mode, since when, and who armed it.

## When Google stops accepting the token

This happens when you revoke netkeeper's access in your Google Account, after seven days in Testing mode, or when the OAuth client is deleted or its secret is reset. Google says so with `invalid_grant`, `invalid_client` or `unauthorized_client`. Within one poll, netkeeper:

1. marks the mailbox **needs re-authorizing** (`reauth_required`),
2. pauses every email step, and
3. shows a banner on every page, naming the mailbox and why.

Any other failure changes nothing, because it says nothing certain about the token: Google being down or slow, a network or proxy error, or an answer netkeeper can't read. The next poll tries again.

To fix it, choose **Re-authorize** on the banner or in Settings, or run `netkeeper gmail login`. Google preselects the same account. The mailbox keeps its history and campaigns.

If the client itself was deleted, create a new one (step 4) and save it (step 5) first.

## Switching accounts or disconnecting

netkeeper sends from one mailbox at a time. To use a different Gmail account, first disconnect the current one, using **Disconnect** in Settings or `netkeeper gmail disconnect you@gmail.com`, and then authorize the new account. Disconnecting forgets the token and pauses email steps. Campaigns that sent from the old mailbox keep their history.

Disconnecting doesn't revoke the grant on Google's side. To revoke it too, open <https://myaccount.google.com/permissions> and remove netkeeper.

## Troubleshooting

The Settings page and `netkeeper gmail status` show a short reason code:

| Reason | What happened | What to do |
|---|---|---|
| `invalid_grant` | Google refused the token: it was revoked, or it's seven days old in Testing | Authorize again (step 6). Consider publishing (step 3). |
| `invalid_client` | Google doesn't know the client: it was deleted, or its secret was reset | Create a client (step 4), save it (step 5), and authorize again |
| `unauthorized_client` | Google won't let the client use the token: it isn't a Desktop app client, or the token was issued to a different client | Create a Desktop app client (step 4), save it (step 5), and authorize again |
| `token_missing` | The Keychain has no token for the mailbox | Authorize again |
| `client_missing` | The Keychain has no OAuth client | Save the client (step 5), then authorize again |
| `gmail_api_refused` | The token works, but the Gmail API is off in the project | Enable it (step 2), then authorize again |
| `scope_not_granted` | The Gmail box was unticked on Google's page | Authorize again and leave it ticked |
| `access_denied` | You chose **Cancel** on Google's page | Authorize again |
| `state_mismatch` | Google's answer didn't match an authorization started in the last 10 minutes, or the server restarted in between | Start again from Settings |
| `other_mailbox_connected` | A different Gmail account is already connected | Disconnect it first |
| `keychain` | The Keychain refused to read or write | Unlock the login Keychain and try again |
| `insufficientPermissions`, `accessNotConfigured`, `authError` | Gmail refused the token during a campaign's call (a send, a draft, a reply poll) | Check the Gmail API is enabled (step 2), then authorize again |

A network failure, an error on Google's side, or a Gmail rate limit never marks a mailbox. netkeeper tries again at the next poll.

On Linux, `keyring` uses the Secret Service (for example GNOME Keyring). Without a running Secret Service, every step that stores a secret fails with a Keychain error.

## Checking the client against your account

The test suite never talks to Gmail; the engine's tests run against an in-memory fake. One test checks the real client, and the fake's threading rule, against your own account. It is skipped unless you ask for it:

```sh
NETKEEPER_GMAIL_TESTS=1 NETKEEPER_TEST_TIME_LIMIT_S=60 .venv/bin/python -m pytest tests/test_gmail_live.py -v
```

It reads the OAuth client and token from your login Keychain, for the mailbox `netkeeper gmail login` connected (user 1, mailbox 1; set `NETKEEPER_GMAIL_TEST_USER` and `NETKEEPER_GMAIL_TEST_MAILBOX` if yours differ; `netkeeper gmail status` shows the mailbox's id). It sends two short messages from the account to itself, one a follow-up in the other's thread, and leaves them and one draft under the label `netkeeper/live-test`. Delete them whenever you like. The raised time limit is for Gmail's round trips; the offline suite never needs it.
