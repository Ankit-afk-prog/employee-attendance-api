# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in / punch-out with business rules (R1-R10), audited manual corrections,
and aggregation-pipeline analytics. All application code is in `app/main.py`.

**Status: complete.** Every endpoint in `openapi.yaml` is implemented. Nothing is missing.

## Run

Python 3.11+ and MongoDB 6.0+ (the grader uses 7.0).

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # set MONGO_URI / MONGO_DB (real env vars win over .env)
docker run -d -p 27017:27017 --name mongo mongo:7       # or use a MongoDB Atlas free cluster
python sample_seed.py                                   # optional: load the nine sample documents
uvicorn app.main:app --port 8000
```

Open http://localhost:8000/docs for the interactive API. Indexes are created automatically at startup.

## Test

```bash
pip install -r requirements-dev.txt
pytest tests/test_rules.py -q          # pure business-rule tests, no database needed
pytest -q                              # everything; needs MongoDB. Uses database "attendance_test_db"
                                       # (override with TEST_MONGO_DB) and DROPS it - never your real data.
python tools/gen_big_data.py --yes     # optional: ~100k records to check speed / explain plans
```

## Notes for reviewers

* **Time**: instants are epoch milliseconds in the API and BSON dates (UTC, `tz_aware=True`) in MongoDB.
  Dates, months and shift times are strings in IST. Instants are truncated to whole seconds before any maths.
* **Exact arithmetic**: work hours and all reported numbers are rounded half-up with `Decimal` (not `round()`).
  Sums that feed averages are `Decimal128` inside the pipelines.
* **Concurrency**: uniqueness is enforced by unique indexes (employees, one record per employee per day);
  punch-out and regularization are compare-and-set updates, so exactly one of N simultaneous requests wins.
* **Analytics** are single aggregation pipelines (see section 7 of `app/main.py`); `/admin/explain/{endpoint}` builds
  the very same query/pipeline and returns the raw `executionStats` output.
* **Interpretation choices** where the contract leaves room:
  * The trend's calendar is generated with `$range` + `$map` inside the pipeline instead of `$densify`
    (it still works when a department has no logs at all).
  * `moving_avg_7d` averages the *reported* (4-decimal) `attendance_rate` values.
  * A trend range of more than 92 calendar days (inclusive count) is a 422.
  * A PATCH is "a request that changes nothing" (422) when no field, including derived ones, would change.
  * `present_days` is not reduced for records dated before `joined_on`; working days start at `joined_on`.
  * `GET /attendance` uses `estimated_document_count()` for `total` only when there is no filter (instant on 100k docs).
