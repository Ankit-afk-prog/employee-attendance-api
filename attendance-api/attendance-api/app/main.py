"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real environment variables win)

Layout of this file (everything lives here on purpose, see the assignment):
    1. Configuration and constants
    2. Time helpers and the business rules R1-R5   (pure functions, no database)
    3. Validation types and request models
    4. Serialisation helpers (BSON document <-> API JSON)
    5. Indexes (created at startup)
    6. Endpoints: system, employees, attendance
    7. Aggregation-pipeline builders (one per analytics endpoint; shared with /admin/explain)
    8. Endpoints: analytics and admin/explain
"""
import calendar
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

# =========================================================================== #
# 1. Configuration and constants
# =========================================================================== #
load_dotenv()  # does NOT override variables that are already set in the real environment

log = logging.getLogger("attendance")

# tz_aware=True makes PyMongo return timezone-aware (UTC) datetimes instead of naive ones, so
# subtracting them from our own aware datetimes can never raise "can't subtract offset-naive...".
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60            # R2: late only if STRICTLY more than 10:00 after shift start
MIN_OVERTIME_MINUTES = 30          # R3
HALF_DAY_HOURS = Decimal("4.50")   # R5
MAX_PUNCH_SPAN = timedelta(hours=24)

MIN_EPOCH_MS = 100_000_000_000     # anything smaller looks like epoch *seconds* -> 422
MAX_EPOCH_MS = 4_102_444_800_000   # 2100-01-01
MAX_SKIP = 10**12                  # protects PyMongo/BSON from absurd "page" values (int64 overflow)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")      # R6
NON_PRESENCE_STATUSES = ("ABSENT", "LEAVE")
ALL_STATUSES = PRESENCE_STATUSES + NON_PRESENCE_STATUSES
MAX_TREND_DAYS = 92

# Mongo's $dayOfWeek: 1 = Sunday ... 7 = Saturday.  Monday-Friday = 2..6
WEEKDAY_NUMBERS = [2, 3, 4, 5, 6]

HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


# =========================================================================== #
# 2. Time helpers and business rules (pure functions)
# =========================================================================== #
# --- BEGIN RULES ---
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to WHOLE SECONDS (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc() -> datetime:
    """'Now' truncated to whole seconds (R1 applies to the default punched_at too)."""
    return datetime.now(UTC).replace(microsecond=0)


def now_ms_precision() -> datetime:
    """'Now' truncated to milliseconds, which is the precision MongoDB stores."""
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def as_utc(dt: datetime) -> datetime:
    """Make any datetime aware-UTC (a naive one is assumed to already be UTC, as MongoDB stores it)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def dt_to_ms(dt: datetime) -> int:
    """Aware/naive datetime -> integer epoch milliseconds (exact integer arithmetic, no float)."""
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def dt_to_ms_or_none(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else dt_to_ms(dt)


def parse_hhmm(value: str) -> tuple:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means the shift ends on the next calendar day."""
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date_type:
    """
    R1: the attendance date is the IST calendar date of punch_in, except for overnight shifts, where a
    punch-in whose IST time is EARLIER than shift_end belongs to the shift that started the previous day.
    """
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        end_h, end_m = parse_hhmm(shift_end)
        if (local.hour, local.minute, local.second) < (end_h, end_m, 0):
            day -= timedelta(days=1)
    return day


def shift_window(day: date_type, shift_start: str, shift_end: str) -> tuple:
    """(shift start, shift end) as aware IST datetimes for the shift that belongs to attendance date `day`."""
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    """
    R2: late only if STRICTLY more than 600 s after shift start; then whole minutes (floor) since shift
    start (not since the end of the grace period).  09:40:00 -> 0, 09:40:01 -> 10, 10:15:59 -> 45.
    """
    seconds = (punch_in - shift_start_dt) // timedelta(seconds=1)
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: whole minutes (floor) after shift end, counted only if at least 30; otherwise 0."""
    minutes = ((punch_out - shift_end_dt) // timedelta(seconds=1)) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def work_hours_decimal(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600 rounded to 2 decimals HALF-UP, using Decimal so there is no float error."""
    seconds = (punch_out - punch_in) // timedelta(seconds=1)
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def derive_fields(
    punch_in: datetime,
    punch_out: Optional[datetime],
    shift_start: str,
    shift_end: str,
    day: date_type,
) -> dict:
    """All four derived fields for a record, from its final punch times (R2-R5)."""
    start_dt, end_dt = shift_window(day, shift_start, shift_end)
    late = compute_late_minutes(punch_in, start_dt)
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    hours = work_hours_decimal(punch_in, punch_out)
    return {
        "late_minutes": late,
        "work_hours": float(hours),
        "overtime_minutes": compute_overtime(punch_out, end_dt),
        "half_day": hours < HALF_DAY_HOURS,  # R5 uses the ROUNDED work_hours
    }


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))  # repr gives the shortest string, so 0.1 stays 0.1
    return Decimal(value)


def round_half_up(value: Any, places: int) -> float:
    """R8: round half-up (NOT Python's round(), which is banker's rounding on binary floats)."""
    quantum = Decimal(1).scaleb(-places)
    return float(to_decimal(value).quantize(quantum, rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple:
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}"


def count_working_days(first: date_type, last: date_type) -> int:
    """R7: Monday-Friday days in [first, last], no holiday calendar."""
    count, day = 0, first
    while day <= last:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count
# --- END RULES ---


# =========================================================================== #
# 3. Validation types and request models
# =========================================================================== #
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def _check_date(value: str) -> str:
    if not _DATE_RE.fullmatch(value):
        raise ValueError("must be a date formatted YYYY-MM-DD")
    try:
        date_type.fromisoformat(value)
    except ValueError:
        raise ValueError("not a real calendar date") from None
    return value


def _check_month(value: str) -> str:
    if not _MONTH_RE.fullmatch(value) or int(value[:4]) < 1:
        raise ValueError("must be a month formatted YYYY-MM")
    return value


DateStr = Annotated[str, AfterValidator(_check_date)]
MonthStr = Annotated[str, AfterValidator(_check_month)]
# strict=True: JSON floats (1.5e12), strings ("1783312500000") and booleans are rejected with 422.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class ExplainTarget(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: DateStr

    @field_validator("name", "department")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # "= None" (without Optional[...]) means: may be omitted, but an explicit JSON null is rejected (422).
    status: Status = None  # type: ignore[assignment]
    punch_in: EpochMs = None  # type: ignore[assignment]
    punch_out: EpochMs = None  # type: ignore[assignment]
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    # Derived fields (work_hours, ...) are not declared, so if a client sends them they are ignored.


def invalid(loc: list, msg: str) -> RequestValidationError:
    """Build a 422 in FastAPI's default body shape: {"detail": [{"loc": ..., "msg": ..., "type": ...}]}."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


# =========================================================================== #
# 4. Serialisation helpers
# =========================================================================== #
def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms_or_none(doc.get("created_at")),
    }


def history_to_api(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):  # nested BSON dates -> epoch ms, easy to forget
            changes[field] = {
                "from": dt_to_ms_or_none(change.get("from")),
                "to": dt_to_ms_or_none(change.get("to")),
            }
        else:
            changes[field] = {"from": change.get("from"), "to": change.get("to")}
    return {"at": dt_to_ms(entry["at"]), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def attendance_to_api(doc: dict) -> dict:
    """Never exposes _id.  Legacy documents may lack half_day / history / derived fields: default them."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms_or_none(doc.get("punch_in")),
        "punch_out": dt_to_ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_to_api(h) for h in (doc.get("history") or [])],
    }


# =========================================================================== #
# 5. Indexes (created at startup, idempotent)
# =========================================================================== #
INDEXES = {
    "employees": [
        # Unique business key. This is what makes concurrent duplicate creates yield exactly one 201.
        ([("emp_code", ASCENDING)], {"name": "uniq_emp_code", "unique": True}),
        # GET /employees?department=X sorted by emp_code; also the department match of trend/summary.
        ([("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "dept_emp_code"}),
        # Headcount (R9): joined_on <= end of period, with and without a department filter.
        ([("joined_on", ASCENDING)], {"name": "joined_on"}),
        ([("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined_on"}),
    ],
    "attendance_logs": [
        # At most one record per (employee, day): the guarantee behind race-safe punch-in. Also serves
        # the monthly summary, per-employee listing and the $lookup of logs for an employee.
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"name": "uniq_emp_date", "unique": True}),
        # Default listing order (date desc, emp_code asc), date-range filters and month scans (leaderboard).
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp"}),
        # Listing filtered by status, still sorted without an in-memory sort.
        ([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp"}),
        # Punch-out: "latest record of this employee with punch_in <= punched_at".
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
    ],
}

_index_lock = threading.Lock()
_indexes_ready = False


def ensure_indexes() -> bool:
    """Create every index. Safe to call repeatedly (same name + same spec = no-op). Returns success."""
    global _indexes_ready
    with _index_lock:
        if _indexes_ready:
            return True
        ok = True
        for collection, specs in INDEXES.items():
            for keys, options in specs:
                try:
                    db[collection].create_index(keys, **options)
                except ConnectionFailure as exc:
                    # MongoDB is unreachable: give up now instead of waiting 5 s for each remaining index.
                    log.error("MongoDB unreachable while creating indexes: %s", exc)
                    return False
                except PyMongoError as exc:
                    ok = False
                    log.error("could not create index %s.%s: %s", collection, options["name"], exc)
        _indexes_ready = ok
        return ok


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except Exception:  # never stop the app from starting; /health retries and reports
        log.exception("index creation failed at startup")
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# =========================================================================== #
# 6. Endpoints: system, employees, attendance
# =========================================================================== #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready:
        ensure_indexes()  # covers the case where MongoDB was not yet reachable at startup
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_ms_precision()
    try:
        # No "find first, then insert": the unique index on emp_code decides the winner atomically.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = {"department": department} if department is not None else {}
    skip = min((page - 1) * page_size, MAX_SKIP)
    total = db.employees.count_documents(query)  # total of the FILTERED set (R10)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    return {"items": [employee_to_api(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    stamp = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(stamp, emp["shift_start"], emp["shift_end"])  # R1
    derived = derive_fields(stamp, None, emp["shift_start"], emp["shift_end"], day)  # R2

    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": stamp,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # Race-safe: two simultaneous requests both try to insert; the unique (emp_code, date) index lets
        # exactly one succeed and the other gets DuplicateKeyError -> 409.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    stamp = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # The employee's most recent record whose punch_in is at or before the punch-out. Searching by
    # punch_in (not by date) is what makes overnight shifts work across midnight.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": stamp}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    stamp_in = as_utc(rec["punch_in"])
    if stamp <= stamp_in:
        raise invalid(["body", "punched_at"], "punched_at must be after punch_in")
    if stamp - stamp_in > MAX_PUNCH_SPAN:
        raise invalid(["body", "punched_at"], "punched_at is more than 24 hours after punch_in")

    derived = derive_fields(
        stamp_in, stamp, emp["shift_start"], emp["shift_end"], date_type.fromisoformat(rec["date"])
    )
    # Compare-and-set: only update if the record is STILL open. Of two simultaneous punch-outs exactly
    # one matches this filter; the other gets None back -> 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": stamp,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_to_api(updated)


def build_attendance_find(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[Status],
    page: int,
    page_size: int,
) -> tuple:
    """Filter, sort, skip, limit of GET /attendance. Shared by the endpoint and /admin/explain."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise invalid(["query", "date_from"], "date_from must not be after date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status.value
    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    return query, sort, min((page - 1) * page_size, MAX_SKIP), page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query, sort, skip, limit = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
    # The database sorts (using an index) and paginates; nothing is loaded into Python memory.
    cursor = db.attendance_logs.find(query).sort(sort).skip(skip).limit(limit)
    total = db.attendance_logs.count_documents(query) if query else db.attendance_logs.estimated_document_count()
    return {"items": [attendance_to_api(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: Annotated[DateStr, Path()], body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    shift_start, shift_end = emp["shift_start"], emp["shift_end"]
    old_in = as_utc(rec["punch_in"]) if rec.get("punch_in") else None
    old_out = as_utc(rec["punch_out"]) if rec.get("punch_out") else None
    old = {
        "status": rec["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day") or False),
    }

    new_status = body.status.value if body.status is not None else old["status"]
    if new_status in NON_PRESENCE_STATUSES:
        # ABSENT / LEAVE clear both punch times, so supplying either is contradictory.
        if body.punch_in is not None or body.punch_out is not None:
            raise invalid(["body", "punch_in"], f"punch times cannot be supplied with status {new_status}")
        new_in = new_out = None
        derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise invalid(["body", "punch_in"], f"status {new_status} requires a punch_in")
        if attendance_date(new_in, shift_start, shift_end).isoformat() != date:
            raise invalid(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if new_out is not None and not (new_in < new_out <= new_in + MAX_PUNCH_SPAN):
            raise invalid(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours")
        # Recompute from the FINAL times (R2-R5), whichever of them the request changed.
        derived = derive_fields(new_in, new_out, shift_start, shift_end, date_type.fromisoformat(date))

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    changes = {f: {"from": old[f], "to": new[f]} for f in old if old[f] != new[f]}  # only what really changed
    if not changes:
        raise invalid(["body"], "request changes nothing")

    entry = {"at": now_ms_precision(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency. The update only applies if nobody touched the record since we read it:
    #  - history.<n> must not exist  -> nobody appended a history entry meanwhile
    #  - status / punch_in / punch_out unchanged -> a concurrent punch-out cannot be silently overwritten
    # $push appends atomically, so history is never overwritten. The loser of a race gets 409.
    history_len = len(rec.get("history") or [])
    guard = {
        "_id": rec["_id"],
        "status": old["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        f"history.{history_len}": {"$exists": False},
    }
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, please retry")
    return attendance_to_api(updated)


# =========================================================================== #
# 7. Aggregation-pipeline builders
# =========================================================================== #
# Small expression builders. `p` is the field prefix: "" for a log document, "logs." after a $lookup.
def _present(p: str = "") -> dict:
    return {"$in": [f"${p}status", list(PRESENCE_STATUSES)]}


def _is_weekday(p: str = "") -> dict:
    day_of_week = {
        "$dayOfWeek": {
            "$dateFromString": {"dateString": f"${p}date", "format": "%Y-%m-%d", "onError": None, "onNull": None}
        }
    }
    return {"$in": [day_of_week, WEEKDAY_NUMBERS]}


def _present_weight(p: str = "") -> dict:
    """1 for a normal day, 0.5 for a half day (R5). Missing half_day counts as false."""
    return {"$cond": [{"$eq": [{"$ifNull": [f"${p}half_day", False]}, True]}, 0.5, 1]}


def _present_value_weekdays_only(p: str = "") -> dict:
    """present_days contribution (R5-R7): presence status AND Monday-Friday."""
    return {"$cond": [{"$and": [_present(p), _is_weekday(p)]}, _present_weight(p), 0]}


def _present_value_any_day(p: str = "") -> dict:
    """present_count contribution for the trend: any weekday or weekend."""
    return {"$cond": [_present(p), _present_weight(p), 0]}


def _late_flag(p: str = "") -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [f"${p}late_minutes", 0]}, 0]}, 1, 0]}


def build_employee_monthly(emp_code: str, month: str) -> tuple:
    """One $match (served by the unique (emp_code, date) index) and one $group. Returns (collection, pipeline)."""
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _present_value_weekdays_only()},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": _late_flag()},
                # Stored values are authoritative: read late_minutes / overtime_minutes, never recompute.
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]
    return "attendance_logs", pipeline


def build_department_summary(month: str, department: Optional[str]) -> tuple:
    """
    Starts from EMPLOYEES (not from logs), so people with zero logs still count in headcount (R9).
    $lookup pulls each employee's logs for the month through the (emp_code, date) index; $unwind with
    preserveNullAndEmptyArrays keeps employees whose logs array is empty.
    """
    start, end = month_bounds(month)
    match: dict = {"joined_on": {"$lte": end}}  # R9: joined on or before the last day of the month
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [_present("logs."), {"$isNumber": "$logs.work_hours"}]}
    pipeline = [
        {"$match": match},
        {"$project": {"_id": 0, "emp_code": 1, "department": 1}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "work_hours": 1, "late_minutes": 1, "half_day": 1}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},  # distinct people -> headcount
                "present_days": {"$sum": _present_value_weekdays_only("logs.")},
                # avg over RECORDS: keep a running sum and count (not an average of averages).
                # Decimal128 keeps the sum exact; rounding half-up happens once at the end.
                "work_hours_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$logs.work_hours"}, 0]}},
                "work_hours_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
                "late_count": {"$sum": _late_flag("logs.")},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "work_hours_sum": 1,
                "work_hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]
    return "employees", pipeline


def build_late_leaderboard(month: str, limit: int, department: Optional[str]) -> tuple:
    """
    Group -> join employees -> RANK in the database -> keep rank <= limit.
    $rank is "standard competition ranking" (1, 2, 2, 4). Because the cut is made on the rank value and
    not on the array position, ties at the cutoff are all kept (the result may exceed `limit` rows).
    """
    start, end = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist have no match and disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank INSIDE the department
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return "attendance_logs", pipeline


def _sum_by_day(array_field: str, value_field: str, day_field: str = "$days.d", op: str = "$eq") -> dict:
    """$reduce helper for the trend: sum value_field over array items whose _id matches the day."""
    return {
        "$reduce": {
            "input": f"${array_field}",
            "initialValue": 0,
            "in": {"$cond": [{op: ["$$this._id", day_field]}, {"$add": ["$$value", f"$$this.{value_field}"]}, "$$value"]},
        }
    }


def build_trend_pipeline(department: str, from_s: str, to_s: str) -> tuple:
    """
    Exactly one row per calendar day, built in the database:
      1. employees of the department, each with their logs in the range ($lookup via the index)
      2. $facet makes ONE document holding (a) per-day sums of the logs and (b) joined_on counts
         ($facet always emits a document, even when there are no logs, so the calendar is never lost)
      3. $range + $map generates one entry per calendar day (this is the gap filling; $densify would be an
         alternative, but it emits nothing when its input is empty)
      4. rate computed with Decimal128 and rounded half-up to 4 places
      5. $setWindowFields: moving average over the current row and up to 6 before it, inside the range.
         $avg ignores null rates (weekends / zero headcount) and is null when nothing is left.
    """
    first = date_type.fromisoformat(from_s)
    n_days = (date_type.fromisoformat(to_s) - first).days + 1
    from_dt = datetime(first.year, first.month, first.day, tzinfo=UTC)  # a calendar day pinned to 00:00 UTC
    day_dt = {"$dateAdd": {"startDate": from_dt, "unit": "day", "amount": "$$i"}}

    rate = {"$divide": [{"$toDecimal": "$present"}, {"$toDecimal": "$headcount"}]}
    # half-up to 4 decimals: floor(x * 10000 + 0.5) / 10000   (Mongo's own $round is half-even)
    rate_rounded = {"$divide": [{"$floor": {"$add": [{"$multiply": [rate, 10000]}, 0.5]}}, 10000]}

    pipeline = [
        {"$match": {"department": department}},
        {"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": from_s, "$lte": to_s}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "late_minutes": 1, "half_day": 1}},
                ],
                "as": "logs",
            }
        },
        {
            "$facet": {
                "daily": [
                    {"$unwind": "$logs"},
                    {
                        "$group": {
                            "_id": "$logs.date",
                            "present": {"$sum": _present_value_any_day("logs.")},
                            "late": {"$sum": _late_flag("logs.")},
                        }
                    },
                ],
                "joins": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
            }
        },
        {
            "$project": {
                "daily": 1,
                "joins": 1,
                "days": {
                    "$map": {
                        "input": {"$range": [0, n_days]},
                        "as": "i",
                        "in": {
                            "$let": {
                                "vars": {"dt": day_dt},
                                "in": {
                                    "d": {"$dateToString": {"date": "$$dt", "format": "%Y-%m-%d"}},
                                    "dow": {"$dayOfWeek": "$$dt"},
                                },
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$days"},
        {
            "$project": {
                "_id": 0,
                "date": "$days.d",
                "working": {"$in": ["$days.dow", WEEKDAY_NUMBERS]},
                "present": _sum_by_day("daily", "present"),
                "late": _sum_by_day("daily", "late"),
                "headcount": _sum_by_day("joins", "n", op="$lte"),  # R9: joined_on <= this day
            }
        },
        {
            "$set": {
                "rate": {"$cond": [{"$and": ["$working", {"$gt": ["$headcount", 0]}]}, rate_rounded, None]}
            }
        },
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
    ]
    return "employees", pipeline


def check_trend_range(from_s: str, to_s: str) -> None:
    first, last = date_type.fromisoformat(from_s), date_type.fromisoformat(to_s)
    if last < first:
        raise invalid(["query", "to"], "to must not be before from")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        raise invalid(["query", "to"], f"range must not exceed {MAX_TREND_DAYS} days")


# =========================================================================== #
# 8. Endpoints: analytics and admin/explain
# =========================================================================== #
def _num(value: Any) -> float:
    return float(value)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[MonthStr, Query()]):
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")

    _, pipeline = build_employee_monthly(emp_code, month)
    row = next(iter(db.attendance_logs.aggregate(pipeline)), None) or {}

    start, end = month_bounds(month)
    first = max(date_type.fromisoformat(start), date_type.fromisoformat(emp["joined_on"]))  # R7
    last = date_type.fromisoformat(end)
    working_days = count_working_days(first, last) if first <= last else 0

    present_days = _num(row.get("present_days", 0))
    pct = None
    if working_days > 0:
        pct = round_half_up(to_decimal(present_days) * 100 / Decimal(working_days), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[MonthStr, Query()], department: Optional[str] = None):
    _, pipeline = build_department_summary(month, department)
    items = []
    for r in db.employees.aggregate(pipeline):
        count = r["work_hours_count"]
        avg = None if count == 0 else round_half_up(to_decimal(r["work_hours_sum"]) / Decimal(count), 2)
        items.append(
            {
                "department": r["department"],
                "headcount": r["headcount"],
                "present_days": _num(r["present_days"]),
                "avg_work_hours": avg,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[MonthStr, Query()],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    _, pipeline = build_late_leaderboard(month, limit, department)
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in db.attendance_logs.aggregate(pipeline)
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[DateStr, Query(alias="from")],
    to: Annotated[DateStr, Query()],
):
    check_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    _, pipeline = build_trend_pipeline(department, from_, to)
    items = []
    for r in db.employees.aggregate(pipeline):
        moving = r.get("moving")
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["working"]),
                "headcount": int(r["headcount"]),
                "present_count": _num(r["present"]),
                "late_count": int(r["late"]),
                "attendance_rate": None if r.get("rate") is None else round_half_up(r["rate"], 4),
                "moving_avg_7d": None if moving is None else round_half_up(moving, 4),
            }
        )
    return {"department": department, "items": items}


def _jsonable(raw: Any) -> Any:
    """Explain output contains BSON types (Int64, dates, ...). Round-trip through extended JSON."""
    return json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))


def _require(value: Any, name: str) -> Any:
    if value is None:
        raise invalid(["query", name], "field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """Runs explain('executionStats') on the SAME query/pipeline the real endpoint builds."""
    try:
        if endpoint is ExplainTarget.attendance_list:
            query, sort, skip, lim = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
            collection = "attendance_logs"
            command = {"find": collection, "filter": query, "sort": dict(sort), "skip": skip, "limit": lim}
        else:
            if endpoint is ExplainTarget.employee_monthly:
                collection, pipeline = build_employee_monthly(_require(emp_code, "emp_code"), _require(month, "month"))
            elif endpoint is ExplainTarget.department_summary:
                collection, pipeline = build_department_summary(_require(month, "month"), department)
            elif endpoint is ExplainTarget.late_leaderboard:
                collection, pipeline = build_late_leaderboard(_require(month, "month"), limit, department)
            else:  # department_trend
                dept = _require(department, "department")
                start, end = _require(from_, "from"), _require(to, "to")
                check_trend_range(start, end)
                collection, pipeline = build_trend_pipeline(dept, start, end)
            command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
        raw = db.command({"explain": command, "verbosity": "executionStats"})
    except PyMongoError as exc:
        raise HTTPException(503, f"explain failed: {exc}")
    return {"endpoint": endpoint.value, "collection": collection, "explain": _jsonable(raw)}
