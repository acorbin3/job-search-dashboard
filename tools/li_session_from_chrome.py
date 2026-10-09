#!/usr/bin/env python3
"""
Populate ~/.config/healthtech-jobs/linkedin_session.json from Chrome's cookie
store, so `linkedin_scrape.py --voyager` can use the signed-in endpoint.

The li_at cookie is HttpOnly, so a page script (or the browser-automation
tools) cannot read it — the only local source is Chrome's own cookie database,
which stores it AES-encrypted with a key held in the macOS Keychain.

This reads a *copy* of that database (never Chrome's live file), asks the
Keychain for the decryption key — which pops the standard macOS auth prompt,
your OS-level consent — decrypts li_at + JSESSIONID, and writes them to the
config file with 0600 permissions. The values go cookie-store -> this script
-> config file; they are never printed. Run it again whenever LinkedIn rotates
your session (the crawler will tell you with an HTTP 401).

Usage:
    python3 li_session_from_chrome.py                 # Chrome "Default" profile
    python3 li_session_from_chrome.py --profile "Profile 1"
    python3 li_session_from_chrome.py --browser chrome-beta
    python3 li_session_from_chrome.py --check         # verify the saved session

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

CONFIG_PATH = Path.home() / ".config" / "healthtech-jobs" / "linkedin_session.json"

# (cookie-store dir under ~/Library/Application Support, Keychain service name)
BROWSERS = {
    "chrome": ("Google/Chrome", "Chrome Safe Storage", "Chrome"),
    "chrome-beta": ("Google/Chrome Beta", "Chrome Safe Storage", "Chrome"),
    "brave": ("BraveSoftware/Brave-Browser", "Brave Safe Storage", "Brave"),
    "edge": ("Microsoft Edge", "Microsoft Edge Safe Storage", "Microsoft Edge"),
}


def keychain_key(service, account):
    """The AES key Chrome derived from its Keychain-stored 'Safe Storage' secret.

    `security find-generic-password` triggers the macOS auth prompt; the secret
    it returns stays in this process and is stretched into the 16-byte AES key.
    """
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
            "WHERE host_key LIKE '%linkedin.com' "
            "AND name IN ('li_at','JSESSIONID')").fetchall()
        con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    got = {name: enc for name, enc in rows}
    if "li_at" not in got:
        raise SystemExit(
            f"No li_at cookie for linkedin.com in {browser} '{profile}'. "
            f"Are you logged in to LinkedIn in that profile?")
    key = keychain_key(service, account)
    out = {}
    for name in ("li_at", "JSESSIONID"):
        if name in got:
            val = decrypt(got[name], key)
            if val:
                out[name] = val.strip('"')      # JSESSIONID is stored quoted
    if not out.get("li_at"):
        raise SystemExit("Decryption failed — the Keychain key did not match. "
                         "This can happen right after a Chrome update; try "
                         "quitting and reopening Chrome, then re-run.")
    return out


def do_check():
    """Fire one signed-in request and report status without showing secrets."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import linkedin_scrape as L
    sess = L.load_session()
    if not sess:
        raise SystemExit("No session configured. Run this script without "
                         "--check first.")
    q = {"keywords": '"Director of Engineering"', "geoId": L.GEO_US,
         "f_TPR": "r86400", "sortBy": "DD"}
    try:
        payload = L.voyager_get(
            f"{L.VOYAGER_URL}?decorationId={L.VOYAGER_DECORATION}"
            f"&count=25&q=jobSearch&query={L.voyager_query_string(q)}&start=0",
            sess)
    except L.SessionExpired as e:
        raise SystemExit(f"Session REJECTED ({e}). The cookies are present but "
                         f"no longer valid — re-run to refresh them.")
    total = (payload or {}).get("paging", {}).get("total")
    print(f"Session OK — signed-in search returned total={total}. "
          f"Boolean keywords and sortBy are now honored.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
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
                         "the two cookie values in by hand.")
    if args.check:
        return do_check()

    cookies = read_cookies(args.browser, args.profile)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
              "w") as f:
        json.dump({"li_at": cookies["li_at"],
                   "JSESSIONID": cookies.get("JSESSIONID", "")}, f, indent=2)
        f.write("\n")
    os.chmod(CONFIG_PATH, 0o600)
    # Report lengths, never values.
    lens = {k: len(v) for k, v in cookies.items()}
    print(f"Wrote {CONFIG_PATH} (0600).")
    print(f"  li_at: {lens.get('li_at', 0)} chars, "
          f"JSESSIONID: {lens.get('JSESSIONID', 0)} chars")
    print("Verify with:  python3 li_session_from_chrome.py --check")


if __name__ == "__main__":
    main()
