# Gmail Organizer

A small app that runs on your own computer, in your browser, for getting an overflowing Gmail INBOX
under control. It downloads the headers of everything in your INBOX into a sortable table and shows
you who is filling it up. Then it lets you archive or trash messages in bulk, unsubscribe, and
create Gmail filters so it doesn't fill up again.

## Features

- **Messages tab:** every INBOX message in one table (date, sender, sender domain, subject, mailing
  list, size, unread, labels). Sort by any column, filter each column, or search across all of them.
  - Click a sender, domain or mailing list to show only those messages.
  - Click a subject to open that email in Gmail in a new tab.
  - Export what's shown to CSV.
- **Stats tab:** totals, messages per month, and top sender domains, sender addresses, mailing
  lists, space used and unread mail. Click any entry to filter the Messages tab.
- **To address (for accounts on your own domain):** if your address isn't on gmail.com (for
  example, a Google Workspace domain), the Messages tab adds a **To address** column and the Stats
  tab adds **Top recipient addresses**. They show which address on your domain each message was
  actually delivered to. That's useful if mail to many addresses at your domain ends up in this one
  mailbox. Plain gmail.com accounts don't see either one.
- **Bulk actions:** archive or move to Trash the selected messages, or everything matching the current
  filter. Every action asks you to confirm first, showing the count, top senders and sample subjects.
- **Action log with Undo:** everything the app changed in Gmail, with an Undo button for each action.
- **Unsubscribe:** one-click unsubscribe (RFC 8058) where the sender supports it; otherwise the app
  opens the sender's unsubscribe page or a pre-addressed unsubscribe email.
- **Gmail filters:** turn the current filter into a `mailFilters.xml` file you can import into Gmail,
  so future mail from that sender is archived, labelled or trashed automatically. You import the file
  yourself; the app never changes your Gmail settings.
- **Several accounts:** set up as many Gmail accounts as you like and switch between them from the
  header. Each account has its own cache, action log and Undo, so an action can never be undone in
  the wrong mailbox.
- **Fast:** a few thousand messages download in seconds. Refresh only downloads new messages.

## Safety

This app is built to be safe to point at a real mailbox:

- **INBOX only.** It reads and changes INBOX and nothing else. The one exception is Undo: to put
  messages back, it finds those exact messages in All Mail or Trash and returns them to INBOX.
- **Never permanently deletes anything.** "Move to Trash" uses Gmail's Trash, where messages stay for
  30 days. The code can't issue EXPUNGE or CLOSE, set the `\Deleted` flag, or delete folders.
  Every IMAP command goes through an allow-list in `gmail_imap.py`.
- **Nothing happens automatically.** It reads INBOX when it starts and when you click Refresh.
  It changes mail only when you confirm an action. There are no rules or schedules.
- **Checks its work.** After each archive or trash, it re-reads INBOX and reports only the messages
  that actually left.
- **Your data stays local.** The app listens only on `127.0.0.1`, and every request needs a token
  created when the app starts. Your password is stored in your system keychain (Windows Credential
  Manager, macOS Keychain, or Secret Service on Linux), never in a file. Downloaded headers and the
  action log are kept outside the project folder, one file per account: `%LOCALAPPDATA%\gmail_idx`
  on Windows, `~/.gmail_idx` elsewhere.
  The only outside service it contacts besides Gmail is a sender's unsubscribe URL, and only when you
  click unsubscribe.

## Requirements

- Python 3.10 or newer (tested with 3.12 on Windows 11)
- A Gmail or Google Workspace account with an **app password**. See
  [Getting an app password](#getting-an-app-password) below.

## Getting an app password

The app signs in to Gmail with an *app password*: a separate 16-character password that only works
for apps like this one. Your normal Google password never goes into the app. Each account you add
needs its own app password.

1. **Sign in to Google as that account** at <https://myaccount.google.com>. If you use several Google
   accounts, check the profile picture in the top-right and switch if needed.
2. **Turn on 2-Step Verification.** Google only offers app passwords when this is on. Go to
   <https://myaccount.google.com/security>, then under "How you sign in to Google" click
   **2-Step Verification** and follow the prompts. Skip this if it's already on.
3. **Create the app password** at <https://myaccount.google.com/apppasswords>. Type a name such as
   `Gmail Organizer` and click **Create**. Copy the 16-character password it shows (e.g.
   `abcd efgh ijkl mnop`). Google shows it only once; if you lose it, delete it and create another.
4. **Check that IMAP is on** in Gmail: Settings → See all settings → Forwarding and POP/IMAP →
   Enable IMAP. It's on by default for most accounts.

`python app.py --setup` shows these steps too, the first time you add an account.

If the App passwords page says *"The setting you are looking for is not available for your account"*:

- 2-Step Verification isn't on yet. Do step 2 first.
- The account uses Google's **Advanced Protection Program**, which doesn't allow app passwords.
- For a Google Workspace (work or school) account, the administrator has turned app passwords off.

You can revoke an app password any time on the same page. Revoking it doesn't affect your normal
Google password.

## Install

Windows (Command Prompt):

```bat
git clone https://github.com/redlog/gmail-organizer.git
cd gmail-organizer
py -3.12 -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
```

macOS / Linux:

```bash
git clone https://github.com/redlog/gmail-organizer.git
cd gmail-organizer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Set up

```
python app.py --setup
```

Setup asks for a Gmail address and its app password. For a new account it first shows how to get an
app password. It tests the login before saving anything, explains what went wrong if Gmail refuses,
and gives you three tries. The password is saved in your system keychain only once the login works.

- **More accounts:** run setup again with another address. Each account needs its own app password,
  created while signed in to that account.
- **Replace a password:** run setup with an address that's already set up. When it asks for the
  password, press Enter to keep the saved one.
- **Remove an account:** run `python app.py --remove you@example.com`. This deletes the saved password,
  cached headers and action log for that account. Nothing in Gmail changes. Exit the app first.

The app opens with the account you set up most recently, or the one you last switched to.

## Run

```
python app.py
```

Your browser opens at <http://127.0.0.1:8765> and the INBOX downloads.

While a Gmail operation runs (refresh, archive, trash, undo, switching account, unsubscribe), a
progress dialog covers the page so nothing else can be started. It has two buttons:

- **Stop** ends the operation after the current small batch. An archive or trash keeps, and logs,
  what it already moved, so you can still Undo it. A stopped refresh picks up where it left off next
  time.
- **Stop and exit app** does the same, then shuts the app down.

Use **Exit** (next to Refresh) to quit when nothing is running. **Ctrl+C** in the terminal always
works too: it gives a running operation up to 15 seconds to stop cleanly, and pressing Ctrl+C again
quits immediately. If Gmail stops responding, the operation fails with an error after 60 seconds
instead of hanging.

## How it works

- **Downloading:** headers are fetched over IMAP from INBOX opened read-only (`EXAMINE`), so nothing
  is marked as read. Sender domains are reduced to the registered domain using the public-suffix
  list, so `email.uber.com` counts as `uber.com`.
- **To address** (accounts not on gmail.com): the address at your domain that the message was
  delivered to. It's taken from X-Original-To, Delivered-To, To, Cc and the `for <…>` part of
  Received headers. This catches BCC and mailing-list mail, where the To header shows something else.
- **Archive:** IMAP `MOVE` from INBOX to All Mail. In Gmail that only removes the Inbox label.
  (Removing the label directly while INBOX is open is silently ignored by Gmail.)
- **Trash:** IMAP `MOVE` from INBOX to Trash.
- **Undo:** copies an archived message from All Mail back to INBOX, or moves a trashed message from
  Trash back to INBOX. It matches messages by Gmail message ID, so only that action's messages move.
- **Open in Gmail:** `https://mail.google.com/mail/?authuser=<you>#all/<message id in hex>`.

## Limitations

- Only headers are downloaded, so you can't search message bodies. Gmail's own search does that.
- Works message by message, not by Gmail conversation.
- Shows one account at a time. Use the account picker in the header to switch. It waits for any
  Gmail operation in progress to finish first.
- Gmail only. It relies on Gmail's IMAP extensions (`X-GM-MSGID`, special folders, `MOVE`).
- Tested on Windows. macOS and Linux should work but haven't been tested.

## Troubleshooting

- **"First run: python app.py --setup":** no account is configured yet. Run setup.
- **Login fails during setup:** setup says why. The usual causes: you entered your normal Google
  password instead of an app password, the app password was created while signed in to a different
  account, or IMAP is off. See [Getting an app password](#getting-an-app-password).
- **"No module named …":** the virtual environment isn't active in this terminal. Activate it again,
  or run `pip install -r requirements.txt`.
- **Port 8765 is in use:** another copy of the app is probably running. Click Exit in that copy first.

## Project layout

| File | What it does |
|---|---|
| `app.py` | FastAPI server, setup / remove commands, refresh / actions / undo / unsubscribe / switch / exit endpoints |
| `gmail_imap.py` | Gmail IMAP access with the safety allow-list |
| `parsing.py` | Header decoding, sender domain, To address |
| `store.py` | Account list, and per account a SQLite cache and action log |
| `static/` | Browser UI (plain HTML/JS, using [Tabulator](https://tabulator.info) for the table) |
