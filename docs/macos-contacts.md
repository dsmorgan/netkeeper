# Importing into macOS Contacts

netkeeper can write your contacts as a vCard file that macOS Contacts imports, with one Contacts group per tag. Once the cards are in an iCloud account, iCloud syncs them to your other devices. This is a one-way copy: netkeeper never reads from Contacts or changes it.

## What the file holds

- One card per contact, with every email, phone number, and link, the company, title, location, and notes. The name you address someone by is the card's display name; their LinkedIn first name stays in the name field, and a different preferred name becomes the nickname.
- The contact's tags, in the card's categories field.
- One group card per tag, listing the contacts who carry that tag. A tag no exported contact carries gets no group.
- No one marked do-not-contact. Mail and Messages suggest addresses from Contacts, so an address book counts as a way to reach someone.
- Archived contacts only if your filter includes them, as with every other export.

The file is vCard 3.0, the version Contacts imports most reliably. Every card has a stable ID, so exporting the same contacts twice gives the same cards.

## Steps

1. In netkeeper, open **Exports**. Set a filter if you want only some contacts, pick the **macOS Contacts** preset, and click **Download**. The file is `contacts-macos-contacts.vcf`. From a terminal, `netkeeper export --preset macos-contacts --format vcard --out contacts.vcf` writes the same file.
2. Optional, but worth it the first time: back up what Contacts has now. In Contacts, choose **File > Export > Contacts Archive**.
3. Choose where the cards go. In **Contacts > Settings > General**, set **Default Account** to **iCloud** to sync them to your other devices, or to **On My Mac** to keep them on this Mac only. Groups need one of these two. A Google or Exchange account may drop the groups or not accept them.
4. Choose **File > Import**, select the `.vcf` file, and click **Open**. Double-clicking the file in Finder does the same.
5. If some cards match contacts already in Contacts, Contacts asks what to do with them. Review them before you accept, especially on a second import.
6. Check the sidebar: each tag is now a group under the account you picked. Select a group to see its members.

## Importing again later

Importing again adds and updates cards, but it doesn't remove anything. A contact you have since archived, marked do-not-contact, or untagged in netkeeper stays in Contacts and in its old group until you remove it there. To start over, delete the groups and cards from Contacts first, then import a fresh export.
