"""Load the IPEDS master list of all US postsecondary institutions."""
import csv
import io
import logging
import re
import zipfile
from pathlib import Path

import requests

DATA = Path(__file__).resolve().parents[1] / "data"
UA = {"User-Agent": "Mozilla/5.0 (college-job-scanner; personal research)"}
DEFAULT_SECTORS = [0, 1, 2, 3, 4, 5, 6]  # system offices + 4yr + 2yr public/private
# Sectors 7-9 (<2-year) are mostly beauty/trade schools, but they also hold real
# technical colleges and career centers that hire IT/CS instructors. Keep those by name.
TECH_NAME = re.compile(r"technical college|technology center|technical center|career (and technical|center|college)|"
                       r"community college|university|institute of technology|college of applied|technical institute|"
                       r"applied technology", re.I)
TRADE_NAME = re.compile(r"beauty|cosmetolog|barber|hair|nail|esthetic|massage|paul mitchell|aveda|nursing|medical|dental|"
                        r"truck|driving|cdl|welding|aviation|flight|healthcare|health care|allied health|phlebotomy|"
                        r"real estate|\bems\b|paramedic|funeral|bible|yeshiva|seminary|rabbinical", re.I)


def get(url, timeout=120):
    return requests.get(url, headers=UA, timeout=timeout)

log = logging.getLogger(__name__)

IPEDS_URL = "https://nces.ed.gov/ipeds/datacenter/data/HD{year}.zip"
CSV_PATH = DATA / "ipeds_institutions.csv"


def download(year: int = 2024) -> Path:
    url = IPEDS_URL.format(year=year)
    log.info("Downloading IPEDS %s", url)
    r = get(url, timeout=120)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    name = [n for n in z.namelist() if n.lower().endswith(".csv")][0]
    raw_b = z.read(name)
    try:
        raw = raw_b.decode("utf-8-sig")
    except UnicodeDecodeError:
        raw = raw_b.decode("latin-1").lstrip("\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(raw)))
    keep = ["UNITID", "INSTNM", "CITY", "STABBR", "WEBADDR", "SECTOR", "ICLEVEL",
            "CONTROL", "CYACTIVE", "INSTSIZE", "INSTCAT", "DEGGRANT", "F1SYSTYP", "F1SYSNAM", "F1SYSCOD"]
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keep)
        w.writeheader()
        for row in rows:
            row = {k.lstrip("﻿"): v for k, v in row.items()}
            w.writerow({k: row.get(k, "") for k in keep})
    log.info("Saved %d institutions to %s", len(rows), CSV_PATH)
    return CSV_PATH


def normalize_url(web: str) -> str | None:
    web = (web or "").strip()
    if not web:
        return None
    if not re.match(r"^https?://", web, re.I):
        web = "https://" + web
    web = web.rstrip("/")
    # drop deep paths; keep scheme + host
    m = re.match(r"^(https?://[^/]+)", web)
    return m.group(1).lower() if m else None


def load_all(active_only=True, sectors=None, technical=True):
    """IPEDS institutions as dicts. `technical` also keeps <2-year schools whose
    name says technical college / career center (not beauty or trade schools)."""
    if not CSV_PATH.exists():
        download()
    with open(CSV_PATH, newline="") as f:
        if "F1SYSNAM" not in (f.readline() or ""):
            download()                       # old CSV without the system columns
    sectors = set(str(s) for s in (sectors or DEFAULT_SECTORS))
    out = []
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            if active_only and row["CYACTIVE"] != "1":
                continue
            if row["SECTOR"] not in sectors:
                tech = technical and row["SECTOR"] in ("7", "8", "9") and \
                    TECH_NAME.search(row["INSTNM"]) and not TRADE_NAME.search(row["INSTNM"])
                if not tech:
                    continue
            url = normalize_url(row["WEBADDR"])
            if not url:
                continue
            out.append({
                "unitid": row["UNITID"],
                "name": row["INSTNM"],
                "city": row["CITY"],
                "state": row["STABBR"],
                "sector": int(row["SECTOR"]),
                "homepage": url,
                "system": row.get("F1SYSNAM", "").strip() if row.get("F1SYSTYP") == "1" else "",
            })
    return out
