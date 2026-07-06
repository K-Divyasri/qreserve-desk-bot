#!/usr/bin/env python3
"""
Keep S605 Desk 16 booked with a 10:00 check-in.

Runs STATELESSLY: fires at 18:00 (the instant tomorrow's booking window opens,
since QReserve allows booking up to 24h before the END time), again at
midnight as a backup, then every 30 minutes from 05:00-09:00 Toronto time
(see book-desk.yml). Each run does ONE check-and-fix, on whichever day it's
currently targeting (tomorrow for the 18:00 run, today for every other run):
  - no booking yet         -> create one (aiming at 10:00)
  - booking, start != 10   -> edit it to pull the check-in to 10:00
  - booking, start == 10   -> do nothing (this is the "stop editing" case)

QReserve has no cookie session: POST /session returns a bearer-style token
that gets attached to every later call via the Authorization and
Vnd-Qreserve-Token headers. Editing a reservation doesn't modify it in
place -- POST /reservation/<id> replaces it with a brand-new reservation_id
(the old one shows up as replaced_reservation_id in the response), so we
always re-fetch the current booking fresh each run rather than remembering
an ID across runs.
"""

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# ------------------------------- CONFIG -------------------------------
TZ = ZoneInfo(os.environ.get("QR_TZ", "America/Toronto"))
CHECKIN_HOUR  = int(os.environ.get("QR_CHECKIN_HOUR", "10"))    # target check-in = 10:00
STRETCH_HOURS = int(os.environ.get("QR_STRETCH_HOURS", "8"))    # 8h stretch -> end 18:00
DAYS_AHEAD    = int(os.environ.get("QR_DAYS_AHEAD", "0"))       # 0 = today, 1 = tomorrow, ...
SKIP_WEEKDAYS = {int(x) for x in os.environ.get("QR_SKIP_WEEKDAYS", "5,6").split(",") if x != ""}

API      = os.environ.get("QR_API", "https://api.qreserve.com")
EMAIL    = os.environ["QR_EMAIL"]
PASSWORD = os.environ["QR_PASSWORD"]

# S605 Desk 16 Communal Desk, Sunnybrook Research Institute site.
DESK_ID = os.environ.get("QR_DESK_ID", "1d9l6e3fe6ekxnhixp9cvsn9twx51m937gzk94")
USER_ID = os.environ.get("QR_USER_ID", "1a5ewz87rv38ka4pszl3zdk30j8t6fwz98hh74")
RESERVED_FOR_TEXT = os.environ.get("QR_RESERVED_FOR_TEXT", "Divyasri")
# ----------------------------------------------------------------------

session = requests.Session()


END_HOUR = (CHECKIN_HOUR + STRETCH_HOURS) % 24  # e.g. 10 + 8 = 18:00


def is_in_schedule(now):
    """Evening window-open run, midnight backup, then 05:00-09:00 morning defends."""
    if now.hour == END_HOUR and now.minute < 30:
        return True                       # window for TOMORROW just opened (end - 24h)
    if now.hour == 0 and now.minute < 30:
        return True                       # midnight backup run (targets today)
    if 5 <= now.hour < 9:
        return True                       # 05:00 - 08:59
    if now.hour == 9 and now.minute == 0:
        return True                       # include 09:00 sharp
    return False


def target_day(now):
    """Evening run targets TOMORROW (its window just opened); every other run targets today."""
    if now.hour == END_HOUR:
        return (now + timedelta(days=DAYS_AHEAD + 1)).date()
    return (now + timedelta(days=DAYS_AHEAD)).date()


def desired_window(day):
    midnight = datetime.combine(day, datetime.min.time(), TZ)
    start = midnight.replace(hour=CHECKIN_HOUR)
    end = start + timedelta(hours=STRETCH_HOURS)
    window_opens = end - timedelta(hours=24)
    return start, end, window_opens


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


def find_my_reservation(day):
    """Return {'id': str, 'start': datetime} for the desk that day, or None."""
    day_start = datetime.combine(day, datetime.min.time(), TZ)
    start_ts = int(day_start.timestamp())
    end_ts = int((day_start + timedelta(days=1)).timestamp())

    resp = session.get(
        f"{API}/user/reservations",
        params={
            "start": start_ts,
            "end": end_ts,
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
        if res["start"][:10] != day.isoformat():
            continue
        return {"id": res["reservation_id"], "start": datetime.fromisoformat(res["start"])}
    return None


def _reservation_payload(start, end):
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


def create_reservation(start, end):
    resp = session.post(f"{API}/reservation", json=_reservation_payload(start, end))
    print(f"create -> HTTP {resp.status_code}")
    if not resp.ok:
        print(resp.text[:400])
    return resp.ok


def edit_reservation(reservation_id, start, end):
    resp = session.post(f"{API}/reservation/{reservation_id}", json=_reservation_payload(start, end))
    print(f"edit -> HTTP {resp.status_code}")
    if not resp.ok:
        print(resp.text[:400])
    return resp.ok


def run_once():
    now = datetime.now(TZ)
    force = os.environ.get("QR_FORCE", "").lower() == "true"
    if not force and not is_in_schedule(now):
        print(f"{now:%H:%M} local is outside the schedule; exiting.")
        return 0

    day = target_day(now)
    if not force and day.weekday() in SKIP_WEEKDAYS:
        print("Skipped weekday; nothing to do.")
        return 0

    start, end, window_opens = desired_window(day)
    if not force and now < window_opens:
        print(f"Window for {day} opens {window_opens}. Too early; exiting.")
        return 0

    login()
    res = find_my_reservation(day)

    if res is None:
        print("No booking yet -> creating at 10:00.")
        create_reservation(start, end)
    elif res["start"].hour == CHECKIN_HOUR and res["start"].minute == 0:
        print("Check-in already 10:00 -> done, leaving it alone.")
    else:
        print(f"Check-in is {res['start'].time()} -> editing to 10:00.")
        edit_reservation(res["id"], start, end)
    return 0


if __name__ == "__main__":
    sys.exit(run_once())
