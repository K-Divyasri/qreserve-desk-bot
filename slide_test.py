#!/usr/bin/env python3
"""
One-off test automation: slide a reservation forward 30 minutes every run,
starting at 2026-07-07 16:00-22:00, until its start time reaches 10:00 (at
which point it stops touching it, forever after).

Separate from book_desk.py (the real daily 10:00-18:00 desk automation) --
this is a standalone experiment and does not affect it.

Runs STATELESSLY, same pattern as book_desk.py: every run re-reads the
reservation's current actual start time from the API and advances it by one
30-minute step, rather than trusting any local memory of "which step we're
on". That makes it safe to run more or less often than exactly every 30 min.
"""

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# ------------------------------- CONFIG -------------------------------
TZ = ZoneInfo(os.environ.get("QR_TZ", "America/Toronto"))
API      = os.environ.get("QR_API", "https://api.qreserve.com")
EMAIL    = os.environ["QR_EMAIL"]
PASSWORD = os.environ["QR_PASSWORD"]

DESK_ID = os.environ.get("QR_DESK_ID", "1d9l6e3fe6ekxnhixp9cvsn9twx51m937gzk94")
USER_ID = os.environ.get("QR_USER_ID", "1a5ewz87rv38ka4pszl3zdk30j8t6fwz98hh74")
RESERVED_FOR_TEXT = os.environ.get("QR_RESERVED_FOR_TEXT", "Divyasri")

INITIAL_START = datetime(2026, 7, 7, 16, 0, tzinfo=TZ)   # fixed anchor, not "tomorrow"
DURATION = timedelta(hours=6)                             # 16:00-22:00, kept constant
STEP = timedelta(minutes=30)
STOP_HOUR, STOP_MINUTE = 10, 0
# ----------------------------------------------------------------------

session = requests.Session()


def login():
    resp = session.post(
        f"{API}/session",
        json={"qrauth": True, "username": EMAIL, "password": PASSWORD},
    )
    resp.raise_for_status()
    token = resp.json()["data"]["token"]
    auth_value = f"QReserveToken {token}"
    session.headers.update({
        "Authorization": auth_value,
        "Vnd-Qreserve-Token": auth_value,
        "Accept": "application/vnd.api+json",
    })


def find_slider():
    """Return {'id': str, 'start': datetime} for our sliding reservation, or None."""
    window_start = INITIAL_START - timedelta(days=1)
    window_end = INITIAL_START + timedelta(days=3)
    resp = session.get(
        f"{API}/user/reservations",
        params={
            "start": int(window_start.timestamp()),
            "end": int(window_end.timestamp()),
            "include_approvals": "true",
            "for_logged_in_user": "true",
            "verbose_checked_in": "true",
            "include_events": "true",
            "include_form_responses": "true",
        },
    )
    if not resp.ok:
        print(f"find -> HTTP {resp.status_code}: {resp.text[:300]}")
        return None
    for res in resp.json().get("data", []):
        if res["reservable"]["reservable_id"] != DESK_ID:
            continue
        if res["duration"] != int(DURATION.total_seconds()):
            continue  # not our slider (some other real booking on this desk)
        return {"id": res["reservation_id"], "start": datetime.fromisoformat(res["start"])}
    return None


def _payload(start, end):
    return {
        "auto_approve_if_self": True,
        "preserve_approvals": False,
        "for_user_id": USER_ID,
        "start": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "meta_data": '{"source":"web application"}',
        "duration": int((end - start).total_seconds()),
        "testing": False,
        "send_user_email": True,
        "send_new_guests_email": True,
        "ignore_buffer": False,
        "reminder_seconds": None,
        "reserved_for_text": RESERVED_FOR_TEXT,
        "units": 1,
        "for_reservable_id": DESK_ID,
        "forms": [],
    }


def create(start, end):
    resp = session.post(f"{API}/reservation", json=_payload(start, end))
    print(f"create -> HTTP {resp.status_code}")
    if not resp.ok:
        print(resp.text[:400])
    return resp.ok


def edit(reservation_id, start, end):
    resp = session.post(f"{API}/reservation/{reservation_id}", json=_payload(start, end))
    print(f"edit -> HTTP {resp.status_code}")
    if not resp.ok:
        print(resp.text[:400])
    return resp.ok


def run_once():
    login()
    res = find_slider()

    if res is None:
        start = INITIAL_START
        end = start + DURATION
        print(f"No slider yet -> creating {start} -> {end}")
        create(start, end)
        return 0

    start = res["start"]
    if start.hour == STOP_HOUR and start.minute == STOP_MINUTE:
        print(f"Start is already {start} -- reached 10:00, stopping (no-op).")
        return 0

    new_start = start + STEP
    new_end = new_start + DURATION
    print(f"Start is {start} -> sliding to {new_start} -> {new_end}")
    edit(res["id"], new_start, new_end)
    return 0


if __name__ == "__main__":
    sys.exit(run_once())
