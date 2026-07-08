#!/usr/bin/env python3
"""
Permanent daily rule: grab an overnight desk slot early, then walk its
check-in forward to a fixed target time.

Every day at 20:00 Toronto (2 days before the target day D), this creates a
reservation from (D-1) 18:00 to D 04:00 and then slides it forward 1 minute
at a time until its start reaches D 08:30, at which point it stops touching
it. The next day's 20:00 kickoff starts a brand-new cycle for the next D.

Two entry points:
  --kickoff   daily trigger (20:00 Toronto). Creates a fresh cycle for
              today's target_day UNLESS one is already active for that same
              target_day (idempotent against repeated/retried firings within
              the same 20:00-20:59 window).
  --continue  frequent safety-net trigger. Never creates anything -- only
              continues an already-active cycle. No-ops if there's nothing
              to continue (nothing active, or already reached 08:30).

Tracks the reservation by ID (not by matching duration/shape), persisted to
STATE_FILE across runs via actions/cache. Editing a reservation replaces it
under a brand-new ID -- each create/edit response hands back the current
id+start directly, which is what gets tracked and persisted, rather than
rediscovering it via a list-and-filter query (which broke before when an
overlap merge changed the reservation's shape).
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
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

TRIGGER_HOUR = 20                          # kickoff fires at 20:00 Toronto, targets D = today+2
RES_START_HOUR = 18                        # reservation starts 18:00 on D-1
RES_END_HOUR = 4                           # reservation ends 04:00 on D
DURATION = timedelta(hours=10)             # fixed: 18:00 -> next-day 04:00
STEP = timedelta(minutes=1)
STOP_HOUR, STOP_MINUTE = 8, 30             # slide until start hits D 08:30

STATE_FILE = Path(os.environ.get("QR_STATE_FILE", ".overnight_state.json"))
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


def target_day(now):
    return (now + timedelta(days=2)).date()


def initial_window(day):
    start = datetime.combine(day - timedelta(days=1), dtime(RES_START_HOUR, 0), TZ)
    end = datetime.combine(day, dtime(RES_END_HOUR, 0), TZ)
    return start, end


def load_state():
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
        return {
            "id": data["id"],
            "start": datetime.fromisoformat(data["start"]),
            "target_day": data["target_day"],
        }
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def save_state(reservation_id, start, target_day_iso):
    STATE_FILE.write_text(json.dumps({
        "id": reservation_id,
        "start": start.isoformat(),
        "target_day": target_day_iso,
    }))


def get_reservation(reservation_id):
    """Direct lookup by ID -- the source of truth, immune to duration/shape drift."""
    resp = session.get(f"{API}/reservations/{reservation_id}")
    if not resp.ok:
        print(f"get_reservation({reservation_id}) -> HTTP {resp.status_code}")
        return None
    data = resp.json().get("data", {})
    if data.get("cancelled"):
        print(f"get_reservation({reservation_id}) -> cancelled")
        return None
    return {"id": data["reservation_id"], "start": datetime.fromisoformat(data["start"])}


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


def _submit(url, start, end):
    """POST create/edit, returning {'id', 'start'} from the response, or None on failure."""
    resp = session.post(url, json=_payload(start, end))
    print(f"POST {url} -> HTTP {resp.status_code}")
    if not resp.ok:
        print(resp.text[:400])
        return None
    data = resp.json().get("data", {})
    return {"id": data["reservation_id"], "start": datetime.fromisoformat(data["start"])}


def create(start, end):
    return _submit(f"{API}/reservation", start, end)


def edit(reservation_id, start, end):
    return _submit(f"{API}/reservation/{reservation_id}", start, end)


def resume_state():
    """Load persisted state and verify the ID is still live. `start` comes
    fresh from the API (source of truth); `target_day` is carried from the
    saved record since it's fixed at creation time.

    QR_SEED_ID/QR_SEED_TARGET_DAY are a manual escape hatch: adopt an
    existing reservation (e.g. one you booked yourself) instead of creating
    a fresh, conflicting one.
    """
    seed_id = os.environ.get("QR_SEED_ID")
    if seed_id:
        live = get_reservation(seed_id)
        if live:
            td = os.environ.get("QR_SEED_TARGET_DAY")
            print(f"Adopted QR_SEED_ID {seed_id} -> {live}, target_day={td}")
            return {**live, "target_day": td}
        print(f"QR_SEED_ID {seed_id} not valid -- ignoring.")

    cached = load_state()
    if cached is None:
        return None
    live = get_reservation(cached["id"])
    if live is None:
        print(f"Cached id {cached['id']} no longer valid.")
        return None
    return {**live, "target_day": cached["target_day"]}


def step(current):
    """One check-and-slide step. Returns (new_state, done)."""
    start = current["start"]
    if start.hour == STOP_HOUR and start.minute == STOP_MINUTE:
        print(f"Start is already {start} -- reached {STOP_HOUR:02d}:{STOP_MINUTE:02d}, stopping (no-op).")
        return current, True

    new_start = start + STEP
    new_end = new_start + DURATION
    print(f"Start is {start} -> sliding to {new_start} -> {new_end}")
    result = edit(current["id"], new_start, new_end)
    if result:
        result["target_day"] = current["target_day"]
    return (result or current), False


def ensure_cycle(now):
    """Return the active cycle for today's target_day, creating a fresh
    reservation only if none exists yet for that specific day (idempotent
    against repeated kickoff firings within the same 20:00-20:59 window)."""
    day = target_day(now)
    current = resume_state()
    if current and current.get("target_day") == day.isoformat():
        print(f"Cycle for {day} already active -> {current}")
        return current

    start, end = initial_window(day)
    print(f"New cycle for {day} -> creating {start} -> {end}")
    result = create(start, end)
    if result:
        result["target_day"] = day.isoformat()
    return result


def _drive_loop(current, interval_seconds, max_iterations):
    for i in range(max_iterations):
        if current is None:
            print("No active reservation to slide -- stopping.")
            return
        current, done = step(current)
        if current:
            save_state(current["id"], current["start"], current["target_day"])
        if done:
            print(f"Loop finished after {i + 1} iteration(s).")
            return
        time.sleep(interval_seconds)
    print(f"Hit max_iterations ({max_iterations}) -- safety net will continue.")


def run_kickoff(interval_seconds=60, max_iterations=280):
    """Daily 20:00 trigger. Only acts if it's actually 20:00 local (the
    workflow's */15 cron fires across both DST UTC offsets; this rejects
    the wrong one, same pattern as book_desk.py)."""
    login()
    now = datetime.now(TZ)
    if now.hour != TRIGGER_HOUR:
        print(f"{now:%H:%M} local is not the {TRIGGER_HOUR}:00 kickoff hour -- exiting.")
        return 0
    current = ensure_cycle(now)
    _drive_loop(current, interval_seconds, max_iterations)
    return 0


def run_continue(interval_seconds=60, max_iterations=280):
    """Frequent safety-net trigger. Never creates -- only continues."""
    login()
    current = resume_state()
    if current is None:
        print("Nothing to continue -- waiting for next kickoff.")
        return 0
    _drive_loop(current, interval_seconds, max_iterations)
    return 0


if __name__ == "__main__":
    if "--kickoff" in sys.argv:
        sys.exit(run_kickoff())
    elif "--continue" in sys.argv:
        sys.exit(run_continue())
    else:
        print("Usage: overnight_slide.py --kickoff | --continue")
        sys.exit(1)
