#!/usr/bin/env python3
"""
Gmail CSV Downloader → Google Drive Uploader
─────────────────────────────────────────────
1. Connects to Gmail via IMAP
2. Searches emails for matching keywords / sentences
3. Downloads CSV/XLSX attachments from those emails
4. Uploads the files to Google Drive using browser automation
   (uses your existing Chrome login — no OAuth / API keys needed)
"""

import imaplib
import email
import os
import sys
import time

# Windows consoles often default to cp1252, which can't print ✔ / → / …
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from email.header import decode_header
from pathlib import Path

import json
import shutil
import socket
import subprocess
import urllib.request
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions

# ─── CONFIGURATION ─────────────────────────────────────────────────────────────

GMAIL_EMAIL    = "your-address@gmail.com"
GMAIL_PASSWORD = ""   # password here  ← paste your 16-char Gmail App Password
                      # Create one at: myaccount.google.com/apppasswords
                      # (requires 2-Step Verification to be ON)

# Keywords / phrases to match. Email subject OR body must contain at least one.
# Set MATCH_ALL = True to require ALL keywords to be present (AND logic).
KEYWORDS  = ["monthly report", "invoice", "Q1 data"]
MATCH_ALL = False   # False = any keyword matches (OR);  True = all must match (AND)

# Where to save downloaded files before uploading
DOWNLOAD_DIR = Path(__file__).parent / "downloads"

# Optional: only check emails received since this date (IMAP format: "01-Jan-2025")
# Set to None to scan all emails.
SINCE_DATE = None   # e.g. "01-May-2026"

# Google Drive folder URL — defaults to "My Drive" root.
# To upload into a specific folder, paste its URL from the browser here.
DRIVE_URL = "https://drive.google.com/drive/my-drive"

# Chrome profile that's logged into Google Drive.
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CHROME_USER_DATA = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "Google", "Chrome", "User Data"
)
CHROME_PROFILE_DIR = "Default"   # the profile signed into Drive (e.g. "Default", "Profile 1")

# Chrome 136+ refuses --remote-debugging-port on the DEFAULT User Data dir,
# so automation runs from its own copy of the profile. Delete this folder
# to force a fresh copy from the real Chrome profile.
AUTOMATION_USER_DATA = Path(__file__).parent / "chrome_data"

# ───────────────────────────────────────────────────────────────────────────────


def _decode(value) -> str:
    """Safely decode an email header value."""
    if value is None:
        return ""
    parts = decode_header(value)
    result = []
    for raw, enc in parts:
        if isinstance(raw, bytes):
            result.append(raw.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(str(raw))
    return " ".join(result)


def _matches(subject: str, body: str) -> bool:
    """Return True if the email text satisfies the keyword rules."""
    combined = (subject + " " + body).lower()
    hits = [kw.lower() in combined for kw in KEYWORDS]
    return all(hits) if MATCH_ALL else any(hits)


# ─── STEP 1: Gmail ─────────────────────────────────────────────────────────────

def download_csvs_from_gmail() -> list[Path]:
    """
    Connect to Gmail via IMAP, find emails that match KEYWORDS,
    and download every CSV/XLSX attachment to DOWNLOAD_DIR.
    Returns a list of downloaded file paths.
    """
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []

    print("► Connecting to Gmail IMAP…")
    conn = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    conn.login(GMAIL_EMAIL, GMAIL_PASSWORD)
    conn.select("INBOX")

    # Build IMAP search query
    if SINCE_DATE:
        _, data = conn.search(None, f'SINCE "{SINCE_DATE}"')
    else:
        _, data = conn.search(None, "ALL")

    ids = data[0].split() if data[0] else []
    print(f"  Scanning {len(ids)} email(s)…")

    seen: set[str] = set()   # avoid duplicate downloads across multiple keyword hits

    for uid in reversed(ids):   # newest first
        _, raw = conn.fetch(uid, "(RFC822)")
        if not raw or not raw[0]:
            continue

        msg = email.message_from_bytes(raw[0][1])
        subject = _decode(msg.get("Subject", ""))
        date    = msg.get("Date", "")

        # Extract plain-text body for keyword matching
        body_parts: list[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        body_parts.append(
                            part.get_payload(decode=True).decode("utf-8", errors="replace")
                        )
                    except Exception:
                        pass
        else:
            try:
                body_parts.append(
                    msg.get_payload(decode=True).decode("utf-8", errors="replace")
                )
            except Exception:
                pass
        body = " ".join(body_parts)

        if not _matches(subject, body):
            continue

        print(f"\n  ✔ Match found")
        print(f"    Subject : {subject}")
        print(f"    Date    : {date}")

        # Walk parts looking for CSV / XLSX attachments
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue

            disposition = part.get("Content-Disposition", "")
            filename    = _decode(part.get_filename() or "")

            # Accept attachments AND inline parts that look like CSV/XLSX files
            # (some senders use inline disposition for spreadsheets)
            if not filename.lower().endswith((".csv", ".xlsx")):
                continue
            if "attachment" not in disposition.lower() and "inline" not in disposition.lower():
                # Some senders omit Content-Disposition; still grab if it has a filename
                if not filename:
                    continue

            if filename in seen:
                continue
            seen.add(filename)

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            dest = DOWNLOAD_DIR / filename
            dest.write_bytes(payload)
            print(f"    ↓ Downloaded: {dest.name}")
            downloaded.append(dest)

    conn.logout()
    return downloaded


# ─── STEP 2: Google Drive upload ───────────────────────────────────────────────

def _wait_for_upload(driver, filename: str, timeout: int = 180):
    """Poll the Drive upload status notification until the upload finishes."""
    deadline = time.time() + timeout
    print("    Uploading", end="", flush=True)

    while time.time() < deadline:
        try:
            src = driver.page_source
            # Drive shows "1 upload complete" or "{filename} upload complete"
            if "upload complete" in src.lower():
                print(" ✔")
                return
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(2)

    print(" (timed out — check Drive manually)")


def _kill_chrome():
    """Force-kill Chrome and wait until every process is fully gone."""
    import psutil

    print("  Closing Chrome…", end="", flush=True)
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
    for _ in range(30):
        procs = [p for p in psutil.process_iter(["name"])
                 if p.info["name"] and p.info["name"].lower() == "chrome.exe"]
        if not procs:
            break
        print(".", end="", flush=True)
        time.sleep(1)
    print(" done")


def _free_port() -> int:
    """
    Pick an unused localhost port for the DevTools endpoint.
    Never hardcode 9222 — another app (e.g. some Windows OEM widget's WebView2)
    may already be listening there, and the driver would silently attach
    to THAT browser instead of ours.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _prepare_profile():
    """
    Build a separate user-data-dir for automation by copying the Google
    login session out of the real Chrome profile.

    Why a copy at all: Chrome 136+ silently IGNORES --remote-debugging-port
    when launched with the default User Data dir, so the debug port never
    opens. A non-default dir is allowed to have one.

    Why a naive copy fails: cookies are encrypted with a key stored in
    "Local State" at the User Data ROOT — copying only the profile folder
    leaves Chrome unable to decrypt them. We copy both.

    The copy is made once and reused on later runs, so if you ever have to
    log in manually inside the automation window, that login sticks.
    """
    profile_dst = AUTOMATION_USER_DATA / CHROME_PROFILE_DIR
    if profile_dst.exists():
        return

    print("  Preparing automation profile (one-time copy)…")
    profile_src = Path(CHROME_USER_DATA) / CHROME_PROFILE_DIR
    profile_dst.mkdir(parents=True, exist_ok=True)

    local_state = Path(CHROME_USER_DATA) / "Local State"
    if local_state.is_file():
        shutil.copy2(local_state, AUTOMATION_USER_DATA / "Local State")

    # "Network" holds the modern cookie DB; the rest keep the session alive.
    # "Secure Preferences" is deliberately skipped — its integrity MACs are
    # tied to the original path and trigger a settings-reset warning.
    for item in ["Network", "Cookies", "Login Data", "Web Data",
                 "Local Storage", "Session Storage", "Preferences"]:
        s = profile_src / item
        d = profile_dst / item
        try:
            if s.is_dir():
                shutil.copytree(s, d, dirs_exist_ok=True)
            elif s.is_file():
                shutil.copy2(s, d)
        except Exception as e:
            print(f"    (skipped {item}: {e})")

    print(f"  Automation profile ready: {AUTOMATION_USER_DATA}")


def upload_to_google_drive(files: list[Path]):
    """
    Open Chrome with your existing profile (already logged into Drive),
    navigate to DRIVE_URL, and upload each file via the hidden file input.
    Returns True if every file was handed to Drive successfully.
    """
    if not files:
        return True

    print("\n► Launching Chrome for Google Drive upload…")
    _kill_chrome()          # a leftover instance would swallow our launch flags
    _prepare_profile()

    port = _free_port()

    # Start Chrome ourselves (not via WebDriver) so it decrypts cookies
    # normally, using the automation copy of the profile (Chrome 136+ only
    # allows a debug port on a non-default user-data-dir).
    chrome_proc = subprocess.Popen([
        CHROME_EXE,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={AUTOMATION_USER_DATA}",
        f"--profile-directory={CHROME_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
    ])

    # Wait for the DevTools endpoint AND confirm it's our browser answering.
    print(f"  Waiting for Chrome (port {port})…", end="", flush=True)
    version_info = None
    for _ in range(30):
        try:
            with urllib.request.urlopen(
                f"http://localhost:{port}/json/version", timeout=1
            ) as resp:
                version_info = json.loads(resp.read())
            break
        except Exception:
            print(".", end="", flush=True)
            time.sleep(1)
    if version_info is None:
        chrome_proc.terminate()
        raise RuntimeError(
            "Chrome never opened its DevTools port. Try deleting the "
            f"'{AUTOMATION_USER_DATA}' folder and re-running."
        )
    print(f" ready ({version_info.get('Browser', 'unknown engine')})")

    # Open the Drive tab through DevTools so we know its exact target id —
    # this guarantees the driver controls THIS tab, not whatever else is open.
    new_tab_url = f"http://localhost:{port}/json/new?{DRIVE_URL}"
    try:
        req = urllib.request.Request(new_tab_url, method="PUT")
        tab = json.loads(urllib.request.urlopen(req).read())
    except Exception:
        # Chrome < 111 used GET for /json/new
        tab = json.loads(urllib.request.urlopen(new_tab_url).read())
    tab_handle = "CDwindow-" + tab["id"]
    print(f"  Opened Drive tab (target {tab['id'][:8]}…)")

    # Attach the matching driver to the running browser. Trust what the
    # DevTools endpoint reports ("Chrome/149..." or "Edg/...") rather than
    # assuming — Selenium Manager auto-downloads the right driver version.
    browser_id = version_info.get("Browser", "")
    try:
        if browser_id.startswith("Edg"):
            opts = EdgeOptions()
            opts.add_experimental_option("debuggerAddress", f"localhost:{port}")
            driver = webdriver.Edge(options=opts)
        else:
            opts = ChromeOptions()
            opts.add_experimental_option("debuggerAddress", f"localhost:{port}")
            driver = webdriver.Chrome(options=opts)
    except Exception:
        chrome_proc.terminate()
        raise
    wait = WebDriverWait(driver, 30)

    # Switch to the tab we created above
    if tab_handle in driver.window_handles:
        driver.switch_to.window(tab_handle)
    else:
        for handle in driver.window_handles:
            driver.switch_to.window(handle)
            if "drive.google.com" in driver.current_url or "accounts.google.com" in driver.current_url:
                break
    time.sleep(3)
    print(f"  Controlling tab: {driver.current_url}")

    try:
        for filepath in files:
            abs_path = str(filepath.resolve())
            print(f"\n  Uploading: {filepath.name}")
            print(f"  File path: {abs_path}")

            print("  Step 1: Navigating to Google Drive…")
            driver.get(DRIVE_URL)
            time.sleep(5)
            print(f"  Step 2: Current URL after navigation: {driver.current_url}")

            # If the copied cookies didn't survive, ask for a one-time manual
            # login — it's saved in the automation profile for future runs.
            if "accounts.google.com" in driver.current_url:
                print("  Not signed in. Please log into Google in the Chrome")
                print("  window that just opened (one time only — it will be")
                print("  remembered). Waiting up to 5 minutes…", end="", flush=True)
                deadline = time.time() + 300
                while time.time() < deadline:
                    if "drive.google.com" in driver.current_url:
                        break
                    print(".", end="", flush=True)
                    time.sleep(3)
                if "drive.google.com" not in driver.current_url:
                    print("\n  ERROR: still not signed in — aborting upload.")
                    return False
                print(" signed in!")
                driver.get(DRIVE_URL)
                time.sleep(5)

            print("  Step 3: Looking for file input…")
            file_input = None
            try:
                # Short wait — the hidden input only exists if the upload
                # dialog has been used before; otherwise use the New menu.
                file_input = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
                )
                print("  Step 3: File input found directly.")
            except Exception:
                print("  Step 3: No file input yet — using New ▸ File upload…")
                try:
                    new_btn = wait.until(
                        EC.element_to_be_clickable(
                            (By.XPATH, "//button[contains(., 'New') or contains(., 'new')]")
                        )
                    )
                    new_btn.click()
                    time.sleep(1)
                    # Target the menu item itself, not the text span inside it —
                    # clicking the span gets intercepted by the parent
                    # li[role='menuitem']. A JS click can't be intercepted.
                    upload_opt = wait.until(
                        EC.presence_of_element_located(
                            (By.XPATH, "//*[@role='menuitem'][contains(., 'File upload')]")
                        )
                    )
                    driver.execute_script("arguments[0].click();", upload_opt)
                    file_input = wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
                    )
                    print("  Step 3: File input found via New menu.")
                except Exception as e2:
                    print(f"  Step 3 FAILED: Could not find upload input: {e2}")
                    return False

            print("  Step 4: Sending file path to input…")
            driver.execute_script(
                "arguments[0].style.display='block';"
                "arguments[0].style.visibility='visible';"
                "arguments[0].style.opacity='1';",
                file_input,
            )
            file_input.send_keys(abs_path)
            print("  Step 5: File sent. Waiting for upload to complete…")
            _wait_for_upload(driver, filepath.name)

    except Exception as e:
        print(f"\n  UNEXPECTED ERROR: {e}")
        return False
    finally:
        time.sleep(2)
        try:
            driver.quit()
        except Exception:
            pass
        try:
            chrome_proc.terminate()
        except Exception:
            pass

    return True


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  Gmail CSV Downloader → Google Drive Uploader")
    print("=" * 55)
    print()

    # 1. Gmail
    files = download_csvs_from_gmail()

    if not files:
        print("\nNo CSV/XLSX attachments found in matching emails.")
        return

    print(f"\n  {len(files)} file(s) ready for upload.")

    # 2. Drive
    if upload_to_google_drive(files):
        print("\nAll done!")
    else:
        print("\nFinished with ERRORS — the upload did not complete. See messages above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
