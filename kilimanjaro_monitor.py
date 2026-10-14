#!/usr/bin/env python3
"""
Monitor Dominic's Kilimanjaro Challenge 2027 page for changes to the
total raised and the list of donations, then notify by email and/or push.

Setup:
    pip install requests beautifulsoup4

Configure with environment variables (or edit the CONFIG block below):
    # Email (optional)
    SMTP_HOST=smtp.gmail.com  SMTP_PORT=587
    SMTP_USER=you@gmail.com   SMTP_PASS=<app password>
    EMAIL_TO=you@example.com
    # Push notification via ntfy.sh (optional, free; install the ntfy app
    # and subscribe to the same topic name)
    NTFY_TOPIC=dom-kili-2027-<something-random>

Run it on a schedule, e.g. every hour with cron:
    0 * * * * /usr/bin/python3 /path/to/kilimanjaro_monitor.py

Flags:
    --test     send a test notification and exit
    --dry-run  check and print changes, but don't notify or save state
"""

import hashlib
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- CONFIG
URL = "https://www.aldingbournetrust.org/store/p298/Dominic-Kilimanjaro-2027.html"
STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).with_name("kili_state.json")))

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
# ------------------------------------------------------------------------

HEADERS = {"User-Agent": "Mozilla/5.0 (fundraiser-change-monitor)"}

DONATION_RE = re.compile(
    r"Name:\s*(?P<name>.+?)\s*"
    r"Donation:\s*(?P<amount>.+?)\s*"
    r"Message:\s*(?P<message>.*?)\s*"
    r"Date:\s*(?P<date>\d{1,2}\.\d{1,2}\.\d{2,4})",
    re.S,
)
TOTAL_RE = re.compile(r"Total raised:\s*£\s*([\d,]+(?:\.\d{1,2})?)", re.I)
AIM_RE = re.compile(r"Aim:\s*£\s*([\d,]+(?:\.\d{1,2})?)", re.I)


def clean(text: str) -> str:
    """Strip zero-width/non-breaking spaces and collapse whitespace."""
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def fetch_page() -> str:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = clean(soup.get_text(" "))

    total = TOTAL_RE.search(text)
    aim = AIM_RE.search(text)

    # Only look for donations after the "Donations" heading if present
    idx = text.rfind("Donations Name:")
    donation_text = text[idx:] if idx != -1 else text

    donations = []
    for m in DONATION_RE.finditer(donation_text):
        d = {k: clean(v) for k, v in m.groupdict().items()}
        d["id"] = hashlib.sha1(
            "|".join([d["name"].lower(), d["amount"], d["date"]]).encode()
        ).hexdigest()[:12]
        donations.append(d)

    return {
        "total": total.group(1) if total else None,
        "aim": aim.group(1) if aim else None,
        "donations": donations,
    }


def fmt(d: dict) -> str:
    msg = "" if d["message"].upper() in ("N/A", "") else f' — "{d["message"]}"'
    return f'{d["date"]}  {d["name"]}: {d["amount"]}{msg}'


def diff(old: dict, new: dict) -> list[str]:
    lines = []
    if old.get("total") != new["total"]:
        lines.append(f'Total raised: £{old.get("total")} → £{new["total"]} (aim £{new["aim"]})')
    if old.get("aim") != new["aim"]:
        lines.append(f'Aim changed: £{old.get("aim")} → £{new["aim"]}')

    old_by_id = {d["id"]: d for d in old.get("donations", [])}
    new_by_id = {d["id"]: d for d in new["donations"]}

    added = [d for i, d in new_by_id.items() if i not in old_by_id]
    removed = [d for i, d in old_by_id.items() if i not in new_by_id]
    edited = [
        (old_by_id[i], d) for i, d in new_by_id.items()
        if i in old_by_id and old_by_id[i]["message"] != d["message"]
    ]

    if added:
        lines.append(f"\nNew donations ({len(added)}):")
        lines += [f"  + {fmt(d)}" for d in added]
    if removed:
        lines.append(f"\nRemoved donations ({len(removed)}):")
        lines += [f"  - {fmt(d)}" for d in removed]
    if edited:
        lines.append("\nEdited messages:")
        lines += [f"  ~ {fmt(n)} (was: \"{o['message']}\")" for o, n in edited]
    return lines


def send_email(subject: str, body: str) -> None:
    if not (SMTP_USER and SMTP_PASS and EMAIL_TO):
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print("Email sent.")


def send_push(title: str, body: str) -> None:
    if not NTFY_TOPIC:
        return
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": title.encode("utf-8"), "Click": URL, "Tags": "mountain"},
        timeout=30,
    )
    print("Push notification sent.")


def notify(subject: str, body: str) -> None:
    if not (SMTP_USER or NTFY_TOPIC):
        print("No notification method configured — set SMTP_* or NTFY_TOPIC.")
    send_email(subject, body)
    send_push(subject, body)


def main() -> int:
    if "--test" in sys.argv:
        notify("Kilimanjaro monitor: test", "If you can read this, notifications work.")
        return 0
    dry_run = "--dry-run" in sys.argv

    try:
        new = parse(fetch_page())
    except Exception as e:
        print(f"Fetch failed: {e}")
        return 1  # transient errors: just try again next run

    # Guard against the page layout changing (avoids "all donations removed" alerts)
    if new["total"] is None or not new["donations"]:
        msg = "Couldn't find the total or donations — the page layout may have changed."
        print(msg)
        if not dry_run:
            notify("Kilimanjaro monitor: check the page", f"{msg}\n\n{URL}")
        return 2

    stamp = datetime.now().strftime("%d/%m/%Y %H:%M")

    if not STATE_FILE.exists():
        print(f"First run: saving baseline (£{new['total']}, {len(new['donations'])} donations).")
        if not dry_run:
            STATE_FILE.write_text(json.dumps(new, indent=2, ensure_ascii=False))
        return 0

    old = json.loads(STATE_FILE.read_text())
    changes = diff(old, new)

    if not changes:
        print(f"[{stamp}] No changes (£{new['total']}, {len(new['donations'])} donations).")
        return 0

    body = "\n".join(changes) + f"\n\nChecked {stamp}\n{URL}"
    print(body)
    if not dry_run:
        notify(f"Dom's Kilimanjaro: £{new['total']} raised", body)
        STATE_FILE.write_text(json.dumps(new, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
