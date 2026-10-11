"""
Shared fixtures for the integration tests (they need a running MongoDB).

SAFETY: these tests DROP the database named in TEST_MONGO_DB (default: attendance_test_db). MONGO_DB is forced
to that name before the app is imported, so your real/dev database is never touched.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ["MONGO_DB"] = os.getenv("TEST_MONGO_DB", "attendance_test_db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import IST, app, db  # noqa: E402

UTC = timezone.utc


@pytest.fixture(scope="session")
def client():
    db.client.drop_database(db.name)
    with TestClient(app) as c:  # entering the context runs the startup hook -> indexes are created
        yield c


@pytest.fixture()
def clean_db(client):
    """Empty collections (indexes stay) for tests that need to control the whole dataset."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield


def ist_ms(y, mo, d, h, mi, s=0):
    """IST wall-clock -> epoch milliseconds."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000


def make_employee(code, dept, joined="2026-01-01", shift_start="09:30", shift_end="18:30"):
    return {
        "emp_code": code,
        "name": f"Name {code}",
        "email": f"{code.lower()}@example.com",
        "department": dept,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }


def make_log(code, day, status="PRESENT", wh=None, late=0, ot=0, half=False):
    """A stored attendance document (stored derived values are authoritative for analytics)."""
    pin = pout = None
    if status in ("PRESENT", "WFH", "ON_DUTY"):
        pin = datetime.fromisoformat(day).replace(hour=4, tzinfo=UTC)
        pout = pin + timedelta(hours=wh) if wh is not None else None
    return {
        "emp_code": code, "date": day, "status": status, "punch_in": pin, "punch_out": pout,
        "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": [],
    }
