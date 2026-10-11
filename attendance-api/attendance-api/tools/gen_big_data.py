"""
Generate a ~100,000-record dataset with awkward cases, to check speed and the /admin/explain plans.

    python tools/gen_big_data.py --yes            # DROPS employees + attendance_logs in $MONGO_DB, then fills them

Awkward cases included on purpose: employees with no logs, mid-year joiners, overnight shifts (Support),
weekend records, open records (no punch_out), half days, LEAVE / ABSENT, missing weekdays, and plenty of
ties in late minutes. The stored derived values come from the app's own rule functions (R1-R5).
Run it BEFORE starting the app (the app then creates the indexes on the existing data, like the grader does).
"""
import argparse
import os
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from pymongo import MongoClient  # noqa: E402

from app.main import UTC, attendance_date, derive_fields, shift_window  # noqa: E402

load_dotenv()
DEPARTMENTS = ["Engineering", "Sales", "Support", "HR", "Finance"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="confirm that the collections may be dropped")
    ap.add_argument("--employees", type=int, default=520)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    uri, name = os.getenv("MONGO_URI", "mongodb://localhost:27017"), os.getenv("MONGO_DB", "attendance_db")
    if not args.yes:
        sys.exit(f"This will DROP employees and attendance_logs in database '{name}'. Re-run with --yes.")
    db = MongoClient(uri, tz_aware=True)[name]
    db.employees.drop()
    db.attendance_logs.drop()

    rng = random.Random(args.seed)
    first_day, last_day = date(2026, 1, 1), date(2026, 9, 30)

    employees = []
    for i in range(1, args.employees + 1):
        dept = rng.choice(DEPARTMENTS)
        overnight = dept == "Support" and rng.random() < 0.3
        joined = first_day if rng.random() < 0.8 else first_day + timedelta(days=rng.randint(1, 240))
        employees.append({
            "emp_code": f"EMP{i:04d}", "name": f"Employee {i}", "email": f"emp{i}@example.com",
            "department": dept,
            "shift_start": "22:00" if overnight else "09:30", "shift_end": "06:00" if overnight else "18:30",
            "joined_on": joined.isoformat(), "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "_no_logs": rng.random() < 0.02,
        })
    db.employees.insert_many([{k: v for k, v in e.items() if k != "_no_logs"} for e in employees])

    batch, total = [], 0
    for emp in employees:
        if emp["_no_logs"]:
            continue
        day = max(first_day, date.fromisoformat(emp["joined_on"]))
        while day <= last_day:
            weekend = day.weekday() >= 5
            if (weekend and rng.random() > 0.01) or (not weekend and rng.random() < 0.03):
                day += timedelta(days=1)       # no record at all for this day
                continue
            batch.append(build_log(rng, emp, day))
            if len(batch) >= 10_000:
                db.attendance_logs.insert_many(batch)
                total += len(batch)
                batch = []
            day += timedelta(days=1)
    if batch:
        db.attendance_logs.insert_many(batch)
        total += len(batch)
    print(f"database '{name}': {len(employees)} employees, {total} attendance records")


def build_log(rng: random.Random, emp: dict, day: date) -> dict:
    roll = rng.random()
    status = ("LEAVE" if roll < 0.05 else "ABSENT" if roll < 0.09 else
              "WFH" if roll < 0.17 else "ON_DUTY" if roll < 0.20 else "PRESENT")
    doc = {"emp_code": emp["emp_code"], "date": day.isoformat(), "status": status, "punch_in": None,
           "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
           "half_day": False, "history": []}
    if status in ("LEAVE", "ABSENT"):
        return doc

    start, end = shift_window(day, emp["shift_start"], emp["shift_end"])
    r = rng.random()
    offset = rng.randint(-1200, 0) if r < 0.6 else rng.randint(0, 600) if r < 0.75 else rng.randint(601, 5400)
    punch_in = (start + timedelta(seconds=offset)).astimezone(UTC)
    punch_out = None
    if rng.random() > 0.01:                                    # ~1% of records stay open
        span = rng.choice([rng.randint(-8 * 3600, -4 * 3600), rng.randint(-3600, 9000), rng.randint(-1800, 5400)])
        punch_out = (end + timedelta(seconds=span)).astimezone(UTC)
        if punch_out <= punch_in:
            punch_out = punch_in + timedelta(hours=rng.choice([2, 3, 4]))
    day_check = attendance_date(punch_in, emp["shift_start"], emp["shift_end"])
    assert day_check == day, (emp["emp_code"], day, day_check)
    doc.update(derive_fields(punch_in, punch_out, emp["shift_start"], emp["shift_end"], day))
    doc.update({"punch_in": punch_in, "punch_out": punch_out})
    return doc


if __name__ == "__main__":
    main()
