# Gmail → Google Drive Automation

Automatically scan your Gmail inbox for emails matching keywords, download their
CSV/XLSX attachments, and upload them to Google Drive — **without using the Google
Drive API or any OAuth setup**. The upload is done by driving your real Chrome
browser, which is already signed into Google.

---

## What it does

1. **Connects to Gmail over IMAP** using an App Password and searches your inbox
   for keywords you choose (match *any* keyword with OR logic, or *all* with AND).
2. **Downloads every `.csv` / `.xlsx` attachment** from matching emails into a
   local `downloads/` folder, skipping duplicates.
3. **Uploads each file to Google Drive** by launching your existing Chrome profile
   (so you're already logged in), opening Drive, and pushing the files through the
   page itself.

Because the upload runs through the browser, you never have to create a Google
Cloud project, enable the Drive API, or manage OAuth tokens.

---

## Requirements

- **Windows** with **Google Chrome** installed.
- **Python 3.10+**
- A Gmail account with **2-Step Verification** enabled (needed for an App Password).

Install the Python dependencies:

```bash
pip install -r requirements.txt
```

---

## Setup

Open `gmail_to_drive.py` and edit the **CONFIGURATION** block at the top:

| Variable             | What to set                                                       |
|----------------------|-------------------------------------------------------------------|
| `GMAIL_EMAIL`        | Your Gmail address                                                |
| `GMAIL_PASSWORD`     | Your 16-character Gmail **App Password** (see below)              |
| `KEYWORDS`           | List of words/phrases to match in the subject or body            |
| `MATCH_ALL`          | `False` = any keyword matches (OR); `True` = all required (AND)  |
| `SINCE_DATE`         | Limit search to emails after this date, e.g. `"01-May-2026"`, or `None` |
| `DRIVE_URL`          | Google Drive folder URL (default is the My Drive root)           |
| `CHROME_PROFILE_DIR` | The Chrome profile signed into Drive — `"Default"`, `"Profile 1"`, etc. |

### Creating a Gmail App Password

1. Go to <https://myaccount.google.com/apppasswords>
2. Choose **Mail** as the app and your device, then click **Generate**.
3. Copy the 16-character password and paste it into `GMAIL_PASSWORD`.

> App Passwords require 2-Step Verification to be turned on for your account.

### Finding your Chrome profile

If you only use one Chrome profile, leave `CHROME_PROFILE_DIR = "Default"`. If you
have multiple, open `chrome://version` in Chrome — the **Profile Path** ends with
the folder name to use (e.g. `Profile 1`).

---

## Usage

```bash
python gmail_to_drive.py
```

Chrome will open automatically, navigate to Google Drive, and upload the files.
You can watch it happen live in the browser window. On a clean exit it prints
`All done!`; on any failure it prints an error and exits with a non-zero code.

The **first run** copies your Chrome login session into a local `chrome_data/`
folder (this is what keeps you signed in). If Drive ever opens signed-out, just
log in once in the window that appears — that login is remembered for next time.

---

## How it works (and why it's built this way)

Driving a logged-in Chrome from automation on modern Windows has a few traps this
script works around:

- **Chrome 136+ ignores `--remote-debugging-port` on the default profile folder.**
  The script copies your profile into a separate `chrome_data/` directory (which
  *is* allowed a debug port) and launches Chrome from there.
- **Cookies are encrypted with a key stored in `Local State`** at the root of the
  user-data directory. Copying only the profile folder leaves the cookies
  undecryptable, so the script copies `Local State` too — that's what preserves
  your login.
- **The debug port is chosen dynamically**, not hardcoded to 9222, so the driver
  can't accidentally attach to some other app's embedded browser.
- **The right driver is selected automatically** based on what the browser reports
  (Chrome vs. Edge), via Selenium Manager — no manual driver downloads.
- **Drive's upload input isn't always present**, so the script falls back to the
  **New ▸ File upload** menu, clicking the menu item via JavaScript so the click
  can't be intercepted by overlapping elements.

---

## Notes & limitations

- **Closes all Chrome windows** when it starts (Chrome only allows one session per
  profile). Save your work in other Chrome windows first.
- **Windows-only** as written (uses `taskkill` and a Windows Chrome path). It could
  be adapted for macOS/Linux by changing `CHROME_EXE` and the kill command.
- If Drive's UI changes substantially, the upload selectors may need updating.

---

## Security

- Your **Gmail App Password is stored in `gmail_to_drive.py`**. Keep this file
  private. If you fork or share this repo, **do not commit your real password** —
  the `GMAIL_PASSWORD` field ships blank on purpose.
- The `chrome_data/` folder contains a copy of your browser login session. It is
  git-ignored — never commit it.
- An App Password only grants mail access and can be revoked anytime at
  <https://myaccount.google.com/apppasswords>.

---

## Project layout

```
gmail-to-drive-automation/
├── gmail_to_drive.py    # the script
├── requirements.txt     # Python dependencies
├── README.md            # this file
├── .gitignore
├── downloads/           # (created at runtime) downloaded attachments
└── chrome_data/         # (created at runtime) copied Chrome login session
```
