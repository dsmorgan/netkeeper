# Draft templates with your AI assistant

You can use an AI chat assistant you already pay for, such as one in your browser, to draft and improve your campaign templates. netkeeper helps you write the prompt and read the reply back, but it never talks to an AI service itself.

Before the 1.0 release, netkeeper has no AI integration. It makes no AI API calls, stores no AI keys, and sends nothing to any AI provider. You carry the text both ways by copy and paste. An in-app AI module may come after 1.0 (see section 12 of [the spec](architecture.md#12-llm-module-optional)).

## Privacy: what never goes into the prompt

Whatever you paste into an AI chat assistant goes to that provider, under its terms. Read them, and assume what you paste may be stored or used to train its models unless your plan says otherwise.

- **Never paste your contacts' personal data.** No names, email addresses, phone numbers, companies, LinkedIn URLs, notes, or exports. A template doesn't need them.
- **Use merge-field placeholders.** The prompt asks for `{{ first_name }}`, `{{ company }}`, and the other fields netkeeper fills in for each contact at send time. The assistant writes one message for everybody, and netkeeper personalizes it on your own machine.
- **Describe the audience as a group.** "Former colleagues in engineering" is enough. "Robin at Example Co" is personal data.

The helper in the template editor builds its prompt from the form you fill in, the list of merge fields, and netkeeper's lint rules. It never includes a contact's data, even when you've picked a contact in the preview.

- **What you type in the form goes into the prompt word for word.** Describe the campaign and the audience in general terms, and never type a contact's name or details there.
- **The current text goes in only when you ask.** **Include the current text** is off by default. When you tick it, the prompt carries the template's subject and body. They hold only merge-field placeholders unless you typed real names or details into them, so read them before you copy.

## Use the helper in the template editor

1. Open **Templates** and start a new template, or open one to improve. Choose the channel first: an email has a subject line, and a LinkedIn message doesn't.
2. Under **Draft with your AI assistant**, choose **Show**.
3. Fill in the short form:
   - **What the campaign is for:** for example, "reconnect and mention I'm looking for a product role."
   - **Who it's for:** a group, like "people I worked with at my last two jobs."
   - **Tone:** warm, friendly and casual, professional, or brief and direct.
   - **Steps:** how many messages, the first one and its follow-ups. A reconnect sequence is usually two.
   - **Anything to mention:** a shared project, an event, a link you want to share.
   - **Include the current text:** tick it to improve the template you have open rather than start from scratch.
4. Choose **Copy prompt**. If your browser doesn't allow copying, the prompt appears selected in a box below; copy it with Cmd+C or Ctrl+C.
5. Paste the prompt into your AI chat assistant and send it.
6. Copy the assistant's whole reply, paste it into **Assistant's reply**, and choose **Paste result**.

netkeeper reads the reply's `Subject:` and `Body:` labels and fills in the subject and body. Then it lints the text at once. If an email step has no `Subject:`, the subject you had stays. If the reply doesn't have the labels, all of it goes into the body for you to edit. Lines outside the labeled format, like the assistant's "Here's a draft", are left out, and the helper says how many.

If the paste replaced a subject or body you had, **Undo paste** puts your text back. It stays available until you edit the template or paste again.

A template holds one step. When the reply has several steps, netkeeper fills the template from step 1 and lists the others under **Other steps**:

- **Use step N here** puts that step in this template instead. The step it replaces moves to the list, so you can switch back.
- **Copy step N** copies the step in the labeled format. Save this template, start a new template for the step, and paste it into the new template's helper.

## The prompt

The helper writes the prompt for you. If you'd rather write your own, use this shape and keep the same rules:

```text
Help me write a sequence of 2 emails: a first message and follow-ups to reconnect with people in my professional network.

What the campaign is for: reconnect and mention I'm looking for a product role
Who it is for: former colleagues
Tone: warm

Rules:
- Plain text only: no HTML, no Markdown, no attachments, no images, no emoji.
- Keep it short and personal: at most 120 words for the first message and 80 words for each follow-up.
- Write a placeholder wherever a detail about the recipient or me goes, exactly as shown, like {{ first_name }}. Never write a real name, company, or other personal detail in its place.
- Use only the placeholders listed below. Any other {{ name }} is an error.

Placeholders you may use:
- {{ first_name }}: ...
- {{ company }}: ...

Answer in exactly this format, with nothing before or after it:

Step 1
Subject: <one line>
Body:
<the message>
End of step 1
```

The helper's prompt lists every merge field netkeeper allows, from the same list the editor's **Merge fields** panel shows, and every lint rule for the channel.

## Tips for a good draft

- **Keep it short and personal.** The first message is a few sentences; a follow-up is shorter. Your name in their inbox matters more than polished copy ([the reconnect method](networking-workflow.md)).
- **Use only netkeeper's merge fields.** A field netkeeper doesn't know would render blank, so lint refuses it. The **Merge fields** panel lists them all.
- **Name the recipient.** Every body needs at least one per-contact field, like `{{ first_name }}`. Identical bulk mail is a spam signal, and lint refuses it.
- **Plain text only.** No attachments, no images, no HTML or Markdown formatting. netkeeper sends plain text.
- **Ask for changes in the chat.** "Make step 2 shorter" or "less formal" works well; paste the new reply when you're happy with it.

## Check the result before you use it

1. **Lint.** Fix every error the editor shows under the subject and body. A campaign can't use a template with lint errors.
2. **Save, then preview.** The preview renders the saved version for a contact you pick, so you see exactly what they'd get, including any field that would be blank for them.
3. **Send a test.** In the campaign's review, page through the rendered messages, then send each email step to yourself with **Test send**, and read it as the recipient would. A campaign can't activate until you do.

Read every message yourself. The assistant can get facts wrong or sound unlike you, and the message goes out under your name.
