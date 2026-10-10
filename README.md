# Employee Attendance & Analytics API

FastAPI and MongoDB 6.0+ implementation for the HROne Engineering Assignment (Contract v2.0).

All endpoints, business rules (R1–R10), validations, and MongoDB aggregation pipelines are self-contained in [`app/main.py`](file:///d:/Projects/SDT_candidate_kit/candidate_kit/app/main.py).

---

## Quick Start

### 1. Requirements & Setup
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment Variables
Configure `MONGO_URI` and `MONGO_DB` via environment variables (or local `.env`):
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```
*Note: Real environment variables take priority over `.env`.*

### 3. Run Application
```bash
uvicorn app.main:app --port 8000
```
- The readiness probe `GET /health` returns `200 {"status": "ok"}` once MongoDB answers a ping (returns `503` if MongoDB is down).
- Indexes on `employees` and `attendance_logs` are created automatically and idempotently at startup.

---

## Key Features & Compliance

- **Complete Business Rules (R1–R10)**:
  - **R1**: Truncation to whole seconds; IST calendar date resolution with overnight shift handling (`shift_end <= shift_start`).
  - **R2**: Strict $>10:00$ grace period threshold with floor minutes calculation from shift start.
  - **R3**: Overtime qualified only when $\ge 30$ minutes with next-day shift end calculation for overnight shifts.
  - **R4 & R8**: Strict round-half-up Decimal calculations for work hours (2 decimals) and rates (4 decimals).
  - **R5**: Half-day evaluation (`work_hours < 4.50`) counting as 0.5 present day.
  - **R6 & R7**: Monday–Friday working days logic from `joined_on` for mid-month joiners; weekend logs contribute to late/overtime totals without inflating Mon–Fri present days.
  - **R9**: Department headcount preserves zero-attendance employees with `joined_on <= end of period`.
- **Concurrency & Race Safety**:
  - `POST /employees` and `POST /attendance/punch-in` enforce database-level unique constraints, returning 409 on duplicate race conditions.
  - `POST /attendance/punch-out` uses atomic `find_one_and_update` on open punch-in records to guarantee single 200 responses.
  - `PATCH /attendance/{emp_code}/{date}` implements optimistic locking on history length to prevent lost audit trails.
- **MongoDB Aggregation Pipelines**:
  - `GET /analytics/employees/{emp_code}/monthly`: Aggregated employee attendance statistics.
  - `GET /analytics/departments/summary`: Cross-collection aggregation preserving headcount for zero-log employees and computing true record-level average work hours.
  - `GET /analytics/leaderboard/late`: MongoDB `$setWindowFields` standard competition ranking with ties (`1, 2, 2, 4`), sorting, and post-ranking limit.
  - `GET /analytics/departments/{department}/trend`: Gap-filling via `$densify` ensuring every calendar day exists, with 7-day moving averages in MongoDB.
  - `GET /admin/explain/{endpoint}`: Raw `executionStats` verification ensuring all queries use `IXSCAN` and zero `COLLSCAN`.

---

## Submission Deliverables

- [`app/main.py`](file:///d:/Projects/SDT_candidate_kit/candidate_kit/app/main.py): All application code in a single file.
- [`REVIEW.md`](file:///d:/Projects/SDT_candidate_kit/candidate_kit/REVIEW.md): Audit table of all starter code defects and fixes.
- [`DECISIONS.md`](file:///d:/Projects/SDT_candidate_kit/candidate_kit/DECISIONS.md): Architectural decisions and scaling answers (150–200 words).
- [`requirements.txt`](file:///d:/Projects/SDT_candidate_kit/candidate_kit/requirements.txt): Python dependencies.
- [`README.md`](file:///d:/Projects/SDT_candidate_kit/candidate_kit/README.md): Project overview and run instructions.
