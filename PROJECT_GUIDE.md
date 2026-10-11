# Project Guide: Employee Attendance & Analytics API

Everything you need to **run**, **understand** and **explain** this project. Read it before the live walkthrough: the interviewers will ask you to explain choices and make a small change on the spot.

---

## 1. What the project is

A backend for an HR attendance system:

| Part | What you build |
|---|---|
| A. Review | Find >10 bugs in a colleague's starter code, write them in `REVIEW.md`, fix them |
| B. Build | Punch-out, corrections (PATCH) with audit trail, 3 hard aggregation pipelines |
| C. Prove | `/admin/explain/...` endpoint + answers in `DECISIONS.md` |

Stack: **Python 3.11+, FastAPI** (web framework), **Pydantic** (validation), **PyMongo** (MongoDB driver), **MongoDB 6+**.

Evaluation: automated tests first (rules, concurrency, analytics on a hidden 100k-record dataset, explain plans), then a live walkthrough.

---

## 2. How to run it

### Step 1: install prerequisites
* Python 3.11+ (`python --version`), Git
* MongoDB: the easiest options
  * **Docker:** `docker run -d -p 27017:27017 --name mongo mongo:7`
  * **Atlas free tier (M0):** create a cluster, add your IP to Network Access, copy the `mongodb+srv://...` string

### Step 2: set up the project
```bash
cd attendance-api
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
```
Edit `.env` if your MongoDB is not on localhost (`MONGO_URI`, `MONGO_DB`).

### Step 3: (optional) load data
```bash
python sample_seed.py              # the 9 sample documents
# or ~100k realistic records (for speed/explain checks); DROPS the collections:
python tools/gen_big_data.py --yes
```

### Step 4: start the server
```bash
uvicorn app.main:app --port 8000
```
Open **http://localhost:8000/docs**: an interactive page where you can try every endpoint. Indexes are created automatically at startup.

### Step 5: try it (curl)
```bash
# create an employee
curl -X POST localhost:8000/employees -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0100","name":"Test","email":"t@x.com","department":"Eng","joined_on":"2026-01-05"}'
# punch in (epoch MILLISECONDS; this is 2026-07-06 10:05 IST, so 35 min late; the punch-out below is 19:20 IST), then out
curl -X POST localhost:8000/attendance/punch-in -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0100","punched_at":1783312500000}'
curl -X POST localhost:8000/attendance/punch-out -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0100","punched_at":1783345800000}'
# analytics
curl "localhost:8000/analytics/employees/EMP0100/monthly?month=2026-07"
curl "localhost:8000/analytics/departments/summary?month=2026-07"
curl "localhost:8000/analytics/leaderboard/late?month=2026-07&limit=5"
curl "localhost:8000/analytics/departments/Eng/trend?from=2026-07-01&to=2026-07-14"
curl "localhost:8000/admin/explain/late_leaderboard?month=2026-07"
```
(Check the epoch values with any "epoch converter"; they must have 13 digits.)

### Step 6: run the tests
```bash
pip install -r requirements-dev.txt
pytest tests/test_rules.py -q      # no DB needed
pytest -q                          # all; needs MongoDB; uses & DROPS database attendance_test_db
```
**Important:** I could not run MongoDB in my environment. The rule logic (R1-R5, rounding, working days) was verified and cross-checked against all 9 sample documents, and every file compiles, but the **API and the aggregation pipelines were not executed against a real MongoDB**. Run `pytest -q` yourself first; if something fails, the failing assertion tells you exactly which expected value (computed by hand in the test comments) differs.

### Step 7: submit
```bash
git init && git add . && git commit -m "Attendance API"
# create a PUBLIC repo on GitHub, then:
git remote add origin <url> && git push -u origin main
```
Check `git status`: **no `.env`, no `.venv`**. Submit the link in the form with the same email you applied with, before **11:59 PM IST, 15 Oct 2026**. Keep the repo public until 31 Oct.

---

## 3. Core concepts you must understand

**Identity.** `emp_code` (e.g. EMP0001) identifies employees everywhere. MongoDB's `_id` is internal and never appears in a request/response. A record is addressed by (`emp_code`, `date`).

**Two kinds of time.**
* *Instants* (punch-in, created_at): BSON datetime in MongoDB (UTC), **epoch milliseconds** in the API. Conversion lives in `ms_to_dt` / `dt_to_ms`. The client uses `tz_aware=True` so PyMongo returns aware datetimes.
* *Calendar values* (date, month, shift time): plain strings in IST.
* Instants are truncated to **whole seconds** before any maths (R1).

**Derived fields** (`work_hours`, `late_minutes`, `overtime_minutes`, `half_day`) are computed by the API, stored, and analytics **read the stored values**; they never recompute.

### The business rules in plain words
| Rule | Meaning | Code |
|---|---|---|
| R1 | Date = IST date of punch-in. Overnight shift (end <= start): a punch-in earlier than shift_end belongs to *yesterday's* shift; shift end is tomorrow | `attendance_date`, `shift_window` |
| R2 | Late only if **strictly >10:00** after shift start; then minutes counted from shift start (floor). 09:40:00→0, 09:40:01→10 | `compute_late_minutes` |
| R3 | Overtime = whole minutes after shift end, only if ≥30 | `compute_overtime` |
| R4 | work_hours = seconds/3600, 2 decimals **half-up** | `work_hours_decimal` |
| R5 | half_day if the *rounded* hours < 4.50 (counts 0.5) | `derive_fields` |
| R6 | PRESENT/WFH/ON_DUTY are present; ABSENT/LEAVE aren't | `PRESENCE_STATUSES` |
| R7 | Working days Mon-Fri from `joined_on`; weekend records don't add present_days but do add late/overtime | `count_working_days`, `_present_value_weekdays_only` |
| R8 | Reported numbers 2 dp half-up (rates 4 dp) | `round_half_up` |
| R9 | Headcount = joined_on ≤ end of period, even with zero logs | pipelines start from `employees` |
| R10 | page ≥ 1, page_size ≤ 100 (default 20), total reflects filters | `Query(ge=1, le=100)` |

**Why Decimal?** Python's `round(4.505, 2)` gives `4.5` (binary floats + banker's rounding). `Decimal` with `ROUND_HALF_UP` gives `4.51`. MongoDB's `$round` is also half-even, so I round in Python (or use `floor(x*10000+0.5)` on Decimal128 in the trend pipeline).

---

## 4. Tour of `app/main.py`

1. **Config**: `MongoClient(tz_aware=True)`, constants.
2. **Rules** (pure functions, unit-tested without a database).
3. **Validation types**: `EpochMs` (strict int in range, so seconds/floats/strings → 422), `DateStr`, `MonthStr`, request models. `RegularizeIn` doesn't declare derived fields, so clients can't set them (ignored).
4. **Serialisers**: BSON doc → API JSON (no `_id`, ms instants, defaults for legacy docs, nested history dates → ms).
5. **Indexes**: created at startup, idempotent (table below).
6. **Endpoints**: health, employees, punch-in, punch-out, list, PATCH.
7. **Pipeline builders**: one function per analytics endpoint, returning `(collection, pipeline)`.
8. **Analytics + explain** endpoints. Explain calls the *same builder functions*, so it explains exactly what the endpoint runs.

### Indexes and what they serve
| Collection | Index | Serves |
|---|---|---|
| employees | unique `emp_code` | uniqueness (concurrent creates), lookups, list sorted by emp_code |
| employees | `department, emp_code` | list/trend filter by department |
| employees | `joined_on`, `department, joined_on` | headcount `joined_on <= end` |
| attendance_logs | **unique** `emp_code, date` | **one record per day** (punch-in race), monthly summary, `$lookup` |
| attendance_logs | `date desc, emp_code` | default list order, date ranges, leaderboard month scan |
| attendance_logs | `status, date desc, emp_code` | list filtered by status |
| attendance_logs | `emp_code, punch_in desc` | punch-out: latest record with punch_in ≤ punched_at |

### Concurrency: the three patterns
1. **Punch-in / create employee**: just `insert_one`; the **unique index** picks one winner, the loser's `DuplicateKeyError` → 409. (Never "find then insert".)
2. **Punch-out**: `find_one_and_update({_id, punch_out: None}, ...)`: only one request can match an *open* record; the other gets `None` → 409.
3. **PATCH**: optimistic lock. The update filter requires `history.<n>` not to exist (nobody appended since we read) and status/punches unchanged; history is added with `$push` (atomic append). The loser gets 409.

### The analytics pipelines
* **Employee monthly**: `$match` (index) → one `$group` with conditional sums. Working days are calendar maths in Python.
* **Department summary**: starts from `employees` (`joined_on <= month end`) → `$lookup` logs (via index) → `$unwind` with `preserveNullAndEmptyArrays` (keeps zero-log employees) → `$group` by department. Headcount = `$addToSet` of emp_codes. Average = running Decimal128 sum ÷ count (average over *records*, not of averages).
* **Late leaderboard**: `$group` per employee → `$lookup` employees → `$unwind` (drops orphan logs) → optional department `$match` → `$setWindowFields` with **`$rank`** (1,2,2,4) → `$match rank <= limit` → sort by total desc, emp_code asc. Cutting on *rank* (not array position) is why ties at the cutoff are all kept.
* **Department trend**: department employees + logs → `$facet` (daily sums + joined_on counts, always yields one doc) → `$range`/`$map` creates one entry per calendar day (gap filling) → per-day headcount/present/late → rate (Decimal128, half-up 4 dp, null on weekends/zero headcount) → `$setWindowFields` `$avg` over `documents: [-6, 0]` (moving average; null rates ignored).

---

## 5. Part A: the bugs (summary; full table in `REVIEW.md`)
Late computed in UTC not IST · grace boundary off by one second · no overnight handling · banker's rounding · overtime without 30-min minimum / naive vs aware datetimes · unknown employee → 500 · server-local time · check-then-insert races (two places) · status not validated · `punched_at` accepts seconds/floats/strings · leaks `_id` as `id` and returns datetimes not ms · employee input unvalidated · `skip = page * page_size` and wrong `total` · list loads everything into memory · `/health` always 200 · naive datetimes · no indexes.

---

## 6. Likely interview questions (and where the answer is)
* *Two identical punch-ins at once?* Unique index → one insert wins, other `DuplicateKeyError` → 409 (`punch_in`).
* *Why `$rank` and not sort+limit?* Ties must share rank and the limit applies after ranking.
* *How do zero-log employees appear in headcount?* Pipeline starts at employees; `$unwind` preserves empties.
* *Why Decimal?* Half-up rounding without float error.
* *Why `tz_aware=True`?* Otherwise PyMongo returns naive datetimes; subtracting aware/naive raises.
* *How do you know the index is used?* `/admin/explain/...` shows `IXSCAN`, no `COLLSCAN`.
* *What would you change at 100x?* A monthly per-employee roll-up collection updated on write.
* Be ready for a small live change: e.g. change the grace period (`GRACE_SECONDS`), the overtime minimum, the half-day threshold, or add a filter. All are single constants/lines.

---

## 7. Troubleshooting
| Symptom | Fix |
|---|---|
| `/health` returns 503 | MongoDB not running / wrong `MONGO_URI` |
| `ModuleNotFoundError: app` | run commands from the repo root; `app/__init__.py` must exist |
| 422 on punch-in | `punched_at` must be an integer in **milliseconds** (13 digits) |
| Atlas connection timeout | add your IP in Network Access |
| Test errors about unique index | run tests against a fresh `attendance_test_db` (they drop it) |
| `$setWindowFields` / `$dateAdd` unknown | MongoDB < 5.0; use 6.0+ |

---

## 8. A note on authorship
The assignment allows AI tools as a reference but expects you to understand every line, compares submissions for similarity, and runs a live walkthrough. So: read `main.py` top to bottom, run the tests, change a rule and watch a test fail, and **rewrite `REVIEW.md` and `DECISIONS.md` in your own words** (the drafts are accurate, but they are meant to reflect your understanding). Add a line to the README if you changed anything.
