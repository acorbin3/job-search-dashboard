#!/usr/bin/env python3
"""
Populate ~/.config/healthtech-jobs/glassdoor_session.json from Chrome's cookie
store, so `glassdoor.py` can fetch ratings server-side.

Glassdoor sits behind a bot wall: a plain requests call gets 403 no matter the
headers. A signed-in browser sails through, and the difference is the cookie
jar (gdId / GSESSIONID / at / __cf_bm ...). Several of those are HttpOnly, so a
page script cannot read them — the only local source is Chrome's own cookie
database, AES-encrypted with a key held in the macOS Keychain.

Same mechanics as li_session_from_chrome.py: read a *copy* of the database,
ask the Keychain for the key (the standard macOS auth prompt is your consent),
decrypt, and write to a 0600 config file. Values go cookie-store -> script ->
config file and are never printed. The bot-wall clearance cookies are pinned to
the browser's User-Agent, so the UA is saved alongside them and glassdoor.py
sends it verbatim. Re-run whenever fetching starts returning 403.

Usage:
    python3 gd_session_from_chrome.py                 # Chrome "Default" profile
    python3 gd_session_from_chrome.py --profile "Profile 1"
    python3 gd_session_from_chrome.py --check         # verify the saved session

macOS + Chromium-family browsers only. Needs the `cryptography` package.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "healthtech-jobs" / "glassdoor_session.json"

# (cookie-store dir under ~/Library/Application Support, Keychain service, account)
BROWSERS = {
    "chrome": ("Google/Chrome", "Chrome Safe Storage", "Chrome"),
    "chrome-beta": ("Google/Chrome Beta", "Chrome Safe Storage", "Chrome"),
    "brave": ("BraveSoftware/Brave-Browser", "Brave Safe Storage", "Brave"),
    "edge": ("Microsoft Edge", "Microsoft Edge Safe Storage", "Microsoft Edge"),
}

# Chrome's own UA is read from the live browser when possible; this is the
# fallback written if that lookup is unavailable.
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/150.0.0.0 Safari/537.36")

# Session cookie without which Glassdoor treats the caller as a bot.
REQUIRED = "gdId"


def keychain_key(service, account):
    """The AES key Chrome derived from its Keychain-stored 'Safe Storage' secret."""
    try:
        secret = subprocess.check_output(
            ["security", "find-generic-password", "-w", "-s", service,
             "-a", account], stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        raise SystemExit(
            f"Could not read '{service}' from the Keychain. If a prompt "
            f"appeared, click Allow and re-run; if you denied it, re-run.")
    return hashlib.pbkdf2_hmac("sha1", secret, b"saltysalt", 1003, 16)


def decrypt(encrypted, key):
    """Decrypt one Chrome cookie value (macOS v10 scheme)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if not encrypted or encrypted[:3] != b"v10":
        return None
    dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    plain = dec.update(encrypted[3:]) + dec.finalize()
    if plain and plain[-1] <= 16:          # strip PKCS7 padding
        plain = plain[:-plain[-1]]
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        # Chrome 130+ prepends a 32-byte SHA256 domain hash to the plaintext.
        return plain[32:].decode("utf-8", "replace")


def read_cookies(browser, profile):
    """Every glassdoor.com cookie, decrypted. Which ones gate the bot wall is
    undocumented and changes, so take the whole jar rather than guessing."""
    store, service, account = BROWSERS[browser]
    ck = (Path.home() / "Library/Application Support" / store / profile
          / "Cookies")
    if not ck.exists():
        raise SystemExit(f"No cookie store at {ck}\n"
                         f"Wrong --profile? Check the browser's profile list.")
    tmp = Path(tempfile.mkdtemp())
    try:
        shutil.copy2(ck, tmp / "Cookies")       # Chrome locks the live file
        con = sqlite3.connect(f"file:{tmp / 'Cookies'}?mode=ro&immutable=1",
                              uri=True)
        rows = con.execute(
            "SELECT name, encrypted_value FROM cookies "
            "WHERE host_key LIKE '%glassdoor.com'").fetchall()
        con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not rows:
        raise SystemExit(
            f"No glassdoor.com cookies in {browser} '{profile}'. Open "
            f"https://www.glassdoor.com in that profile (sign in), then re-run.")
    key = keychain_key(service, account)
    out = {}
    for name, enc in rows:
        val = decrypt(enc, key)
        if val:
            out[name] = val.strip('"')
    if REQUIRED not in out:
        raise SystemExit(
            f"Decrypted {len(out)} cookies but none named {REQUIRED}. If the "
            f"count is 0 the Keychain key did not match — this happens right "
            f"after a Chrome update; quit and reopen Chrome, then re-run.")
    return out


def chrome_user_agent():
    """Chrome's UA, from the running browser's version. cf clearance cookies are
    UA-bound, so a mismatch here reads as a different client and gets 403."""
    app = Path("/Applications/Google Chrome.app/Contents/Info.plist")
    if app.exists():
        try:
            ver = subprocess.check_output(
                ["defaults", "read", str(app.with_suffix("")), "CFBundleShortVersionString"],
                stderr=subprocess.DEVNULL).decode().strip()
            major = ver.split(".")[0]
            if major.isdigit():
                return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        f"Chrome/{major}.0.0.0 Safari/537.36")
        except (subprocess.CalledProcessError, OSError):
            pass
    return DEFAULT_UA


def do_check():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import glassdoor as G
    sess = G.load_session()
    if not sess:
        raise SystemExit("No session configured. Run this script without "
                         "--check first.")
    try:
        hit = G.search_employer("Databricks", sess)
    except G.SessionExpired as e:
        raise SystemExit(f"Session REJECTED ({e}). The cookies are present but "
                         f"no longer pass the bot wall — re-run to refresh.")
    if not hit:
        raise SystemExit("Session works but the probe search returned nothing — "
                         "Glassdoor may have changed the typeahead response.")
    print(f"Session OK — typeahead resolved Databricks to employer "
          f"{hit['employer_id']} ({hit['name']}).")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--browser", default="chrome", choices=sorted(BROWSERS))
    ap.add_argument("--profile", default="Default",
                    help='browser profile dir (default "Default"; others look '
                         'like "Profile 1")')
    ap.add_argument("--check", action="store_true",
                    help="don't extract — just test the saved session")
    args = ap.parse_args()

    if sys.platform != "darwin":
        raise SystemExit("This extractor is macOS-only. On Linux/Windows, copy "
                         "the cookie values in by hand.")
    if args.check:
        return do_check()

    cookies = read_cookies(args.browser, args.profile)
    ua = chrome_user_agent()
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
              "w") as f:
        json.dump({"cookies": cookies, "user_agent": ua}, f, indent=2)
        f.write("\n")
    os.chmod(CONFIG_PATH, 0o600)
    # Report names and lengths, never values.
    print(f"Wrote {CONFIG_PATH} (0600).")
    print(f"  {len(cookies)} cookies: {', '.join(sorted(cookies)[:12])}"
          f"{' …' if len(cookies) > 12 else ''}")
    print(f"  UA: {ua}")
    print("Verify with:  python3 gd_session_from_chrome.py --check")


if __name__ == "__main__":
    main()
