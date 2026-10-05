# Gmail setup

netkeeper sends campaign email through your own Gmail account, using the Gmail API and an OAuth client you create in your own Google Cloud project (ADR 0003). Nobody else's server is involved: your browser talks to Google, and Google hands netkeeper a token for your mailbox only. This guide takes about fifteen minutes the first time.

You need:

- The Gmail account you want campaigns to send from.
- netkeeper installed and its database set up (`netkeeper db upgrade`).
- A browser where you can sign in to that Gmail account.
- Optionally, the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) (`gcloud`), which can do steps 1 and 2 for you.

## The setup guide in Settings

The quickest way through is the setup guide on the Settings page. Run `netkeeper serve` and open <http://127.0.0.1:8000/settings>. Under **Gmail**, the guide shows one step at a time:

- Each console step has a link to the exact page for your project, and a **Copy** button for every value you paste there (the project ID, the app name, your address, the client name).
- netkeeper checks the steps it can see: whether the OAuth client is saved, and whether the mailbox is connected. Connecting proves the token works and the Gmail API is on, because netkeeper asks Gmail for your address before it stores anything. If Gmail refuses, the guide reopens step 2 and marks it as the problem.
- You mark the rest done yourself, since netkeeper can't see into Google's console. You can reopen any step, and **Mark not done** undoes a step you marked by mistake.
- When a mailbox is connected, the guide folds away. **Show setup steps** reopens it, for the optional publishing step.

The links need your project ID, not its name. Until you save an ID in step 1, the links that take a project are disabled, with a hint.

netkeeper never drives the Google console. Every link opens in your own browser, and netkeeper never runs `gcloud`: it shows the commands, and you run them. The rest of this page is the same steps in writing, for the command line or for reference.

To read this guide next to the console, use Chrome's split view. Right-click a link and choose **Open link in split view**. Or right-click the console's tab and choose **New split view with current tab**. The shortcut is Cmd+Option+N on macOS and Shift+Alt+N on Windows and Linux. To leave, choose the split view icon next to the address bar, then **Separate split view**. See [Google's split view help](https://support.google.com/chrome/answer/16971124). In another browser, drag the console's tab out into its own window and place the two windows side by side. A web page can't ask Chrome for a split view, and netkeeper never launches or drives your browser, so the choice is yours.

## What netkeeper asks Google for

One scope: `https://www.googleapis.com/auth/gmail.modify`. It lets netkeeper read mail (to see replies and bounces), write drafts, send, and add labels. It doesn't allow permanent deletion, and netkeeper never deletes mail.

The refresh token goes into your macOS Keychain, under the service `netkeeper`, as `<user id>/gmail/mailbox/<mailbox id>`. The OAuth client's ID and secret are stored next to it as `<user id>/gmail/oauth_client`. Neither is ever written to the database or a log. The setup guide's progress (the project ID, your address, and the steps you marked done) is stored in the database, since none of it is secret.

## What `gcloud` can and can't do

Two of the steps have public commands. The rest are console-only, so the guide links to them.

| Step | `gcloud` or a public API | Console |
|---|---|---|
| 1. Create the project | `gcloud projects create` | **New project** |
| 2. Enable the Gmail API | `gcloud services enable` | **APIs & Services** → **Library** |
| 3. Consent screen (Branding, Audience) | None. The old IAP OAuth Admin API (`gcloud iap oauth-brands`) is deprecated, only made Internal brands, and never made an External one | **Google Auth Platform** → **Branding** |
| 4. Test users | None | **Google Auth Platform** → **Audience** |
| 5. Desktop OAuth client | None. `gcloud iap oauth-clients` made IAP clients only, and `gcloud iam oauth-clients` is for workforce identity federation, not Gmail | **Google Auth Platform** → **Clients** |
| Publishing | None | **Google Auth Platform** → **Audience** |

## 1. Create a Google Cloud project

Pick a project ID. It must be 6 to 30 lowercase letters, digits or hyphens, start with a letter, and not end with a hyphen, and it must be unique across all of Google Cloud: `netkeeper-` plus a few random characters works well. The setup guide's **Suggest one** button makes one.

**With `gcloud`:**

```sh
gcloud auth login
gcloud projects create netkeeper-ab12cd --name=netkeeper
```

**In the console:**

1. Open <https://console.cloud.google.com/projectcreate> and sign in. Any Google account can own the project; it doesn't have to be the one you send from.
2. Name it `netkeeper`. Under the name, choose **Edit** next to the project ID and enter yours. Leave the organization as it is and choose **Create**.
3. Make sure the new project is selected in the project picker before you go on.

If you already have a project, use its ID instead. The ID isn't the name. To find it, open the project picker at the top of the console: the list shows **Name**, **Type** and **ID**. When Google creates a project for you, it usually adds a number to the ID, so a project named `netkeeper` might have the ID `netkeeper-510123`. Every console link in this guide takes the ID in `?project=`. If you use the name, the console doesn't show an error. It opens a page that suggests you request more permissions.

## 2. Enable the Gmail API

**With `gcloud`:**

```sh
gcloud services enable gmail.googleapis.com --project=netkeeper-ab12cd
```

**In the console:** open `https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=<your project ID>` and choose **Enable**.

If you skip this step, authorization still looks successful on Google's page. netkeeper then reports **"The Gmail API refused the token"** (`gmail_api_refused`), because it asks Gmail for your address before it stores anything.

## 3. Configure the consent screen

Google calls this the **Google Auth Platform** (older consoles call it **OAuth consent screen**).

1. Open `https://console.cloud.google.com/auth/branding?project=<your project ID>`, and choose **Get started** if the console offers it.
2. **App name:** `netkeeper` (anything works; you're the only one who sees it). **User support email:** your address.
3. **Audience:** choose **External**. (Internal is only available to Google Workspace organizations.)
4. **Contact information:** your address. Accept the policy and choose **Create**.

Leave the homepage and privacy policy links empty. You only need them to publish, which is optional (step 10).

You don't need to add the scope under **Data Access**. netkeeper requests it when you authorize.

## 4. Add yourself as a test user

A new app starts in **Testing**, and stays there unless you publish it. In Testing, only the test users you list can authorize the app, so add the address you send from:

1. Open `https://console.cloud.google.com/auth/audience?project=<your project ID>`.
2. Under **Test users**, choose **Add users**, enter the Gmail address you send from, and save.

In Testing, Google expires the token after **7 days**. netkeeper notices within one poll, marks the mailbox **needs re-authorizing**, pauses every email step, and shows a banner. Choose **Re-authorize** on the banner and click through Google's page again. That's safe, but it's a weekly chore. Publishing removes it.

## 5. Create the OAuth client

1. Open `https://console.cloud.google.com/auth/clients/create?project=<your project ID>` (older consoles: **APIs & Services** → **Credentials** → **Create credentials** → **OAuth client ID**).
2. **Application type:** choose **Desktop app**. Don't choose **Web application**. netkeeper refuses a web client, because Google only sends a web client back to a fixed list of addresses, and netkeeper's address includes a port it picks at run time.
3. Name it `netkeeper` and choose **Create**.
4. Keep the dialog open, or choose **Download JSON** and keep the file (`client_secret_….json`). Either way, you need the client ID and client secret next.

## 6. Give netkeeper the client

Use either the web UI or the command line.

**Web UI:** in the setup guide's step 6, paste the **Client ID** (it ends in `.apps.googleusercontent.com`) and the **Client secret**. Then choose **Save client**.

**Command line:**

```sh
netkeeper gmail client ~/Downloads/client_secret_XXXX.json
```

You can delete the downloaded file afterward. netkeeper keeps its own copy in the Keychain.

## 7. Authorize your mailbox

**Web UI:** in the setup guide's step 7, choose **Connect Gmail**. Your browser goes to Google.

**Command line:**

```sh
netkeeper gmail login
```

netkeeper prints a Google URL and waits for up to five minutes. Open the URL in the browser where you're signed in to the Gmail account. netkeeper never opens a browser itself.

On Google's page:

1. Choose the Gmail account you send from. It must be a test user (step 4).
2. Google shows **"Google hasn't verified this app."** This is expected: the app is yours, and it hasn't been through Google's review. Choose **Advanced**, then **Go to netkeeper (unsafe)**.
3. Google lists what netkeeper may do. Make sure the Gmail box is **ticked**. If you untick it, netkeeper refuses the result (`scope_not_granted`). Choose **Continue**.

Google sends your browser back to netkeeper. The web UI shows **Gmail is connected**. The command line prints `connected you@gmail.com (mailbox 1, cap 80/day)` and your browser shows a page you can close.

## 8. Check it

- Settings shows the mailbox as **connected**, with its daily cap (`[campaigns] mailbox_daily_cap`, 80 by default and never more than 400) and when its token was last refreshed.
- `netkeeper gmail status` lists it without asking Google.
- `netkeeper gmail check` (or **Check now** in Settings) refreshes the token right away.

While `netkeeper serve` runs, it refreshes every connected mailbox's token every `[campaigns] reply_poll_minutes` (10 by default).

## 9. Arm the mailbox: drafts first, then send

A connected mailbox does nothing on its own. `netkeeper serve` hands it campaign steps only once you arm it, and every mailbox starts disarmed. Arming takes two separate steps.

1. **Arm for drafts**: **Arm for drafts** in Settings, or `netkeeper gmail arm you@gmail.com`. From the next minute, every due step on the mailbox becomes a Gmail draft, `send` steps included, and you send each one yourself. netkeeper never calls `messages.send` for a mailbox armed for drafts only.
2. **Arm to send**: **Arm to send** in Settings, or `netkeeper gmail arm you@gmail.com --send`. From then on, `send` steps go out on their own. This step is refused until netkeeper has found one of its own drafts on the mailbox by its Message-ID. It checks the first draft it makes on its next drafts check, within about 10 minutes. On a new mailbox, that first draft is usually a campaign's test: while the mailbox is armed for drafts, **Send a test** in the campaign's review makes each step's test a draft addressed to you, with a `[Test]` subject, in your Drafts, and never sends it. The test draft counts for the review, so you can activate the campaign while armed for drafts, and the drafts check verifies the mailbox from it. netkeeper doesn't delete test drafts; discard them once the mailbox is armed to send. If you discard one before the drafts check finds it, **Arm to send** says no netkeeper test draft was found in your Drafts: make a new test draft from the campaign's review. That's the live check that Gmail keeps the Message-ID netkeeper sets, which netkeeper relies on to find out whether a send whose answer never came went out.

**Disarm** (in Settings, or `netkeeper gmail disarm you@gmail.com`) undoes both steps. From the next minute, nothing more is claimed on the mailbox, and netkeeper makes no Gmail call for it. A step already handed to Gmail isn't recalled. Disconnecting disarms too, and a mailbox you connect again starts disarmed. Settings, the dashboard's mailbox card and `netkeeper gmail status` show the mode, since when, and who armed it.

## 10. Publishing, later (optional)

Publishing is optional. It changes one thing for you: the token stops expiring every 7 days.

| | Testing (the default) | In production (unverified) |
|---|---|---|
| Who can authorize | Only the test users you list | Any Google account (an unverified app is capped at 100 users) |
| Token lifetime | **Expires after 7 days** | Persists until you revoke it |
| Warning screen | "Google hasn't verified this app" | The same warning |

Google keeps **Audience** → **Publish app** disabled until **Branding** has all four of these:

- an app name,
- a user support email,
- an **app home page** URL, and
- a **privacy policy link** URL.

Google's branding rules say the home page must be on a domain you own, the privacy policy must be on the home page's domain and linked from it, and both domains must be listed under **Branding** → **Authorized domains**. Google verifies ownership of those domains (through Google Search Console) when an app is submitted for verification. You don't submit netkeeper for verification, since only you use it.

**Can you use a GitHub URL?**

- A GitHub repository URL (`https://github.com/you/repo`) isn't on a domain you own, so Google's rules don't allow it. We haven't confirmed whether the console refuses it outright for an app that's never submitted for verification.
- A GitHub Pages site (`https://you.github.io/`) probably fits Google's rules: `github.io` is on the Public Suffix List, so each `you.github.io` counts as its own domain, which you should be able to verify in Google Search Console. We haven't confirmed that Google accepts it either.
- If you have neither, or don't want to publish pages about a tool only you use, stay in Testing and re-authorize once a week.

To publish, fill in the two URLs and the authorized domain on **Branding**, then open **Audience**, choose **Publish app**, and confirm. Mark the guide's last step done if you like; it's only a note for you.

## When Google stops accepting the token

This happens when you revoke netkeeper's access in your Google Account, after seven days in Testing mode, or when the OAuth client is deleted or its secret is reset. Google says so with `invalid_grant`, `invalid_client` or `unauthorized_client`. Within one poll, netkeeper:

1. marks the mailbox **needs re-authorizing** (`reauth_required`),
2. pauses every email step, and
3. shows a banner on every page, naming the mailbox and why.

Any other failure changes nothing, because it says nothing certain about the token: Google being down or slow, a network or proxy error, or an answer netkeeper can't read. The next poll tries again.

To fix it, choose **Re-authorize** on the banner or in Settings, or run `netkeeper gmail login`. Google preselects the same account. The mailbox keeps its history and campaigns.

If the client itself was deleted, create a new one (step 5) and save it (step 6) first.

## Switching accounts or disconnecting

netkeeper sends from one mailbox at a time. To use a different Gmail account, first disconnect the current one, using **Disconnect** in Settings or `netkeeper gmail disconnect you@gmail.com`, and then authorize the new account. Disconnecting forgets the token and pauses email steps. Campaigns that sent from the old mailbox keep their history.

Disconnecting doesn't revoke the grant on Google's side. To revoke it too, open <https://myaccount.google.com/permissions> and remove netkeeper.

## Testing reply detection

netkeeper never counts a reply you send from the mailbox it reads, including from a `+` alias of it such as `you+test@gmail.com`. Two rules drop such a reply:

- Gmail labels every message you send **Sent**, even when it also lands in your inbox, and netkeeper skips sent messages so your own follow-ups never count as replies.
- A reply counts only when it comes from one of the contact's addresses. Gmail sends from your main address unless you set the alias up as a send-as address, so the reply's `From` doesn't match a test contact enrolled at the alias.

To test reply detection, enroll a contact whose address is a different Gmail account, and reply from that account. A real contact replying from their own mailbox is unaffected.

## Troubleshooting

The Settings page and `netkeeper gmail status` show a short reason code:

| Reason | What happened | What to do |
|---|---|---|
| `invalid_grant` | Google refused the token: it was revoked, or it's seven days old in Testing | Authorize again (step 7). To stop the 7-day expiry, publish (step 10). |
| `invalid_client` | Google doesn't know the client: it was deleted, or its secret was reset | Create a client (step 5), save it (step 6), and authorize again |
| `unauthorized_client` | Google won't let the client use the token: it isn't a Desktop app client, or the token was issued to a different client | Create a Desktop app client (step 5), save it (step 6), and authorize again |
| `token_missing` | The Keychain has no token for the mailbox | Authorize again |
| `client_missing` | The Keychain has no OAuth client | Save the client (step 6), then authorize again |
| `gmail_api_refused` | The token works, but the Gmail API is off in the project | Enable it (step 2), then authorize again |
| `scope_not_granted` | The Gmail box was unticked on Google's page | Authorize again and leave it ticked |
| `access_denied` | You chose **Cancel** on Google's page | Authorize again |
| `state_mismatch` | Google's answer didn't match an authorization started in the last 10 minutes, or the server restarted in between | Start again from Settings |
| `other_mailbox_connected` | A different Gmail account is already connected | Disconnect it first |
| `keychain` | The Keychain refused to read or write | Unlock the login Keychain and try again |
| `insufficientPermissions`, `accessNotConfigured`, `authError` | Gmail refused the token during a campaign's call (a send, a draft, a reply poll) | Check the Gmail API is enabled (step 2), then authorize again |

If Google's own page stops you with **"Access blocked: netkeeper has not completed the Google verification process"** (`Error 403: access_denied`), the account you chose isn't a test user and the app is in Testing. Add it (step 4) and authorize again.

A network failure, an error on Google's side, or a Gmail rate limit never marks a mailbox. netkeeper tries again at the next poll.

On Linux, `keyring` uses the Secret Service (for example GNOME Keyring). Without a running Secret Service, every step that stores a secret fails with a Keychain error.

## Checking the client against your account

The test suite never talks to Gmail; the engine's tests run against an in-memory fake. One test checks the real client, and the fake's threading rule, against your own account. It is skipped unless you ask for it:

```sh
NETKEEPER_GMAIL_TESTS=1 NETKEEPER_TEST_TIME_LIMIT_S=60 .venv/bin/python -m pytest tests/test_gmail_live.py -v
```

It reads the OAuth client and token from your login Keychain, for the mailbox `netkeeper gmail login` connected (user 1, mailbox 1; set `NETKEEPER_GMAIL_TEST_USER` and `NETKEEPER_GMAIL_TEST_MAILBOX` if yours differ; `netkeeper gmail status` shows the mailbox's id). It sends two short messages from the account to itself, one a follow-up in the other's thread, and leaves them and one draft under the label `netkeeper/live-test`. Delete them whenever you like. The raised time limit is for Gmail's round trips; the offline suite never needs it.
