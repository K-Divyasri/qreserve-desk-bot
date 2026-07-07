#!/usr/bin/env python3
"""
One-off test automation: slide a reservation forward 2 minutes every step,
until its start time reaches 10:00 (at which point it stops touching it,
forever after).

Separate from book_desk.py (the real daily 10:00-18:00 desk automation) --
this is a standalone experiment and does not affect it. NOTE: the stop
condition (start=10:00) will always land on whatever day production's own
10:00-18:00 booking already occupies, since that's a fixed daily slot -- the
final step here necessarily collides with it. That's inherent to "stop at
10am", not a bug.

Tracks its reservation by ID, persisted to STATE_FILE across runs (restored
via actions/cache in the workflow), rather than rediscovering it by matching
a duration signature. The duration-match approach broke when QReserve's
overlap-resolution merged the reservation with another one into a new
duration -- ID-based tracking survives that, since we read the *actual*
current start directly from the reservation, and update our tracked ID
whenever an edit response hands us a new one (edits replace the reservation
under a new ID; the response tells us what it is).
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
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

INITIAL_START = datetime(2026, 7, 7, 19, 0, tzinfo=TZ)   # fixed anchor, after production's 18:00 end
DURATION = timedelta(hours=3)                              # 19:00-22:00, kept constant
STEP = timedelta(minutes=2)
STOP_HOUR, STOP_MINUTE = 10, 0

STATE_FILE = Path(os.environ.get("QR_STATE_FILE", ".slide_state.json"))
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


def load_state():
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
        return {"id": data["id"], "start": datetime.fromisoformat(data["start"])}
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def save_state(reservation_id, start):
    STATE_FILE.write_text(json.dumps({"id": reservation_id, "start": start.isoformat()}))


def get_reservation(reservation_id):
    """Direct lookup by ID -- the source of truth, immune to duration drift."""
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


def step(current):
    """One check-and-slide step. Takes/returns {'id','start'} state directly --
    no rediscovery via list-and-filter. Returns (new_state, done)."""
    if current is None:
        start, end = INITIAL_START, INITIAL_START + DURATION
        print(f"No tracked reservation -> creating {start} -> {end}")
        result = create(start, end)
        return result, False

    start = current["start"]
    if start.hour == STOP_HOUR and start.minute == STOP_MINUTE:
        print(f"Start is already {start} -- reached 10:00, stopping (no-op).")
        return current, True

    new_start = start + STEP
    new_end = new_start + DURATION
    print(f"Start is {start} -> sliding to {new_start} -> {new_end}")
    result = edit(current["id"], new_start, new_end)
    return (result or current), False


def resume_state():
    """Load state from disk; if present, verify it directly by ID (source of
    truth) rather than trusting the file blindly.

    QR_SEED_ID is a manual escape hatch: point it at an existing reservation
    ID to adopt (e.g. one created outside the normal tracked flow) instead of
    trying to create a fresh, conflicting one.
    """
    seed_id = os.environ.get("QR_SEED_ID")
    if seed_id:
        live = get_reservation(seed_id)
        if live:
            print(f"Adopted QR_SEED_ID {seed_id} -> {live}")
            return live
        print(f"QR_SEED_ID {seed_id} not valid -- ignoring.")

    cached = load_state()
    if cached is None:
        return None
    live = get_reservation(cached["id"])
    if live is None:
        print(f"Cached id {cached['id']} no longer valid -- starting fresh.")
        return None
    return live


def run_once():
    login()
    current = resume_state()
    new_state, _done = step(current)
    if new_state:
        save_state(new_state["id"], new_state["start"])
    return 0


def run_loop(interval_seconds=120, max_iterations=140):
    """Login once, then repeatedly step() with a real sleep between calls,
    carrying state forward in memory (not re-querying) between iterations.

    Doesn't depend on GitHub's scheduler firing repeatedly -- one job kicks
    this off and it drives itself to completion (or to max_iterations, a
    safety cap keeping total runtime well under the 6h GitHub job limit:
    140 * 2min = ~4.7h). State is saved to disk after every iteration so a
    fresh job can resume exactly where this one left off.
    """
    login()
    current = resume_state()
    for i in range(max_iterations):
        current, done = step(current)
        if current:
            save_state(current["id"], current["start"])
        if done:
            print(f"Loop finished after {i + 1} iteration(s).")
            return 0
        time.sleep(interval_seconds)
    print(f"Hit max_iterations ({max_iterations}) without reaching 10:00 -- "
          f"stopping; the hourly safety-net run will pick up from here.")
    return 0


if __name__ == "__main__":
    if "--loop" in sys.argv:
        sys.exit(run_loop())
    else:
        sys.exit(run_once())
