"""
Unit tests for the pure business rules (R1-R8). They need no MongoDB.
Run:  pytest tests/test_rules.py -q
"""
from datetime import date, datetime

from app.main import (
    IST,
    UTC,
    attendance_date,
    compute_late_minutes,
    compute_overtime,
    count_working_days,
    derive_fields,
    dt_to_ms,
    month_bounds,
    ms_to_dt,
    round_half_up,
    shift_window,
    work_hours_decimal,
)


def ist(y, mo, d, h, mi, s=0, micro=0):
    """An IST wall-clock time as an aware datetime."""
    return datetime(y, mo, d, h, mi, s, micro, tzinfo=IST)


def late(punch, start="09:30", end="18:30"):
    day = attendance_date(punch, start, end)
    s, _ = shift_window(day, start, end)
    return compute_late_minutes(punch, s)


# ---- R2: late ------------------------------------------------------------- #
def test_late_grace_boundaries():
    assert late(ist(2026, 7, 6, 9, 40, 0)) == 0      # exactly 10:00 -> not late
    assert late(ist(2026, 7, 6, 9, 40, 1)) == 10     # 1 s over -> late, counted from shift start
    assert late(ist(2026, 7, 6, 10, 15, 59)) == 45   # floor of 45.98
    assert late(ist(2026, 7, 6, 9, 0, 0)) == 0       # early is never late
    assert late(ist(2026, 7, 6, 9, 30, 0)) == 0


def test_late_uses_ist_not_utc():
    # 09:50 IST is 04:20 UTC. A UTC-based calculation would not see this as 20 minutes late.
    assert late(ist(2026, 7, 6, 9, 50)) == 20


def test_instants_truncated_to_whole_seconds():
    ms = dt_to_ms(ist(2026, 7, 6, 9, 40, 0)) + 900  # 09:40:00.900
    assert late(ms_to_dt(ms)) == 0                   # counts as 09:40:00
    assert ms_to_dt(ms).microsecond == 0


# ---- R1: attendance date and overnight shifts ----------------------------- #
def test_attendance_date_day_shift_is_ist_calendar_date():
    # 23:30 UTC on the 5th is 05:00 IST on the 6th
    punch = datetime(2026, 7, 5, 23, 30, tzinfo=UTC)
    assert attendance_date(punch, "09:30", "18:30") == date(2026, 7, 6)


def test_overnight_shift_dates():
    assert attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == date(2026, 7, 6)
    # after midnight, earlier than shift_end -> belongs to the previous day's shift
    assert attendance_date(ist(2026, 7, 7, 0, 30), "22:00", "06:00") == date(2026, 7, 6)
    # exactly at shift_end is NOT earlier -> same calendar day
    assert attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == date(2026, 7, 7)
    assert attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == date(2026, 7, 6)


def test_overnight_late_and_overtime():
    punch_in = ist(2026, 7, 7, 0, 30)               # shift 22:00 started on the 6th -> 150 min late
    assert late(punch_in, "22:00", "06:00") == 150
    d = derive_fields(ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40), "22:00", "06:00", date(2026, 7, 6))
    assert d["overtime_minutes"] == 40
    assert d["work_hours"] == 8.75
    assert d["late_minutes"] == 0 and d["half_day"] is False


# ---- R3: overtime --------------------------------------------------------- #
def test_overtime_threshold_and_floor():
    _, end = shift_window(date(2026, 7, 6), "09:30", "18:30")
    assert compute_overtime(ist(2026, 7, 6, 19, 0, 0), end) == 30     # exactly 30 counts
    assert compute_overtime(ist(2026, 7, 6, 18, 59, 59), end) == 0    # 29:59 -> 29 -> below minimum
    assert compute_overtime(ist(2026, 7, 6, 19, 10, 59), end) == 40   # floor
    assert compute_overtime(ist(2026, 7, 6, 17, 0, 0), end) == 0      # left early


# ---- R4 / R5: work hours and half day ------------------------------------- #
def test_work_hours_half_up():
    base = ist(2026, 7, 6, 9, 0, 0)
    out = lambda seconds: datetime.fromtimestamp(base.timestamp() + seconds, tz=IST)
    assert float(work_hours_decimal(base, out(16218))) == 4.51   # 4.505 exactly -> half-up (round() gives 4.5)
    assert float(work_hours_decimal(base, out(16182))) == 4.50   # 4.495 -> 4.50
    assert float(work_hours_decimal(base, out(16181))) == 4.49


def test_half_day_uses_rounded_hours():
    base = ist(2026, 7, 6, 9, 30)
    mk = lambda seconds: derive_fields(base, datetime.fromtimestamp(base.timestamp() + seconds, tz=IST),
                                       "09:30", "18:30", date(2026, 7, 6))
    assert mk(16182)["half_day"] is False   # 4.495 rounds to 4.50, which is NOT < 4.50
    assert mk(16181)["half_day"] is True    # rounds to 4.49
    assert mk(12600)["work_hours"] == 3.5 and mk(12600)["half_day"] is True


def test_open_record_has_no_work_hours():
    d = derive_fields(ist(2026, 7, 6, 10, 5), None, "09:30", "18:30", date(2026, 7, 6))
    assert d == {"late_minutes": 35, "work_hours": None, "overtime_minutes": 0, "half_day": False}


# ---- R7 / R8 --------------------------------------------------------------- #
def test_month_bounds_and_working_days():
    assert month_bounds("2026-07") == ("2026-07-01", "2026-07-31")
    assert month_bounds("2028-02") == ("2028-02-01", "2028-02-29")
    assert count_working_days(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert count_working_days(date(2026, 7, 13), date(2026, 7, 31)) == 15   # mid-month joiner
    assert count_working_days(date(2026, 8, 1), date(2026, 7, 31)) == 0


def test_round_half_up():
    assert round_half_up(0.03125, 4) == 0.0313   # banker's rounding would give 0.0312
    assert round_half_up(2.675, 2) == 2.68       # float round() gives 2.67
    assert round_half_up(6.521739130434782, 2) == 6.52
    assert round_half_up(0.5, 0) == 1.0
