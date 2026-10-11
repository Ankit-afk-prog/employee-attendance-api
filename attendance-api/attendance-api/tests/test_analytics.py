"""
Analytics tests over a small dataset whose answers are computed BY HAND (see the comments).
July 2026: the 1st is a Wednesday, 23 working days (Mon-Fri). 4 and 5 July is a weekend, 11/12 too.

Needs MongoDB:  pytest tests/test_analytics.py -q
"""
import json

import pytest
from conftest import make_employee, make_log

from app.main import db


@pytest.fixture()
def data(clean_db):
    db.employees.insert_many([
        make_employee("EMP9001", "QA-A"),
        make_employee("EMP9002", "QA-A", joined="2026-07-13"),   # mid-month joiner
        make_employee("EMP9003", "QA-A"),                         # no logs at all
        make_employee("EMP9004", "QA-A", joined="2026-08-01"),   # not yet joined in July
        make_employee("EMP9005", "QA-B"),
        make_employee("EMP9006", "QA-B"),
    ])
    db.attendance_logs.insert_many([
        # EMP9001
        make_log("EMP9001", "2026-07-06", wh=9.0, late=15),
        make_log("EMP9001", "2026-07-07", wh=3.5, half=True),
        make_log("EMP9001", "2026-07-08", status="LEAVE"),
        make_log("EMP9001", "2026-07-09", status="ABSENT"),
        make_log("EMP9001", "2026-07-11", wh=8.0, late=20, ot=40),     # Saturday
        make_log("EMP9001", "2026-06-30", wh=8.0, late=50),            # other month
        # EMP9002 (joined 13 July)
        make_log("EMP9002", "2026-07-13", wh=8.0),
        make_log("EMP9002", "2026-07-14", status="WFH", wh=9.0, late=12),
        make_log("EMP9002", "2026-07-15", status="ON_DUTY"),            # open record, no work_hours
        # department B
        make_log("EMP9005", "2026-07-06", wh=8.0, late=35),
        make_log("EMP9006", "2026-07-06", wh=8.0, late=12),
        # log whose employee does not exist: must be ignored everywhere
        make_log("EMP9999", "2026-07-06", wh=8.0, late=100),
    ])


# ------------------------------------------------------------------ monthly
def test_monthly_normal_employee(client, data):
    r = client.get("/analytics/employees/EMP9001/monthly", params={"month": "2026-07"})
    assert r.status_code == 200
    assert r.json() == {
        "emp_code": "EMP9001", "month": "2026-07",
        "working_days": 23,
        "present_days": 1.5,            # 1 full + 1 half; the Saturday record does not count
        "leave_days": 1,
        "late_count": 2,                # 15 and 20 (the Saturday still counts for late)
        "total_late_minutes": 35,
        "total_overtime_minutes": 40,   # Saturday overtime counts
        "attendance_pct": 6.52,         # 1.5 / 23 * 100 = 6.5217...
    }


def test_monthly_mid_month_joiner(client, data):
    body = client.get("/analytics/employees/EMP9002/monthly", params={"month": "2026-07"}).json()
    assert body["working_days"] == 15   # 13 July onwards
    assert body["present_days"] == 3.0
    assert body["attendance_pct"] == 20.0


def test_monthly_no_logs_and_not_yet_joined(client, data):
    no_logs = client.get("/analytics/employees/EMP9003/monthly", params={"month": "2026-07"}).json()
    assert no_logs["present_days"] == 0 and no_logs["attendance_pct"] == 0.0
    future = client.get("/analytics/employees/EMP9004/monthly", params={"month": "2026-07"}).json()
    assert future["working_days"] == 0 and future["attendance_pct"] is None


def test_monthly_errors(client, data):
    assert client.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"}).status_code == 404
    assert client.get("/analytics/employees/EMP9001/monthly", params={"month": "2026-13"}).status_code == 422
    assert client.get("/analytics/employees/EMP9001/monthly").status_code == 422


# ------------------------------------------------------------------ department summary
def test_department_summary(client, data):
    body = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert body["month"] == "2026-07"
    a, b = body["items"]               # sorted by department
    assert a == {
        "department": "QA-A",
        "headcount": 3,                # 9001, 9002, 9003 (no logs) - NOT 9004 (joins in August)
        "present_days": 4.5,           # 1.5 + 3
        "avg_work_hours": 7.5,         # (9 + 3.5 + 8 + 8 + 9) / 5 records; the open ON_DUTY record has no hours
        "late_count": 3,
        "total_late_minutes": 47,
        "leave_count": 1,
        "on_duty_count": 1,
    }
    assert b["department"] == "QA-B" and b["headcount"] == 2 and b["avg_work_hours"] == 8.0
    assert b["late_count"] == 2 and b["total_late_minutes"] == 47 and b["present_days"] == 2.0


def test_department_summary_filter_and_empty_months(client, data):
    one = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "QA-B"}).json()
    assert [i["department"] for i in one["items"]] == ["QA-B"]
    august = client.get("/analytics/departments/summary", params={"month": "2026-08", "department": "QA-A"}).json()
    row = august["items"][0]
    assert row["headcount"] == 4 and row["present_days"] == 0 and row["avg_work_hours"] is None
    assert client.get("/analytics/departments/summary", params={"month": "2025-01"}).json()["items"] == []


# ------------------------------------------------------------------ leaderboard
def test_leaderboard_ties_and_limit_after_ranking(client, data):
    def rows(**params):
        r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", **params})
        assert r.status_code == 200
        return [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in r.json()["items"]]

    top = [(1, "EMP9001", 35), (1, "EMP9005", 35)]
    low = [(3, "EMP9002", 12), (3, "EMP9006", 12)]
    assert rows(limit=1) == top        # a tie at the cutoff keeps BOTH rows
    assert rows(limit=2) == top        # rank 2 does not exist (1, 1, 3, 3), so still just the tied pair
    assert rows(limit=3) == top + low
    assert rows() == top + low         # default limit 10; the orphan log (100 min) is ignored


def test_leaderboard_department_and_validation(client, data):
    r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "QA-B"}).json()
    assert [(i["rank"], i["emp_code"]) for i in r["items"]] == [(1, "EMP9005"), (2, "EMP9006")]  # ranked INSIDE dept
    first = client.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()["items"][0]
    assert first == {"rank": 1, "emp_code": "EMP9001", "name": "Name EMP9001", "department": "QA-A",
                     "total_late_minutes": 35, "late_count": 2}
    assert client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 0}).status_code == 422
    assert client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422


# ------------------------------------------------------------------ trend
def test_trend_week_with_gaps_and_weekend(client, data):
    r = client.get("/analytics/departments/QA-A/trend", params={"from": "2026-07-06", "to": "2026-07-12"})
    assert r.status_code == 200
    got = [(i["date"], i["is_working_day"], i["headcount"], i["present_count"], i["late_count"],
            i["attendance_rate"], i["moving_avg_7d"]) for i in r.json()["items"]]
    assert got == [
        ("2026-07-06", True, 2, 1.0, 1, 0.5, 0.5),
        ("2026-07-07", True, 2, 0.5, 0, 0.25, 0.375),
        ("2026-07-08", True, 2, 0.0, 0, 0.0, 0.25),     # LEAVE record
        ("2026-07-09", True, 2, 0.0, 0, 0.0, 0.1875),   # ABSENT record
        ("2026-07-10", True, 2, 0.0, 0, 0.0, 0.15),     # no record at all: still rate 0.0
        ("2026-07-11", False, 2, 1.0, 1, None, 0.15),   # Saturday: counted present, but no rate
        ("2026-07-12", False, 2, 0.0, 0, None, 0.15),
    ]


def test_trend_joiner_changes_headcount_and_null_average(client, data):
    items = client.get("/analytics/departments/QA-A/trend",
                       params={"from": "2026-07-12", "to": "2026-07-14"}).json()["items"]
    assert items[0]["moving_avg_7d"] is None and items[0]["attendance_rate"] is None   # Sunday, window has no rate
    assert [i["headcount"] for i in items] == [2, 3, 3]                                 # 9002 joins on the 13th
    assert items[1]["attendance_rate"] == 0.3333 and items[2]["attendance_rate"] == 0.3333
    assert items[2]["late_count"] == 1


def test_trend_range_without_any_logs_still_returns_every_day(client, data):
    items = client.get("/analytics/departments/QA-A/trend",
                       params={"from": "2026-09-01", "to": "2026-09-07"}).json()["items"]
    assert len(items) == 7
    assert [i["is_working_day"] for i in items] == [True] * 4 + [False, False, True]
    assert [i["attendance_rate"] for i in items] == [0.0] * 4 + [None, None, 0.0]
    assert all(i["headcount"] == 4 for i in items)


def test_trend_validation(client, data):
    base = "/analytics/departments/QA-A/trend"
    assert client.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert client.get(base, params={"from": "2026-07-05", "to": "2026-07-01"}).status_code == 422
    assert client.get(base, params={"from": "2026-07-01", "to": "2026-10-01"}).status_code == 422   # 93 days
    ok = client.get(base, params={"from": "2026-07-01", "to": "2026-09-30"})                          # 92 days
    assert ok.status_code == 200 and len(ok.json()["items"]) == 92
    assert client.get(base, params={"from": "2026-07-01"}).status_code == 422


# ------------------------------------------------------------------ explain
EXPLAIN_CASES = {
    "attendance_list": {"date_from": "2026-07-01", "date_to": "2026-07-31"},
    "employee_monthly": {"emp_code": "EMP9001", "month": "2026-07"},
    "department_summary": {"month": "2026-07"},
    "late_leaderboard": {"month": "2026-07", "limit": 5},
    "department_trend": {"department": "QA-A", "from": "2026-07-01", "to": "2026-07-31"},
}


@pytest.mark.parametrize("endpoint", list(EXPLAIN_CASES))
def test_explain_uses_index_and_never_scans_collection(client, data, endpoint):
    r = client.get(f"/admin/explain/{endpoint}", params=EXPLAIN_CASES[endpoint])
    assert r.status_code == 200
    body = r.json()
    assert body["endpoint"] == endpoint and body["collection"] in ("employees", "attendance_logs")
    text = json.dumps(body["explain"])
    assert "IXSCAN" in text
    assert "COLLSCAN" not in text


def test_explain_validation(client, data):
    assert client.get("/admin/explain/department_trend").status_code == 422
    assert client.get("/admin/explain/employee_monthly", params={"emp_code": "EMP9001"}).status_code == 422
    assert client.get("/admin/explain/not_an_endpoint").status_code == 422
