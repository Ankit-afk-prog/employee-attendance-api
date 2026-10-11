"""
API behaviour tests: validation, punch-in/out rules, regularization, listing, and race conditions.
Needs MongoDB:  pytest tests/test_api.py -q
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import ist_ms, make_employee, make_log
from fastapi.testclient import TestClient

from app.main import app, db


@pytest.fixture(autouse=True)
def fresh(clean_db):
    yield


def new_employee(client, code, dept="Eng", **extra):
    body = {"emp_code": code, "name": "Test Person", "email": "t@example.com", "department": dept,
            "joined_on": "2026-01-05", **extra}
    r = client.post("/employees", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def run_parallel(fn, n=10):
    """Fire n requests at (almost) the same instant, each from its own client."""
    barrier = threading.Barrier(n)

    def worker():
        c = TestClient(app)
        barrier.wait()
        return fn(c)

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(worker) for _ in range(n)]
        return [f.result() for f in futures]


# ------------------------------------------------------------------ health & employees
def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_employee_and_validation(client):
    emp = new_employee(client, "EMP9101")
    assert emp["shift_start"] == "09:30" and emp["shift_end"] == "18:30"
    assert isinstance(emp["created_at"], int) and emp["created_at"] > 10**12
    assert "_id" not in emp and "id" not in emp
    assert client.post("/employees", json={**emp, "joined_on": "2026-01-05"}).status_code == 409

    ok = {"emp_code": "EMP9102", "name": "A", "email": "a@b.co", "department": "Eng", "joined_on": "2026-01-05"}
    for bad in ({"emp_code": "BAD1"}, {"emp_code": "EMP12"}, {"email": "nope"}, {"joined_on": "2026-13-45"},
                {"shift_start": "9:30"}, {"shift_start": "10:00", "shift_end": "10:00"}, {"name": ""},
                {"department": "x" * 51}):
        assert client.post("/employees", json={**ok, **bad}).status_code == 422, bad
    assert client.post("/employees", json={k: v for k, v in ok.items() if k != "email"}).status_code == 422


def test_concurrent_duplicate_employee_creates(client):
    body = {"emp_code": "EMP9150", "name": "A", "email": "a@b.co", "department": "Eng", "joined_on": "2026-01-05"}
    codes = sorted(r.status_code for r in run_parallel(lambda c: c.post("/employees", json=body)))
    assert codes == [201] + [409] * 9


def test_list_employees_pagination_filter_sort(client):
    for code in ("EMP9203", "EMP9201", "EMP9202"):
        new_employee(client, code, dept="Pag")
    new_employee(client, "EMP9210", dept="Other")
    r = client.get("/employees", params={"department": "Pag", "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP9201", "EMP9202"]
    assert r["total"] == 3 and r["page"] == 1 and r["page_size"] == 2          # total reflects the filter
    page2 = client.get("/employees", params={"department": "Pag", "page_size": 2, "page": 2}).json()
    assert [e["emp_code"] for e in page2["items"]] == ["EMP9203"]
    assert client.get("/employees", params={"department": "pag"}).json()["total"] == 0   # case-sensitive
    assert client.get("/employees", params={"page": 0}).status_code == 422
    assert client.get("/employees", params={"page_size": 101}).status_code == 422


# ------------------------------------------------------------------ punch-in validation
def test_punch_in_validation(client):
    new_employee(client, "EMP9301")
    ok_ms = ist_ms(2026, 7, 6, 9, 30)
    assert client.post("/attendance/punch-in", json={"emp_code": "NOPE"}).status_code == 404
    for bad in ({"punched_at": 1783312500},            # seconds
                {"punched_at": ok_ms + 0.5},           # float
                {"punched_at": str(ok_ms)},            # string
                {"punched_at": True},                  # boolean
                {"punched_at": 5 * 10**12},            # out of range
                {"status": "ABSENT"}, {"status": "LEAVE"}, {"status": "nonsense"}):
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP9301", **bad})
        assert r.status_code == 422, bad
    assert db.attendance_logs.count_documents({"emp_code": "EMP9301"}) == 0


# ------------------------------------------------------------------ punch-in / punch-out / regularize
def test_full_flow_punch_out_and_regularize(client):
    new_employee(client, "EMP9101")
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP9101", "punched_at": ist_ms(2026, 7, 6, 10, 15, 59)})
    assert r.status_code == 201
    rec = r.json()
    assert rec["date"] == "2026-07-06" and rec["late_minutes"] == 45 and rec["status"] == "PRESENT"
    assert rec["punch_out"] is None and rec["work_hours"] is None and rec["history"] == []
    assert "id" not in rec and "_id" not in rec

    again = client.post("/attendance/punch-in", json={"emp_code": "EMP9101", "punched_at": ist_ms(2026, 7, 6, 11, 0)})
    assert again.status_code == 409

    out = client.post("/attendance/punch-out", json={"emp_code": "EMP9101", "punched_at": ist_ms(2026, 7, 6, 19, 10, 30)})
    assert out.status_code == 200
    body = out.json()
    assert body["work_hours"] == 8.91            # 32071 s / 3600 = 8.9086
    assert body["overtime_minutes"] == 40 and body["half_day"] is False and body["late_minutes"] == 45
    assert body["history"] == []                  # punching never writes history

    second = client.post("/attendance/punch-out", json={"emp_code": "EMP9101", "punched_at": ist_ms(2026, 7, 6, 19, 30)})
    assert second.status_code == 409

    new_in = ist_ms(2026, 7, 6, 9, 28)
    p = client.patch("/attendance/EMP9101/2026-07-06",
                     json={"punch_in": new_in, "reason": "biometric glitch", "regularized_by": "hr.admin",
                           "work_hours": 99, "late_minutes": 99})        # derived fields are ignored
    assert p.status_code == 200, p.text
    fixed = p.json()
    assert fixed["late_minutes"] == 0 and fixed["work_hours"] == 9.71 and fixed["overtime_minutes"] == 40
    assert len(fixed["history"]) == 1
    entry = fixed["history"][0]
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch" and isinstance(entry["at"], int)
    assert set(entry["changes"]) == {"punch_in", "late_minutes", "work_hours"}     # only what really changed
    assert entry["changes"]["punch_in"] == {"from": ist_ms(2026, 7, 6, 10, 15, 59), "to": new_in}
    assert entry["changes"]["late_minutes"] == {"from": 45, "to": 0}
    assert entry["changes"]["work_hours"] == {"from": 8.91, "to": 9.71}

    base = {"reason": "second try", "regularized_by": "hr.admin"}
    assert client.patch("/attendance/EMP9101/2026-07-06", json={"punch_in": new_in, **base}).status_code == 422   # nothing changes
    assert client.patch("/attendance/EMP9101/2026-07-06", json={"punch_in": ist_ms(2026, 7, 7, 9, 28), **base}).status_code == 422  # other day
    assert client.patch("/attendance/EMP9101/2026-07-06", json={"punch_out": new_in - 1000, **base}).status_code == 422          # out before in
    assert client.patch("/attendance/EMP9101/2026-07-06", json={"status": "LEAVE", "punch_in": new_in, **base}).status_code == 422
    assert client.patch("/attendance/EMP9101/2026-07-06", json={"status": "bogus", **base}).status_code == 422
    assert client.patch("/attendance/EMP9101/2026-07-06", json={"status": "WFH", "reason": "abc", "regularized_by": "x"}).status_code == 422

    leave = client.patch("/attendance/EMP9101/2026-07-06", json={"status": "LEAVE", **base})
    assert leave.status_code == 200
    rec = leave.json()
    assert rec["punch_in"] is None and rec["punch_out"] is None and rec["work_hours"] is None
    assert rec["overtime_minutes"] == 0 and len(rec["history"]) == 2
    assert rec["history"][0] == fixed["history"][0]                                   # old entries are never altered
    assert set(rec["history"][1]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "overtime_minutes"}
    assert client.patch("/attendance/EMP9101/2026-07-06", json={"status": "WFH", **base}).status_code == 422  # needs punch_in

    assert client.patch("/attendance/EMP9101/2026-07-20", json={"status": "LEAVE", **base}).status_code == 404
    assert client.patch("/attendance/NOPE/2026-07-06", json={"status": "LEAVE", **base}).status_code == 404
    assert client.patch("/attendance/EMP9101/2026-02-30", json={"status": "LEAVE", **base}).status_code == 422


def test_overnight_shift_across_midnight(client):
    new_employee(client, "EMP9102", shift_start="22:00", shift_end="06:00")
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP9102", "punched_at": ist_ms(2026, 7, 7, 0, 30)})
    assert r.status_code == 201
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 150   # belongs to the shift that began on the 6th
    out = client.post("/attendance/punch-out", json={"emp_code": "EMP9102", "punched_at": ist_ms(2026, 7, 7, 6, 40)})
    assert out.status_code == 200
    assert out.json()["overtime_minutes"] == 40 and out.json()["work_hours"] == 6.17
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9102", "punched_at": ist_ms(2026, 7, 6, 21, 55)}).status_code == 409


def test_punch_out_errors(client):
    new_employee(client, "EMP9104")
    new_employee(client, "EMP9105")
    t_in = ist_ms(2026, 7, 6, 9, 30)
    assert client.post("/attendance/punch-out", json={"emp_code": "NOPE"}).status_code == 404
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9105", "punched_at": t_in}).status_code == 404  # never punched in
    client.post("/attendance/punch-in", json={"emp_code": "EMP9104", "punched_at": t_in})
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9104", "punched_at": t_in - 60_000}).status_code == 404  # before any punch-in
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9104", "punched_at": t_in}).status_code == 422          # not after
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9104", "punched_at": t_in + 25 * 3600_000}).status_code == 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9104", "punched_at": t_in + 4 * 3600_000}).json()["half_day"] is True


# ------------------------------------------------------------------ concurrency
def test_concurrent_punch_ins_exactly_one_201(client):
    new_employee(client, "EMP9103")
    body = {"emp_code": "EMP9103", "punched_at": ist_ms(2026, 7, 6, 9, 30)}
    codes = sorted(r.status_code for r in run_parallel(lambda c: c.post("/attendance/punch-in", json=body)))
    assert codes == [201] + [409] * 9
    assert db.attendance_logs.count_documents({"emp_code": "EMP9103"}) == 1


def test_concurrent_punch_outs_exactly_one_200(client):
    new_employee(client, "EMP9106")
    client.post("/attendance/punch-in", json={"emp_code": "EMP9106", "punched_at": ist_ms(2026, 7, 6, 9, 30)})
    body = {"emp_code": "EMP9106", "punched_at": ist_ms(2026, 7, 6, 18, 30)}
    codes = sorted(r.status_code for r in run_parallel(lambda c: c.post("/attendance/punch-out", json=body)))
    assert codes == [200] + [409] * 9


def test_concurrent_regularizations_never_lose_history(client):
    new_employee(client, "EMP9107")
    db.attendance_logs.insert_one(make_log("EMP9107", "2026-07-20", status="ABSENT"))
    payload = {"status": "LEAVE", "reason": "approved leave", "regularized_by": "hr"}
    results = run_parallel(lambda c: c.patch("/attendance/EMP9107/2026-07-20", json=payload), n=8)
    codes = [r.status_code for r in results]
    assert codes.count(200) == 1 and set(codes) <= {200, 409, 422}
    stored = db.attendance_logs.find_one({"emp_code": "EMP9107", "date": "2026-07-20"})
    assert stored["status"] == "LEAVE" and len(stored["history"]) == 1


# ------------------------------------------------------------------ listing
def test_list_attendance_order_filters_and_pagination(client):
    db.employees.insert_many([make_employee("EMP9401", "Eng"), make_employee("EMP9402", "Eng")])
    legacy = make_log("EMP9402", "2026-07-07", wh=8.0)
    del legacy["half_day"], legacy["history"]                       # legacy documents omit these fields
    db.attendance_logs.insert_many([
        make_log("EMP9402", "2026-07-06", wh=8.0), make_log("EMP9401", "2026-07-06", wh=8.0),
        make_log("EMP9401", "2026-07-07", status="LEAVE"), legacy,
    ])
    r = client.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-07"}).json()
    order = [(i["date"], i["emp_code"]) for i in r["items"]]
    assert order == [("2026-07-07", "EMP9401"), ("2026-07-07", "EMP9402"), ("2026-07-06", "EMP9401"), ("2026-07-06", "EMP9402")]
    assert r["total"] == 4
    legacy_item = next(i for i in r["items"] if i["emp_code"] == "EMP9402" and i["date"] == "2026-07-07")
    assert legacy_item["half_day"] is False and legacy_item["history"] == []
    assert isinstance(legacy_item["punch_in"], int) and "_id" not in legacy_item and "id" not in legacy_item

    leave = client.get("/attendance", params={"status": "LEAVE"}).json()
    assert leave["total"] == 1 and leave["items"][0]["emp_code"] == "EMP9401"
    p2 = client.get("/attendance", params={"page": 2, "page_size": 3}).json()
    assert len(p2["items"]) == 1 and p2["total"] == 4
    assert client.get("/attendance", params={"emp_code": "EMP9402"}).json()["total"] == 2
    assert client.get("/attendance", params={"emp_code": "NOBODY"}).json() == {"items": [], "total": 0, "page": 1, "page_size": 20}

    assert client.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-06"}).status_code == 422
    assert client.get("/attendance", params={"date_from": "07/06/2026"}).status_code == 422
    assert client.get("/attendance", params={"status": "SICK"}).status_code == 422
    assert client.get("/attendance", params={"page": 0}).status_code == 422
    assert client.get("/attendance", params={"page_size": 101}).status_code == 422
    assert client.get("/attendance", params={"page": 10**15}).status_code == 200      # huge page: empty, not a 500
